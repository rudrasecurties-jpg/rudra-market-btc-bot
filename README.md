# 🚀 Indian Market Bot Deploy Guide

Step-by-step guide to deploy your Telegram bot on Railway using GitHub.

> Beginner friendly — No technical knowledge required.

---

# 📌 What You Need

- Telegram account
- GitHub account
- Railway account
- Bot files:
  - `bot.py`
  - `requirements.txt`
  - `Procfile`
  - `runtime.txt`
  - `.env.example`

---

# 🟢 Step 1 — Create Telegram Bot Token

## Open Telegram

Search:

```bash
@BotFather
```

Send:

```bash
/newbot
```

Create bot name and username ending with `_bot`.

Example:

```text
mymarket2024_bot
```

BotFather will give a token:

```bash
1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ-example
```

⚠️ Keep this token safe.

---

# 🟢 Step 2 — Upload Code to GitHub

Create account:

👉 https://github.com/signup

Create repository:

```text
market-bot
```

Upload files:

```text
bot.py
requirements.txt
Procfile
runtime.txt
.env.example
```

⚠️ Do NOT upload `.env`

Commit changes.

---

# 🟢 Step 3 — Deploy on Railway

Open:

👉 https://railway.app

Steps:

```text
New Project
→ Deploy from GitHub repo
→ Select market-bot
→ Deploy Now
```

⚠️ First deploy may fail because BOT_TOKEN is not set yet.

---

# 🟢 Step 4 — Add Environment Variables

Go to:

```text
Project → Variables
```

Add:

```env
BOT_TOKEN=YOUR_BOT_TOKEN
```

Optional:

```env
CHANNEL_ID=@your_channel_username
ADMIN_ID=your_telegram_user_id
```

Get Telegram user ID from:

```text
@userinfobot
```

---

# 🟢 Step 5 — Test Bot

Send commands:

```bash
/start
/signal
/status
```

If bot replies successfully 🎉 your bot is live.

---

# 📌 Available Commands

| Command | Description |
|---|---|
| `/start` | Start bot |
| `/signal` | Get market signals |
| `/subscribe` | Enable auto signals |
| `/unsubscribe` | Disable auto signals |
| `/status` | Market status |

---

# ⚠️ Troubleshooting

Check Railway logs:

```text
Railway → Deployments → View Logs
```

---

# 🛠 Tech Stack

- Python
- Telegram Bot API
- Railway
- GitHub

---

# ⭐ Support

If this project helped you, give it a ⭐ on GitHub.
