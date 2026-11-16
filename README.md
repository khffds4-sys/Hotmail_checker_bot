# Hotmail Checker — Telegram Bot

High-speed Hotmail/Outlook account checker as a Telegram bot.

## Features

- Proxyless + optional proxy rotation
- Thread control (admin)
- Free user daily limit **5000 lines** (extra auto-capped)
- All hits saved in bot DB — only admin sees full pool
- Admin **Download All Hits + Clear**
- Default keywords: netflix, spotify, steam, paypal, amazon, instagram, tiktok, xbox…
- Custom keyword add (admin button)
- Inline button UI
- My Hits file for each user

## Setup

1. Create bot with [@BotFather](https://t.me/BotFather) → copy token  
2. Get your Telegram user id (e.g. @userinfobot)  
3. Install & run:

```bash
cd hotmail_bot
pip install -r requirements.txt

export BOT_TOKEN="123456:ABC-DEF..."
export ADMIN_IDS="123456789"          # your telegram id, comma for multiple
export DAILY_LIMIT=5000
export DEFAULT_THREADS=80

python bot.py
```

## Render / VPS

Same env vars. Start command:

```
python bot.py
```

Free Render sleeps — for always-on use a small VPS or paid instance.

## User flow

1. `/start` → menu buttons  
2. **Start Check** → paste `email:pass` or send .txt  
3. Bot checks with current threads/proxies  
4. Finishes → sends valid hits file + stats  
5. **My Hits** → personal history  

## Admin flow

Set `ADMIN_IDS` to your telegram id.

Buttons:
- Download All Hits + Clear
- Set Threads
- Add Proxies / Clear Proxies
- Add Keyword
- Bot Stats

## Notes

- Login uses Microsoft `login.live.com` flow (same family as public checkers)
- Proxyless works; residential proxies recommended for heavy volume
- Threads 50–120 is stable on most hosts
