# Outage Recovery

The bot detects its own downtime (crash, deploy, server/network outage) on the
next startup and automatically catches up on what it missed, instead of
silently losing pending join requests and verification timers.

## How outage detection works

- A `BotState` row (id=1) tracks `last_heartbeat_at`, written every
  `HEARTBEAT_INTERVAL_SECONDS` (default 60s) by a background loop while the
  bot is running.
- On startup, before polling begins, the bot compares `now` against
  `last_heartbeat_at`:
  - No `BotState` row at all → first run ever, state is seeded, no recovery.
  - Gap `<= OUTAGE_THRESHOLD_SECONDS` (default 180s) → normal restart, no
    recovery needed.
  - Gap `> OUTAGE_THRESHOLD_SECONDS` → an outage is recorded
    (`outage_started_at`, `outage_seconds`, capped at 86400s - see below) and
    a recovery sweep runs.
- Detection is fully automatic. There is no manual maintenance-mode toggle.

## What recovery covers

1. **Telegram backlog replay.** `delete_webhook` is called with
   `drop_pending_updates=False` so any `chat_join_request`, `message`
   (verification answers) and `chat_member` updates that queued up while the
   bot was offline are replayed and handled by the normal routers as soon as
   polling starts.
2. **Leftover join requests.** After a `RECOVERY_GRACE_SECONDS` (default 30s)
   pause - to let the replayed backlog settle first - a sweep
   (`recovery/sweep.py`) scans every active, non-banned group for
   `JoinRequest` rows still `pending`/`manual_review` with `resolved_at IS
   NULL`:
   - `get_chat_member` shows the user already joined (member/admin/creator, or
     restricted-but-still-a-member) → the request is marked `approved`
     retroactively, no DM sent.
   - `get_chat_member` shows `kicked` → marked `rejected`, no DM sent.
   - Otherwise, verification catch-up runs (see below).
3. **Verification catch-up.**
   - No session yet and the question was never sent (`notified_at IS NULL`) →
     the question is sent now and the countdown starts *from now*, not from
     the original request time. If the group has no enabled question, the
     same score-based fallback used in `bot/handlers/join_requests.py` runs.
   - A session exists and was already notified before the outage began →
     `expires_at` is extended by the outage duration (never shortened), and
     the user gets one short Hebrew notice that the timer was extended. The
     question itself is never re-sent.
4. **Member-event catch-up during the outage window**
   (`bot/handlers/member_events.py`, requires `chat_member` in
   `allowed_updates` and the bot to be a group admin): a join with no matching
   `JoinRequest` during a detected outage gets the same "send question, start
   countdown" treatment as above; a leave/kick during the outage closes any
   open session/request as rejected. Outside a detected outage this handler
   only records the event - it never DMs, since the normal join-request flow
   already owns that. The `OutageLog` row this catch-up relies on is opened by
   `detect_outage()` itself, before `dp.start_polling` runs - not by the sweep
   - since the replayed backlog can start arriving within seconds of startup,
   well before the sweep's grace-period sleep elapses.

   **Two hard limits apply here:**
   - Telegram does not deliver updates for a type that was not in
     `allowed_updates` when those updates were originally created. `chat_member`
     was added to `allowed_updates` in this version, so this catch-up only
     works for outages that happen *after* this version is deployed - it
     cannot backfill `chat_member` events from before the deploy, even if they
     are still within Telegram's retention window.
   - Nothing older than 24 hours can be recovered at all, regardless of
     `allowed_updates` (see below).
5. **Owner report.** Each group that had at least one recovered/extended user
   gets one Hebrew summary DM to its owner: outage window, duration, requests
   recovered, timers extended, errors skipped.
6. **Idempotency.** Re-running the sweep (e.g. a second restart before it
   finished) never sends a duplicate question or notice and never creates a
   duplicate session, because each action is gated on state the previous run
   already changed (`resolved_at`, `notified_at`, a future `expires_at`).
7. **Cleanup-loop safety.** The periodic expired-session cleanup
   (`verification/cleanup.py`) waits for the recovery sweep to finish before
   its first run (bounded by a safety timeout so a stuck sweep can't block it
   forever), and skips any session whose `expires_at` falls inside an outage
   window that hasn't finished being reconciled (`OutageLog.ended_at IS
   NULL`) - so it can't decline a user the sweep is about to approve or
   extend.

## Known Telegram errors from `decline_chat_join_request` in cleanup

The cleanup loop always marks an expired session's `JoinRequest` as
`rejected` in the database regardless of whether the Telegram-side
`decline_chat_join_request` call succeeds - the call is best-effort, and a
failure is logged as a warning, never raised. Confirmed from production
logs:

- `Forbidden: user is deactivated` - the Telegram account was deleted
  between the join request and the timeout. Not an error in the bot; there
  is nothing left on Telegram's side to decline.
- `Bad Request: HIDE_REQUESTER_MISSING` - the join request itself no longer
  exists on Telegram's side (the user cancelled it, or it was already
  resolved outside the bot). This confirms the "treated as already gone"
  bucket in `docs/BAN.md`'s purge-deleted error handling.
- `Forbidden: bot can't initiate conversation with a user` / `Forbidden: bot
  was blocked by the user` - seen when the *verification question* send
  fails (`bot/handlers/join_requests.py`), not the decline itself: the user
  never started a chat with the bot, or blocked it. The join request stays
  pending until the cleanup loop's timeout declines it.
- Transient network errors (e.g. `ClientConnectorError: ... Connection reset
  by peer`) are infrastructure noise, not a signal about the user or the
  request. The row is already marked `rejected` locally before the Telegram
  call is attempted, so a network failure here means only that Telegram
  never received the explicit decline - the local decision is unaffected.

None of these change the local decision: the session is always closed as
`rejected` in the database so the bot's own state stays consistent even when
the Telegram-side cleanup call fails.

## The 24-hour Telegram window - and what is NOT recoverable

Telegram only retains undelivered updates for **up to 24 hours**. Anything
older is gone from Telegram's side before the bot can ever see it. Recovery
therefore caps the applied timer extension at 86400 seconds even if the
measured downtime gap is larger.

More fundamentally, **the Bot API has no method to list pending join requests
and no method to list current chat members.** The only primitives available
are `getChatMember(chat_id, user_id)` (point lookup for one known user),
`getChatMemberCount`, and the `chat_member` update stream (no backfill for the
past, requires the bot to already be an admin with `chat_member` explicitly in
`allowed_updates`). Because of this:

- A join request that arrived, was never stored as a `JoinRequest` (e.g. the
  bot was down at the exact moment `chat_join_request` fired and the update
  fell outside the 24h Telegram retention window), and whose user never
  interacted with the bot again is **not recoverable** - there is no API call
  that can enumerate "join requests Telegram is still holding for chat X".
  Recovery only reconciles requests the bot already has a `JoinRequest` row
  for.
- A member who joined/left more than 24h before the bot came back online, and
  for whom the bot has no `JoinRequest` or `MemberEvent` row, cannot be
  discovered retroactively - there is no "list all members" call to diff
  against.
- Recovery cannot tell the difference between "this user never received our
  question" and "this user received it via some channel other than this bot"
  - it relies entirely on `VerificationSession.notified_at`, which is only
  ever set by this bot's own send calls.

## SQLite WAL mode and backups

The database connection (`database/session.py`) switches SQLite into WAL
(Write-Ahead Log) journal mode on every new connection, so the app's
concurrent async writers (heartbeat loop, cleanup loop, update handlers) do
not fail each other with "database is locked" errors. A warning is logged if
a connection ever fails to switch into WAL mode (for example a read-only
file, some network filesystems, or a database owned by another user), since
in that case the old lock-contention behavior silently comes back.

In WAL mode, committed data can live in two sidecar files next to the main
database file, not just in the `.db` file itself:

- `<database>.db-wal` - the write-ahead log, holding recently committed
  transactions that have not yet been checkpointed into the main file.
- `<database>.db-shm` - a shared-memory index used to coordinate readers and
  writers over the WAL file.

A backup or copy that only takes the `.db` file while the bot is running can
therefore miss committed data sitting in `-wal` that has not been
checkpointed yet. A safe backup must do one of the following:

- Copy the `.db`, `-wal`, and `-shm` files together, while the bot is fully
  stopped (so no writer can checkpoint or write mid-copy).
- Or use SQLite's own backup mechanism instead of copying files directly,
  which is safe to run while the bot is live:
  - `sqlite3 data/moderation.db ".backup backup.db"`
  - or, from any SQLite connection: `VACUUM INTO 'backup.db'`

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `OUTAGE_THRESHOLD_SECONDS` | 180 | Heartbeat gap above which a restart is treated as an outage instead of a normal restart. |
| `HEARTBEAT_INTERVAL_SECONDS` | 60 | How often the running bot records `last_heartbeat_at`. |
| `RECOVERY_GRACE_SECONDS` | 30 | Delay before the recovery sweep starts, to let the replayed Telegram backlog be handled by the normal routers first. |
