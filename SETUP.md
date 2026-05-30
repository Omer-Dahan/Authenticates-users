# Telegram Join Request Moderation System — Setup Guide

## Quick Start (Local)

### 1. Prerequisites
- Python 3.11+
- Node.js 20+
- A Telegram Bot Token (from @BotFather)
- Your Telegram Group ID

### 2. Configure Environment
```bash
cp .env.example .env
```

Edit `.env`:
```env
BOT_TOKEN=your_bot_token_from_botfather
GROUP_ID=-1001234567890        # Your group's chat ID (negative number)
SECRET_KEY=change-this-random-string
ADMIN_USERNAME=admin
ADMIN_PASSWORD=your-secure-password
```

### 3. Install Python Dependencies
```bash
pip install -r requirements.txt
```

### 4. Initialize Database
```bash
python setup.py
```

### 5. Run the API Server
```bash
python run_api.py
```
API will be available at http://localhost:8000
API docs at http://localhost:8000/docs

### 6. Run the Bot
In a separate terminal:
```bash
python run_bot.py
```

### 7. Run the Dashboard (Development)
```bash
cd dashboard
npm install
npm run dev
```
Dashboard will be available at http://localhost:3000

---

## Docker Deployment

### 1. Build and Start All Services
```bash
cp .env.example .env
# Edit .env with your values
docker-compose up -d --build
```

Services:
- **Bot**: Telegram bot (internal)
- **API**: http://localhost:8000
- **Dashboard**: http://localhost:3000

### 2. View Logs
```bash
docker-compose logs -f bot
docker-compose logs -f api
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
   - ✅ Invite users via link
   - ✅ Ban users
   - ✅ Restrict members

### 3. Get Group ID
- Add @userinfobot to your group temporarily
- Or use the API: `https://api.telegram.org/bot<TOKEN>/getUpdates`
- The chat ID is a negative number like `-1001234567890`

### 4. Enable Join Request Approval
In your Telegram group settings:
- Group settings → Advanced → Who can add members → **Admins only**
- This triggers `chat_join_request` events

### 5. Enable Join Requests (Required!)
For the bot to receive join requests, the group must require approval:
- Group Info → Edit → Who can join → **Requires Admin Approval**

---

## Admin Panel

Access the web dashboard at http://localhost:3000

Default credentials (set in .env):
- Username: `admin`
- Password: `changeme`

### Dashboard Features
| Page | Function |
|------|----------|
| Dashboard | Stats overview, recent activity chart |
| Review Queue | Manually approve/reject/ban users |
| Rules | Create regex/keyword/blacklist rules |
| Verification | Configure challenge questions |
| Blacklist/Whitelist | Manage keywords and trusted users |
| Languages | Configure per-language score weights |
| Logs | Full audit trail with filtering |
| Configuration | Thresholds, messages, security mode |

---

## Bot Commands (in the group)

| Command | Description |
|---------|-------------|
| `/stats` | Show approval/rejection statistics |
| `/mode` | Change security mode (Normal/Strict/Lockdown) |
| `/pending` | List users awaiting manual review |
| `/reload` | Reload rules from database |
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

1. User sends a join request
2. Bot extracts: `first_name`, `last_name`, `username`
3. Engine runs all enabled rules and language filters
4. Fuzzy matching checks for Israeli names
5. Scores are summed
6. If `verification_required=true`, user is sent a question
7. Verification score is added to total
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

## Architecture

```
Request → Bot Handler
            ↓
         Moderation Engine
           ├── Rule Evaluator (regex/keyword/blacklist)
           ├── Language Detector (character set matching)
           └── Israeli Name Matcher (fuzzy, rapidfuzz)
            ↓
         Scoring → Decision
            ↓
    [Approved] [Rejected] [Banned] [Verification] [Manual Review]
            ↓
         Verification Engine (if required)
            ↓
         Final Decision → Telegram API
            ↓
         Database Log
```

---

## Important Notes

- This system uses **only the official Telegram Bot API** — no userbots, MTProto, or Telethon
- The bot **cannot** read user bio/about — only name, username, and verification answers
- The bot **must be an admin** in the target group with `can_invite_users` and `can_restrict_members`
- SQLite is used by default; for production scale, change `DATABASE_URL` to PostgreSQL
