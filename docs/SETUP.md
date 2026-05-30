# Telegram Join Request Moderation System — Setup Guide

## Quick Start (Local)

### 1. Prerequisites
- Python 3.11+
- A Telegram Bot Token (from @BotFather)
- Your Telegram Group ID

### 2. Configure Environment
```bash
cp .env.example .env
```

Edit `.env`:
```env
BOT_TOKEN=your_bot_token_from_botfather
SUPER_ADMIN_ID=123456789       # Your Telegram User ID
TRACKING_CHANNEL_ID=-1001234567890 # Optional: channel to send logging reports
```

### 3. Install Python Dependencies
```bash
pip install -r requirements.txt
```

### 4. Initialize Database
```bash
python scripts/setup_db.py
```

### 5. Run the Bot
```bash
python scripts/run_bot.py
```

---

## Docker Deployment

### 1. Build and Start Services
```bash
cp .env.example .env
# Edit .env with your values
docker-compose up -d --build
```

### 2. View Logs
```bash
docker-compose logs -f bot
```

### 3. Stop
```bash
docker-compose down
```

---

## Bot Setup in Telegram

### 1. Create the Bot
1. Open @BotFather on Telegram
2. Send `/newbot`
3. Follow instructions to get your `BOT_TOKEN`

### 2. Add Bot to Group
1. Add your bot to the target group
2. Make it an **Administrator** with these permissions:
   - ✅ Invite users via link (or Approve new members)
   - ✅ Ban users
   - ✅ Restrict members

### 3. Get Group ID
- Add @userinfobot to your group temporarily to get the group's Chat ID.
- The chat ID is a negative number like `-1001234567890`.

### 4. Enable Join Request Approval
In your Telegram group settings:
- Group Info → Edit → Who can join → **Requires Admin Approval**
- This ensures Telegram generates `chat_join_request` events instead of adding members directly.

---

## Bot Settings and Admin Interface

Since the system is 100% Telegram-Native, you can configure everything directly inside Telegram:
- Private message the bot with `/settings` to open the interactive configuration dashboard (for group owners).
- Use `/admin` (accessible only to `SUPER_ADMIN_ID`) to open the system-wide super-administrator panel.

---

## Bot Commands (in the group/private chat)

| Command | Description |
|---------|-------------|
| `/settings` | Open interactive settings dashboard (Private DM only) |
| `/admin` | Open Super-Admin control panel (Private DM only) |
| `/stats` | Show approval/rejection statistics |
| `/help` | Show command help |

---

## Security Modes

| Mode | Behavior |
|------|----------|
| 🟢 Normal | Standard scoring thresholds apply |
| 🟡 Strict | Verification always required regardless of score |
| 🔴 Lockdown | All requests go to manual review queue |

---

## How Scoring Works

1. User sends a join request.
2. Bot extracts: `first_name`, `last_name`, `username`.
3. Engine runs all enabled rules and language filters.
4. Fuzzy matching checks for Israeli names.
5. Scores are summed.
6. If verification is required, user is sent a question via private DM.
7. Verification score is added to the total.
8. Decision is made based on threshold:
   - `score >= approve_threshold (60)` → ✅ Approve
   - `score in manual_review_range (30-60)` → 👀 Manual Review
   - `score < reject_threshold (0)` → ❌ Reject
   - `score <= auto_ban_threshold (-100)` → 🔨 Ban

### Example Score Calculation
| Signal | Score |
|--------|-------|
| Hebrew characters in name | +70 |
| Israeli name match (fuzzy) | +40 |
| Verification passed | +100 |
| Blacklisted username keyword | -100 |

---

## Important Notes

- This system uses **only the official Telegram Bot API** — no userbots, MTProto, or Telethon.
- The bot **cannot** read user bio/about — only name, username, and verification answers.
- The bot **must be an admin** in the target group with `can_invite_users` (approve members) and `can_restrict_members`.
- SQLite is used by default; for production scale, change `DATABASE_URL` to PostgreSQL.
