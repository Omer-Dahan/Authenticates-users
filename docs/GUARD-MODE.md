# Guard mode, the Mini App, and Telegram Serverless

Status: **planned, not implemented.** This document records the target architecture so the
current Python service and the future web-facing part do not drift apart.

## What guard mode is

The Bot API 10.0 release (May 8, 2026) added **join request queries**. A group can assign one
bot as its guard bot (`ChatFullInfo.guard_bot`, visible to chat administrators). From then on
every join request arrives carrying `ChatJoinRequest.query_id`, and the bot has **10 seconds**
to answer it with one of:

| Call | Meaning |
|---|---|
| `answerChatJoinRequestQuery(query_id, "approve")` | let the user in |
| `answerChatJoinRequestQuery(query_id, "decline")` | refuse |
| `answerChatJoinRequestQuery(query_id, "queue")` | leave the decision to the other administrators |
| `sendChatJoinRequestWebApp(query_id, web_app_url)` | show the applicant a Mini App first, then resolve |

`getMe` advertises the capability as `supports_join_request_queries`. It is off today for
`@Confirm_users_ilbot` (verified live), and the toggle lives in BotFather as **Guard Mode**
("gate group joins through a custom mini app and approve, reject, or queue each request").

Why this matters here: the current verification flow sends the question to the applicant by DM
**after** the request, which only works if the applicant presses Start in a private chat. Anyone
who never does stays pending forever, which is exactly the backlog of stuck requests the owner
sees. Guard mode asks the question **before** the join completes, inside a Mini App the applicant
must interact with, and the bot decides from that interaction. `queue` remains available as the
safe hybrid: answer immediately and keep the current DM flow as the fallback.

## What it requires

1. A public **HTTPS** Mini App URL and the endpoint calls behind it.
2. Verification that the Mini App caller is really the applicant (`initData`).
3. A decision path that answers Telegram within **10 seconds**.

That is a web service. The bot is currently pure long polling in Python with no HTTP surface at
all, so this is new infrastructure, not a configuration toggle.

## Target hosting: Telegram Serverless

Owner decision: run it on Telegram itself, not on a VPS.

Telegram Serverless (early access, docs at https://core.telegram.org/bots/serverless) runs the
bot's backend in V8 isolates on Telegram's infrastructure, next to the Bot API and a built-in
SQLite database. Facts that shape the plan:

- **JavaScript only.** Modules are plain `.js`; the runtime exposes exactly two things, the
  platform `sdk` and the project's own modules. There are no npm packages, no filesystem and no
  network except `sdk.fetch`. **The aiogram/Python codebase cannot be deployed there as it is.**
- Project layout: `tgcloud/schema.js` (database), `tgcloud/handlers/<update_type>.js` (one file
  per update type, flat), `tgcloud/endpoints/<name>.js` (functions the Mini App calls, flat),
  `tgcloud/lib/` (shared modules, subdirectories allowed).
- Handlers exist for **any Bot API update type**, so `handlers/chat_join_request.js` is the
  guard-mode entry point and receives the join request payload directly; the second argument
  `ctx` carries the raw update.
- **Mini App hosting is included** (since Oct 6, 2026): the front-end build output ships in the
  same `push` and is served at `https://app<app_id>.tgcloud.ai/`, configured through
  `tgcloud.jsonc` (`static.source`, `spa`, `headers`, `redirects`). The same host answers
  endpoints under `/api/`, called from the Mini App with `Telegram.WebApp.Serverless.call`.
  Do not set `X-Frame-Options` or a restrictive `frame-ancestors`: Telegram opens Mini Apps in
  an iframe.
- **`ctx.initData` is verified by the platform** before an endpoint runs, and `ctx.initData.user`
  is trusted. That removes hand-rolled initData signature checks from our side.
- Database: SQLite-backed, Drizzle-like DSL in `schema.js`, applied with a separate
  `npx tgcloud migrate` step. **No foreign keys** (the runtime runs with
  `PRAGMA foreign_keys off`, and declaring one fails the deploy), so relationships are plain
  columns and integrity is application-level. Our current SQLAlchemy models use FKs and rely on
  cascades; that has to be handled in code.
- The platform manages the webhook, so **there is no polling** and no `allowed_updates` list:
  an update type is processed only if its handler file exists.
- Deploy flow: `npm create @tgcloud/bot`, `npx tgcloud login` (BotFather -> Serverless ->
  CLI Access -> token of the form `app<id>:<secret>`), `npx tgcloud push`, `npx tgcloud migrate`,
  `npx tgcloud run handlers/<name> '<payload>'` to test locally without deploying. Node.js 18+.

## Consequences for this project

- The moderation engine, the SQLAlchemy models, the handlers and the reports would have to be
  reimplemented in JavaScript for a full migration. There is no partial path that keeps the
  Python bot authoritative while Serverless answers the 10-second join-request window, unless the
  two share a database over HTTP, which adds latency to a 10-second budget and splits the data.
- A full migration is therefore the realistic plan, and it should happen **when guard mode is
  actually wanted**, not before: it is a rewrite of a working, tested service.
- What carries over unchanged: the scoring rules and thresholds, the message copy, the settings
  model, the ban scopes, the deleted-request cleanup policy, the recovery semantics.
- What must be re-verified in JS: SQLite behaviour without foreign keys, timestamp handling
  (the current code compares naive and aware datetimes in places), rate limiting, and the
  10-second decision budget under real traffic.

## Running both at once (parallel)

One bot token has exactly **one** update sink: `getUpdates` (polling) or a webhook. Serverless
installs and manages a webhook for its bot, so a Serverless app and the current polling bot
**cannot share one token** - whichever side starts second breaks the other (setting a webhook
makes `getUpdates` fail, and the `deleteWebhook` call in `bot/main.py` stops the platform from
routing updates to the Serverless handlers).

So "both at once" means a **second bot**: keep `@Confirm_users_ilbot` polling in Python exactly
as today, register another bot for guard mode on Serverless, and assign that one as the group's
guard bot. The two then do different jobs: the guard bot is the front door that answers the
10-second join-request query, the existing bot keeps scoring, settings, bans and the DM
verification flow.

Costs of that split, to decide before building it:

- Two databases. The guard app has its own SQLite inside Serverless; nothing is shared.
- Serverless endpoints can only be called **from that bot's Mini App by a Telegram user** (the
  platform verifies `initData` and rejects anything else), so the Python bot cannot push questions,
  thresholds or ban lists into the guard app. The guard app can only call outward with `sdk.fetch`.
  Keeping its job minimal (ask the configured question, then approve, decline or queue) is what
  makes the split workable.
- Telegram does not document whether a non-guard admin bot still receives the plain
  `chat_join_request` update when a guard bot answers the query. Assume it may, and deduplicate.

## Hosting the Mini App on GitHub Pages

GitHub Pages can serve the **static front-end** (it is only files), and for a landing or info page
that is plenty. It cannot serve the endpoints, because a Mini App in the join-request flow must
(a) prove to a backend who the user is (`initData` verification) and (b) tell the bot the outcome
so it can call `answerChatJoinRequestQuery`. Static hosting can do neither.

The documented Serverless path keeps the front-end and its endpoints on **the same origin**: the
build output is served from `https://app<app_id>.tgcloud.ai/` and the Mini Apps SDK posts endpoint
calls to `/api/` on that host, with `ctx.initData` already verified by the platform. Splitting the
front-end onto Pages means adding a separate backend (Cloudflare Worker, Vercel, a VPS) and
hand-rolling `initData` signature verification, which is exactly the work Serverless removes.

Since Telegram opens Mini Apps in an iframe, a Pages host must not send `X-Frame-Options` or a
restrictive `frame-ancestors`. Recommendation: Pages for a public landing page, and the guard Mini
App itself lives with the Serverless app that answers it.

Timing nuance worth knowing before the design is locked: the **10-second** deadline covers the
*initial* reply (`sendChatJoinRequestWebApp` or a direct `answerChatJoinRequestQuery`). The final
approve or decline is expected after the user finishes interacting with the Mini App, so the
window is not as tight as it first looks.

## Open questions before starting

1. Does guard mode replace the DM verification question for every group, or is it opt-in per
   group with `queue` as the default?
2. Which features stay in the bot after the migration (the current service has verification
   sessions, manual review, bans, lists, settings menus and outage recovery)?
3. Migrating the existing SQLite data: keep the schema shape and move it as-is, or redesign
   without foreign keys and write a one-off importer?
4. Who hosts the Mini App front-end if the guard handler is ever needed before the migration?
