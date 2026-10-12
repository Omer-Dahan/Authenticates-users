# Manual user ban

A manual ban blocks and/or removes a user independent of the moderation
engine's scoring. It is tracked in the `UserBan` table (`database/models.py`)
and enforced by `moderation/engine.py::ModerationEngine.evaluate()`, which
checks for an active ban (`lifted_at IS NULL`) immediately after the
whitelist check, before any config or scoring is loaded. A match short-circuits
evaluation and returns `DecisionEnum.banned` with `total_score=-100.0` and
`details={"reason": "manual_ban"}`.

The shared ban/unban logic lives in `moderation/actions.py` as `ban_user()`
and `unban_user()`, both returning a `BanReport` (`banned_in`, `removed_from`,
`failed`, `skipped`). Every access path below calls into this one module, so
behavior (and its edge cases) is identical regardless of how the ban was
issued.

## Scopes

- **global**: the user is blocked in every group the bot manages (any future
  join request anywhere is auto-rejected) and is removed (banned + any pending
  join request declined) from every currently active, non-banned `Group`.
  The `UserBan` row has `group_id = NULL`.
- **global_remove**: the user is removed from every active, non-banned group
  right now, but the persistent block (what the engine checks on future join
  requests) applies only to the group the ban was issued from. They may still
  request to join other groups later. The `UserBan` row's `group_id` is that
  origin group.
- **local**: both the removal and the persistent block are limited to the one
  origin group. The `UserBan` row's `group_id` is that group.

Re-banning a user who already has an active ban updates the existing row
(scope/reason/actor) instead of inserting a duplicate. At most one active
`scope="global"` row exists per `telegram_id`, and at most one active row
exists per `(telegram_id, group_id)` pair.

Removal on Telegram is `ban_chat_member` followed by `decline_chat_join_request`
in each affected group. A `TelegramAPIError` from `ban_chat_member` puts the
group (title + error text) in `failed` and moves on to the next group; a
missing pending join request on `decline_chat_join_request` is not treated as
a failure. Groups that are inactive or already banned are listed in `skipped`
and never touched.

## Authorization

- The **super-admin** (`settings.super_admin_id`) may ban anywhere and pick
  any of the three scopes.
- A **group owner** (`Group.owner_id`) may ban only inside their own group,
  and only with scope `local`.
- Anyone else gets a short Hebrew refusal and no action is taken.

This is enforced independently at every access path below.

## Access paths

### 1. Commands (`bot/handlers/ban_commands.py`)

- `/ban <telegram_id|@username> [reason]`
  - Super-admin: replies with an inline scope picker
    (`ban:scope:<telegram_id>:<global|global_remove|local>`). For `local` and
    `global_remove`, the origin group is resolved from the chat the picker
    message lives in; if that chat isn't a managed group, the admin is told
    to run `/ban` inside the target group instead. The free-text `reason`
    does not survive the button round-trip (callback data size); it is only
    applied when an owner's local ban runs immediately, or via the CLI.
  - Group owner, invoked inside their own group: runs the `local` scope
    immediately.
  - Anyone else: refused.
- `/unban <telegram_id|@username>` - super-admin only. Lifts every active ban
  row for that user and calls `unban_chat_member` in every group affected.
- `/bans` - super-admin only. Lists active bans with a per-row "✅ הסר חסימה"
  lift button.

`@username` is resolved through the `TelegramUser` table; a numeric Telegram
ID always works even for a user the bot has never seen.

### 2. Manual-review button (`bot/handlers/settings_menu.py::cb_manual_review`)

The 🔨 חסום button on a manual-review message now opens the same three-option
scope picker (`rev:ban_scope:<user_id>:<jr_id>:<group_id>:<scope>`) instead of
banning immediately. The handler authorizes every `rev:` action (approve,
reject, ban) against the join request's own group: only the super-admin or
that group's owner may act, and an owner is only offered the `local` button
(the picker hides the global options for them; picking anything but `local`
if sent directly is also rejected server-side). Unauthorized presses get
`callback.answer(..., show_alert=True)` with a Hebrew refusal and change
nothing.

### 3. Super-admin panel (`bot/handlers/superadmin.py`)

- `/admin` and its `sa:main` refresh now have a "🚫 חסימות" button (`sa:bans`)
  listing active bans with an unban button per row (same keyboard as `/bans`).
- `/search_user <telegram_id>` now also replies with the scope picker so a
  ban can be issued straight from the search result.

### 4. Terminal script (`scripts/ban_user.py`)

```
venv/bin/python scripts/ban_user.py <telegram_id|@username> --scope global|global_remove|local \
    [--group <chat_id>] [--reason "..."] [--actor <telegram_id>] [--unban] [--dry-run]
```

Loads `config.settings` (`.env`), uses the real
`database.session.AsyncSessionLocal`, and builds a live `aiogram.Bot` from the
configured token, so it must run on the host that runs the bot, from the
project root. `--group` takes a Telegram `chat_id` (not the internal `Group.id`)
and is required for `--scope local` and `--scope global_remove`. `--dry-run`
performs the database upsert and closes open join requests/sessions, but
returns before making any Telegram API call. Prints the Hebrew report and
exits `0`, or `1` on a hard failure (unknown scope, missing origin group for
`local`/`global_remove`, or an unresolvable identifier).

## Side effects of a ban

`ban_user()` always, regardless of access path:

1. Upserts the `UserBan` row for the chosen scope.
2. Marks every open `JoinRequest` (`pending` or `manual_review`) for that
   `telegram_id` as `banned`, with `resolved_at` set and a `ModerationLog` row
   carrying `details={"manual_ban": True, "scope": ..., "actor": ...}`.
3. Closes every open `VerificationSession` for that user
   (`completed_at=now`, `passed=False`).
4. Removes the user on Telegram per the scope's rules above.

`unban_user()` lifts the affected `UserBan` row(s) (`lifted_at`, `lifted_by`)
and calls `unban_chat_member` in every group that was part of the lifted
ban's reach.

## Purging pending requests from deleted accounts

A pending or manual-review `JoinRequest` can sit open forever if the
applicant deletes their Telegram account before an admin ever looks at it.
`moderation/actions.py::purge_deleted_requests()` sweeps for exactly that
case and returns a `PurgeReport` (`checked`, `deleted_found`, `removed`,
`request_gone`, `already_member`, `decline_failed`, `alive`, `inconclusive`,
`access_errors`, `per_group`, `stopped_early`). `per_group` is keyed by
`Group.id` (two groups with the same title stay apart) and each entry carries
its `title`.

### What the bot can and cannot know

Checked against the Bot API (7.10, as shipped with the installed aiogram):

- There is **no** method that lists pending join requests or chat members.
  The only methods touching a request are `approveChatJoinRequest` and
  `declineChatJoinRequest`.
- The `chat_join_request` update is sent only when a request is **created**.
  Nothing is sent when another admin approves or declines it inside the
  Telegram app, or when the user withdraws it. So an open `JoinRequest` row
  means "a request the bot once saw", not "a request Telegram still has".
  The number shown in the Telegram app is the real number of pending
  requests; the bot's number is always the same or larger.
- `getChatMember` on a pending requester returns status `left` ("not a
  member"). It can prove the account exists or was deleted, and that the
  user is now a member, but never that a request is still pending.
- `declineChatJoinRequest` is the only decisive call: `True` means the
  request existed and is now declined, `Bad Request: HIDE_REQUESTER_MISSING`
  means Telegram no longer has it. The purge sends it **only** for deleted
  accounts, so a live applicant is never declined.

This is why the production run reported `נבדקו 449` against roughly 30
requests visible in the app: the 449 were open rows across every managed
group, most of them requests that had been handled in the app or withdrawn
long before, and the old "לא ניתן לקבוע 408" bucket read as if the bot had
looked and found nothing, when in fact it cannot know.

Two places now close such rows when Telegram does give a decisive signal
(both database-only, nothing is sent to Telegram):

- `bot/handlers/member_events.py`: a `chat_member` join (not a member before,
  a member after) outside an outage closes the user's open rows for that
  chat as `approved` with `notes = "joined, request resolved outside the
  bot"`, and completes their open `VerificationSession` rows. This is what an
  approval inside the Telegram app looks like to the bot.
- `cb_manual_review` (approve / reject buttons): `HIDE_REQUESTER_MISSING`
  closes the row (`rejected`, `notes = "request no longer on Telegram
  (handled outside the bot)"`) instead of showing an error and leaving it
  open forever.

No age-based cleanup exists or is planned: an old row is not evidence that
the request is gone.

### Detection rule

The Bot API has no `is_deleted` field anywhere on `User`. The only reliable
signal is that `first_name` is a *required* field on a live account, so a
probe that **resolves successfully** but returns a `User`/`Chat` with an
empty `first_name` and no `username`/`last_name` means the account was
deleted:

1. Probe `getChatMember(chat_id, user_id)` for the request's group and user.
   - Success with an empty `first_name`/`username`/`last_name` → deleted
     candidate.
   - Success with a member status (`member`, `administrator`, `creator`, or
     `restricted` with `is_member`) → `already_member`: the request is gone.
   - Success with anything else → `alive`: the account exists, whether the
     request is still pending is unknown.
   - Any `TelegramAPIError` → the row is left alone for now; one extra
     read-only `getChat(user_id)` probe is made as a second check, *unless*
     the error is one of the access-problem strings below, in which case the
     row is reported and left untouched without the extra probe.
2. The extra `getChat(user_id)` probe: success with an empty profile means
   deleted, success with a name means `alive`; any error (the common case -
   most applicants never DMed the bot) means the row stays **inconclusive**
   and untouched.

### Errors that are NOT evidence of deletion

- `Bad Request: member not found` and `Bad Request: PARTICIPANT_ID_INVALID` -
  routine for any pending requester, zero discriminating power. Never
  inferred as deletion.
- `Bad Request: chat not found` from the `getChatMember` call (where
  `chat_id` is the group) means the bot lost access to that **group**, not
  that the user is gone. Reported in `access_errors`; the row is never
  touched.
- `Bad Request: not enough rights` means the bot lacks admin rights
  (`can_invite_users`) in that group. Also reported in `access_errors` and
  never touched. Closing a live applicant's request by mistake is the worst
  possible outcome of this feature, so both of these short-circuit straight
  to "leave it alone" without even trying the `getChat` fallback.

### What happens on apply

- `dry_run=True` (always the first pass): rows are only counted, nothing
  changes on Telegram or in the database.
- `dry_run=False`:
  - `already_member` rows are closed as `approved` with `notes = "already a
    member, request resolved outside the bot"`. No Telegram call.
  - Deleted candidates get `decline_chat_join_request(chat_id, user_id)`:
    - `True` → `removed` (Telegram still had the request), row closed with
      `notes = "account deleted (empty profile)"`.
    - `HIDE_REQUESTER_MISSING` → `request_gone` (Telegram no longer had it),
      row closed with `notes = "account deleted, request no longer on
      Telegram"`.
    - Any other error (access, flood, network) → `decline_failed`, the row
      stays **open**: nothing decisive came back, so the request may still
      exist. Access errors are also listed in `access_errors`.
  - `alive` and `inconclusive` rows are never touched.

  Closing a deleted candidate's row sets `decision = DecisionEnum.rejected`,
  `resolved_at = now`, closes the user's open `VerificationSession` rows for
  that chat, and adds a `ModerationLog` with `details={"purge_deleted": True,
  "evidence": "empty first_name", "decline_error": ...}`.

### The numbers in the report

| Label | Meaning |
| --- | --- |
| 📦 בקשות פתוחות במסד הנתונים של הבוט | `checked`: open rows the bot holds, not requests Telegram has |
| 🗑 חשבונות מחוקים | `deleted_found` |
| ✅ טלגרם עדיין הכיר את הבקשה, ונדחתה | `removed` (apply only) |
| 👻 טלגרם כבר לא הכיר את הבקשה, נסגרה אצל הבוט | `request_gone` (apply only) |
| 👥 כבר חברים בקבוצה | `already_member` |
| ⚠️ הדחייה נכשלה, השורה נשארה פתוחה | `decline_failed` (apply only) |
| ❔ החשבון קיים, לא ידוע אם הבקשה עדיין ממתינה | `alive` |
| ❓ טלגרם לא החזיר מידע, לא ידוע אם הבקשה קיימת | `inconclusive` |

Every report ends with a note that the bot cannot see Telegram's list and
that the number in the app is the real one. The terminal script prints the
same report as plain text.

Probes are throttled with `await asyncio.sleep(0.05)` between Telegram calls
and capped at `max_probes` (default 2000); hitting the cap sets
`report.stopped_early = True` and the report says so.

### Dry-run-then-confirm flow

Every access path below always runs a dry run first and shows the Hebrew
summary with a confirm button that carries no actor in the callback data.
The dry run's scope is instead parked server-side in a short-lived, one-shot
record keyed by the admin's telegram id (10 minute TTL,
`_store_pending_purge` / `_pop_pending_purge` in `ban_commands.py`). The
confirm press consumes that record: a missing or expired record (already
used, timed out, or never started) refuses and performs no purge. Nothing is
ever removed without that record, and the confirm press re-checks
authorization independently.

1. `/purge_deleted [chat_id]` in `bot/handlers/ban_commands.py` - super-admin
   only. Omit `chat_id` to scan every active, non-banned group; the confirm
   button is `purge:apply:all` for that case or `purge:apply:<group_id>` when
   a `chat_id` was given. The scope travels inside the button itself, so a
   button only ever acts on the dry run it came from: if a newer dry run (in
   any of these four places) overwrites the pending record before the button
   is pressed, the press is refused instead of running against the wrong
   scope.
2. The super-admin panel (`bot/handlers/superadmin.py`) has a
   "🧹 ממתינים של חשבונות מחוקים" button (`sa:purge_prompt`) that runs the
   same dry run across every group and shows the same `purge:apply:all`
   confirm button.
3. The group settings menu (`/settings` → "🧹 ממתינים של חשבונות מחוקים",
   `sm:purge:<group_id>` in `bot/handlers/settings_menu.py`): a dry run
   scoped to that one group, for the same people who can open that group's
   screen (the group owner or an admin registered for that group via
   `/settings`, plus the super-admin). The confirm button
   `sm:purge_apply:<group_id>` is only shown when there is something to act
   on, and acts only if the pending record was created by the same admin for
   the **same** group: a crafted confirm for a group that was never shown,
   or a second press, is refused.
4. `scripts/purge_deleted.py` - terminal CLI:

   ```
   venv/bin/python scripts/purge_deleted.py [--group <chat_id>] [--apply] [--actor <telegram_id>]
   ```

   Calls `init_db()` first (same reason as `scripts/ban_user.py`: the tables
   are created by the bot at startup), defaults to a dry run, and only
   mutates anything with `--apply`.
