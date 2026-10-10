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
`alive`, `inconclusive`, `access_errors`, `per_group`, `stopped_early`).

### Detection rule

The Bot API has no `is_deleted` field anywhere on `User`. The only reliable
signal is that `first_name` is a *required* field on a live account, so a
probe that **resolves successfully** but returns a `User`/`Chat` with an
empty `first_name` and no `username`/`last_name` means the account was
deleted:

1. Probe `getChatMember(chat_id, user_id)` for the request's group and user.
   - Success with an empty `first_name`/`username`/`last_name` → deleted
     candidate.
   - Success with anything else → alive.
   - Any `TelegramAPIError` → the row is left alone for now; one extra
     read-only `getChat(user_id)` probe is made as a second check, *unless*
     the error is one of the access-problem strings below, in which case the
     row is reported and left untouched without the extra probe.
2. The extra `getChat(user_id)` probe: success with an empty profile means
   deleted; any error (the common case - most applicants never DMed the bot)
   means the row stays **inconclusive** and untouched.

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

### What happens to a confirmed deleted candidate

- `dry_run=True` (always the first pass): added to `deleted_found`, nothing
  else changes.
- `dry_run=False`: `decline_chat_join_request(chat_id, user_id)` is called.
  - Success → counted in `removed`.
  - `not enough rights` / `chat not found` → `access_errors`, the row is
    left **open** (it could not actually be removed).
  - Any other error → treated as already gone on Telegram's side; the row is
    still closed, but the raw error string is stored in the `ModerationLog`
    row's `details.decline_error` so the bucketing above can be corrected
    from real data later (what Telegram actually returns for a vanished
    request is unverified).

  Closing a row sets `decision = DecisionEnum.rejected`,
  `resolved_at = now`, `notes = "account deleted (empty profile)"`, closes
  the user's open `VerificationSession` rows for that chat, and adds a
  `ModerationLog` with `details={"purge_deleted": True, "evidence": "empty
  first_name", "decline_error": ...}`.

Probes are throttled with `await asyncio.sleep(0.05)` between Telegram calls
and capped at `max_probes` (default 2000); hitting the cap sets
`report.stopped_early = True` and the report says so.

### Dry-run-then-confirm flow

Every access path below always runs a dry run first and shows the Hebrew
summary with an inline "🧹 בצע הסרה" button (plain `purge:apply`, carrying
no group id or actor in the callback data). The dry run's scope is instead
parked server-side in a short-lived, one-shot record keyed by the
super-admin's telegram id (10 minute TTL). The confirm press consumes that
record: a missing or expired record (already used, timed out, or never
started) refuses and performs no purge, telling the admin to run
`/purge_deleted` again. Nothing is ever removed without that record, and the
confirm press re-checks super-admin authorization independently.

1. `/purge_deleted [chat_id]` in `bot/handlers/ban_commands.py` - super-admin
   only. Omit `chat_id` to scan every active, non-banned group.
2. The super-admin panel (`bot/handlers/superadmin.py`) has a
   "🧹 ממתינים של חשבונות מחוקים" button (`sa:purge_prompt`) that runs the
   same dry run across every group and shows the same confirm button.
3. `scripts/purge_deleted.py` - terminal CLI:

   ```
   venv/bin/python scripts/purge_deleted.py [--group <chat_id>] [--apply] [--actor <telegram_id>]
   ```

   Calls `init_db()` first (same reason as `scripts/ban_user.py`: the tables
   are created by the bot at startup), defaults to a dry run, and only
   mutates anything with `--apply`.
