#!/usr/bin/env python3
"""
🤖 VPS BOT HOSTING MANAGER — User Interface Bot
- Users upload ZIP, bot scans, deploys via PM2
- Flagged uploads go to admin via approval bot
- Notification worker DMs users on approve/reject
"""
from __future__ import annotations
import os, sys, asyncio, zipfile, shutil, subprocess, json, time, re, uuid, errno, sqlite3, html, shlex
import traceback
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List, Tuple

try:
    import aiosqlite
except ImportError:
    print("Missing aiosqlite. pip install -r requirements.txt")
    sys.exit(1)

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from telegram.error import BadRequest, RetryAfter

from script_scanner import scan_file

# RICH: Bot API 10.3 rich-message renderer; every call has a PTB fallback.
from core import rich_message as rm

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:
    Fernet = None
    InvalidToken = Exception

# ================== CONFIG ==================
BOT_TOKEN           = os.getenv("BOT_TOKEN", "")
APPROVAL_BOT_TOKEN  = os.getenv("APPROVAL_BOT_TOKEN", "")
OWNER_ID            = 5628671567
ADMIN_IDS           = [5628671567]
SUPPORT_CHANNEL     = "https://t.me/pdf_making_hub"
# FEATURE3: Credit shown on user-facing screens; configure without changing code flow.
CREDIT_OWNER_USERNAME = "OG_SAGAR_ddos"  # no @

# UI: Shared visual language for the Telegram dashboard.
class UI:
    DIV = "━━━━━━━━━━━━━━━━━━"
    DIV_THIN = "─────"
    RUNNING = "🟢"; STOPPED = "🔴"; PENDING = "⏳"; WARN = "🟡"
    ERROR = "❌"; SUCCESS = "✅"; INFO = "ℹ️"; LOADING = "⏳"
    PYTHON = "🐍"; NODE = "🟢"; WHATSAPP = "💬"; GITHUB = "🐙"
    ZIP = "📦"; FILE = "📄"; DEPLOY = "📦"; ENV = "🔐"; LIMITS = "⚙️"
    LOGS = "📋"; STATUS = "📊"; START = "🟢"; STOP_ACTION = "🔴"
    RESTART = "🔄"; DELETE = "🗑️"; BACK = "🔙"; REFRESH = "🔄"
    ADD = "➕"; EDIT = "✏️"; USERS = "👥"; BOTS = "🤖"; ADMIN = "🔧"
    BROADCAST = "📢"; AUDIT = "📋"; MAINTENANCE = "⚙️"; PREMIUM = "💎"
    BAN = "🚫"; MUTE = "🔇"; UNMUTE = "🔊"
    USER = "👤"; NAME = "📛"; ID = "🆔"; DOWNLOAD = "⬇️"; EXTRACT = "📦"
    SAVE = "📄"; SCAN = "🔎"; LAUNCH = "🚀"; REVIEW = "🚨"; OPEN = "▶️"
    HOME = "🏠"; ARROW = "👇"; HIGH = "🔴"; MEDIUM = "🟡"

def credit_footer() -> str:
    # FIX_9: Use double-quoted HTML href for Telegram-compatible credit links.
    # FEATURE3: Centralized credit footer.
    username = re.sub(r"[^A-Za-z0-9_]", "", CREDIT_OWNER_USERNAME or "")
    if not username:
        return ""
    return f'\n\n👑 Bot by <a href="https://t.me/{username}">@{username}</a>'
HOME_VIDEO_URL      = "https://files.catbox.moe/m4sadt.mp4"
# BUGFIX 2: Match the actual default screen session used by the VPS startup commands.
PANEL_SCREEN_SESSION = os.getenv("PANEL_SCREEN_SESSION", "panel")
APPROVAL_SCREEN_SESSION = os.getenv("APPROVAL_SCREEN_SESSION", "approval")

HOSTED_BOTS_DIR     = Path("/root/hosted_bots")
LOGS_DIR            = Path("/root/hosted_bots_logs")
DATABASE_PATH       = HOSTED_BOTS_DIR / "inf" / "bot_data.db"

MAX_ZIP_SIZE_MB     = 50
# FEATURE3: Public GitHub deployment limits.
GITHUB_MAX_REPO_SIZE_MB = 200
# FIX_6: Use single escaped dots so real HTTPS GitHub URLs match the validation regex.
GITHUB_URL_RE = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?$")
GITHUB_TIMEOUT_SECONDS = 300
GITHUB_ALLOWED_SINGLE_EXTS = {".py", ".js", ".ts", ".mjs", ".cjs"}
MAX_BOTS_PER_USER   = 10
MAX_FILES_PER_SCAN  = 300
MUTE_DURATION_HOURS = 2
CACHE_TTL           = 120
NOTIFY_POLL_SECONDS = 10
NOTIFIED_USERS_RETENTION_DAYS = 7
DEPLOY_SEMAPHORE    = asyncio.Semaphore(3)

# ================== FEATURE 2: RESOURCE LIMITS / ADMIN ==================
LIMIT_MIN_MEMORY_MB = 64
LIMIT_DEFAULT_MEMORY_MB = 256
LIMIT_MAX_MEMORY_MB = 2048
LIMIT_SMALL_VPS_MAX_MB = 512
LIMIT_DEFAULT_CPU_PERCENT = 100
LIMIT_MIN_CPU_PERCENT = 1
LIMIT_MAX_CPU_PERCENT = 100
LIMIT_DEFAULT_CAP_PERCENT = 80
LIMIT_INPUT_TIMEOUT_SECONDS = 120
LIMIT_PAGE_SIZE = 10
AUDIT_PAGE_SIZE = 20
AUDIT_PAGE_MAX = 50
BROADCAST_RATE_PER_SECOND = 30
RESOURCE_MONITOR_SECONDS = 15

# ================== ENV MANAGER ==================
ENV_KEY_FILE = Path("/root/HostBotv3/.env_encryption_key")
ENV_KEY_VERSION = 1
ENV_MAX_KEYS = 50
ENV_MAX_VALUE_BYTES = 4096
ENV_MAX_KEY_LENGTH = 128
ENV_TIMEOUT_SECONDS = 300
ENV_RATE_LIMIT = 10
ENV_RATE_WINDOW = 60
ENV_PAGE_SIZE = 10
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
ENV_SECRET_KEY_RE = re.compile(r"^.*(TOKEN|SECRET|KEY|PASSWORD|PASS|PWD|CREDENTIAL).*$", re.IGNORECASE)
ENV_MIXED_ALNUM_RE = re.compile(r"^(?=.*[A-Za-z])(?=.*[0-9])[A-Za-z0-9]+$")
_env_fernet = None
_env_feature_error = None
_env_schema_ready = False
_env_rate: Dict[int, List[float]] = {}
FEATURE2_MIGRATION_OK = False


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_env_crypto() -> bool:
    """Load or create the shared Fernet key. Failure disables only env encryption."""
    global _env_fernet, _env_feature_error
    _env_fernet = None
    _env_feature_error = None
    if Fernet is None:
        _env_feature_error = "cryptography package unavailable"
        return False
    try:
        override = os.getenv("ENV_ENCRYPTION_KEY")
        if override:
            key = override.strip().encode("ascii")
        else:
            ENV_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
            try:
                key = ENV_KEY_FILE.read_bytes().strip()
            except FileNotFoundError:
                key = Fernet.generate_key()
                fd = os.open(str(ENV_KEY_FILE), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    os.write(fd, key)
                    os.fsync(fd)
                finally:
                    os.close(fd)
            os.chmod(ENV_KEY_FILE, 0o600)
        _env_fernet = Fernet(key)
        return True
    except Exception:
        _env_feature_error = "encryption key unavailable or corrupt"
        log_err("Environment encryption unavailable", traceback.format_exc())
        return False


def check_env_schema() -> bool:
    """Check whether the Feature-1 schema migration has completed (PRAGMA user_version >= 1).

    The hosting bot normally sets this via migrate_env_schema() during
    _post_init, but this lazy check lets env_available() recover if the
    schema becomes ready after startup or if state was reset.
    """
    global _env_schema_ready
    try:
        with sqlite3.connect(DATABASE_PATH, timeout=10) as conn:
            conn.execute("PRAGMA busy_timeout=10000")
            _env_schema_ready = int(conn.execute("PRAGMA user_version").fetchone()[0]) >= 1
            return _env_schema_ready
    except Exception:
        _env_schema_ready = False
        log_err("Environment schema check failed", traceback.format_exc())
        return False


def env_available() -> bool:
    """Return whether encryption and the Feature-1 schema are currently usable.

    The hosting bot owns migrations, but the encryption/schema state can change
    while the process is running (for example after another panel instance
    completes migration).  Re-checking the schema lazily avoids a permanent
    false-negative after startup.
    """
    if _env_fernet is None:
        return False
    global _env_schema_ready
    if not _env_schema_ready:
        try:
            _env_schema_ready = check_env_schema()
        except Exception:
            _env_schema_ready = False
    return bool(_env_schema_ready)


def encrypt_env_value(value: str) -> str:
    if not env_available():
        raise RuntimeError("Encryption unavailable")
    return f"v{ENV_KEY_VERSION}:" + _env_fernet.encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_env_value(value: str) -> str:
    if not env_available():
        raise RuntimeError("Encryption unavailable")
    if value.startswith("v") and ":" in value:
        version_text, value = value.split(":", 1)
        if version_text != f"v{ENV_KEY_VERSION}":
            raise InvalidToken
    return _env_fernet.decrypt(value.encode("ascii")).decode("utf-8")


def validate_env_key(key: str) -> None:
    if not isinstance(key, str) or not key or len(key) > ENV_MAX_KEY_LENGTH or not ENV_KEY_RE.fullmatch(key):
        raise ValueError("Invalid environment key")


def validate_env_value(value: str) -> None:
    if not isinstance(value, str) or value == "":
        raise ValueError("Empty value not allowed")
    if "\x00" in value:
        raise ValueError("Null bytes not allowed")
    if "\n" in value or "\r" in value:
        raise ValueError("Multi-line values not allowed")
    if len(value.encode("utf-8")) > ENV_MAX_VALUE_BYTES:
        raise ValueError("Value too large (max 4096 bytes)")


def is_secret_env(key: str, value: str) -> bool:
    return bool(ENV_SECRET_KEY_RE.fullmatch(key) or (len(value) >= 20 and ENV_MIXED_ALNUM_RE.fullmatch(value)))


def mask_env_value(value: str) -> str:
    if len(value) >= 8:
        return value[:4] + "***" + value[-4:]
    return "***"


def quote_env_value(value: str) -> str:
    # Data-only .env representation. No shell expansion occurs.
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + escaped + '"'


def env_rate_allowed(uid: int) -> bool:
    now = time.monotonic()
    recent = [t for t in _env_rate.get(uid, []) if now - t < ENV_RATE_WINDOW]
    if len(recent) >= ENV_RATE_LIMIT:
        _env_rate[uid] = recent
        return False
    recent.append(now)
    _env_rate[uid] = recent
    return True


def env_error_message(exc: Exception) -> str:
    if isinstance(exc, PermissionError):
        return "❌ File permission issue. Contact admin."
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == errno.ENOSPC:
        return "❌ Disk full. Contact admin."
    if isinstance(exc, FileNotFoundError):
        return "❌ Bot directory missing."
    if isinstance(exc, InvalidToken):
        return "❌ Encryption key mismatch. Contact admin."
    if isinstance(exc, sqlite3.OperationalError):
        msg = str(exc).lower()
        if "busy" in msg or "locked" in msg:
            return "❌ Database busy. Try again."
        return "❌ Database error. Try again."
    if isinstance(exc, ValueError):
        return f"❌ {exc}"
    return "❌ Operation failed. Check logs."

HOSTED_BOTS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

# ================== CACHE ==================
_cache: Dict[str, tuple] = {}
def cache_get(k):
    if k in _cache:
        ts, v = _cache[k]
        if time.time() - ts < CACHE_TTL: return v
        del _cache[k]
    return None
def cache_set(k, v): _cache[k] = (time.time(), v)
def cache_invalidate(prefix):
    for k in [k for k in _cache if k.startswith(prefix)]: del _cache[k]

# ================== SKIP EXTENSIONS FOR SCAN ==================
SKIP_SCAN_EXTS = {
    '.png','.jpg','.jpeg','.gif','.ico','.svg','.webp','.bmp',
    '.so','.dll','.dylib','.whl','.egg','.pyc','.pyo',
    '.mp3','.mp4','.avi','.mkv','.wav','.ogg','.flac',
    '.zip','.tar','.gz','.bz2','.7z','.rar','.xz',
    '.woff','.woff2','.ttf','.eot','.otf',
    '.exe','.bin','.dat','.db','.sqlite','.sqlite3',
    '.pdf','.doc','.docx','.xls','.xlsx'
}

# ================== LOGGING ==================
def log_err(msg, extra=None):
    try:
        with open(LOGS_DIR / "errors.log", "a") as f:
            f.write(f"[{datetime.now()}] {msg}\n")
            if extra: f.write(f"EXTRA: {extra}\n")
    except Exception:
        pass

def log_notify(msg):
    try:
        with open(LOGS_DIR / "notifications.log", "a") as f:
            f.write(f"[{datetime.now()}] {msg}\n")
    except Exception:
        pass

def log_admin_notify_failure(admin_id, error, text):
    try:
        with open(LOGS_DIR / "admin_notification_failures.log", "a") as f:
            preview = str(text).replace("\n", " ")[:500]
            f.write(
                f"[{datetime.now()}] admin={admin_id} "
                f"error={type(error).__name__}: {error} text={preview}\n"
            )
    except Exception:
        pass

# ================== ENV MIGRATION ==================
async def migrate_env_schema() -> bool:
    """Hosting bot owns Feature-1 and Feature-2 migrations under one DB lock."""
    for attempt in range(3):
        try:
            async with aiosqlite.connect(DATABASE_PATH) as conn:
                await conn.execute("PRAGMA busy_timeout=10000;")
                await conn.execute("PRAGMA journal_mode=WAL;")
                await conn.execute("BEGIN IMMEDIATE")
                cur = await conn.execute("PRAGMA user_version")
                version = int((await cur.fetchone())[0])
                if version < 1:
                    await conn.execute("""CREATE TABLE IF NOT EXISTS bot_env_vars (
                        bot_id TEXT NOT NULL, env_key TEXT NOT NULL, encrypted_value TEXT NOT NULL,
                        enc_version INTEGER NOT NULL DEFAULT 1, is_secret INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                        PRIMARY KEY (bot_id, env_key)
                    )""")
                    await conn.execute("CREATE INDEX IF NOT EXISTS idx_bot_env_vars_bot ON bot_env_vars(bot_id)")
                    version = 1
                if version < 2:
                    await conn.execute("""CREATE TABLE IF NOT EXISTS bot_limits (
                        bot_id TEXT PRIMARY KEY, memory_limit_mb INTEGER NOT NULL DEFAULT 256,
                        cpu_limit_percent INTEGER NOT NULL DEFAULT 100, restart_on_exceed INTEGER NOT NULL DEFAULT 1,
                        last_exceeded_at TEXT, exceed_count INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                    )""")
                    await conn.execute("""CREATE TABLE IF NOT EXISTS admin_audit (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER NOT NULL, action TEXT NOT NULL,
                        target TEXT, details TEXT, created_at TEXT NOT NULL
                    )""")
                    await conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_created ON admin_audit(created_at DESC)")
                    await conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_admin ON admin_audit(admin_id)")
                    await conn.execute("""CREATE TABLE IF NOT EXISTS user_bans (
                        user_id INTEGER PRIMARY KEY, banned_by INTEGER NOT NULL, reason TEXT, created_at TEXT NOT NULL
                    )""")
                    await conn.execute("""CREATE TABLE IF NOT EXISTS app_settings (
                        key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
                    )""")
                    now = utc_now_iso()
                    defaults = {
                        "maintenance_mode": "0", "deploys_enabled": "1",
                        "default_memory_mb": str(LIMIT_DEFAULT_MEMORY_MB),
                        "max_memory_per_bot_mb": str(LIMIT_MAX_MEMORY_MB),
                        "vps_cap_percent": str(LIMIT_DEFAULT_CAP_PERCENT),
                    }
                    for k, v in defaults.items():
                        await conn.execute("INSERT OR IGNORE INTO app_settings(key,value,updated_at) VALUES(?,?,?)", (k,v,now))
                    await conn.execute("""INSERT OR IGNORE INTO bot_limits
                        (bot_id,memory_limit_mb,cpu_limit_percent,restart_on_exceed,created_at,updated_at)
                        SELECT bot_id,256,100,1,COALESCE(created_at,?),? FROM bot_registry""", (now,now))
                    version = 2
                if version >= 2:
                    await conn.execute("PRAGMA user_version = 2")
                await conn.commit()
                return True
        except sqlite3.OperationalError:
            if attempt == 2:
                log_err("feature migrations database error", traceback.format_exc())
            else:
                await asyncio.sleep(0.5 * (attempt + 1))
        except Exception:
            log_err("feature migrations failed", traceback.format_exc())
            return False
    return False

# ================== DB ==================
async def init_db():
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA busy_timeout=10000;")
        await conn.executescript("""
            CREATE TABLE IF NOT EXISTS bot_registry (
                bot_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                dir TEXT NOT NULL,
                type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                extra TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_reg_user ON bot_registry(user_id);

            CREATE TABLE IF NOT EXISTS pending_deploys (
                bot_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                staging_dir TEXT NOT NULL,
                bot_type TEXT NOT NULL,
                findings TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                scanned_at TEXT,
                reviewed_by INTEGER,
                reviewed_at TEXT,
                reason TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_deploys(status);

            CREATE TABLE IF NOT EXISTS premium_users (
                user_id INTEGER PRIMARY KEY,
                added_at TEXT NOT NULL,
                added_by INTEGER
            );

            CREATE TABLE IF NOT EXISTS muted_users (
                user_id INTEGER PRIMARY KEY,
                mute_until TEXT NOT NULL,
                reason TEXT
            );

            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS notified_users (
                bot_id TEXT PRIMARY KEY,
                notified_at TEXT
            );
        """)
        await conn.commit()

class DB:
    @staticmethod
    async def get_user_bots(uid):
        ck = f"ubots_{uid}"
        c = cache_get(ck)
        if c is not None: return c
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            if uid in ADMIN_IDS:
                cur = await conn.execute("SELECT * FROM bot_registry")
            else:
                cur = await conn.execute("SELECT * FROM bot_registry WHERE user_id=?", (uid,))
            rows = await cur.fetchall()
            res = {}
            for r in rows:
                res[r["bot_id"]] = {
                    "user_id": r["user_id"], "name": r["name"],
                    "dir": r["dir"], "type": r["type"],
                    "created_at": r["created_at"],
                    **(json.loads(r["extra"]) if r["extra"] else {})
                }
            cache_set(ck, res)
            return res

    @staticmethod
    async def add_bot(bid, uid, name, bdir, btype, extra=None):
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO bot_registry VALUES (?,?,?,?,?,?,?)",
                (bid, uid, name, bdir, btype, datetime.now().isoformat(),
                 json.dumps(extra) if extra else None)
            )
            await conn.commit()
        cache_invalidate("ubots_")

    @staticmethod
    async def remove_bot(bid):
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            await conn.execute("DELETE FROM bot_registry WHERE bot_id=?", (bid,))
            await conn.commit()
        cache_invalidate("ubots_")

    @staticmethod
    async def get_env_vars(bid):
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA busy_timeout=10000;")
            cur = await conn.execute(
                "SELECT env_key, encrypted_value, enc_version, is_secret FROM bot_env_vars WHERE bot_id=? ORDER BY env_key COLLATE NOCASE",
                (bid,)
            )
            return [dict(row) for row in await cur.fetchall()]

    @staticmethod
    async def env_count(bid):
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            await conn.execute("PRAGMA busy_timeout=10000;")
            cur = await conn.execute("SELECT COUNT(*) FROM bot_env_vars WHERE bot_id=?", (bid,))
            row = await cur.fetchone()
            return int(row[0])

    @staticmethod
    async def upsert_env(bid, key, value):
        validate_env_key(key)
        validate_env_value(value)
        if not env_available():
            raise RuntimeError("Encryption unavailable")
        encrypted = encrypt_env_value(value)
        now = utc_now_iso()
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            await conn.execute("PRAGMA busy_timeout=10000;")
            await conn.execute("BEGIN IMMEDIATE")
            cur = await conn.execute("SELECT 1 FROM bot_env_vars WHERE bot_id=? AND env_key=?", (bid, key))
            exists = await cur.fetchone()
            if not exists:
                cur = await conn.execute("SELECT COUNT(*) FROM bot_env_vars WHERE bot_id=?", (bid,))
                if int((await cur.fetchone())[0]) >= ENV_MAX_KEYS:
                    await conn.rollback()
                    raise ValueError("Maximum 50 environment variables allowed")
            await conn.execute(
                "INSERT INTO bot_env_vars(bot_id,env_key,encrypted_value,enc_version,is_secret,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(bot_id,env_key) DO UPDATE SET encrypted_value=excluded.encrypted_value, "
                "enc_version=excluded.enc_version,is_secret=excluded.is_secret,updated_at=excluded.updated_at",
                (bid, key, encrypted, ENV_KEY_VERSION, int(is_secret_env(key, value)), now, now)
            )
            await conn.commit()

    @staticmethod
    async def delete_env(bid, key):
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            await conn.execute("PRAGMA busy_timeout=10000;")
            await conn.execute("BEGIN IMMEDIATE")
            cur = await conn.execute("DELETE FROM bot_env_vars WHERE bot_id=? AND env_key=?", (bid, key))
            await conn.commit()
            return cur.rowcount == 1

    @staticmethod
    async def count_user_bots(uid):
        bots = await DB.get_user_bots(uid)
        if uid in ADMIN_IDS: return len(bots)
        return len([b for b in bots.values() if b["user_id"] == uid])

    @staticmethod
    async def count_user_pending(uid):
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            cur = await conn.execute(
                "SELECT COUNT(*) FROM pending_deploys WHERE user_id=? AND status='pending'",
                (uid,)
            )
            r = await cur.fetchone()
            return r[0] if r else 0

    @staticmethod
    async def add_pending(bid, uid, name, sdir, btype, findings):
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            await conn.execute(
                "INSERT INTO pending_deploys (bot_id,user_id,name,staging_dir,bot_type,findings,status,created_at,scanned_at) "
                "VALUES (?,?,?,?,?,?,'pending',?,?)",
                (bid, uid, name, sdir, btype, json.dumps(findings),
                 datetime.now().isoformat(), datetime.now().isoformat())
            )
            await conn.commit()

    @staticmethod
    async def get_pending(bid):
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute("SELECT * FROM pending_deploys WHERE bot_id=?", (bid,))
            r = await cur.fetchone()
            return dict(r) if r else None

    @staticmethod
    async def set_pending_status(bid, status, admin_id=None, reason=None):
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            await conn.execute(
                "UPDATE pending_deploys SET status=?,reviewed_by=?,reviewed_at=?,reason=? WHERE bot_id=?",
                (status, admin_id, datetime.now().isoformat(), reason, bid)
            )
            await conn.commit()

    @staticmethod
    async def is_premium(uid):
        ck = f"prem_{uid}"
        c = cache_get(ck)
        if c is not None: return c
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            cur = await conn.execute("SELECT 1 FROM premium_users WHERE user_id=?", (uid,))
            r = await cur.fetchone()
            v = bool(r)
            cache_set(ck, v)
            return v

    @staticmethod
    async def add_premium(uid, by):
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO premium_users VALUES (?,?,?)",
                (uid, datetime.now().isoformat(), by)
            )
            await conn.commit()
        cache_invalidate(f"prem_{uid}")

    @staticmethod
    async def remove_premium(uid):
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            cur = await conn.execute("DELETE FROM premium_users WHERE user_id=?", (uid,))
            await conn.commit()
            changed = cur.rowcount > 0
        cache_invalidate(f"prem_{uid}")
        return changed

    @staticmethod
    async def is_unlocked():
        c = cache_get("unlocked")
        if c is not None: return c
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            cur = await conn.execute("SELECT value FROM app_state WHERE key='unlocked'")
            r = await cur.fetchone()
            v = (r[0].lower() == "true") if r else False
            cache_set("unlocked", v)
            return v

    @staticmethod
    async def set_unlocked(val):
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO app_state VALUES ('unlocked', ?)",
                ("true" if val else "false",)
            )
            await conn.commit()
        cache_invalidate("unlocked")

    @staticmethod
    async def is_muted(uid) -> bool:
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            cur = await conn.execute("SELECT mute_until FROM muted_users WHERE user_id=?", (uid,))
            r = await cur.fetchone()
            if not r: return False
            try:
                until = datetime.fromisoformat(r[0])
            except Exception:
                return False
            if datetime.now() >= until:
                await conn.execute("DELETE FROM muted_users WHERE user_id=?", (uid,))
                await conn.commit()
                return False
            return True

    @staticmethod
    async def mute_user(uid, hours, reason):
        until = (datetime.now() + timedelta(hours=hours)).isoformat()
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO muted_users VALUES (?,?,?)",
                (uid, until, reason)
            )
            await conn.commit()

    @staticmethod
    async def unmute_user(uid):
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            cur = await conn.execute("DELETE FROM muted_users WHERE user_id=?", (uid,))
            await conn.commit()
            return cur.rowcount > 0

async def ensure_bot_limits(bid: str) -> Dict[str, Any]:
    """Create a missing limit row using current admin-configured defaults."""
    now = utc_now_iso()
    async with aiosqlite.connect(DATABASE_PATH, timeout=10) as conn:
        await conn.execute("PRAGMA busy_timeout=10000")
        await conn.execute("BEGIN IMMEDIATE")
        row = await (await conn.execute("SELECT * FROM bot_limits WHERE bot_id=?", (bid,))).fetchone()
        if not row:
            default_mem = await get_setting_int_conn(conn, "default_memory_mb", LIMIT_DEFAULT_MEMORY_MB)
            max_mem = await get_setting_int_conn(conn, "max_memory_per_bot_mb", LIMIT_MAX_MEMORY_MB)
            max_allowed = effective_memory_max(max_mem)
            mem = max(LIMIT_MIN_MEMORY_MB, min(default_mem, max_allowed))
            await conn.execute("INSERT INTO bot_limits(bot_id,memory_limit_mb,cpu_limit_percent,restart_on_exceed,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                               (bid,mem,100,1,now,now))
            row = await (await conn.execute("SELECT * FROM bot_limits WHERE bot_id=?", (bid,))).fetchone()
        await conn.commit()
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        r = await (await conn.execute("SELECT * FROM bot_limits WHERE bot_id=?", (bid,))).fetchone()
        return dict(r)

async def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        row = await (await conn.execute("SELECT value FROM app_settings WHERE key=?", (key,))).fetchone()
        return str(row[0]) if row else default

async def set_setting(key: str, value: str):
    now = utc_now_iso()
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        await conn.execute("INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (key,value,now))
        await conn.commit()

def effective_memory_max(configured_max: int) -> int:
    if not HAS_PSUTIL:
        return min(configured_max, LIMIT_MAX_MEMORY_MB)
    try:
        total = psutil.virtual_memory().total
        if total < 2 * 1024**3:
            return min(configured_max, LIMIT_SMALL_VPS_MAX_MB)
    except Exception:
        pass
    return min(configured_max, LIMIT_MAX_MEMORY_MB)

def get_setting_int_sync(conn, key: str, default: int) -> int:
    row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    try: return int(row[0]) if row else default
    except (TypeError, ValueError): return default

async def get_setting_int_conn(conn, key: str, default: int) -> int:
    row = await (await conn.execute("SELECT value FROM app_settings WHERE key=?", (key,))).fetchone()
    try: return int(row[0]) if row else default
    except (TypeError, ValueError): return default

def get_vps_memory_cap_mb() -> Tuple[int,int,int]:
    total_mb = int(psutil.virtual_memory().total / (1024**2)) if HAS_PSUTIL else 0
    cap_percent = LIMIT_DEFAULT_CAP_PERCENT
    configured_max = LIMIT_MAX_MEMORY_MB
    try:
        with sqlite3.connect(DATABASE_PATH, timeout=10) as conn:
            cap_percent = get_setting_int_sync(conn, "vps_cap_percent", LIMIT_DEFAULT_CAP_PERCENT)
            configured_max = get_setting_int_sync(conn, "max_memory_per_bot_mb", LIMIT_MAX_MEMORY_MB)
    except Exception:
        pass
    cap_mb = int(total_mb * max(1, min(100, cap_percent)) / 100) if total_mb else 0
    return total_mb, cap_mb, effective_memory_max(configured_max)

def validate_limit_memory(value: str) -> int:
    try: n = int(value.strip())
    except Exception: raise ValueError("Memory must be an integer in MB")
    _, _, max_allowed = get_vps_memory_cap_mb()
    if n < LIMIT_MIN_MEMORY_MB or n > max_allowed:
        raise ValueError(f"Memory must be {LIMIT_MIN_MEMORY_MB}–{max_allowed} MB")
    return n

def validate_limit_cpu(value: str) -> int:
    try: n = int(value.strip())
    except Exception: raise ValueError("CPU must be an integer from 1–100")
    if n < LIMIT_MIN_CPU_PERCENT or n > LIMIT_MAX_CPU_PERCENT:
        raise ValueError("CPU must be between 1 and 100%")
    return n

async def memory_allocation_check(bid: str, new_mb: int) -> Tuple[bool,str]:
    total_mb, cap_mb, _ = get_vps_memory_cap_mb()
    if not total_mb: return False, "psutil is required for resource limits"
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        cur = await conn.execute("SELECT COALESCE(SUM(memory_limit_mb),0) FROM bot_limits WHERE bot_id<>?", (bid,))
        others = int((await cur.fetchone())[0])
    if others + new_mb > cap_mb:
        return False, f"VPS memory allocation cap exceeded: {others + new_mb} MB > {cap_mb} MB ({int(cap_mb*100/total_mb)}% of RAM)"
    return True, ""

async def save_bot_limit(bid: str, field: str, value: int) -> Tuple[bool,str]:
    async with aiosqlite.connect(DATABASE_PATH, timeout=10) as conn:
        await conn.execute("PRAGMA busy_timeout=10000")
        await conn.execute("BEGIN IMMEDIATE")
        row = await (await conn.execute("SELECT * FROM bot_limits WHERE bot_id=?", (bid,))).fetchone()
        if not row:
            now = utc_now_iso(); await conn.execute("INSERT INTO bot_limits(bot_id,memory_limit_mb,cpu_limit_percent,restart_on_exceed,created_at,updated_at) VALUES(?,?,?,?,?,?)", (bid,256,100,1,now,now))
        if field == "memory_limit_mb":
            total_mb = int(psutil.virtual_memory().total/(1024**2)) if HAS_PSUTIL else 0
            # BUGFIX 1: Use the async DB helper with the aiosqlite connection.
            cap_pct = await get_setting_int_conn(conn, "vps_cap_percent", 80)
            cap = int(total_mb * max(1, min(100, cap_pct)) / 100) if total_mb else 0
            cur = await conn.execute("SELECT COALESCE(SUM(memory_limit_mb),0) FROM bot_limits WHERE bot_id<>?", (bid,))
            others = int((await cur.fetchone())[0])
            if not total_mb or others + value > cap:
                await conn.rollback(); return False, f"Total allocation would be {others+value} MB; cap is {cap} MB"
        await conn.execute(f"UPDATE bot_limits SET {field}=?,updated_at=? WHERE bot_id=?", (value,utc_now_iso(),bid))
        await conn.commit()
    return True, ""

async def record_limit_exceeded(bid: str, reason: str) -> Optional[Dict[str,Any]]:
    now = utc_now_iso()
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("INSERT OR IGNORE INTO bot_limits(bot_id,created_at,updated_at) VALUES(?,?,?)", (bid,now,now))
        await conn.execute("UPDATE bot_limits SET last_exceeded_at=?,exceed_count=exceed_count+1,updated_at=? WHERE bot_id=?", (now,now,bid))
        row = await (await conn.execute("SELECT * FROM bot_limits WHERE bot_id=?", (bid,))).fetchone()
        await conn.commit()
        return dict(row) if row else None

async def admin_audit(admin_id:int, action:str, target:Optional[str]=None, details:Optional[str]=None):
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        await conn.execute("INSERT INTO admin_audit(admin_id,action,target,details,created_at) VALUES(?,?,?,?,?)", (admin_id,action,target,details,utc_now_iso()))
        await conn.commit()

async def is_banned(uid:int) -> bool:
    if uid in ADMIN_IDS: return False
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        return bool(await (await conn.execute("SELECT 1 FROM user_bans WHERE user_id=?",(uid,))).fetchone())

async def get_ban_reason(uid:int) -> Optional[str]:
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        r=await (await conn.execute("SELECT reason FROM user_bans WHERE user_id=?",(uid,))).fetchone(); return r[0] if r else None

async def can_use(uid):
    if uid in ADMIN_IDS: return True
    if await is_banned(uid): return False
    if (await get_setting("maintenance_mode", "0")) == "1": return False
    if await DB.is_unlocked(): return True
    return await DB.is_premium(uid)

# ================== ADMIN NOTIFY ==================
def _post_admin_notify(url, payload):
    import requests
    response = requests.post(url, data=payload, timeout=10)
    response.raise_for_status()
    return response

async def notify_admin_via_approval_bot(text, reply_markup=None):
    for aid in ADMIN_IDS:
        try:
            url = f"https://api.telegram.org/bot{APPROVAL_BOT_TOKEN}/sendMessage"
            payload = {"chat_id": aid, "text": text, "parse_mode": "HTML"}
            if reply_markup is not None:
                payload["reply_markup"] = json.dumps(reply_markup)
            await asyncio.to_thread(_post_admin_notify, url, payload)
        except Exception as e:
            log_admin_notify_failure(aid, e, text)
            log_err(f"notify admin {aid}: {e}")

# ================== SAFE ZIP ==================
def _is_within(root, cand):
    try:
        Path(cand).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False

def _safe_zip_extract(zip_path, dest):
    root = Path(dest).resolve()
    with zipfile.ZipFile(zip_path) as zip_ref:
        members = zip_ref.infolist()
        if len(members) > 5000:
            raise ValueError("ZIP has too many entries (max 5000)")
        expanded = 0
        for m in members:
            name = m.filename.replace("\\", "/")
            if not name or name.startswith("/") or name.startswith("../") or "/../" in name:
                raise ValueError(f"Unsafe path in ZIP: {name}")
            target = (root / name).resolve()
            if not _is_within(root, target):
                raise ValueError(f"ZIP escapes dest: {name}")
            expanded += max(0, m.file_size)
            if expanded > 200 * 1024 * 1024:
                raise ValueError("ZIP expands past 200MB")
        zip_ref.extractall(root)

# ================== SCANNER WRAPPER ==================
def scan_directory(dir_path: Path) -> Dict[str, Any]:
    res = {
        "verdict": "clear",
        "files_scanned": 0, "files_skipped": 0,
        "flagged_files": [], "high_count": 0, "medium_count": 0,
        "truncated": False,
    }
    hit_cap = False
    for root, _, files in os.walk(dir_path):
        for fname in files:
            if res["files_scanned"] >= MAX_FILES_PER_SCAN:
                hit_cap = True
                break
            fp = Path(root) / fname
            if fp.suffix.lower() in SKIP_SCAN_EXTS:
                res["files_skipped"] += 1
                continue
            try:
                verdict, findings = scan_file(fp)
                res["files_scanned"] += 1
                if verdict == "flagged":
                    rel = str(fp.relative_to(dir_path))
                    res["flagged_files"].append({"file": rel, "findings": findings})
                    for _, sev in findings:
                        if sev == "high": res["high_count"] += 1
                        elif sev == "medium": res["medium_count"] += 1
            except Exception as e:
                log_err(f"scan {fp}: {e}")
        if hit_cap:
            break

    if hit_cap:
        res["truncated"] = True
        res["verdict"] = "flagged"
        res["flagged_files"].append({
            "file": "(scan limit reached)",
            "findings": [(f"Upload exceeds {MAX_FILES_PER_SCAN}-file scan limit — "
                          "remaining files were not scanned", "high")]
        })
        res["high_count"] += 1
    elif res["high_count"] >= 1 or res["medium_count"] >= 2:
        res["verdict"] = "flagged"
    return res

def _html_text(value, limit=None):
    text = "" if value is None else str(value)
    if limit is not None:
        text = text[:limit]
    return html.escape(text, quote=False)

def _build_findings_text(scan_result, max_files=5, max_per_file=3):
    lines = []
    for ff in scan_result["flagged_files"][:max_files]:
        lines.append(f"📄 <code>{_html_text(ff['file'])}</code>")
        for label, sev in ff["findings"][:max_per_file]:
            emo = "🔴" if sev == "high" else "🟡"
            lines.append(f"  {emo} {_html_text(label, 80)}")
    if len(scan_result["flagged_files"]) > max_files:
        lines.append(f"<i>... and {len(scan_result['flagged_files']) - max_files} more files</i>")
    return "\n".join(lines)

# ================== ENV FILE GENERATION ==================
def _parse_env_line(line: str) -> Optional[Tuple[str, str]]:
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return None
    if raw.startswith("export "):
        raw = raw[7:].lstrip()
    if "=" not in raw:
        raise ValueError("Malformed env line")
    key, value = raw.split("=", 1)
    key = key.strip()
    validate_env_key(key)
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        quote = value[0]
        value = value[1:-1]
        if quote == '"':
            value = value.replace('\\"', '"').replace('\\\\', '\\')
    validate_env_value(value)
    return key, value


def write_env_atomic(bot_dir: Path, items: List[Tuple[str, str, bool]]) -> None:
    bot_dir = Path(bot_dir).resolve()
    if not bot_dir.is_dir():
        raise FileNotFoundError(str(bot_dir))
    env_path = bot_dir / ".env"
    tmp_path = bot_dir / ".env.tmp"
    content = "".join(f"{key}={quote_env_value(value)}\n" for key, value, _ in items)
    fd = None
    try:
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fp:
            fd = None
            fp.write(content)
            fp.flush()
            os.fsync(fp.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, env_path)
        os.chmod(env_path, 0o600)
    except Exception:
        if fd is not None:
            try: os.close(fd)
            except Exception: pass
        try:
            if tmp_path.exists(): tmp_path.unlink()
        except Exception: pass
        raise


def prepare_env_for_bot_sync(bid: str, bot_dir: str) -> Tuple[bool, Optional[str]]:
    """Import root .env once (only when DB empty), then atomically regenerate .env."""
    if not env_available():
        return False, "Encryption unavailable"
    bdir = Path(bot_dir).resolve()
    conn = None
    try:
        conn = sqlite3.connect(DATABASE_PATH, timeout=10)
        conn.execute("PRAGMA busy_timeout=10000")
        cur = conn.execute("SELECT COUNT(*) FROM bot_env_vars WHERE bot_id=?", (bid,))
        count = int(cur.fetchone()[0])
        env_path = bdir / ".env"
        if count == 0 and env_path.is_file() and not env_path.is_symlink():
            imported = []
            for line_no, line in enumerate(env_path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                try:
                    item = _parse_env_line(line)
                    if item:
                        imported.append(item)
                except Exception as e:
                    log_err(f"env import malformed line {line_no} for {bid}: {type(e).__name__}")
            if len(imported) > ENV_MAX_KEYS:
                imported = imported[:ENV_MAX_KEYS]
                log_err(f"env import truncated at {ENV_MAX_KEYS} keys for {bid}")
            conn.execute("BEGIN IMMEDIATE")
            now = utc_now_iso()
            for key, value in imported:
                conn.execute(
                    "INSERT OR IGNORE INTO bot_env_vars(bot_id,env_key,encrypted_value,enc_version,is_secret,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (bid, key, encrypt_env_value(value), ENV_KEY_VERSION, int(is_secret_env(key, value)), now, now)
                )
            conn.commit()
            # Rename only after the DB transaction succeeds. If rename fails, preserve the source file.
            try:
                os.replace(env_path, bdir / ".env.imported")
                os.chmod(bdir / ".env.imported", 0o600)
            except Exception:
                log_err(f"env import rename failed for {bid}", traceback.format_exc())
        cur = conn.execute("SELECT env_key,encrypted_value,is_secret FROM bot_env_vars WHERE bot_id=? ORDER BY env_key COLLATE NOCASE", (bid,))
        items = [(key, decrypt_env_value(enc), bool(secret)) for key, enc, secret in cur.fetchall()]
        conn.close()
        conn = None
        write_env_atomic(bdir, items)
        return True, None
    except Exception as e:
        if conn is not None:
            try: conn.rollback(); conn.close()
            except Exception: pass
        log_err(f"prepare_env_for_bot {bid}: {type(e).__name__}", traceback.format_exc())
        return False, env_error_message(e)


# Only these non-secret process settings are inherited by deployed bots.
# IMPORTANT: never pass the panel process environment wholesale to a user bot.
# Panel secrets such as BOT_TOKEN, APPROVAL_BOT_TOKEN, ENV_ENCRYPTION_KEY,
# admin credentials, database credentials, etc. must stay in the panel process.
BOT_SAFE_INHERITED_ENV = {
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
    "LC_MESSAGES", "TZ", "TMPDIR", "PM2_HOME", "XDG_RUNTIME_DIR",
}

def get_safe_bot_base_env(bot_dir: Optional[str] = None) -> dict:
    """Build a minimal non-secret environment for a deployed bot.

    Do NOT use os.environ.copy(): the hosting/approval panel environment can
    contain Telegram tokens, the Fernet key, admin credentials and other
    secrets. Bot-specific secrets are added separately from the encrypted DB.
    """
    env = {k: v for k, v in os.environ.items() if k in BOT_SAFE_INHERITED_ENV}
    if bot_dir:
        env["PWD"] = str(Path(bot_dir).resolve())
    return env

def get_env_process_env_sync(bid: str, bot_dir: Optional[str] = None) -> dict:
    """Return minimal safe env + this bot's decrypted variables for PM2 child."""
    env = get_safe_bot_base_env(bot_dir)
    if not env_available():
        return env
    with sqlite3.connect(DATABASE_PATH, timeout=10) as conn:
        conn.execute("PRAGMA busy_timeout=10000")
        rows = conn.execute("SELECT env_key, encrypted_value FROM bot_env_vars WHERE bot_id=?", (bid,)).fetchall()
        for key, encrypted in rows:
            env[key] = decrypt_env_value(encrypted)
    return env


async def prepare_env_for_bot(bid: str, bot_dir: str) -> Tuple[bool, Optional[str]]:
    return await asyncio.to_thread(prepare_env_for_bot_sync, bid, bot_dir)


async def get_owned_bot(uid: int, bid: str):
    bots = await DB.get_user_bots(uid)
    info = bots.get(bid)
    if not info:
        return None
    if uid not in ADMIN_IDS and info.get("user_id") != uid:
        return None
    return info


async def import_env_for_display(bid: str, bot_dir: str):
    """Prepare the display environment only when the on-disk .env is stale.

    We do not rewrite .env on every menu refresh.  If DB has no rows, the
    normal preparation helper is still allowed to perform the one-time root
    .env import.  Otherwise the DB's newest updated_at timestamp is compared
    with the root .env mtime before any disk write occurs.
    """
    env_path = Path(bot_dir).resolve() / ".env"
    try:
        async with aiosqlite.connect(DATABASE_PATH, timeout=10) as conn:
            await conn.execute("PRAGMA busy_timeout=10000")
            cur = await conn.execute(
                "SELECT COUNT(*), MAX(updated_at) FROM bot_env_vars WHERE bot_id=?",
                (bid,),
            )
            count, last_updated = await cur.fetchone()

        if count and last_updated and env_path.is_file() and not env_path.is_symlink():
            try:
                db_mtime = datetime.fromisoformat(last_updated).timestamp()
                if env_path.stat().st_mtime >= db_mtime:
                    return True, None
            except (ValueError, OSError, OverflowError):
                pass

        return await prepare_env_for_bot(bid, bot_dir)
    except Exception:
        # Preparation performs the detailed logging/error mapping.  Falling
        # back to it also preserves the one-time import behavior for empty DB.
        return await prepare_env_for_bot(bid, bot_dir)

# ================== HOSTED BOT CONTROL ==================
def get_bot_limits_sync(bid: str) -> Dict[str,Any]:
    now=utc_now_iso()
    keys=["bot_id","memory_limit_mb","cpu_limit_percent","restart_on_exceed","last_exceeded_at","exceed_count","created_at","updated_at"]
    try:
        with sqlite3.connect(DATABASE_PATH,timeout=10) as conn:
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("BEGIN IMMEDIATE")
            row=conn.execute("SELECT bot_id,memory_limit_mb,cpu_limit_percent,restart_on_exceed,last_exceeded_at,exceed_count,created_at,updated_at FROM bot_limits WHERE bot_id=?",(bid,)).fetchone()
            if not row:
                try: default_mem=get_setting_int_sync(conn,"default_memory_mb",LIMIT_DEFAULT_MEMORY_MB)
                except Exception: default_mem=LIMIT_DEFAULT_MEMORY_MB
                try: max_mem=get_setting_int_sync(conn,"max_memory_per_bot_mb",LIMIT_MAX_MEMORY_MB)
                except Exception: max_mem=LIMIT_MAX_MEMORY_MB
                mem=max(LIMIT_MIN_MEMORY_MB,min(default_mem,effective_memory_max(max_mem)))
                conn.execute("INSERT INTO bot_limits(bot_id,memory_limit_mb,cpu_limit_percent,restart_on_exceed,created_at,updated_at) VALUES(?,?,?,?,?,?)",(bid,mem,100,1,now,now))
                row=conn.execute("SELECT bot_id,memory_limit_mb,cpu_limit_percent,restart_on_exceed,last_exceeded_at,exceed_count,created_at,updated_at FROM bot_limits WHERE bot_id=?",(bid,)).fetchone()
            conn.commit()
            return dict(zip(keys,row))
    except sqlite3.OperationalError:
        # Migration failure must not prevent existing bots from starting.
        return dict(zip(keys,(bid,LIMIT_DEFAULT_MEMORY_MB,100,1,None,0,now,now)))

class HostedBot:
    def __init__(self, bid, name, bdir, btype, uid=None):
        self.bot_id = bid
        self.name = name
        self.bot_dir = str(bdir)
        self.bot_type = btype
        self.user_id = uid
        self.service_name = f"hosted-bot-{bid}"

    def is_running(self):
        try:
            r = subprocess.run(["pm2", "jlist"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout:
                try:
                    for p in json.loads(r.stdout):
                        if p.get("name") == self.service_name:
                            return p.get("pm2_env", {}).get("status") == "online"
                except Exception:
                    pass
            return False
        except Exception:
            return False

    def get_logs(self, lines=30):
        try:
            r = subprocess.run(
                ["pm2", "logs", self.service_name, "--lines", str(lines), "--nostream"],
                capture_output=True, text=True, timeout=10
            )
            txt = (r.stdout or "") + (r.stderr or "")
            if txt.strip():
                return re.sub(r'\x1B\[[0-?]*[ -/]*[@-~]', '', txt)[-3500:]
            lf = f"/root/.pm2/logs/{self.service_name}-out.log"
            if os.path.exists(lf):
                with open(lf) as f:
                    return f.read()[-3500:] or "Empty"
            return "No logs yet."
        except Exception as e:
            return f"Log error: {e}"

    def _resource_args(self, include_autorestart=True):
        limits = get_bot_limits_sync(self.bot_id)
        args = ["--max-memory-restart", f"{limits['memory_limit_mb']}M"]
        if include_autorestart and not limits["restart_on_exceed"]:
            args.append("--no-autorestart")
        return args, limits

    def start(self) -> bool:
        try:
            env_ok, env_err = prepare_env_for_bot_sync(self.bot_id, self.bot_dir)
            if not env_ok:
                log_err(f"start {self.bot_id}: env warning: {env_err}")
            pm2_env = get_env_process_env_sync(self.bot_id, self.bot_dir) if env_ok else get_safe_bot_base_env(self.bot_dir)
            if self.bot_type in ("nodejs", "whatsapp"):
                pkg = os.path.join(self.bot_dir, "package.json")
                use_npm = False
                main_file = "index.js"
                if os.path.exists(pkg):
                    try:
                        with open(pkg) as f:
                            p = json.load(f)
                            use_npm = bool(p.get("scripts", {}).get("start"))
                            main_file = p.get("main", "index.js")
                    except Exception:
                        pass
                if use_npm:
                    r = subprocess.run(
                        ["pm2", "start", "npm", "--name", self.service_name] + self._resource_args()[0] + ["--", "start"],
                        capture_output=True, text=True, timeout=25, cwd=self.bot_dir, env=pm2_env
                    )
                else:
                    target = os.path.join(self.bot_dir, main_file)
                    if not os.path.exists(target):
                        for f in os.listdir(self.bot_dir):
                            if f.endswith(".js"):
                                target = os.path.join(self.bot_dir, f); break
                    r = subprocess.run(
                        ["pm2", "start", target, "--name", self.service_name] + self._resource_args()[0],
                        capture_output=True, text=True, timeout=25, cwd=self.bot_dir, env=pm2_env
                    )
                subprocess.run(["pm2", "save"], capture_output=True, timeout=5)
                return r.returncode == 0
            else:
                main_file = None
                # Prefer common names first
                for mf in ("main.py", "bot.py", "app.py", "run.py", "simple.py"):
                    if os.path.exists(os.path.join(self.bot_dir, mf)):
                        main_file = mf; break
                # Fallback: first .py file in the directory
                if not main_file:
                    pyfiles = [f for f in os.listdir(self.bot_dir) if f.endswith(".py")]
                    if pyfiles:
                        # Prefer files that look like entry points
                        for pref in ("main", "bot", "app", "run", "simple"):
                            for f in pyfiles:
                                if f.lower().startswith(pref):
                                    main_file = f; break
                            if main_file: break
                        if not main_file:
                            main_file = pyfiles[0]
                if not main_file:
                    raise FileNotFoundError("No .py file found in uploaded bot")
                target = os.path.join(self.bot_dir, main_file)
                r = subprocess.run(
                    ["pm2", "start", target, "--name", self.service_name] + self._resource_args()[0] + [
                     "--interpreter", "python3", "--cwd", self.bot_dir],
                    capture_output=True, text=True, timeout=25, env=pm2_env
                )
                subprocess.run(["pm2", "save"], capture_output=True, timeout=5)
                return r.returncode == 0
        except Exception as e:
            log_err(f"start {self.name}: {e}")
            return False

    def stop(self) -> bool:
        try:
            r=subprocess.run(["pm2", "stop", self.service_name], capture_output=True, text=True, timeout=15)
            return r.returncode == 0
        except Exception:
            return False

    def restart(self) -> bool:
        try:
            # BUGFIX 7: PM2 restart does not reliably apply a changed
            # --max-memory-restart value on all PM2 versions. Delete + start
            # guarantees fresh resource arguments are applied.
            delete_result = subprocess.run(
                ["pm2", "delete", self.service_name],
                capture_output=True, text=True, timeout=10
            )
            if delete_result.returncode != 0 and "not found" not in (delete_result.stdout + delete_result.stderr).lower():
                log_err(f"restart {self.bot_id}: pm2 delete failed")
            return self.start()
        except Exception as e:
            log_err(f"restart {self.bot_id}: {type(e).__name__}", traceback.format_exc())
            return False

    def delete(self):
        try:
            subprocess.run(["pm2", "delete", self.service_name], capture_output=True, timeout=10)
            subprocess.run(["pm2", "save"], capture_output=True, timeout=5)
        except Exception:
            pass

# ================== SYSTEM MONITOR ==================
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

def get_system_stats():
    if not HAS_PSUTIL: return None
    cpu = psutil.cpu_percent(interval=0.3)
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    up = time.time() - psutil.boot_time()
    td = timedelta(seconds=int(up))
    d, h, m = td.days, td.seconds // 3600, (td.seconds % 3600) // 60
    uptime = " ".join(x for x in (f"{d}d" if d else "", f"{h}h" if h else "", f"{m}m") if x) or "0m"
    return {
        "cpu": cpu,
        "ram_used": ram.used / (1024**3), "ram_total": ram.total / (1024**3),
        "ram_pct": ram.percent,
        "disk_used": disk.used / (1024**3), "disk_total": disk.total / (1024**3),
        "disk_pct": disk.percent, "uptime": uptime,
    }

# ================== KEYBOARDS ==================
def _short_button_name(name: str, limit: int = 30) -> str:
    text = str(name or "")
    return text if len(text) <= limit else text[:limit - 1] + "…"

def kb_main():
    # UI: Mobile-first, max two buttons per row.
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{UI.BOTS} My Bots", callback_data="my_bots")],
        [InlineKeyboardButton(f"{UI.DEPLOY} Deploy New Bot", callback_data="deploy"),
         InlineKeyboardButton(f"{UI.GITHUB} GitHub Repo", callback_data="github_deploy")],
        [InlineKeyboardButton(f"{UI.STATUS} VPS Status", callback_data="vps_status"),
         InlineKeyboardButton(f"{UI.INFO} Help", callback_data="help")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
        [InlineKeyboardButton("📢 Channel", url=SUPPORT_CHANNEL)],
    ])

def kb_bot_actions(bid, running):
    rows = []
    if running:
        rows.append([
            InlineKeyboardButton(f"{UI.STOP_ACTION} Stop", callback_data=f"stop:{bid}"),
            InlineKeyboardButton(f"{UI.RESTART} Restart", callback_data=f"restart:{bid}"),
        ])
    else:
        rows.append([InlineKeyboardButton(f"{UI.START} Start", callback_data=f"start:{bid}")])
    rows.append([
        InlineKeyboardButton(f"{UI.LOGS} Logs", callback_data=f"logs:{bid}"),
        InlineKeyboardButton(f"{UI.STATUS} Status", callback_data=f"bot_status:{bid}"),
    ])
    rows.append([
        InlineKeyboardButton(f"{UI.ENV} Env Vars", callback_data=f"env_menu:{bid}"),
        InlineKeyboardButton(f"{UI.LIMITS} Limits", callback_data=f"limits_menu:{bid}"),
    ])
    rows.append([InlineKeyboardButton(f"{UI.DELETE} Delete", callback_data=f"delete:{bid}")])
    rows.append([InlineKeyboardButton(f"{UI.BACK} Back to Bots", callback_data="my_bots")])
    return InlineKeyboardMarkup(rows)

def kb_back(to="menu"):
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"{UI.BACK} Back", callback_data=to)]])

def kb_confirm_delete(bid):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"{UI.ERROR} Cancel", callback_data=f"bot_detail:{bid}"),
        InlineKeyboardButton(f"{UI.DELETE} Yes, Delete", callback_data=f"confirm_delete:{bid}"),
    ]])

def kb_deploy_types():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{UI.NODE} Node.js", callback_data="deploy_type:nodejs"),
         InlineKeyboardButton(f"{UI.PYTHON} Python", callback_data="deploy_type:python")],
        [InlineKeyboardButton(f"{UI.WHATSAPP} WhatsApp", callback_data="deploy_type:whatsapp"),
         InlineKeyboardButton(f"{UI.GITHUB} GitHub Repo", callback_data="github_deploy")],
        [InlineKeyboardButton(f"{UI.BACK} Back", callback_data="menu")],
    ])


def kb_help():
    # FIX_2: Provide the keyboard referenced by show_help() so the Help screen cannot raise NameError.
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"{UI.BACK} Back", callback_data="menu")
    ]])

# ================== ENV UI ==================
def _env_state_clear(ctx):
    ctx.user_data.pop("awaiting_env_value", None)
    task = ctx.user_data.pop("env_timeout_task", None)
    # FIX_RICH_5: Never cancel the currently executing timeout worker itself.
    if task and not task.done() and task is not asyncio.current_task():
        task.cancel()


def _schedule_env_timeout(ctx, chat_id: int, prompt_id: int):
    # Only cancel any previously scheduled timeout task here — do NOT call
    # _env_state_clear(ctx), which would also wipe the awaiting_env_value
    # state that the caller just set before invoking this function.
    old_task = ctx.user_data.pop("env_timeout_task", None)
    if old_task and not old_task.done():
        old_task.cancel()
    ctx.user_data["env_timeout_task"] = asyncio.create_task(_env_timeout_worker(ctx, chat_id, prompt_id))


async def _env_timeout_worker(ctx, chat_id: int, prompt_id: int):
    try:
        await asyncio.sleep(ENV_TIMEOUT_SECONDS)
        state = ctx.user_data.get("awaiting_env_value")
        if state and state.get("msg_id") == prompt_id and state.get("expires_at", 0) <= time.time():
            ctx.user_data.pop("awaiting_env_value", None)
            try:
                await ctx.bot.edit_message_text(chat_id=chat_id, message_id=prompt_id, text="⏱️ Timed out")
            except Exception:
                pass
    except asyncio.CancelledError:
        pass
    except Exception:
        log_err("env timeout worker", traceback.format_exc())


async def _env_pre_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    parts = (update.message.text or "").split() if update.message else []
    if parts and parts[0].split("@")[0].lower() == "/cancel": return
    if ctx.user_data.get("awaiting_limit_input") or ctx.user_data.get("awaiting_admin_setting") or ctx.user_data.get("awaiting_broadcast"): return
    if ctx.user_data.get("awaiting_env_value"):
        _env_state_clear(ctx)
        if update.message: await update.message.reply_text("Previous env input cancelled")
    # FIX_7: Cancel stale GitHub URL input when a normal command arrives.
    if ctx.user_data.get("awaiting_github_url"):
        _github_state_clear(ctx)
        if update.message: await update.message.reply_text("Previous GitHub input cancelled")


async def _env_pre_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.user_data.get("awaiting_env_value"):
        _env_state_clear(ctx)
        try:
            await update.callback_query.answer("Previous env input cancelled")
        except Exception:
            pass


# FIX_RICH_9: Removed dead environment-cancel command handler; /cancel is handled by cmd_cancel.

async def show_env_menu(update, ctx, bid, page=0):
    q = update.callback_query
    u = update.effective_user
    if not env_available():
        await safe_edit(q, "❌ Encryption unavailable. Contact admin.", kb_back(f"bot_detail:{bid}"))
        return
    info = await get_owned_bot(u.id, bid)
    if not info:
        await q.answer("Not found or not yours", show_alert=True)
        return
    try:
        await import_env_for_display(bid, info["dir"])
        rows = await DB.get_env_vars(bid)
        total = len(rows)
        pages = max(1, (total + ENV_PAGE_SIZE - 1) // ENV_PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        chunk = rows[page * ENV_PAGE_SIZE:(page + 1) * ENV_PAGE_SIZE]
        lines = [f"🔐 <b>Environment: {_html_text(info['name'])}</b>", "━━━━━━━━━━━━━━━━━━"]
        if not chunk:
            lines.append("No environment variables.")
        for row in chunk:
            value = decrypt_env_value(row["encrypted_value"])
            shown = mask_env_value(value) if row["is_secret"] else value
            shown = _html_text(shown, 160)
            if len(shown) > 160:
                shown = shown[:157] + "..."
            lines.append(f"<code>{_html_text(row['env_key'])}</code> = <code>{shown}</code>")
        lines.append(f"Variables: {total}/{ENV_MAX_KEYS}")
        buttons = []
        for idx, row in enumerate(chunk, start=page * ENV_PAGE_SIZE):
            buttons.append([
                InlineKeyboardButton(f"✏️ {row['env_key'][:24]}", callback_data=f"env_edit:{bid}:{idx}"),
                InlineKeyboardButton("🗑️", callback_data=f"env_del:{bid}:{idx}")
            ])
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"env_list:{bid}:{page-1}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data=f"env_list:{bid}:{page}"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"env_list:{bid}:{page+1}"))
        buttons.append(nav)
        buttons.append([InlineKeyboardButton("➕ Add New", callback_data=f"env_add:{bid}"), InlineKeyboardButton("🔄 Refresh", callback_data=f"env_list:{bid}:{page}")])
        buttons.append([InlineKeyboardButton("🔙 Back", callback_data=f"env_back:{bid}")])
        await safe_edit(q, "\n".join(lines), InlineKeyboardMarkup(buttons))
    except Exception as e:
        log_err(f"show_env_menu {bid}: {type(e).__name__}", traceback.format_exc())
        await safe_edit(q, env_error_message(e), kb_back(f"bot_detail:{bid}"))


async def env_begin_input(update, ctx, bid, key=None):
    q = update.callback_query
    u = update.effective_user
    if not env_available():
        await q.answer("Encryption unavailable", show_alert=True)
        return
    if not env_rate_allowed(u.id):
        await q.answer("Too many env operations. Try again later.", show_alert=True)
        return
    info = await get_owned_bot(u.id, bid)
    if not info:
        await q.answer("Not found or not yours", show_alert=True)
        return
    if key is None and await DB.env_count(bid) >= ENV_MAX_KEYS:
        await q.answer("Maximum 50 variables reached", show_alert=True)
        return
    msg_src = q.message
    # FIX_RICH_7: Guard q.message before sending the environment input prompt.
    if msg_src is None:
        log_err("RICH: env_begin_input missing q.message", traceback.format_exc())
        try:
            await q.answer("Message expired, please retry", show_alert=True)
        except Exception:
            log_err("RICH: env_begin_input failed to answer missing-message callback", traceback.format_exc())
        return
    _env_state_clear(ctx)
    mode = "edit" if key is not None else "add"
    if mode == "edit":
        prompt_text = f"🔐 <b>Edit <code>{_html_text(key)}</code></b>\n\nSend the new value.\n/cancel to cancel. Input expires in 5 minutes."
    else:
        prompt_text = f"{UI.ENV} <b>Add Environment Variable</b>\n{UI.DIV}\nSend <code>KEY=VALUE</code> in one line.\nExample: <code>API_KEY=my value</code>\n<code>/cancel</code> to cancel. Input expires in 5 minutes."
    msg = await msg_src.chat.send_message(prompt_text, parse_mode="HTML")
    ctx.user_data["awaiting_env_value"] = {
        "bot_id": bid,
        "key": key,
        "mode": mode,
        "msg_id": msg.message_id,
        "expires_at": time.time() + ENV_TIMEOUT_SECONDS,
    }
    # FIX_RICH_21: use msg_src consistently after the guard.
    _schedule_env_timeout(ctx, msg_src.chat_id, msg.message_id)


async def handle_env_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = ctx.user_data.get("awaiting_env_value")
    if not state:
        return
    if time.time() >= state.get("expires_at", 0):
        _env_state_clear(ctx)
        await update.message.reply_text("⏱️ Timed out")
        return
    u = update.effective_user
    if not env_rate_allowed(u.id):
        _env_state_clear(ctx)
        await update.message.reply_text("❌ Too many env operations. Try again later.")
        return
    bid = state["bot_id"]
    info = await get_owned_bot(u.id, bid)
    if not info:
        _env_state_clear(ctx)
        await update.message.reply_text("❌ Bot not found or not yours.")
        return
    text = update.message.text or ""
    try:
        if state["mode"] == "edit":
            key = state["key"]
            value = text
        else:
            if "=" not in text:
                raise ValueError("Use KEY=VALUE format")
            key, value = text.split("=", 1)
            key = key.strip()
        validate_env_key(key)
        validate_env_value(value)
        await DB.upsert_env(bid, key, value)
        ok, err = await prepare_env_for_bot(bid, info["dir"])
        if not ok:
            log_err(f"env file generation after save failed {bid}: {err}")
            await update.message.reply_text(f"⚠️ Saved in database, but .env update failed: {err}")
        else:
            await update.message.reply_text(
                "✅ Saved. Restart bot to apply?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Restart Now", callback_data=f"env_restart:{bid}"), InlineKeyboardButton("⏭️ Later", callback_data=f"env_back:{bid}")],
                    [InlineKeyboardButton("🔐 Back to Env", callback_data=f"env_menu:{bid}")]
                ])
            )
        _env_state_clear(ctx)
    except Exception as e:
        log_err(f"env input {bid}: {type(e).__name__}", traceback.format_exc())
        await update.message.reply_text(env_error_message(e))


async def env_delete_confirm(update, ctx, bid, index):
    q = update.callback_query
    u = update.effective_user
    if not env_rate_allowed(u.id):
        await q.answer("Too many env operations. Try again later.", show_alert=True)
        return
    info = await get_owned_bot(u.id, bid)
    if not info:
        await q.answer("Not found or not yours", show_alert=True)
        return
    rows = await DB.get_env_vars(bid)
    if index < 0 or index >= len(rows):
        await q.answer("Invalid variable", show_alert=True)
        return
    key = rows[index]["env_key"]
    await safe_edit(q, f"⚠️ Delete <code>{_html_text(key)}</code>?", InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel", callback_data=f"env_menu:{bid}"),
        InlineKeyboardButton("✅ Delete", callback_data=f"env_confirm_del:{bid}:{index}")
    ]]), parse_mode="HTML")


async def env_delete(update, ctx, bid, index):
    q = update.callback_query
    u = update.effective_user
    if not env_available():
        await q.answer("Encryption unavailable", show_alert=True)
        return
    if not env_rate_allowed(u.id):
        await q.answer("Too many env operations. Try again later.", show_alert=True)
        return
    info = await get_owned_bot(u.id, bid)
    if not info:
        await q.answer("Not found or not yours", show_alert=True)
        return
    rows = await DB.get_env_vars(bid)
    if index < 0 or index >= len(rows):
        await q.answer("Invalid variable", show_alert=True)
        return
    key = rows[index]["env_key"]
    msg_src = q.message
    # FIX_RICH_7: Guard q.message before sending environment-delete results.
    if msg_src is None:
        log_err("RICH: env_delete missing q.message", traceback.format_exc())
        try:
            await q.answer("Message expired, please retry", show_alert=True)
        except Exception:
            log_err("RICH: env_delete failed to answer missing-message callback", traceback.format_exc())
        return
    try:
        deleted = await DB.delete_env(bid, key)
        if not deleted:
            raise ValueError("Variable not found")
        ok, err = await prepare_env_for_bot(bid, info["dir"])
        if not ok:
            log_err(f"env file generation after delete failed {bid}: {err}")
            await msg_src.reply_text(f"⚠️ Deleted from database, but .env update failed: {err}")
        await msg_src.reply_text(
            "✅ Saved. Restart bot to apply?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Restart Now", callback_data=f"env_restart:{bid}"), InlineKeyboardButton("⏭️ Later", callback_data=f"env_back:{bid}")],
                [InlineKeyboardButton("🔐 Back to Env", callback_data=f"env_menu:{bid}")]
            ])
        )
    except Exception as e:
        log_err(f"env delete {bid}: {type(e).__name__}", traceback.format_exc())
        await q.answer(env_error_message(e), show_alert=True)


# ================== RESOURCE LIMITS UI ==================
def limits_markup(bid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Edit Memory", callback_data=f"limits_edit_mem:{bid}"), InlineKeyboardButton("✏️ Edit CPU", callback_data=f"limits_edit_cpu:{bid}")],
        [InlineKeyboardButton("🔄 Toggle Auto-restart", callback_data=f"limits_toggle_restart:{bid}")],
        [InlineKeyboardButton("🔙 Back", callback_data=f"bot_detail:{bid}")],
    ])

def limit_input_clear(ctx):
    ctx.user_data.pop("awaiting_limit_input", None)
    task=ctx.user_data.pop("limit_timeout_task",None)
    # FIX_RICH_5: Never self-cancel the currently executing limit timeout worker.
    if task and not task.done() and task is not asyncio.current_task(): task.cancel()

async def limit_timeout_worker(ctx, chat_id, prompt_id):
    try:
        await asyncio.sleep(LIMIT_INPUT_TIMEOUT_SECONDS)
        st=ctx.user_data.get("awaiting_limit_input")
        if st and st.get("msg_id")==prompt_id and st.get("expires_at",0)<=time.time():
            ctx.user_data.pop("awaiting_limit_input",None)
            try: await ctx.bot.edit_message_text(chat_id=chat_id,message_id=prompt_id,text="⏱️ Timed out")
            except Exception: pass
    except asyncio.CancelledError: pass
    except Exception: log_err("limit timeout worker",traceback.format_exc())

def schedule_limit_timeout(ctx,chat_id,prompt_id):
    old=ctx.user_data.pop("limit_timeout_task",None)
    if old and not old.done(): old.cancel()
    ctx.user_data["limit_timeout_task"]=asyncio.create_task(limit_timeout_worker(ctx,chat_id,prompt_id))

async def show_limits_menu(update,ctx,bid):
    q=update.callback_query; u=update.effective_user
    info=await get_owned_bot(u.id,bid)
    if not info: await q.answer("Not found or not yours",show_alert=True); return
    try:
        lim=await ensure_bot_limits(bid)
        total,cap,maxbot=get_vps_memory_cap_mb()
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            allocated=int((await (await conn.execute("SELECT COALESCE(SUM(memory_limit_mb),0) FROM bot_limits")).fetchone())[0])
        used=(psutil.Process(os.getpid()).memory_info().rss/(1024**2)) if HAS_PSUTIL else 0
        total_gb=total/(1024**3) if total else 0
        cap_gb=cap/(1024**3) if cap else 0
        txt=(f"⚙️ <b>Resource Limits: {_html_text(info['name'])}</b>\n"
             f"━━━━━━━━━━━━━━━━━━\n"
             f"Memory: <code>{lim['memory_limit_mb']} MB</code> (max {maxbot} MB)\n"
             f"CPU: <code>{lim['cpu_limit_percent']}%</code>\n"
             f"Auto-restart on exceed: <code>{'ON' if lim['restart_on_exceed'] else 'OFF'}</code>\n"
             f"Total allocated: <code>{allocated} MB</code> of <code>{cap_gb:.1f} GB</code> cap\n"
             f"VPS RAM: <code>{total_gb:.1f} GB</code> | Current panel RSS: <code>{used:.0f} MB</code>")
        fallback_markup=limits_markup(bid)
        # RICH: Resource limits card; fallback preserves the existing HTML screen and callbacks.
        blocks=[
            rm.heading(f"⚙️ Resource Limits: {info['name']}"),
            rm.compact_table(
                ["Setting", "Value"],
                [
                    ["Memory Limit", f"{lim['memory_limit_mb']} MB"],
                    ["Memory Max", f"{maxbot} MB"],
                    ["CPU Limit", f"{lim['cpu_limit_percent']}%"],
                    ["Auto-restart", "ON" if lim['restart_on_exceed'] else "OFF"],
                    ["Total Allocated", f"{allocated} MB"],
                    ["VPS Cap", f"{cap_gb:.1f} GB"],
                ],
            ),
            rm.button_row([
                _rich_callback_button("✏️ Edit Memory", f"limits_edit_mem:{bid}", "primary"),
                _rich_callback_button("✏️ Edit CPU", f"limits_edit_cpu:{bid}", "primary"),
            ]),
            rm.button_row([_rich_callback_button("🔄 Toggle Auto-restart", f"limits_toggle_restart:{bid}", "primary")]),
            rm.button_row([_rich_callback_button("🔙 Back", f"bot_detail:{bid}", "primary")]),
        ]
        msg = q.message
        # FIX_RICH_2: Callback queries can rarely arrive without their source message.
        if msg is None:
            log_err(f"RICH: show_limits_menu missing q.message for {bid}")
            return
        try:
            # RICH: Blocking HTTP must never run directly on the PTB event loop.
            await _rich_edit_with_fallback(
                msg.chat_id, msg.message_id, blocks,
                is_media=(getattr(msg, "video", None) is not None or getattr(msg, "photo", None) is not None),
            )
            return
        except rm.RichMessageError:
            log_err(f"RICH: show_limits_menu rich edit failed {bid}", traceback.format_exc())
        await safe_edit(q,txt,fallback_markup,parse_mode="HTML")
    except Exception as e:
        log_err(f"show_limits_menu {bid}: {type(e).__name__}",traceback.format_exc()); await safe_edit(q,"❌ Resource limits unavailable.",kb_back(f"bot_detail:{bid}"))

async def begin_limit_input(update,ctx,bid,field):
    q=update.callback_query; u=update.effective_user
    info=await get_owned_bot(u.id,bid)
    if not info: await q.answer("Not found or not yours",show_alert=True); return
    lim=await ensure_bot_limits(bid)
    current=lim[field]
    if field=="memory_limit_mb": prompt=f"Send memory limit in MB (64–{get_vps_memory_cap_mb()[2]}). Current: {current}. /cancel to cancel."
    else: prompt=f"Send CPU limit in % (1–100). Current: {current}. /cancel to cancel."
    # BUGFIX 4: A limit input must cancel any active env/admin/broadcast input state.
    _env_state_clear(ctx)
    limit_input_clear(ctx)
    ctx.user_data.pop("awaiting_admin_setting", None)
    ctx.user_data.pop("awaiting_broadcast", None)
    # FIX_RICH_11: guard q.message in begin_limit_input
    msg_src = q.message
    if msg_src is None:
        log_err("RICH: begin_limit_input missing q.message", traceback.format_exc())
        try: await q.answer("Message expired, please retry", show_alert=True)
        except Exception: log_err("RICH: begin_limit_input answer failed", traceback.format_exc())
        return
    msg=await msg_src.chat.send_message(prompt)
    ctx.user_data["awaiting_limit_input"]={"bot_id":bid,"field":field,"msg_id":msg.message_id,"expires_at":time.time()+LIMIT_INPUT_TIMEOUT_SECONDS}
    schedule_limit_timeout(ctx,msg_src.chat_id,msg.message_id)

async def handle_limit_value(update,ctx):
    st=ctx.user_data.get("awaiting_limit_input")
    if not st: return False
    if time.time()>=st.get("expires_at",0): limit_input_clear(ctx); await update.message.reply_text("⏱️ Timed out"); return True
    u=update.effective_user; bid=st["bot_id"]; field=st["field"]
    info=await get_owned_bot(u.id,bid)
    if not info: limit_input_clear(ctx); await update.message.reply_text("❌ Bot not found or not yours."); return True
    try:
        value=validate_limit_memory(update.message.text) if field=="memory_limit_mb" else validate_limit_cpu(update.message.text)
        if field=="memory_limit_mb":
            ok,msg=await memory_allocation_check(bid,value)
            if not ok: raise ValueError(msg)
        ok,msg=await save_bot_limit(bid,field,value)
        if not ok: raise ValueError(msg)
        limit_input_clear(ctx)
        b=HostedBot(bid,info["name"],info["dir"],info["type"],info["user_id"])
        # FIX_RICH_24: pm2 subprocess is blocking; run off the event loop.
        restarted = await asyncio.to_thread(b.restart)
        if not restarted: log_err(f"limit change restart failed {bid}")
        await admin_audit(u.id,"limit_change",bid,f"{field}={value}") if u.id in ADMIN_IDS else asyncio.sleep(0)
        await update.message.reply_text("⚙️ Limit updated" + (" and bot restarted." if restarted else ", but restart failed; check logs."))
    except Exception as e:
        log_err(f"limit input {bid}: {type(e).__name__}",traceback.format_exc()); await update.message.reply_text(env_error_message(e))
    return True

# ================== ADMIN PANEL ==================
def admin_only(uid): return uid in ADMIN_IDS or uid==OWNER_ID

def admin_main_markup():
    # UI: Organized admin cards with maximum two buttons per row.
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{UI.USERS} Users",callback_data="admin_users:0"),
         InlineKeyboardButton(f"{UI.BOTS} All Bots",callback_data="admin_bots:0")],
        [InlineKeyboardButton(f"{UI.STATUS} Stats",callback_data="admin_stats"),
         InlineKeyboardButton(f"{UI.BROADCAST} Broadcast",callback_data="admin_broadcast")],
        [InlineKeyboardButton(f"{UI.AUDIT} Audit",callback_data="admin_audit:0:all"),
         InlineKeyboardButton(f"{UI.MAINTENANCE} Maintenance",callback_data="admin_maintenance")],
        [InlineKeyboardButton(f"{UI.LIMITS} Resource Defaults",callback_data="admin_limits_defaults")],
        [InlineKeyboardButton(f"{UI.ERROR} Close",callback_data="menu")]
    ])

def admin_page_markup(kind,page,total,extra=None):
    rows=[]
    if kind=="users":
        for uid in extra or []: rows.append([InlineKeyboardButton(f"👤 {uid}",callback_data=f"admin_user:{uid}:0")])
    elif kind=="bots":
        for bid,name in extra or []: rows.append([InlineKeyboardButton(f"🤖 {name[:35]}",callback_data=f"admin_bot:{bid}")])
    nav=[]
    if page>0: nav.append(InlineKeyboardButton("⬅️ Prev",callback_data=f"admin_{kind}:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{max(1,(total+9)//10)}",callback_data=f"admin_{kind}:{page}"))
    if (page+1)*10<total: nav.append(InlineKeyboardButton("➡️ Next",callback_data=f"admin_{kind}:{page+1}"))
    rows.append(nav); rows.append([InlineKeyboardButton("🔙 Admin",callback_data="admin_panel")]); return InlineKeyboardMarkup(rows)

async def show_admin_panel(update,ctx):
    q=update.callback_query; await safe_edit(q,f"{UI.ADMIN} <b>Admin Panel</b>\n{UI.DIV}\nChoose an administrative action.{credit_footer()}",
                        admin_main_markup(),parse_mode="HTML")

async def admin_users(update,ctx,page=0):
    q=update.callback_query
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        cur=await conn.execute("SELECT DISTINCT user_id FROM bot_registry ORDER BY user_id"); ids=[int(r[0]) for r in await cur.fetchall()]
    total=len(ids); ids_page=ids[page*10:(page+1)*10]
    lines=[f"👥 <b>Users</b> — {total} total","━━━━━━━━━━━━━━━━━━"]
    for uid in ids_page:
        bots=await DB.count_user_bots(uid); prem=await DB.is_premium(uid); muted=await DB.is_muted(uid); banned=await is_banned(uid)
        try: chat=await ctx.bot.get_chat(uid); uname=f"@{chat.username}" if chat.username else "@unknown"
        except Exception: uname="@unknown"
        lines.append(f"User <code>{uid}</code> | {_html_text(uname)} | {bots} bots | Premium: {'yes' if prem else 'no'} | Muted: {'yes' if muted else 'no'} | Ban: {'yes' if banned else 'no'}")
    await safe_edit(q,"\n".join(lines),admin_page_markup("users",page,total,ids_page),parse_mode="HTML")

async def admin_user_detail(update,ctx,uid):
    q=update.callback_query
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        row=await (await conn.execute("SELECT COUNT(*) FROM bot_registry WHERE user_id=?",(uid,))).fetchone(); n=int(row[0])
    prem=await DB.is_premium(uid); muted=await DB.is_muted(uid); banned=await is_banned(uid)
    kb=InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 View Bots",callback_data=f"admin_user_bots:{uid}:0")],
        [InlineKeyboardButton("➕ Add Premium",callback_data=f"admin_premium:{uid}:1"),InlineKeyboardButton("➖ Remove Premium",callback_data=f"admin_premium:{uid}:0")],
        [InlineKeyboardButton("🔇 Mute 2h",callback_data=f"admin_mute:{uid}"),InlineKeyboardButton("🔊 Unmute",callback_data=f"admin_unmute:{uid}")],
        [InlineKeyboardButton("🚫 Ban",callback_data=f"admin_ban_confirm:{uid}"),InlineKeyboardButton("♻️ Unban",callback_data=f"admin_unban:{uid}")],
        [InlineKeyboardButton("🗑️ Delete All Bots",callback_data=f"admin_delall_confirm:{uid}")],
        [InlineKeyboardButton("🔙 Users",callback_data="admin_users:0")]])
    txt=f"👤 <b>User {uid}</b>\nBots: <code>{n}</code>\nPremium: <code>{'yes' if prem else 'no'}</code>\nMuted: <code>{'yes' if muted else 'no'}</code>\nBanned: <code>{'yes' if banned else 'no'}</code>"
    await safe_edit(q,txt,kb,parse_mode="HTML")

async def admin_all_bots(update,ctx,page=0):
    q=update.callback_query
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        conn.row_factory=aiosqlite.Row
        rows=[dict(r) for r in await (await conn.execute("SELECT * FROM bot_registry ORDER BY created_at DESC")).fetchall()]
    total=len(rows); chunk=rows[page*10:(page+1)*10]
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        pending_count=int((await (await conn.execute("SELECT COUNT(*) FROM pending_deploys WHERE status='pending'")).fetchone())[0])
    lines=[f"🤖 <b>All Bots</b> — {total} registered | pending: {pending_count}","━━━━━━━━━━━━━━━━━━"]
    btn=[]
    # BUGFIX 10: Fetch the current page's limits with one DB query.
    ids = [r['bot_id'] for r in chunk]
    if ids:
        placeholders = ','.join('?' * len(ids))
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            limits_rows = await (await conn.execute(
                f"SELECT * FROM bot_limits WHERE bot_id IN ({placeholders})", ids
            )).fetchall()
        limits_map = {row['bot_id']: dict(row) for row in limits_rows}
    else:
        limits_map = {}

    for r in chunk:
        lim = limits_map.get(r['bot_id'])
        if lim is None:
            lim = await ensure_bot_limits(r['bot_id'])
        b=HostedBot(r['bot_id'],r['name'],r['dir'],r['type'],r['user_id'])
        # FIX_RICH_17: pm2 jlist/logs is blocking; keep it off the event loop.
        _running = await asyncio.to_thread(b.is_running)
        st='🟢' if _running else '🔴'
        lines.append(f"{_html_text(r['name'],40)} | Owner:<code>{r['user_id']}</code> | {st} | RAM:<code>{lim['memory_limit_mb']}MB</code>")
        btn.append([InlineKeyboardButton(f"Details: {r['name'][:28]}",callback_data=f"admin_bot:{r['bot_id']}")])
    nav=[]
    if page>0: nav.append(InlineKeyboardButton("⬅️ Prev",callback_data=f"admin_bots:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{max(1,(total+9)//10)}",callback_data=f"admin_bots:{page}"))
    if (page+1)*10<total: nav.append(InlineKeyboardButton("➡️ Next",callback_data=f"admin_bots:{page+1}"))
    btn.append(nav); btn.append([InlineKeyboardButton("🔙 Admin",callback_data="admin_panel")])
    await safe_edit(q,"\n".join(lines),InlineKeyboardMarkup(btn),parse_mode="HTML")

async def admin_bot_detail(update,ctx,bid):
    q=update.callback_query
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        conn.row_factory=aiosqlite.Row; r=await (await conn.execute("SELECT * FROM bot_registry WHERE bot_id=?",(bid,))).fetchone()
    if not r: await q.answer("Bot not found",show_alert=True); return
    lim=await ensure_bot_limits(bid); b=HostedBot(bid,r['name'],r['dir'],r['type'],r['user_id'])
    # FIX_RICH_17: pm2 jlist/logs is blocking; keep it off the event loop.
    st='Running' if await asyncio.to_thread(b.is_running) else 'Stopped'
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("📊 Details",callback_data=f"bot_detail:{bid}"),InlineKeyboardButton("⚙️ Limits",callback_data=f"limits_menu:{bid}")],[InlineKeyboardButton("🛑 Force Stop",callback_data=f"admin_force_stop:{bid}"),InlineKeyboardButton("🗑️ Force Delete",callback_data=f"admin_force_del_confirm:{bid}")],[InlineKeyboardButton("📋 View Logs",callback_data=f"logs:{bid}")],[InlineKeyboardButton("🔙 Bots",callback_data="admin_bots:0")]])
    txt=f"🤖 <b>{_html_text(r['name'])}</b>\nOwner:<code>{r['user_id']}</code>\nStatus:<code>{st}</code>\nRAM:<code>{lim['memory_limit_mb']} MB</code>\nCPU:<code>{lim['cpu_limit_percent']}%</code>"
    await safe_edit(q,txt,kb,parse_mode="HTML")

async def admin_server_stats(update,ctx):
    q=update.callback_query; st=get_system_stats()
    if not st:
        await safe_edit(q,"❌ psutil is required for server stats.",kb_back("admin_panel")); return
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        allocated=int((await (await conn.execute("SELECT COALESCE(SUM(memory_limit_mb),0) FROM bot_limits")).fetchone())[0])
        totalbots=int((await (await conn.execute("SELECT COUNT(*) FROM bot_registry")).fetchone())[0])
    try:
        # FIX_RICH_22: pm2 subprocess is blocking; run off the event loop.
        r = await asyncio.to_thread(subprocess.run, ["pm2","jlist"], capture_output=True, text=True, timeout=5)
        plist=json.loads(r.stdout) if r.returncode==0 and r.stdout else []
        pm2_count=len(plist); active=sum(1 for p in plist if p.get("pm2_env",{}).get("name","").startswith("hosted-bot-") and p.get("pm2_env",{}).get("status")=="online")
    except Exception: pm2_count=active=0
    available_gb=psutil.virtual_memory().available/(1024**3) if HAS_PSUTIL else 0
    dbsize=DATABASE_PATH.stat().st_size/(1024**2) if DATABASE_PATH.exists() else 0
    panel_started=await get_setting("panel_started_at","unknown"); approval_started=await get_setting("approval_started_at","unknown")
    txt=(f"📊 <b>Server Stats</b>\n"
         f"CPU: <code>{st['cpu']:.1f}%</code>\n"
         f"RAM: <code>{st['ram_used']:.1f}/{st['ram_total']:.1f} GB</code> ({st['ram_pct']}%) | Available: <code>{available_gb:.1f} GB</code>\n"
         f"Disk: <code>{st['disk_used']:.1f}/{st['disk_total']:.1f} GB</code> ({st['disk_pct']}%)\n"
         f"PM2 processes: <code>{pm2_count}</code>\nActive hosted bots: <code>{active}</code>\nRegistered bots: <code>{totalbots}</code>\n"
         f"Allocated bot memory: <code>{allocated} MB</code>\nDB size: <code>{dbsize:.2f} MB</code>\nPanel uptime: <code>{st['uptime']}</code>\n"
         f"Panel started: <code>{_html_text(panel_started,32)}</code>\nApproval started: <code>{_html_text(approval_started,32)}</code>")
    fallback_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh",callback_data="admin_stats")],[InlineKeyboardButton("🔙 Admin",callback_data="admin_panel")]])
    blocks=[
        rm.heading("📊 Server Stats"),
        rm.compact_table(["🖥️ SYSTEM", "Value"], [
            ["CPU", f"{st['cpu']:.1f}%"],
            ["RAM Used", f"{st['ram_used']:.1f}/{st['ram_total']:.1f} GB"],
            ["RAM %", f"{st['ram_pct']}%"],
            ["Disk Used", f"{st['disk_used']:.1f}/{st['disk_total']:.1f} GB"],
            ["Uptime", st['uptime']],
        ]),
        rm.compact_table(["⚙️ PM2", "Value"], [
            ["Total Processes", str(pm2_count)],
            ["Active Bots", str(active)],
            ["Registered Bots", str(totalbots)],
            ["Allocated RAM", f"{allocated} MB"],
        ]),
        rm.compact_table(["🗄️ DATABASE", "Value"], [
            ["DB Size", f"{dbsize:.2f} MB"],
            ["Panel Started", str(panel_started)[:19]],
            ["Approval Started", str(approval_started)[:19]],
        ]),
        rm.button_row([
            _rich_callback_button("🔄 Refresh", "admin_stats", "primary"),
            _rich_callback_button("🔙 Admin", "admin_panel", "primary"),
        ]),
    ]
    msg = q.message
    # FIX_RICH_2: Avoid AttributeError when a callback has no source message.
    if msg is None:
        log_err("RICH: admin_server_stats missing q.message")
        return
    try:
        # RICH: Sectioned admin stats; plain HTML keyboard remains the mandatory fallback.
        await _rich_edit_with_fallback(
            msg.chat_id, msg.message_id, blocks,
            is_media=(getattr(msg, "video", None) is not None or getattr(msg, "photo", None) is not None),
        )
        return
    except rm.RichMessageError:
        log_err("RICH: admin_server_stats rich edit failed", traceback.format_exc())
    await safe_edit(q,txt,fallback_markup,parse_mode="HTML")

async def admin_audit_view(update,ctx,page=0,filter_kind="all",filter_value=None):
    q=update.callback_query
    where=""; args=[]
    if filter_kind=="admin": where="WHERE admin_id=?"; args=[int(filter_value)]
    elif filter_kind=="action": where="WHERE action=?"; args=[filter_value]
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        conn.row_factory=aiosqlite.Row; rows=await (await conn.execute(f"SELECT * FROM admin_audit {where} ORDER BY id DESC LIMIT ? OFFSET ?",(*args,AUDIT_PAGE_MAX,page*AUDIT_PAGE_SIZE))).fetchall()
        total=int((await (await conn.execute(f"SELECT COUNT(*) FROM admin_audit {where}",tuple(args))).fetchone())[0])
    lines=[f"📋 <b>Audit Log</b> — {total}","━━━━━━━━━━━━━━━━━━"]
    for r in rows[:AUDIT_PAGE_SIZE]: lines.append(f"{_html_text(r['created_at'],19)} | admin:<code>{r['admin_id']}</code> | {_html_text(r['action'],40)} | {_html_text(r['target'] or '-',40)}")
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("All",callback_data=f"admin_audit:0:all"),InlineKeyboardButton("By Admin",callback_data="admin_audit_filter_admin"),InlineKeyboardButton("By Action",callback_data="admin_audit_filter_action")],[InlineKeyboardButton("🔙 Admin",callback_data="admin_panel")]])
    if page>0 or len(rows)>=AUDIT_PAGE_SIZE:
        nav=[]
        if page>0: nav.append(InlineKeyboardButton("⬅️ Prev",callback_data=f"admin_audit:{page-1}:{filter_kind}"))
        if len(rows)>=AUDIT_PAGE_SIZE: nav.append(InlineKeyboardButton("➡️ Next",callback_data=f"admin_audit:{page+1}:{filter_kind}"))
        kb.inline_keyboard.insert(1,nav)
    await safe_edit(q,"\n".join(lines),kb,parse_mode="HTML")

async def admin_limits_defaults(update,ctx):
    q=update.callback_query
    d=await get_setting("default_memory_mb","256"); m=await get_setting("max_memory_per_bot_mb","2048"); c=await get_setting("vps_cap_percent","80")
    total,cap,maxbot=get_vps_memory_cap_mb()
    txt=(f"⚙️ <b>Resource Defaults</b>\nDefault memory: <code>{d} MB</code>\nMax per bot: <code>{m} MB</code> (runtime max {maxbot} MB)\nVPS cap: <code>{c}%</code> ({cap} MB of {total} MB)" )
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("✏️ Default Memory",callback_data="admin_default_mem"),InlineKeyboardButton("✏️ Max Memory",callback_data="admin_max_mem")],[InlineKeyboardButton("✏️ VPS Cap %",callback_data="admin_cap")],[InlineKeyboardButton("🔙 Admin",callback_data="admin_panel")]])
    await safe_edit(q,txt,kb,parse_mode="HTML")

async def begin_admin_setting_input(update,ctx,key):
    q=update.callback_query
    # BUGFIX 4: Starting an admin setting input cancels competing input states.
    _env_state_clear(ctx)
    limit_input_clear(ctx)
    ctx.user_data.pop("awaiting_broadcast", None)
    labels={"default_memory_mb":"default memory in MB","max_memory_per_bot_mb":"maximum memory per bot in MB","vps_cap_percent":"VPS cap percentage (1–100)"}
    current=await get_setting(key,"256" if key=="default_memory_mb" else "2048" if key=="max_memory_per_bot_mb" else "80")
    # FIX_RICH_12: guard q.message in begin_admin_setting_input
    msg_src = q.message
    if msg_src is None:
        log_err("RICH: begin_admin_setting_input missing q.message", traceback.format_exc())
        try: await q.answer("Message expired, please retry", show_alert=True)
        except Exception: log_err("RICH: begin_admin_setting_input answer failed", traceback.format_exc())
        return
    msg=await msg_src.chat.send_message(f"Send {labels[key]}. Current: {current}. /cancel to cancel.")
    ctx.user_data["awaiting_admin_setting"]={"key":key,"msg_id":msg.message_id,"expires_at":time.time()+120}

async def handle_admin_setting_value(update,ctx):
    st=ctx.user_data.get("awaiting_admin_setting")
    if not st: return False
    if time.time()>st.get("expires_at",0): ctx.user_data.pop("awaiting_admin_setting",None); await update.message.reply_text("⏱️ Timed out"); return True
    try:
        n=int((update.message.text or "").strip()); key=st["key"]
        if key=="default_memory_mb":
            n=validate_limit_memory(str(n))
        elif key=="max_memory_per_bot_mb":
            if n<64 or n>LIMIT_MAX_MEMORY_MB: raise ValueError("Max memory must be 64–2048 MB")
            n=effective_memory_max(n)
        else:
            if n<1 or n>100: raise ValueError("VPS cap must be 1–100%")
        if key=="default_memory_mb":
            maxv=effective_memory_max(int(await get_setting("max_memory_per_bot_mb","2048")))
            if n>maxv: raise ValueError(f"Default memory cannot exceed max per-bot value ({maxv} MB)")
        if key=="max_memory_per_bot_mb":
            async with aiosqlite.connect(DATABASE_PATH) as conn:
                mx=int((await (await conn.execute("SELECT COALESCE(MAX(memory_limit_mb),0) FROM bot_limits")).fetchone())[0])
            if mx>n: raise ValueError(f"Existing bot limit {mx} MB is above this new maximum")
        if key=="vps_cap_percent":
            total=int(psutil.virtual_memory().total/(1024**2)) if HAS_PSUTIL else 0
            new_cap=int(total*n/100) if total else 0
            async with aiosqlite.connect(DATABASE_PATH) as conn:
                allocated=int((await (await conn.execute("SELECT COALESCE(SUM(memory_limit_mb),0) FROM bot_limits")).fetchone())[0])
            if total and allocated>new_cap: raise ValueError(f"Existing allocation {allocated} MB exceeds the new VPS cap {new_cap} MB")
        await set_setting(key,str(n)); await admin_audit(update.effective_user.id,"resource_default_change",key,str(n)); ctx.user_data.pop("awaiting_admin_setting",None)
        await update.message.reply_text("⚙️ Resource default updated")
    except Exception as e:
        log_err(f"admin setting input: {type(e).__name__}",traceback.format_exc()); await update.message.reply_text(env_error_message(e))
    return True

async def admin_maintenance(update,ctx):
    q=update.callback_query; mm=await get_setting("maintenance_mode","0"); de=await get_setting("deploys_enabled","1"); unlocked=await DB.is_unlocked()
    txt=f"🔧 <b>Maintenance</b>\nMaintenance: <code>{'ON' if mm=='1' else 'OFF'}</code>\nNew deploys: <code>{'ON' if de=='1' else 'OFF'}</code>\nPanel lock: <code>{'UNLOCKED' if unlocked else 'LOCKED'}</code>"
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Maintenance",callback_data="admin_toggle_maintenance"),InlineKeyboardButton("📦 Deploys",callback_data="admin_toggle_deploys")],[InlineKeyboardButton("🔒 Toggle Panel Lock",callback_data="admin_toggle_lock")],[InlineKeyboardButton("🔄 Restart Panel Bot",callback_data="admin_restart_panel"),InlineKeyboardButton("🔄 Restart Approval Bot",callback_data="admin_restart_approval")],[InlineKeyboardButton("🛑 Shutdown All Bots",callback_data="admin_shutdown_confirm")],[InlineKeyboardButton("🔙 Admin",callback_data="admin_panel")]])
    await safe_edit(q,txt,kb,parse_mode="HTML")

async def cmd_admin(update,ctx):
    u=update.effective_user
    if not u: return
    if not admin_only(u.id):
        await update.message.reply_text(f"🔒 Admin only\nYour ID: <code>{u.id}</code>",parse_mode="HTML"); return
    await update.message.reply_text("🔧 <b>Admin Panel</b>\n━━━━━━━━━━━━━━━━━━\nChoose an administrative action.",reply_markup=admin_main_markup(),parse_mode="HTML")

async def handle_limit_commands(update,ctx):
    if ctx.user_data.get("awaiting_limit_input"):
        await handle_limit_value(update,ctx); return True
    return False

# ================== HANDLERS ==================
async def safe_edit(q, text, markup=None, parse_mode="HTML"):
    # UI: Centralized HTML rendering with plain-text fallback for callback queries and messages.
    target = getattr(q, "message", None) if hasattr(q, "message") else q
    # FIX_RICH_6: Guard CallbackQuery with no source message before any edit_text call.
    if target is None:
        log_err("safe_edit: callback query missing message", traceback.format_exc())
        return
    if not hasattr(target, "edit_text"):
        log_err("safe_edit: target has no edit_text method", traceback.format_exc())
        return
    try:
        is_media = bool(getattr(target, "video", None) or getattr(target, "photo", None) or
                        getattr(target, "animation", None) or getattr(target, "document", None))
        if is_media:
            try: await target.delete()
            except Exception: pass
            await target.chat.send_message(text, parse_mode=parse_mode, reply_markup=markup)
        else:
            await target.edit_text(text, parse_mode=parse_mode, reply_markup=markup)
    except BadRequest as e:
        if "Message is not modified" in str(e): return
        if parse_mode == "HTML":
            try:
                plain = re.sub(r"<[^>]+>", "", text)
                await target.edit_text(plain, reply_markup=markup)
                return
            except Exception: pass
        raise

async def check_access(update) -> bool:
    u = update.effective_user
    if not u: return False
    if u.id not in ADMIN_IDS and await is_banned(u.id):
        reason=await get_ban_reason(u.id)
        msg=f"🚫 You have been banned. Reason: {_html_text(reason or 'Admin decision')}"
        if update.callback_query:
            try: await update.callback_query.answer(msg[:180],show_alert=True)
            except Exception: pass
        elif update.message: await update.message.reply_text(msg,parse_mode="HTML")
        return False
    if u.id not in ADMIN_IDS and await get_setting("maintenance_mode","0")=="1":
        if update.callback_query:
            try: await update.callback_query.answer("🛠️ Under maintenance",show_alert=True)
            except Exception: pass
        elif update.message: await update.message.reply_text("🛠️ Under maintenance")
        return False
    if not await can_use(u.id):
        if update.callback_query:
            try: await update.callback_query.answer("🔒 Premium required", show_alert=True)
            except Exception: pass
        elif update.message:
            await update.message.reply_text(
                f"{UI.ERROR} Access denied.\nYour ID: <code>{u.id}</code>{credit_footer()}", parse_mode="HTML")
        return False
    return True


# RICH: Centralized wrappers keep blocking HTTP off the PTB event loop and log every failure.
async def _rich_send_with_fallback(chat_id: int, blocks: list, token=None) -> dict:
    return await asyncio.to_thread(rm.send_blocks, chat_id, blocks, token)


async def _rich_edit_with_fallback(chat_id: int, message_id: int, blocks: list, token=None, is_media: bool = False) -> dict:
    # FIX_RICH_2: Guard invalid callback-message identifiers before entering the worker thread.
    if not chat_id or not message_id:
        log_err("RICH: missing chat_id/message_id for rich edit")
        raise rm.RichMessageError("missing chat_id/message_id")
    # FIX_RICH_3: Try a rich caption edit first when the source message is media.
    if is_media:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "rich_message": {"blocks": list(blocks)},
        }
        try:
            return await asyncio.to_thread(rm._call_api, "editMessageCaption", payload, token)
        except rm.RichMessageError:
            log_err("FIX_RICH_3: editMessageCaption rich edit failed", traceback.format_exc())
            raise
    return await asyncio.to_thread(rm.edit_blocks, chat_id, message_id, blocks, token)

def _rich_callback_button(text: str, callback_data: str, style: str = "primary") -> dict:
    return {"text": text, "callback_data": callback_data, "style": style}


def _rich_url_button(text: str, url: str, style: str = "primary") -> dict:
    return {"text": text, "url": url, "style": style}

async def show_help(update, ctx):
    q=update.callback_query
    txt=(f"{UI.INFO} <b>How it works</b>\n{UI.DIV}\n"
         "1. Deploy a bot (ZIP, single file, or GitHub)\n"
         "2. Set environment variables\n"
         "3. Configure memory/CPU limits\n"
         "4. Monitor logs and status\n\n"
         "Everything is managed from Telegram.\n\n"
         "<b>Commands</b>\n<code>/start</code> — main menu\n"
         "<code>/admin</code> — admin panel (admins only)\n"
         "<code>/cancel</code> — cancel current input\n"
         f"{UI.DIV}{credit_footer()}")
    fallback_markup=kb_help()
    blocks=[
        rm.heading("ℹ️ How it works"),
        rm.paragraph("1. Deploy a bot (ZIP, single file, or GitHub)"),
        rm.paragraph("2. Set environment variables"),
        rm.paragraph("3. Configure memory/CPU limits"),
        rm.paragraph("4. Monitor logs and status"),
        rm.paragraph("Everything is managed from Telegram."),
        rm.compact_table(["Command", "Purpose"], [
            ["/start", "Main menu"],
            ["/admin", "Admin panel"],
            ["/cancel", "Cancel current input"],
        ]),
        rm.button_row([_rich_callback_button("🔙 Back", "menu", "primary")]),
    ]
    msg = q.message
    # FIX_RICH_2: Guard deleted/orphaned callback messages before reading message IDs.
    if msg is None:
        log_err("RICH: show_help missing q.message")
        return
    try:
        # RICH: Help card keeps the existing back callback and HTML fallback intact.
        await _rich_edit_with_fallback(
            msg.chat_id, msg.message_id, blocks,
            is_media=(getattr(msg, "video", None) is not None or getattr(msg, "photo", None) is not None),
        )
        return
    except rm.RichMessageError:
        log_err("RICH: show_help rich edit failed", traceback.format_exc())
    await safe_edit(q,txt,fallback_markup,parse_mode="HTML")

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u=update.effective_user
    if not u: return
    if not await can_use(u.id):
        await update.message.reply_text(f"{UI.ERROR} Access denied.\nYour ID: <code>{u.id}</code>{credit_footer()}",
                                        parse_mode="HTML"); return

    # RICH: Calculate live bot status for the card; the original reply_video path remains the fallback.
    bots=await DB.get_user_bots(u.id)
    total_bots=len(bots) if u.id in ADMIN_IDS else len([b for b in bots.values() if b.get("user_id")==u.id])
    running_count=0
    for bid,info in bots.items():
        if u.id not in ADMIN_IDS and info.get("user_id") != u.id:
            continue
        try:
            if await asyncio.to_thread(HostedBot(bid,info["name"],info["dir"],info["type"],info["user_id"]).is_running):
                running_count += 1
        except Exception:
            pass
    stopped_count=max(0,total_bots-running_count)

    blocks=[
        rm.video(HOME_VIDEO_URL),
        rm.heading("💠 VPS HOSTING MANAGER"),
        rm.paragraph(f"Welcome, {u.first_name or u.full_name}! Deploy and manage bots from Telegram."),
        rm.compact_table(["Information", "Details"], [
            ["Owner", "@OG_SAGAR_ddos"],
            ["Version", "3.0"],
            ["Your Bots", str(total_bots)],
            ["Running", str(running_count)],
            ["Stopped", str(stopped_count)],
        ]),
        rm.paragraph("Choose an option below 👇"),
        rm.button_row([
            _rich_callback_button("📦 Deploy New Bot", "deploy", "success"),
            _rich_callback_button("🐙 GitHub Repo", "github_deploy", "success"),
        ]),
        rm.button_row([
            _rich_callback_button("🤖 My Bots", "my_bots", "primary"),
            _rich_callback_button("📊 VPS Status", "vps_status", "primary"),
        ]),
        rm.button_row([
            _rich_callback_button("ℹ️ Help", "help", "primary"),
            _rich_callback_button("⚙️ Settings", "settings", "primary"),
        ]),
        rm.button_row([_rich_url_button("📢 Channel", SUPPORT_CHANNEL, "primary")]),
    ]
    try:
        # RICH: sendRichMessage gives the requested in-message buttons; blocking requests run in a worker thread.
        await _rich_send_with_fallback(update.effective_chat.id, blocks)
        return
    except rm.RichMessageError:
        log_err("RICH: cmd_start rich send failed, falling back", traceback.format_exc())

    # Existing fallback preserved exactly: video + HTML caption + original keyboard.
    caption=(f"{UI.BOTS} <b>VPS Bot Hosting Manager</b>\n{UI.DIV}\n"
             f"{UI.NODE} Node.js  ·  {UI.PYTHON} Python  ·  {UI.WHATSAPP} WhatsApp\n\n"
             f"👤 User ID: <code>{u.id}</code>\nChoose an option below 👇{credit_footer()}")
    try:
        await update.message.reply_video(video=HOME_VIDEO_URL,caption=caption,parse_mode="HTML",reply_markup=kb_main())
    except Exception:
        log_err("cmd_start reply_video failed",traceback.format_exc())
        await update.message.reply_text(caption,parse_mode="HTML",reply_markup=kb_main())

async def cmd_unlock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u or u.id != OWNER_ID: return
    args = ctx.args
    if not args or args[0].lower() not in ("on", "off"):
        cur = await DB.is_unlocked()
        await update.message.reply_text(
            f"Status: <code>{'UNLOCKED' if cur else 'LOCKED'}</code>\nUse <code>/unlock on|off</code>{credit_footer()}",
            parse_mode="HTML")
        return
    await DB.set_unlocked(args[0].lower() == "on")
    await update.message.reply_text(f"{UI.SUCCESS} Set to <b>{_html_text(args[0].upper())}</b>{credit_footer()}", parse_mode="HTML")

async def cmd_addpremium(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u or u.id not in ADMIN_IDS: return
    if not ctx.args:
        await update.message.reply_text("Usage: <code>/addpremium &lt;user_id&gt;</code>", parse_mode="HTML")
        return
    try: tid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID"); return
    await DB.add_premium(tid, u.id)
    await update.message.reply_text(f"{UI.SUCCESS} Added premium: <code>{tid}</code>{credit_footer()}", parse_mode="HTML")

async def cmd_removepremium(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u or u.id not in ADMIN_IDS: return
    if not ctx.args:
        await update.message.reply_text("Usage: <code>/removepremium &lt;user_id&gt;</code>", parse_mode="HTML")
        return
    try: tid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("Invalid ID"); return
    ok = await DB.remove_premium(tid)
    await update.message.reply_text(f"{UI.SUCCESS if ok else UI.ERROR} {'Removed' if ok else 'Not found'}: <code>{tid}</code>", parse_mode="HTML")

async def cmd_unmute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u or u.id not in ADMIN_IDS: return
    if not ctx.args:
        await update.message.reply_text("Usage: <code>/unmute &lt;user_id&gt;</code>", parse_mode="HTML")
        return
    try:
        tid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID")
        return
    ok = await DB.unmute_user(tid)
    await update.message.reply_text(
        f"{UI.SUCCESS if ok else UI.ERROR} {'Unmuted' if ok else 'Not muted'}: <code>{tid}</code>", parse_mode="HTML")

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u: return
    # BUGFIX 3: /cancel is the single cancellation entry point for Feature 1
    # and Feature 2 states, including the env timeout task.
    had_env = bool(ctx.user_data.get("awaiting_env_value"))
    _env_state_clear(ctx)
    _github_state_clear(ctx)
    was_waiting = bool(ctx.user_data.get("awaiting_zip"))
    had_limit = bool(ctx.user_data.get("awaiting_limit_input"))
    had_admin_setting = bool(ctx.user_data.get("awaiting_admin_setting"))
    limit_input_clear(ctx)
    ctx.user_data.pop("awaiting_admin_setting",None)
    ctx.user_data.pop("awaiting_broadcast",None)
    ctx.user_data.pop("broadcast_confirm",None)
    ctx.user_data.pop("awaiting_audit_filter",None)
    ctx.user_data["awaiting_zip"] = False
    ctx.user_data.pop("deploy_type", None)
    await update.message.reply_text(
        "✅ Cancelled — input cleared." if (had_env or was_waiting or had_limit or had_admin_setting) else "Nothing to cancel.")

async def cmd_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u or u.id not in ADMIN_IDS: return
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM pending_deploys WHERE status='pending' ORDER BY created_at DESC LIMIT 50")
        rows = await cur.fetchall()
    if not rows:
        await update.message.reply_text("✅ No pending deploys.")
        return
    for r in rows:
        txt = (
            f"⏳ <b>Pending Deploy</b>\n"
            f"User: <code>{r['user_id']}</code>\nBot: <code>{_html_text(r['name'])}</code> ({_html_text(r['bot_type'])})\n"
            f"ID: <code>{_html_text(r['bot_id'])}</code>"
        )
        await update.message.reply_text(txt, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Approve", callback_data=f"host_approve:{r['bot_id']}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"host_reject:{r['bot_id']}"),
            ]]))

async def admin_callback_error_handler(update,ctx):
    try:
        u=update.effective_user
        if u and admin_only(u.id):
            log_err(f"admin action failed {u.id}: {type(ctx.error).__name__}",traceback.format_exc())
            if update.callback_query:
                try: await update.callback_query.answer("❌ Admin action failed. Check logs.",show_alert=True)
                except Exception: pass
            elif update.message:
                try: await update.message.reply_text("❌ Admin action failed. Check logs.")
                except Exception: pass
    except Exception: pass

async def cb_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; u=update.effective_user
    if not u: return
    if not await check_access(update): return
    data=q.data or ""; parts=data.split(":"); action=parts[0] if parts else ""; bid=parts[1] if len(parts)>1 else ""
    # BUGFIX 5: Do not pre-answer callbacks; the action handler answers once.
    # Feature 2 resource-limit callbacks
    if action=="limits_menu" and bid:
        await show_limits_menu(update,ctx,bid); return
    if action=="limits_edit_mem" and bid:
        await begin_limit_input(update,ctx,bid,"memory_limit_mb"); return
    if action=="limits_edit_cpu" and bid:
        await begin_limit_input(update,ctx,bid,"cpu_limit_percent"); return
    if action=="limits_toggle_restart" and bid:
        info=await get_owned_bot(u.id,bid)
        if not info: await q.answer("Not found or not yours",show_alert=True); return
        lim=await ensure_bot_limits(bid); new=0 if lim["restart_on_exceed"] else 1
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            await conn.execute("UPDATE bot_limits SET restart_on_exceed=?,updated_at=? WHERE bot_id=?",(new,utc_now_iso(),bid)); await conn.commit()
        if u.id in ADMIN_IDS: await admin_audit(u.id,"limit_restart_toggle",bid,f"restart_on_exceed={new}")
        await show_limits_menu(update,ctx,bid); return

    # Feature 2 admin callbacks
    if data=="admin_panel" and admin_only(u.id): await show_admin_panel(update,ctx); return
    if action=="admin_users" and admin_only(u.id): await admin_users(update,ctx,max(0,int(bid or 0))); return
    if action=="admin_bots" and admin_only(u.id): await admin_all_bots(update,ctx,max(0,int(bid or 0))); return
    if action=="admin_user" and admin_only(u.id) and len(parts)>=2: await admin_user_detail(update,ctx,int(bid)); return
    if action=="admin_user_bots" and admin_only(u.id) and len(parts)>=3:
        uid=int(bid); page=max(0,int(parts[2])); bots=await DB.get_user_bots(uid); items=list(bots.items()); chunk=items[page*10:(page+1)*10]
        lines=[f"🤖 <b>Bots of {uid}</b>","━━━━━━━━━━━━━━━━━━"]
        for k,v in chunk: lines.append(f"{_html_text(v['name'],40)} — <code>{_html_text(k)}</code>")
        rows=[[InlineKeyboardButton(v["name"][:30],callback_data=f"admin_bot:{k}")] for k,v in chunk]
        nav=[]
        if page>0: nav.append(InlineKeyboardButton("⬅️ Prev",callback_data=f"admin_user_bots:{uid}:{page-1}"))
        if (page+1)*10<len(items): nav.append(InlineKeyboardButton("➡️ Next",callback_data=f"admin_user_bots:{uid}:{page+1}"))
        if nav: rows.append(nav)
        rows.append([InlineKeyboardButton("🔙 User",callback_data=f"admin_user:{uid}:0")])
        await safe_edit(q,"\n".join(lines),InlineKeyboardMarkup(rows),parse_mode="HTML"); return
    if action=="admin_bot" and admin_only(u.id) and bid: await admin_bot_detail(update,ctx,bid); return
    if action=="admin_stats" and admin_only(u.id): await admin_server_stats(update,ctx); return
    if action=="admin_audit" and admin_only(u.id): await admin_audit_view(update,ctx,max(0,int(bid or 0)),parts[2] if len(parts)>2 else "all"); return
    if data=="admin_audit_filter_admin" and admin_only(u.id):
        # FIX_RICH_14: guard q.message in broadcast/audit-filter callbacks
        msg_src = q.message
        if msg_src is None:
            log_err("RICH: admin_audit_filter_admin missing q.message", traceback.format_exc())
            try: await q.answer("Message expired, please retry", show_alert=True)
            except Exception: pass
            return
        ctx.user_data["awaiting_audit_filter"]={"kind":"admin","expires_at":time.time()+120}; await msg_src.chat.send_message("Send admin ID to filter. /cancel to cancel."); return
    if data=="admin_audit_filter_action" and admin_only(u.id):
        # FIX_RICH_14: guard q.message in broadcast/audit-filter callbacks
        msg_src = q.message
        if msg_src is None:
            log_err("RICH: admin_audit_filter_action missing q.message", traceback.format_exc())
            try: await q.answer("Message expired, please retry", show_alert=True)
            except Exception: pass
            return
        ctx.user_data["awaiting_audit_filter"]={"kind":"action","expires_at":time.time()+120}; await msg_src.chat.send_message("Send action name to filter. /cancel to cancel."); return
    if data=="admin_limits_defaults" and admin_only(u.id): await admin_limits_defaults(update,ctx); return
    if data=="admin_default_mem" and admin_only(u.id): await begin_admin_setting_input(update,ctx,"default_memory_mb"); return
    if data=="admin_max_mem" and admin_only(u.id): await begin_admin_setting_input(update,ctx,"max_memory_per_bot_mb"); return
    if data=="admin_cap" and admin_only(u.id): await begin_admin_setting_input(update,ctx,"vps_cap_percent"); return
    if data=="admin_broadcast" and admin_only(u.id):
        # BUGFIX 4: Broadcast input must not coexist with env/limit/admin-setting input.
        _env_state_clear(ctx)
        limit_input_clear(ctx)
        ctx.user_data.pop("awaiting_admin_setting", None)
        ctx.user_data["awaiting_broadcast"]={"expires_at":time.time()+120}
        await safe_edit(q,"📢 Send the broadcast message now. /cancel to cancel.",InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel",callback_data="admin_panel")]])); return
    if data=="admin_broadcast_send" and admin_only(u.id):
        st=ctx.user_data.pop("broadcast_confirm",None)
        if not st: await q.answer("Broadcast expired",show_alert=True); return
        # FIX_RICH_14: guard q.message in broadcast/audit-filter callbacks
        msg_src = q.message
        if msg_src is None:
            log_err("RICH: admin_broadcast_send missing q.message", traceback.format_exc())
            try: await q.answer("Message expired, please retry", show_alert=True)
            except Exception: pass
            return
        ids=list(st["ids"]); sent=failed=0; progress=await msg_src.chat.send_message(f"📢 Broadcast started: 0/{len(ids)}")
        for i,uid in enumerate(ids,1):
            try:
                await ctx.bot.send_message(uid,st["text"])
                sent+=1
            except RetryAfter as e:
                # BUGFIX 11: Honor Telegram's RetryAfter delay, then retry once.
                retry_after = max(0.0, float(getattr(e, "retry_after", 1)))
                log_err(f"broadcast rate limited user {uid}: retry_after={retry_after}")
                await asyncio.sleep(retry_after)
                try:
                    await ctx.bot.send_message(uid,st["text"])
                    sent+=1
                except Exception as retry_exc:
                    failed+=1
                    log_err(f"broadcast retry failed user {uid}: {type(retry_exc).__name__}")
            except Exception as e:
                failed+=1
                log_err(f"broadcast failed user {uid}: {type(e).__name__}")
            if i%20==0:
                try: await progress.edit_text(f"📢 Broadcast progress: {i}/{len(ids)}\\nSent: {sent} | Failed: {failed}")
                except Exception: pass
            await asyncio.sleep(1.5/BROADCAST_RATE_PER_SECOND)
        try: await progress.edit_text(f"📢 Broadcast complete\nSent: {sent} | Failed: {failed}")
        except Exception: pass
        await admin_audit(u.id,"broadcast",None,f"sent={sent},failed={failed}"); return
    if action=="admin_premium" and admin_only(u.id) and len(parts)>=3:
        uid=int(bid); val=int(parts[2])
        if val: await DB.add_premium(uid,u.id); await admin_audit(u.id,"premium_add",str(uid))
        else: await DB.remove_premium(uid); await admin_audit(u.id,"premium_remove",str(uid))
        await admin_user_detail(update,ctx,uid); return
    if action=="admin_mute" and admin_only(u.id):
        uid=int(bid); await DB.mute_user(uid,2,"admin mute"); await admin_audit(u.id,"user_mute",str(uid),"2h"); await admin_user_detail(update,ctx,uid); return
    if action=="admin_unmute" and admin_only(u.id):
        uid=int(bid); await DB.unmute_user(uid); await admin_audit(u.id,"user_unmute",str(uid)); await admin_user_detail(update,ctx,uid); return
    if action=="admin_ban_confirm" and admin_only(u.id):
        await safe_edit(q,f"🚫 <b>Ban user {bid}?</b>",InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel",callback_data=f"admin_user:{bid}:0"),InlineKeyboardButton("🚫 Confirm Ban",callback_data=f"admin_ban:{bid}")]]),parse_mode="HTML"); return
    if action=="admin_ban" and admin_only(u.id):
        uid=int(bid)
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            await conn.execute("INSERT OR REPLACE INTO user_bans(user_id,banned_by,reason,created_at) VALUES(?,?,?,?)",(uid,u.id,"Banned by admin",utc_now_iso())); await conn.commit()
        await admin_audit(u.id,"user_ban",str(uid))
        try: await ctx.bot.send_message(uid,"🚫 You have been banned. Reason: Banned by admin")
        except Exception: pass
        await admin_user_detail(update,ctx,uid); return
    if action=="admin_unban" and admin_only(u.id):
        uid=int(bid)
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            await conn.execute("DELETE FROM user_bans WHERE user_id=?",(uid,)); await conn.commit()
        await admin_audit(u.id,"user_unban",str(uid)); await admin_user_detail(update,ctx,uid); return
    if action=="admin_delall_confirm" and admin_only(u.id):
        await safe_edit(q,f"⚠️ <b>Delete ALL bots of user {bid}?</b>",InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel",callback_data=f"admin_user:{bid}:0"),InlineKeyboardButton("🗑️ Confirm",callback_data=f"admin_delall:{bid}")]]),parse_mode="HTML"); return
    if action=="admin_delall" and admin_only(u.id):
        uid=int(bid)
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            conn.row_factory=aiosqlite.Row; rows=await (await conn.execute("SELECT * FROM bot_registry WHERE user_id=?",(uid,))).fetchall()
        for r in rows:
            b=HostedBot(r["bot_id"],r["name"],r["dir"],r["type"],r["user_id"])
            # FIX_RICH_22: pm2 subprocess is blocking; run off the event loop.
            await asyncio.to_thread(b.stop)
            await asyncio.to_thread(b.delete)
            if Path(r["dir"]).exists(): shutil.rmtree(r["dir"],ignore_errors=True)
            await DB.remove_bot(r["bot_id"])
        await admin_audit(u.id,"delete_all_bots",str(uid),f"count={len(rows)}"); await admin_user_detail(update,ctx,uid); return
    if action=="admin_force_stop" and admin_only(u.id):
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            conn.row_factory=aiosqlite.Row; r=await (await conn.execute("SELECT * FROM bot_registry WHERE bot_id=?",(bid,))).fetchone()
        if r:
            # FIX_RICH_22: pm2 subprocess is blocking; run off the event loop.
            await asyncio.to_thread(HostedBot(bid,r["name"],r["dir"],r["type"],r["user_id"]).stop)
            await admin_audit(u.id,"force_stop",bid)
        await admin_bot_detail(update,ctx,bid); return
    if action=="admin_force_del_confirm" and admin_only(u.id):
        await safe_edit(q,"⚠️ <b>Force delete this bot?</b>",InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel",callback_data=f"admin_bot:{bid}"),InlineKeyboardButton("🗑️ Confirm Delete",callback_data=f"admin_force_del:{bid}")]]),parse_mode="HTML"); return
    if action=="admin_force_del" and admin_only(u.id):
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            conn.row_factory=aiosqlite.Row; r=await (await conn.execute("SELECT * FROM bot_registry WHERE bot_id=?",(bid,))).fetchone()
        if r:
            # FIX_RICH_22: pm2 subprocess is blocking; run off the event loop.
            await asyncio.to_thread(HostedBot(bid,r["name"],r["dir"],r["type"],r["user_id"]).delete)
            if Path(r["dir"]).exists(): shutil.rmtree(r["dir"],ignore_errors=True)
            await DB.remove_bot(bid); await admin_audit(u.id,"force_delete_bot",bid)
        await admin_all_bots(update,ctx,0); return
    if data=="admin_maintenance" and admin_only(u.id): await admin_maintenance(update,ctx); return
    if data=="admin_toggle_maintenance" and admin_only(u.id):
        val="0" if await get_setting("maintenance_mode","0")=="1" else "1"; await set_setting("maintenance_mode",val); await admin_audit(u.id,"maintenance_toggle",None,val); await admin_maintenance(update,ctx); return
    if data=="admin_toggle_deploys" and admin_only(u.id):
        val="0" if await get_setting("deploys_enabled","1")=="1" else "1"; await set_setting("deploys_enabled",val); await admin_audit(u.id,"deploys_toggle",None,val); await admin_maintenance(update,ctx); return
    if data=="admin_toggle_lock" and admin_only(u.id):
        val=not await DB.is_unlocked(); await DB.set_unlocked(val); await admin_audit(u.id,"panel_lock_toggle",None,str(val)); await admin_maintenance(update,ctx); return
    if data=="admin_shutdown_confirm" and admin_only(u.id):
        await safe_edit(q,"🛑 <b>Stop ALL hosted bots?</b>",InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel",callback_data="admin_maintenance"),InlineKeyboardButton("🛑 Confirm",callback_data="admin_shutdown")]]),parse_mode="HTML"); return
    if data=="admin_shutdown" and admin_only(u.id):
        try:
            # FIX_RICH_22: pm2 subprocess is blocking; run off the event loop.
            await asyncio.to_thread(subprocess.run, ["pm2","stop","all"], capture_output=True, text=True, timeout=20)
            await admin_audit(u.id,"shutdown_all_bots"); await admin_maintenance(update,ctx)
        except Exception: log_err("admin shutdown",traceback.format_exc()); await q.answer("Shutdown failed",show_alert=True)
        return
    if action=="admin_restart_panel" and admin_only(u.id):
        await admin_audit(u.id,"restart_panel_bot"); await q.answer("Panel restart requested",show_alert=True)
        # FIX_RICH_20: use venv python so the restarted bot has all deps.
        cmd=f"sleep 1; screen -S {shlex.quote(PANEL_SCREEN_SESSION)} -X quit >/dev/null 2>&1 || true; screen -dmS {shlex.quote(PANEL_SCREEN_SESSION)} bash -lc 'cd /root/HostBotv3 && exec ./venv/bin/python hosting_panel_bot.py'"
        subprocess.Popen(["bash","-lc",cmd],start_new_session=True); return
    if action=="admin_restart_approval" and admin_only(u.id):
        await admin_audit(u.id,"restart_approval_bot"); await q.answer("Approval restart requested",show_alert=True)
        # FIX_RICH_20: use venv python so the restarted bot has all deps.
        cmd=f"sleep 1; screen -S {shlex.quote(APPROVAL_SCREEN_SESSION)} -X quit >/dev/null 2>&1 || true; screen -dmS {shlex.quote(APPROVAL_SCREEN_SESSION)} bash -lc 'cd /root/HostBotv3 && exec ./venv/bin/python approval_bot.py'"
        subprocess.Popen(["bash","-lc",cmd],start_new_session=True); return

# Feature 1 callbacks remain unchanged below.
    if action=="env_menu" and bid: await show_env_menu(update,ctx,bid,0)
    elif action=="env_list" and len(parts)==3 and bid:
        try: page=int(parts[2])
        except ValueError: page=0
        await show_env_menu(update,ctx,bid,page)
    elif action=="env_add" and bid: await env_begin_input(update,ctx,bid)
    elif action=="env_edit" and len(parts)==3 and bid:
        try: index=int(parts[2])
        except ValueError: index=-1
        info=await get_owned_bot(u.id,bid); rows=await DB.get_env_vars(bid) if info else []
        if 0<=index<len(rows): await env_begin_input(update,ctx,bid,rows[index]["env_key"])
        else: await q.answer("Invalid variable",show_alert=True)
    elif action=="env_del" and len(parts)==3 and bid:
        try: index=int(parts[2])
        except ValueError: index=-1
        await env_delete_confirm(update,ctx,bid,index)
    elif action=="env_confirm_del" and len(parts)==3 and bid:
        try: index=int(parts[2])
        except ValueError: index=-1
        await env_delete(update,ctx,bid,index)
    elif action=="env_restart" and bid:
        info=await get_owned_bot(u.id,bid)
        if not info: await q.answer("Not found or not yours",show_alert=True)
        else:
            # FIX_RICH_22: pm2 subprocess is blocking; run off the event loop.
            ok = await asyncio.to_thread(HostedBot(bid,info["name"],info["dir"],info["type"],info["user_id"]).restart)
            await q.answer("✅ Restarted" if ok else "❌ Restart failed",show_alert=not ok); await show_bot_detail(update,ctx,bid)
    elif action=="env_back" and bid: await show_bot_detail(update,ctx,bid)
    elif data=="help":
        await show_help(update,ctx); return
    elif data=="github_deploy":
        if u.id not in ADMIN_IDS and await get_setting("deploys_enabled","1")!="1":
            await q.answer("🛠️ New deploys are disabled",show_alert=True)
        else:
            await begin_github_input(update,ctx)
    elif data=="menu":
        await safe_edit(q,f"{UI.BOTS} <b>VPS Bot Manager</b>\n{UI.DIV}\nChoose an option below 👇{credit_footer()}", kb_main(), parse_mode="HTML")
    elif data=="my_bots": await show_my_bots(update,ctx)
    elif action=="bot_detail" and bid: await show_bot_detail(update,ctx,bid)
    elif action in ("start","stop","restart") and bid: await do_action(update,ctx,bid,action)
    elif action=="logs" and bid: await show_logs(update,ctx,bid)
    elif action=="bot_status" and bid: await show_bot_status(update,ctx,bid)
    elif action=="delete" and bid: await safe_edit(q,"⚠️ **Delete this bot?**",kb_confirm_delete(bid))
    elif action=="confirm_delete" and bid: await do_delete(update,ctx,bid)
    elif data=="deploy":
        if u.id not in ADMIN_IDS and await get_setting("deploys_enabled","1")!="1":
            await q.answer("🛠️ New deploys are disabled",show_alert=True)
        else:
            await safe_edit(q,f"{UI.DEPLOY} <b>Deploy New Bot</b>\n{UI.DIV}\nChoose a deployment method 👇{credit_footer()}",
                            kb_deploy_types(),parse_mode="HTML")
    elif action=="deploy_type" and bid:
        _env_state_clear(ctx); _github_state_clear(ctx); limit_input_clear(ctx)
        ctx.user_data["deploy_type"]=bid; ctx.user_data["awaiting_zip"]=True
        await safe_edit(q,f"{UI.DEPLOY} <b>Deploy {_html_text(bid.title())} Bot</b>\n{UI.DIV}\n"
                        "Send a ZIP or a single <code>.py/.js/.ts/.mjs/.cjs</code> file.\n"
                        "<code>/cancel</code> to cancel.",kb_back("deploy"),parse_mode="HTML")
    elif data in ("vps_status","refresh_vps"): await show_vps(update,ctx)
    elif data=="settings":
        txt=f"⚙️ **Settings**\n\nYour ID: `{u.id}`\nPremium: `{await DB.is_premium(u.id)}`\nBots: `{await DB.count_user_bots(u.id)}`"; await safe_edit(q,txt,kb_back("menu"))
    elif action in ("host_approve","host_reject") and bid and u.id in ADMIN_IDS:
        # FIX_RICH_16: surface execute_approval result in hosting cb_handler
        ok = await execute_approval(bid, u.id, approve=(action=="host_approve"))
        if ok:
            icon = (f"{UI.SUCCESS} Approved: " if action=="host_approve" else f"{UI.ERROR} Rejected: ")
        else:
            icon = "⚠️ Failed to process: "
        try:
            await q.edit_message_text(icon + f"<code>{_html_text(bid)}</code>{credit_footer()}", parse_mode="HTML")
        except Exception:
            log_err("RICH: host approval result edit failed", traceback.format_exc())

async def show_my_bots(update, ctx):
    # UI: SaaS-lite bot list with friendly empty state.
    q = update.callback_query; u = update.effective_user
    bots = await DB.get_user_bots(u.id)
    if u.id not in ADMIN_IDS:
        bots = {k:v for k,v in bots.items() if v["user_id"] == u.id}
    if not bots:
        txt=(f"{UI.BOTS} <b>No bots yet</b>\n{UI.DIV}\n"
             "Deploy your first bot in 30 seconds.\nPick an option below 👇"
             f"{credit_footer()}")
        kb=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{UI.DEPLOY} Deploy",callback_data="deploy"),
             InlineKeyboardButton(f"{UI.GITHUB} GitHub",callback_data="github_deploy")],
            [InlineKeyboardButton(f"{UI.BACK} Back",callback_data="menu")]])
        blocks=[
            rm.heading("🤖 My Bots"),
            rm.paragraph("You have no deployed bots yet."),
            rm.paragraph("Deploy your first bot in 30 seconds."),
            rm.button_row([
                _rich_callback_button(f"{UI.DEPLOY} Deploy", "deploy", "success"),
                _rich_callback_button(f"{UI.GITHUB} GitHub", "github_deploy", "success"),
            ]),
            rm.button_row([_rich_callback_button(f"{UI.BACK} Back", "menu", "primary")]),
        ]
        msg = q.message
        # FIX_RICH_2: Guard the callback source message before rich editing.
        if msg is None:
            log_err("RICH: show_my_bots empty-state missing q.message")
            return
        try:
            # RICH: My Bots empty-state card; original keyboard is the fallback.
            await _rich_edit_with_fallback(
                msg.chat_id, msg.message_id, blocks,
                is_media=(getattr(msg, "video", None) is not None or getattr(msg, "photo", None) is not None),
            )
            return
        except rm.RichMessageError:
            log_err("RICH: show_my_bots empty-state edit failed", traceback.format_exc())
        await safe_edit(q,txt,kb,parse_mode="HTML"); return

    rows=[]
    rich_button_rows=[]
    table_rows=[]
    for bid,info in bots.items():
        b=HostedBot(bid,info["name"],info["dir"],info["type"],info["user_id"])
        # FIX_RICH_17: pm2 jlist/logs is blocking; keep it off the event loop.
        running = await asyncio.to_thread(b.is_running)
        st=UI.RUNNING if running else UI.STOPPED
        typ={"nodejs":"Node.js","python":"Python","whatsapp":"WhatsApp"}.get(info["type"],str(info["type"]).title())
        rows.append([InlineKeyboardButton(f"{st} {typ} {_short_button_name(info['name'])}",callback_data=f"bot_detail:{bid}")])
        table_rows.append(["🟢" if st==UI.RUNNING else "🔴", info["name"], typ])
        rich_button_rows.append(rm.button_row([_rich_callback_button(f"{st} {typ} {_short_button_name(info['name'])}", f"bot_detail:{bid}", "primary")]))
    rows.append([InlineKeyboardButton(f"{UI.BACK} Back",callback_data="menu")])
    txt=f"{UI.BOTS} <b>My Bots</b>\n{UI.DIV}\nTotal: <code>{len(bots)}</code>{credit_footer()}"
    blocks=[
        rm.heading("🤖 My Bots"),
        rm.compact_table(["Status", "Name", "Type"], table_rows),
        *rich_button_rows,
        rm.button_row([_rich_callback_button(f"{UI.BACK} Back", "menu", "primary")]),
    ]
    msg = q.message
    # FIX_RICH_2: Callback queries can lack their original message.
    if msg is None:
        log_err("RICH: show_my_bots missing q.message")
        return
    try:
        # RICH: One rich card contains the table and one callback row per bot; original keyboard is fallback.
        await _rich_edit_with_fallback(
            msg.chat_id, msg.message_id, blocks,
            is_media=(getattr(msg, "video", None) is not None or getattr(msg, "photo", None) is not None),
        )
        return
    except rm.RichMessageError:
        log_err("RICH: show_my_bots rich edit failed", traceback.format_exc())
    await safe_edit(q,txt,InlineKeyboardMarkup(rows),parse_mode="HTML")

async def show_bot_detail(update, ctx, bid):
    # UI: Consistent bot card with status/resource metadata.
    q=update.callback_query; u=update.effective_user
    bots=await DB.get_user_bots(u.id)
    if bid not in bots:
        try: await q.answer("Not found",show_alert=True)
        except Exception: pass
        return
    info=bots[bid]
    if u.id not in ADMIN_IDS and info["user_id"]!=u.id:
        try: await q.answer("Not yours",show_alert=True)
        except Exception: pass
        return
    b=HostedBot(bid,info["name"],info["dir"],info["type"],info["user_id"])
    # FIX_RICH_17: pm2 jlist/logs is blocking; keep it off the event loop.
    running = await asyncio.to_thread(b.is_running)
    lim=await ensure_bot_limits(bid)
    try: env_count=len(await DB.get_env_vars(bid))
    except Exception: env_count=0
    typ_name={"nodejs":"Node.js","python":"Python","whatsapp":"WhatsApp"}.get(info["type"],str(info["type"]).title())
    typ_icon={"nodejs":UI.NODE,"python":UI.PYTHON,"whatsapp":UI.WHATSAPP}.get(info["type"],UI.BOTS)
    txt=(f"{typ_icon} <b>{_html_text(info['name'])}</b>\n{UI.DIV}\n"
         f"{UI.RUNNING if running else UI.STOPPED} Status: <code>{'Running' if running else 'Stopped'}</code>\n"
         f"{UI.ENV} Env Vars: <code>{env_count}</code>\n"
         f"{UI.LIMITS} Memory: <code>{lim['memory_limit_mb']} MB</code>\n"
         f"{UI.STATUS} CPU Limit: <code>{lim['cpu_limit_percent']}%</code>\n"
         f"{UI.DIV}\n👤 Owner: <code>{info['user_id']}</code>\n"
         f"🆔 <code>{_html_text(bid)}</code>{credit_footer()}")
    fallback_markup=kb_bot_actions(bid,running)
    action_buttons=(
        [_rich_callback_button(f"{UI.STOP_ACTION} Stop",f"stop:{bid}","danger"),_rich_callback_button(f"{UI.RESTART} Restart",f"restart:{bid}","primary")]
        if running else
        [_rich_callback_button(f"{UI.START} Start",f"start:{bid}","success")]
    )
    blocks=[
        rm.heading(f"🤖 {info['name']}"),
        rm.compact_table(["Field", "Value"], [
            ["Status", "🟢 Running" if running else "🔴 Stopped"],
            ["Type", typ_name],
            ["Memory", f"{lim['memory_limit_mb']} MB"],
            ["CPU", f"{lim['cpu_limit_percent']}%"],
            ["Env Vars", str(env_count)],
            ["Owner", str(info["user_id"])],
        ]),
        rm.paragraph(f"🆔 {bid}"),
        rm.button_row(action_buttons),
        rm.button_row([
            _rich_callback_button(f"{UI.LOGS} Logs",f"logs:{bid}","primary"),
            _rich_callback_button(f"{UI.STATUS} Status",f"bot_status:{bid}","primary"),
        ]),
        rm.button_row([
            _rich_callback_button(f"{UI.ENV} Env Vars",f"env_menu:{bid}","primary"),
            _rich_callback_button(f"{UI.LIMITS} Limits",f"limits_menu:{bid}","primary"),
        ]),
        rm.button_row([_rich_callback_button(f"{UI.DELETE} Delete",f"delete:{bid}","danger")]),
        rm.button_row([_rich_callback_button(f"{UI.BACK} Back", "my_bots", "primary")]),
    ]
    msg = q.message
    # FIX_RICH_2: Guard against orphaned/deleted callback messages.
    if msg is None:
        log_err(f"RICH: show_bot_detail missing q.message for {bid}")
        return
    try:
        # RICH: Bot detail preserves every Feature 1/2 callback_data value; fallback is the original keyboard.
        await _rich_edit_with_fallback(
            msg.chat_id, msg.message_id, blocks,
            is_media=(getattr(msg, "video", None) is not None or getattr(msg, "photo", None) is not None),
        )
        return
    except rm.RichMessageError:
        log_err(f"RICH: show_bot_detail rich edit failed {bid}", traceback.format_exc())
    await safe_edit(q,txt,fallback_markup,parse_mode="HTML")

async def do_action(update, ctx, bid, action):
    q = update.callback_query
    u = update.effective_user
    bots = await DB.get_user_bots(u.id)
    if bid not in bots: return
    info = bots[bid]
    if u.id not in ADMIN_IDS and info["user_id"] != u.id:
        try: await q.answer("Not yours", show_alert=True)
        except Exception: pass
        return
    b = HostedBot(bid, info["name"], info["dir"], info["type"], info["user_id"])
    # FIX_RICH_22: pm2 subprocess is blocking; run off the event loop.
    ok = False
    if action == "start": ok = await asyncio.to_thread(b.start)
    elif action == "stop": ok = await asyncio.to_thread(b.stop)
    elif action == "restart": ok = await asyncio.to_thread(b.restart)
    await asyncio.sleep(0.5)
    total, cap, _ = get_vps_memory_cap_mb()
    allocation_warning = ""
    if total:
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            allocated = int((await (await conn.execute(
                "SELECT COALESCE(SUM(memory_limit_mb),0) FROM bot_limits"
            )).fetchone())[0])
        if allocated > cap:
            allocation_warning = f"⚠️ Allocation {allocated} MB > cap {cap} MB\n"
    await show_bot_detail(update, ctx, bid)

    # BUGFIX 5: Telegram permits only one answer per callback query.
    answer_msg = f"{'✅' if ok else '❌'} {action} {'ok' if ok else 'failed'}"
    show_alert = not ok
    if allocation_warning:
        answer_msg = allocation_warning + answer_msg
        show_alert = True
    try:
        await q.answer(answer_msg, show_alert=show_alert)
    except Exception:
        pass

async def show_logs(update, ctx, bid):
    q=update.callback_query; u=update.effective_user; bots=await DB.get_user_bots(u.id)
    if bid not in bots: return
    info=bots[bid]
    if u.id not in ADMIN_IDS and info["user_id"]!=u.id: return
    b=HostedBot(bid,info["name"],info["dir"],info["type"],info["user_id"])
    # FIX_RICH_17: pm2 jlist/logs is blocking; keep it off the event loop.
    logs = await asyncio.to_thread(b.get_logs, 25)
    if not logs:
        txt=(f"{UI.LOGS} <b>No logs yet</b>\n{UI.DIV}\n"
             "Your bot hasn't printed anything.\n"
             "If it just started, wait 30 seconds and refresh."+credit_footer())
        kb=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{UI.REFRESH} Refresh",callback_data=f"logs:{bid}")],
            [InlineKeyboardButton(f"{UI.BACK} Back",callback_data=f"bot_detail:{bid}")]])
    else:
        txt=f"{UI.LOGS} <b>Logs: {_html_text(info['name'])}</b>\n{UI.DIV}\n<pre>{_html_text(logs[-3000:])}</pre>{credit_footer()}"
        kb=InlineKeyboardMarkup([[InlineKeyboardButton(f"{UI.BACK} Back",callback_data=f"bot_detail:{bid}")]])
    await safe_edit(q,txt,kb,parse_mode="HTML")

async def show_bot_status(update, ctx, bid):
    q = update.callback_query
    u = update.effective_user
    bots = await DB.get_user_bots(u.id)
    if bid not in bots: return
    info = bots[bid]
    if u.id not in ADMIN_IDS and info["user_id"] != u.id: return
    b = HostedBot(bid, info["name"], info["dir"], info["type"], info["user_id"])
    # FIX_RICH_17: pm2 jlist/logs is blocking; keep it off the event loop.
    _running = await asyncio.to_thread(b.is_running)
    txt = (
        f"📊 <b>Status: {_html_text(info['name'])}</b>\n"
        f"Running: <code>{_running}</code>\n"
        f"Service: <code>{_html_text(b.service_name)}</code>"
    )
    await safe_edit(q, txt, InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 Back", callback_data=f"bot_detail:{bid}")
    ]]), parse_mode="HTML")

async def do_delete(update, ctx, bid):
    q = update.callback_query
    u = update.effective_user
    bots = await DB.get_user_bots(u.id)
    if bid not in bots: return
    info = bots[bid]
    if u.id not in ADMIN_IDS and info["user_id"] != u.id: return
    b = HostedBot(bid, info["name"], info["dir"], info["type"], info["user_id"])
    # FIX_RICH_22: pm2 subprocess is blocking; run off the event loop.
    await asyncio.to_thread(b.stop)
    await asyncio.to_thread(b.delete)
    try:
        if os.path.exists(info["dir"]):
            shutil.rmtree(info["dir"], ignore_errors=True)
    except Exception: pass
    await DB.remove_bot(bid)
    if u.id in ADMIN_IDS: await admin_audit(u.id,"bot_delete",bid)
    await safe_edit(q, "✅ Bot deleted.", kb_back("my_bots"))

async def show_vps(update, ctx):
    # UI: VPS status card.
    q=update.callback_query; st=get_system_stats()
    if not st:
        await safe_edit(q,f"{UI.ERROR} <b>VPS status unavailable</b>\n{UI.DIV}\npsutil is not installed.{credit_footer()}",
                        kb_back("menu"),parse_mode="HTML"); return
    txt=(f"{UI.STATUS} <b>VPS Status</b>\n{UI.DIV}\n"
         f"💻 CPU: <code>{st['cpu']:.1f}%</code>\n"
         f"🧠 RAM: <code>{st['ram_used']:.2f}/{st['ram_total']:.2f} GB ({st['ram_pct']}%)</code>\n"
         f"💾 Disk: <code>{st['disk_used']:.2f}/{st['disk_total']:.2f} GB ({st['disk_pct']}%)</code>\n"
         f"⏱️ Uptime: <code>{st['uptime']}</code>\n{UI.DIV}{credit_footer()}")
    await safe_edit(q,txt,InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{UI.REFRESH} Refresh",callback_data="refresh_vps")],
        [InlineKeyboardButton(f"{UI.BACK} Back",callback_data="menu")]]),parse_mode="HTML")

# ================== FEATURE 3: GITHUB DEPLOY ==================
def github_url_valid(url: str) -> bool:
    # FEATURE3: Public HTTPS GitHub URL only; credentials/SSH URLs are rejected.
    value=(url or "").strip()
    return bool(GITHUB_URL_RE.fullmatch(value)) and "@" not in value

def _github_state_clear(ctx):
    ctx.user_data.pop("awaiting_github_url",None)
    task=ctx.user_data.pop("github_timeout_task",None)
    # FIX_RICH_5: Never self-cancel the currently executing GitHub timeout worker.
    if task and not task.done() and task is not asyncio.current_task(): task.cancel()

async def _github_timeout_worker(ctx,chat_id,prompt_id):
    try:
        await asyncio.sleep(GITHUB_TIMEOUT_SECONDS)
        state=ctx.user_data.get("awaiting_github_url")
        if state and state.get("msg_id")==prompt_id:
            _github_state_clear(ctx)
            try:
                await ctx.bot.edit_message_text(chat_id=chat_id,message_id=prompt_id,
                    text=f"{UI.ERROR} GitHub URL input timed out.")
            except Exception: pass
    except asyncio.CancelledError:
        return
    except Exception:
        log_err("github timeout worker",traceback.format_exc())

async def begin_github_input(update,ctx):
    q=update.callback_query
    # FIX_8: Acknowledge the GitHub button callback immediately before sending the prompt.
    try: await q.answer()
    except Exception: pass
    # FEATURE3: GitHub input cancels competing input state.
    _env_state_clear(ctx); limit_input_clear(ctx)
    ctx.user_data.pop("awaiting_admin_setting",None); ctx.user_data.pop("awaiting_broadcast",None)
    _github_state_clear(ctx)
    # FIX_RICH_13: guard q.message in begin_github_input
    msg_src = q.message
    if msg_src is None:
        log_err("RICH: begin_github_input missing q.message", traceback.format_exc())
        try: await q.answer("Message expired, please retry", show_alert=True)
        except Exception: log_err("RICH: begin_github_input answer failed", traceback.format_exc())
        return
    prompt=await msg_src.chat.send_message(
        f"{UI.GITHUB} <b>GitHub Deploy</b>\n{UI.DIV}\n"
        "Send a public GitHub repository URL.\n"
        "Example: <code>https://github.com/user/repo</code>\n"
        "Only public HTTPS repositories are supported.\n\n"
        "<code>/cancel</code> to cancel.",parse_mode="HTML",
        reply_markup=kb_back("deploy"))
    ctx.user_data["awaiting_github_url"]={"msg_id":prompt.message_id,"expires_at":time.time()+GITHUB_TIMEOUT_SECONDS}
    ctx.user_data["github_timeout_task"]=asyncio.create_task(
        _github_timeout_worker(ctx,update.effective_chat.id,prompt.message_id))

async def handle_github_url_input(update,ctx):
    state=ctx.user_data.get("awaiting_github_url")
    if not state: return False
    if time.time()>state.get("expires_at",0):
        _github_state_clear(ctx); await update.message.reply_text(f"{UI.ERROR} GitHub URL input timed out."); return True
    url=(update.message.text or "").strip()
    if not github_url_valid(url):
        await update.message.reply_text(
            f"{UI.ERROR} Invalid GitHub URL.\n"
            "Use <code>https://github.com/user/repo</code> with optional <code>.git</code>.\n"
            "SSH and credential URLs are blocked.",parse_mode="HTML"); return True
    _github_state_clear(ctx)
    uid=update.effective_user.id
    if uid not in ADMIN_IDS:
        cnt=await DB.count_user_bots(uid)+await DB.count_user_pending(uid)
        if cnt>=MAX_BOTS_PER_USER:
            await update.message.reply_text(f"{UI.ERROR} Max {MAX_BOTS_PER_USER} bots per user."); return True
    if shutil.which("git") is None:
        await update.message.reply_text(f"{UI.ERROR} Git is not installed on this server. Contact admin."); return True
    status=await update.message.reply_text(
        f"{UI.LOADING} <b>Deploying GitHub repo...</b>\n{UI.DIV}\n[1/4] {UI.GITHUB} Validating URL",
        parse_mode="HTML")
    staging=None
    async with DEPLOY_SEMAPHORE:
        try:
            repo_name=url.rstrip("/").rsplit("/",1)[-1]
            if repo_name.endswith(".git"): repo_name=repo_name[:-4]
            bot_id=f"{uid}_{uuid.uuid4().hex[:8]}"; bot_name=Path(repo_name).name[:30]
            staging=HOSTED_BOTS_DIR/f"_staging_{bot_id}"; staging.mkdir(parents=True,exist_ok=True); os.chmod(staging,0o700)
            await safe_edit(status,
                f"{UI.LOADING} <b>Deploying GitHub repo...</b>\n{UI.DIV}\n"
                f"<s>[1/4] {UI.GITHUB} Validating URL</s> {UI.SUCCESS}\n[2/4] {UI.DOWNLOAD} Cloning repository",parse_mode="HTML")
            r=await asyncio.to_thread(lambda: subprocess.run(
                ["git","clone","--depth","1",url,str(staging)],capture_output=True,text=True,timeout=120))
            combined=(r.stdout or "")+"\n"+(r.stderr or "")
            if r.returncode!=0:
                low=combined.lower()
                if "rate limit" in low or "429" in low: raise RuntimeError("GitHub rate limit reached. Try again later.")
                if "empty repository" in low: raise RuntimeError("Repository is empty.")
                if "not found" in low or "does not exist" in low: raise RuntimeError("Repository not found or unavailable.")
                raise RuntimeError("Couldn't clone that repository. Make sure it is public and exists.")
            git_dir=staging/".git"
            if git_dir.exists(): shutil.rmtree(git_dir,ignore_errors=True)
            total_bytes=await asyncio.to_thread(lambda: sum(p.stat().st_size for p in staging.rglob("*") if p.is_file()))
            if total_bytes>GITHUB_MAX_REPO_SIZE_MB*1024*1024:
                raise ValueError(f"Repository is larger than {GITHUB_MAX_REPO_SIZE_MB} MB.")
            code_files=[p for p in staging.rglob("*") if p.is_file() and p.name!="requirements.txt"]
            if not code_files: raise ValueError("Repository is empty.")
            # FEATURE3: Infer type before scanning so flagged repositories retain a valid pending type.
            bot_type = "python" if any(p.suffix.lower()==".py" for p in code_files) and not (staging/"package.json").exists() else "nodejs"
            await safe_edit(status,
                f"{UI.LOADING} <b>Deploying GitHub repo...</b>\n{UI.DIV}\n"
                f"<s>[1/4] {UI.GITHUB} Validating URL</s> {UI.SUCCESS}\n<s>[2/4] {UI.DOWNLOAD} Cloning repository</s> {UI.SUCCESS}\n[3/4] {UI.SCAN} Scanning",parse_mode="HTML")
            sr=await asyncio.to_thread(scan_directory,staging)
            await safe_edit(status,
                f"{UI.LOADING} <b>Deploying GitHub repo...</b>\n{UI.DIV}\n"
                f"<s>[1/4] {UI.GITHUB} Validating URL</s> {UI.SUCCESS}\n<s>[2/4] {UI.DOWNLOAD} Cloning repository</s> {UI.SUCCESS}\n"
                f"<s>[3/4] {UI.SCAN} Scanning</s> {UI.SUCCESS if sr['verdict']=='clear' else UI.WARN}\n[4/4] {UI.LAUNCH} Finalizing",parse_mode="HTML")
            if sr["verdict"]=="clear":
                final_dir=HOSTED_BOTS_DIR/bot_id; shutil.move(str(staging),str(final_dir)); staging=None
                bot_type="python" if any(p.suffix.lower()==".py" for p in final_dir.rglob("*") if p.is_file()) and not (final_dir/"package.json").exists() else "nodejs"
                await DB.add_bot(bot_id,uid,bot_name,str(final_dir),bot_type)
                # FIX_RICH_24: pm2 subprocess is blocking; run off the event loop.
                started = await asyncio.to_thread(HostedBot(bot_id,bot_name,str(final_dir),bot_type,uid).start)
                if started:
                    txt=(f"{UI.SUCCESS} <b>Bot deployed!</b>\n{UI.DIV}\n{UI.NAME} <code>{_html_text(bot_name)}</code>\n"
                         f"{UI.GITHUB} Public repository · <code>{'Python' if bot_type=='python' else 'Node.js'}</code>{credit_footer()}")
                    # FIX_RICH_4: safe_edit takes positional markup, not reply_markup kwarg
                    await safe_edit(status,txt,InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"{UI.OPEN} Open Bot",callback_data=f"bot_detail:{bot_id}"),
                         InlineKeyboardButton(f"{UI.HOME} Main Menu",callback_data="menu")]]),parse_mode="HTML")
                else:
                    await safe_edit(status,f"{UI.WARN} <b>Bot registered, but couldn't start.</b>\n{UI.DIV}\n"
                                           "Check logs and try Start again."+credit_footer(),kb_back("my_bots"),parse_mode="HTML")
            else:
                await DB.add_pending(bot_id,uid,bot_name,str(staging),bot_type,sr["flagged_files"]); staging=None
                admin_text=(f"{UI.REVIEW} <b>Pending GitHub Deploy — Review</b>\n{UI.USER} User: <code>{uid}</code>\n"
                            f"{UI.BOTS} Bot: <code>{_html_text(bot_name)}</code>\n{UI.ID} ID: <code>{_html_text(bot_id)}</code>\n"
                            f"{UI.STATUS} Files: <code>{sr['files_scanned']}</code> scanned, <code>{len(sr['flagged_files'])}</code> flagged\n"
                            f"<b>Findings:</b>\n{_build_findings_text(sr)}")
                await notify_admin_via_approval_bot(admin_text,{"inline_keyboard":[[
                    {"text":"✅ Approve","callback_data":f"approve:{bot_id}"},
                    {"text":"❌ Reject","callback_data":f"reject:{bot_id}"}
                ]]})
                await safe_edit(status,f"{UI.PENDING} <b>Under Review</b>\n{UI.DIV}\n"
                                       f"Scanner flagged <code>{len(sr['flagged_files'])}</code> file(s).\nAdmin will review shortly.{credit_footer()}",
                                       parse_mode="HTML")
        except subprocess.TimeoutExpired:
            log_err("github clone timeout",traceback.format_exc())
            await safe_edit(status,f"{UI.ERROR} <b>GitHub clone timed out</b>\n{UI.DIV}\nTry again later.{credit_footer()}",
                                   kb_back("deploy"),parse_mode="HTML")
        except (ValueError,RuntimeError) as e:
            log_err(f"github deploy {type(e).__name__}",traceback.format_exc())
            await safe_edit(status,f"{UI.ERROR} <b>GitHub deploy failed</b>\n{UI.DIV}\n{_html_text(str(e),300)}{credit_footer()}",
                                   kb_back("deploy"),parse_mode="HTML")
        except Exception:
            log_err("github deploy unexpected",traceback.format_exc())
            await safe_edit(status,f"{UI.ERROR} <b>GitHub deploy failed</b>\n{UI.DIV}\nCouldn't complete the deployment.{credit_footer()}",
                                   kb_back("deploy"),parse_mode="HTML")
        finally:
            if staging and staging.exists(): shutil.rmtree(staging,ignore_errors=True)
    return True

# ================== FEATURE 3: SINGLE FILE DEPLOY ==================
# FEATURE3: Deploy a supported single file or an existing ZIP.
async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u=update.effective_user
    if not u or not await can_use(u.id): return
    if not ctx.user_data.get("awaiting_zip"): return
    doc=update.message.document
    if not doc: return
    if await DB.is_muted(u.id):
        await update.message.reply_text("🚫 You're temporarily muted due to a flagged upload. Try again later."); return

    # FEATURE3: Basename sanitization prevents Telegram filenames from becoming paths.
    file_name=Path(doc.file_name or "").name
    suffix=Path(file_name).suffix.lower()
    if not file_name or file_name in (".",".."):
        await update.message.reply_text(f"{UI.ERROR} Invalid filename."); return
    if suffix != ".zip" and suffix not in GITHUB_ALLOWED_SINGLE_EXTS:
        await update.message.reply_text(f"{UI.ERROR} Unsupported file type. Send ZIP, .py, .js, .ts, .mjs or .cjs."); return
    if not doc.file_size or doc.file_size <= 0:
        await update.message.reply_text(f"{UI.ERROR} Empty files are not allowed."); return
    if doc.file_size > MAX_ZIP_SIZE_MB*1024*1024:
        await update.message.reply_text(f"{UI.ERROR} File too large (max {MAX_ZIP_SIZE_MB} MB)."); return

    bot_type=ctx.user_data.get("deploy_type","python")
    if suffix in GITHUB_ALLOWED_SINGLE_EXTS:
        bot_type="python" if suffix==".py" else ("whatsapp" if bot_type=="whatsapp" else "nodejs")
    ctx.user_data["awaiting_zip"]=False
    if u.id not in ADMIN_IDS:
        cnt=await DB.count_user_bots(u.id)+await DB.count_user_pending(u.id)
        if cnt>=MAX_BOTS_PER_USER:
            await update.message.reply_text(f"{UI.ERROR} Max {MAX_BOTS_PER_USER} bots per user (including pending review)."); return

    status=await update.message.reply_text(
        f"{UI.LOADING} <b>Deploying bot...</b>\n{UI.DIV}\n[1/4] {UI.DOWNLOAD} Downloading",parse_mode="HTML")
    async with DEPLOY_SEMAPHORE:
        staging=None
        try:
            f=await ctx.bot.get_file(doc.file_id)
            fb=await f.download_as_bytearray()
            if not fb: raise ValueError("empty upload")
            if len(fb)>MAX_ZIP_SIZE_MB*1024*1024: raise ValueError("file too large")

            bot_id=f"{u.id}_{uuid.uuid4().hex[:8]}"; bot_name=Path(file_name).stem[:30]
            staging=HOSTED_BOTS_DIR/f"_staging_{bot_id}"; staging.mkdir(parents=True,exist_ok=True); os.chmod(staging,0o700)
            # FIX_4: Every interpolated UI token in the deploy progress card uses an f-string.
            step2 = f"{UI.EXTRACT} Extracting" if suffix==".zip" else f"{UI.SAVE} Saving file"
            await safe_edit(status,f"{UI.LOADING} <b>Deploying bot...</b>\n{UI.DIV}\n"
                            f"<s>[1/4] {UI.DOWNLOAD} Downloading</s> ✅\n"
                            f"[2/4] {step2}",parse_mode="HTML")
            if suffix==".zip":
                zp=staging/"temp.zip"
                with open(zp,"wb") as fp: fp.write(fb)
                await asyncio.to_thread(_safe_zip_extract,zp,staging); zp.unlink(missing_ok=True)
            else:
                target=staging/file_name
                with open(target,"wb") as fp: fp.write(fb)
                os.chmod(target,0o600)
                req=staging/"requirements.txt"; req.write_text("",encoding="utf-8"); os.chmod(req,0o600)

            # FIX_5: Reuse the concrete step2 label; nested literal strings cannot interpolate UI constants.
            await safe_edit(status,f"{UI.LOADING} <b>Deploying bot...</b>\n{UI.DIV}\n"
                            f"<s>[1/4] {UI.DOWNLOAD} Downloading</s> ✅\n<s>[2/4] {step2}</s> ✅\n"
                            f"[3/4] {UI.SCAN} Scanning",parse_mode="HTML")
            sr=await asyncio.to_thread(scan_directory,staging)
            await safe_edit(status,f"{UI.LOADING} <b>Deploying bot...</b>\n{UI.DIV}\n"
                            f"<s>[1/4] {UI.DOWNLOAD} Downloading</s> ✅\n<s>[2/4] {step2}</s> ✅\n"
                            f"<s>[3/4] {UI.SCAN} Scanning</s> {'✅' if sr['verdict']=='clear' else '⚠️'}\n[4/4] {UI.LAUNCH} Finalizing",parse_mode="HTML")
            if sr["verdict"]=="clear":
                final_dir=HOSTED_BOTS_DIR/bot_id; shutil.move(str(staging),str(final_dir)); staging=None
                await DB.add_bot(bot_id,u.id,bot_name,str(final_dir),bot_type)
                b=HostedBot(bot_id,bot_name,str(final_dir),bot_type,u.id)
                # FIX_RICH_24: pm2 subprocess is blocking; run off the event loop.
                started = await asyncio.to_thread(b.start)
                if started:
                    lim=await ensure_bot_limits(bot_id)
                    txt=(f"{UI.SUCCESS} <b>Bot deployed!</b>\n{UI.DIV}\n📛 <code>{_html_text(bot_name)}</code>\n"
                         f"{UI.PYTHON if bot_type=='python' else UI.NODE} {_html_text(bot_type.title())} · <code>{lim['memory_limit_mb']} MB</code>\n"
                         f"Files scanned: <code>{sr['files_scanned']}</code>{credit_footer()}")
                    await safe_edit(status,txt,InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"{UI.OPEN} Open Bot",callback_data=f"bot_detail:{bot_id}"),
                         InlineKeyboardButton(f"{UI.HOME} Main Menu",callback_data="menu")]]),parse_mode="HTML")
                else:
                    await safe_edit(status,f"{UI.WARN} <b>Bot registered, but couldn't start.</b>\n{UI.DIV}\n"
                                    "Check logs and try Start again."+credit_footer(),kb_back("my_bots"),parse_mode="HTML")
            else:
                await DB.add_pending(bot_id,u.id,bot_name,str(staging),bot_type,sr["flagged_files"]); staging=None
                admin_text=(f"{UI.REVIEW} <b>Pending Deploy — Review</b>\n{UI.USER} User: <code>{u.id}</code>\n"
                            f"{UI.BOTS} Bot: <code>{_html_text(bot_name)}</code>\n{UI.ID} ID: <code>{_html_text(bot_id)}</code>\n"
                            f"{UI.STATUS} Files: <code>{sr['files_scanned']}</code> scanned, <code>{len(sr['flagged_files'])}</code> flagged\n"
                            f"<b>Findings:</b>\n{_build_findings_text(sr)}")
                await notify_admin_via_approval_bot(admin_text,{"inline_keyboard":[[
                    {"text":"✅ Approve","callback_data":f"approve:{bot_id}"},
                    {"text":"❌ Reject","callback_data":f"reject:{bot_id}"}]]})
                await safe_edit(status,f"{UI.PENDING} <b>Under Review</b>\n{UI.DIV}\n"
                                f"Scanner flagged <code>{len(sr['flagged_files'])}</code> file(s).\nAdmin will review shortly.{credit_footer()}",
                                parse_mode="HTML")
        except zipfile.BadZipFile:
            log_err("deploy invalid zip",traceback.format_exc())
            await safe_edit(status,f"{UI.ERROR} <b>Deploy failed</b>\n{UI.DIV}\nThat file isn't a valid ZIP. Try re-zipping it.{credit_footer()}",
                            kb_back("deploy"),parse_mode="HTML")
        except Exception:
            log_err("deploy unexpected",traceback.format_exc())
            await safe_edit(status,f"{UI.ERROR} <b>Deploy failed</b>\n{UI.DIV}\nCouldn't complete the deployment. Check logs or try again.{credit_footer()}",
                            kb_back("deploy"),parse_mode="HTML")
        finally:
            if staging and staging.exists(): shutil.rmtree(staging,ignore_errors=True)

# FIX_1: Removed stray module-level rmtree(staging); staging only exists inside deployment handlers.

# ================== APPROVAL (backup path) ==================
async def _claim_pending(bot_id: str):
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM pending_deploys WHERE bot_id=?", (bot_id,))
        row = await cur.fetchone()
        if not row:
            return None
        upd = await conn.execute(
            "UPDATE pending_deploys SET status='processing' WHERE bot_id=? AND status='pending'",
            (bot_id,)
        )
        await conn.commit()
        if upd.rowcount != 1:
            return None
        return dict(row)

async def execute_approval(bot_id, admin_id, approve: bool):
    p = await _claim_pending(bot_id)
    if not p:
        return False
    staging = Path(p["staging_dir"])

    try:
        if not approve:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            await DB.set_pending_status(bot_id, "rejected", admin_id,
                                        reason="admin rejected after review")
            await admin_audit(admin_id,"bot_reject",bot_id,"flagged upload")
            await DB.mute_user(p["user_id"], MUTE_DURATION_HOURS, "flagged upload")
            return True

        if not staging.exists():
            existing = await DB.get_user_bots(p["user_id"])
            if bot_id in existing:
                await DB.set_pending_status(bot_id, "approved", admin_id,
                                            reason="admin approved (recovered)")
                return True
            await DB.set_pending_status(bot_id, "rejected", admin_id, "staging missing")
            return False

        final_dir = HOSTED_BOTS_DIR / bot_id
        shutil.move(str(staging), str(final_dir))
        await DB.add_bot(bot_id, p["user_id"], p["name"], str(final_dir), p["bot_type"])
        b = HostedBot(bot_id, p["name"], str(final_dir), p["bot_type"], p["user_id"])
        # FIX_RICH_22: pm2 subprocess is blocking; run off the event loop.
        started = await asyncio.to_thread(b.start)
        reason = "admin approved" if started else "admin approved (pm2 start failed — check logs)"
        await DB.set_pending_status(bot_id, "approved", admin_id, reason=reason)
        await admin_audit(admin_id,"bot_approve",bot_id,reason)
        return True
    except Exception as e:
        log_err(f"execute_approval: {e}", traceback.format_exc())
        await DB.set_pending_status(bot_id, "error", admin_id, reason=f"exception: {e}"[:200])
        return False

# ================== NOTIFICATION WORKER ==================
async def notification_worker(app):
    while True:
        try:
            async with aiosqlite.connect(DATABASE_PATH) as conn:
                conn.row_factory = aiosqlite.Row
                cur = await conn.execute("""
                    SELECT * FROM pending_deploys
                    WHERE status IN ('approved','rejected')
                      AND reviewed_at IS NOT NULL
                      AND bot_id NOT IN (SELECT bot_id FROM notified_users)
                """)
                rows = await cur.fetchall()

                cleanup_cutoff = (
                    datetime.now() - timedelta(days=NOTIFIED_USERS_RETENTION_DAYS)
                ).isoformat()
                await conn.execute(
                    "DELETE FROM notified_users "
                    "WHERE notified_at IS NOT NULL AND notified_at < ?",
                    (cleanup_cutoff,)
                )

                for r in rows:
                    if r["status"] == "approved":
                        msg = (
                            f"✅ <b>Bot approved, deploy started!</b>\n"
                            f"Name: <code>{_html_text(r['name'])}</code>\n"
                            f"Type: <code>{_html_text(r['bot_type'])}</code>"
                        )
                        if r["reason"] and "failed" in r["reason"].lower():
                            msg += f"\n⚠️ <code>{_html_text(r['reason'], 200)}</code>"
                    else:
                        reason = r["reason"] or "N/A"
                        msg = (
                            f"❌ <b>Bot rejected by admin.</b>\n"
                            f"Name: <code>{_html_text(r['name'])}</code>\n"
                            f"Reason: <code>{_html_text(reason, 200)}</code>\n"
                            f"You've been muted for {MUTE_DURATION_HOURS}h."
                        )
                    try:
                        await app.bot.send_message(r["user_id"], msg, parse_mode="HTML")
                        log_notify(f"{r['status']} -> user {r['user_id']} (bot {r['bot_id']})")
                        await conn.execute(
                            "INSERT OR REPLACE INTO notified_users VALUES (?,?)",
                            (r["bot_id"], datetime.now().isoformat())
                        )
                    except BadRequest as e:
                        # A permanent Telegram formatting/chat error must not poison
                        # the queue forever. The payload is already HTML-escaped.
                        log_err(f"notify permanent telegram error {r['user_id']}: {type(e).__name__}")
                        await conn.execute(
                            "INSERT OR REPLACE INTO notified_users VALUES (?,?)",
                            (r["bot_id"], datetime.now().isoformat())
                        )
                    except Exception as e:
                        # Transient/network errors remain retryable.
                        log_err(f"notify {r['user_id']}: {type(e).__name__}")
                await conn.commit()
        except Exception as e:
            log_err(f"notification_worker: {e}")
        await asyncio.sleep(NOTIFY_POLL_SECONDS)

async def handle_limit_or_broadcast_or_env(update,ctx):
    if await handle_limit_commands(update,ctx): return
    if await handle_admin_setting_value(update,ctx): return
    # FIX_3: Route GitHub URL input before broadcast/env handlers consume the message.
    if await handle_github_url_input(update,ctx): return
    if ctx.user_data.get("awaiting_audit_filter"):
        st=ctx.user_data.pop("awaiting_audit_filter")
        if time.time()>st.get("expires_at",0): await update.message.reply_text("⏱️ Timed out"); return
        val=(update.message.text or "").strip()
        if st["kind"]=="admin":
            try: val=str(int(val))
            except ValueError: await update.message.reply_text("Invalid admin ID"); return
        await update.message.reply_text("📋 Filter applied")
        # Render a fresh audit view from the current chat message is not possible without a callback; store and show inline query-style result.
        where="WHERE admin_id=?" if st["kind"]=="admin" else "WHERE action=?"
        args=(val,)
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            conn.row_factory=aiosqlite.Row; rows=await (await conn.execute(f"SELECT * FROM admin_audit {where} ORDER BY id DESC LIMIT 20",args)).fetchall()
        lines=["📋 <b>Audit Filter</b>","━━━━━━━━━━━━━━━━━━"]+[f"{_html_text(r['created_at'],19)} | admin:<code>{r['admin_id']}</code> | {_html_text(r['action'])} | {_html_text(r['target'] or '-') }" for r in rows]
        await update.message.reply_text("\n".join(lines),parse_mode="HTML",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin",callback_data="admin_panel")]])); return
    if ctx.user_data.get("awaiting_broadcast"):
        st=ctx.user_data.get("awaiting_broadcast")
        if time.time()>st.get("expires_at",0): ctx.user_data.pop("awaiting_broadcast",None); await update.message.reply_text("⏱️ Timed out"); return
        if not admin_only(update.effective_user.id): ctx.user_data.pop("awaiting_broadcast",None); return
        text=update.message.text or ""
        if len(text) > 4000: await update.message.reply_text("❌ Broadcast is too long (max 4000 characters)."); return True
        ctx.user_data["pending_broadcast_text"]=text; ctx.user_data.pop("awaiting_broadcast",None)
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            ids=[int(r[0]) for r in await (await conn.execute("SELECT DISTINCT user_id FROM bot_registry ORDER BY user_id")).fetchall()]
        ctx.user_data["broadcast_confirm"]={"text":ctx.user_data.pop("pending_broadcast_text"),"ids":ids}
        await update.message.reply_text(f"📢 Send to {len(ids)} users?",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Send",callback_data="admin_broadcast_send"),InlineKeyboardButton("❌ Cancel",callback_data="admin_panel")]])); return
    await handle_env_value(update,ctx)

async def resource_monitor_worker(app):
    """Observe hosted PM2 processes for CPU/memory exceed events and notify users."""
    if not HAS_PSUTIL: return
    seen={}
    while True:
        try:
            # FIX_RICH_22: pm2 subprocess is blocking; run off the event loop.
            r = await asyncio.to_thread(subprocess.run, ["pm2","jlist"], capture_output=True, text=True, timeout=8)
            plist=json.loads(r.stdout) if r.returncode==0 and r.stdout else []
            for p in plist:
                env=p.get("pm2_env",{}); name=p.get("name","")
                if not name.startswith("hosted-bot-"): continue
                bid=name[len("hosted-bot-"):]; pid=env.get("pid")
                if not pid or not psutil.pid_exists(pid): continue
                try:
                    proc=psutil.Process(pid); mem=int(proc.memory_info().rss/(1024**2)); cpu=float(proc.cpu_percent(interval=0.05))
                    # FIX_RICH_25: sync SQLite fetch; keep potential lock waits off the event loop.
                    lim = await asyncio.to_thread(get_bot_limits_sync, bid)
                    exceeded_mem=mem>lim["memory_limit_mb"]; exceeded_cpu=lim["cpu_limit_percent"]<100 and cpu>lim["cpu_limit_percent"]
                    if exceeded_mem or exceeded_cpu:
                        key=(bid,exceeded_mem,exceeded_cpu,lim["memory_limit_mb"],lim["cpu_limit_percent"]); now=time.time()
                        if now-seen.get(key,0)>60:
                            seen[key]=now; await record_limit_exceeded(bid,"memory" if exceeded_mem else "cpu")
                            async with aiosqlite.connect(DATABASE_PATH) as conn:
                                conn.row_factory=aiosqlite.Row; row=await (await conn.execute("SELECT name,user_id,dir,type FROM bot_registry WHERE bot_id=?",(bid,))).fetchone()
                            if row:
                                msg=f"⚠️ Bot {_html_text(row['name'])} ne {'memory' if exceeded_mem else 'CPU'} limit exceed kiya."
                                if lim["restart_on_exceed"]:
                                    # FIX_RICH_22: pm2 subprocess is blocking; run off the event loop.
                                    ok = await asyncio.to_thread(HostedBot(bid,row['name'],row['dir'],row['type'],row['user_id']).restart)
                                    msg += " Restart kiya gaya." if ok else " Restart fail hua; logs check karein."
                                try: await app.bot.send_message(row['user_id'],msg,parse_mode="HTML")
                                except Exception: log_err(f"limit notification failed {bid}")
                except (psutil.NoSuchProcess,psutil.AccessDenied): continue
                except Exception: log_err(f"resource monitor bot {bid}",traceback.format_exc())
        except Exception: log_err("resource monitor",traceback.format_exc())
        await asyncio.sleep(RESOURCE_MONITOR_SECONDS)

# ================== STALE CLEANUP ==================
async def cleanup_stale_pending():
    cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT bot_id,staging_dir FROM pending_deploys WHERE status='pending' AND created_at<?",
            (cutoff,))
        rows = await cur.fetchall()
        for r in rows:
            d = Path(r["staging_dir"])
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
            await conn.execute(
                "UPDATE pending_deploys SET status='expired' WHERE bot_id=?", (r["bot_id"],))
        await conn.commit()

# ================== MAIN ==================
def main():
    if not BOT_TOKEN:
        print("No BOT_TOKEN"); sys.exit(1)

    async def _post_init(app):
        # Runs inside PTB's own event loop — safe to await async setup here.
        init_env_crypto()
        await init_db()
        global FEATURE2_MIGRATION_OK
        migrated = await migrate_env_schema()
        FEATURE2_MIGRATION_OK = migrated
        if not migrated:
            log_err("Feature migrations unavailable")
        await set_setting("panel_started_at",utc_now_iso())
        await cleanup_stale_pending()
        asyncio.create_task(notification_worker(app))
        asyncio.create_task(resource_monitor_worker(app))
        print(f"👂 Notification worker started (poll: {NOTIFY_POLL_SECONDS}s)")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("unlock", cmd_unlock))
    app.add_handler(CommandHandler("addpremium", cmd_addpremium))
    app.add_handler(CommandHandler("removepremium", cmd_removepremium))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("unmute", cmd_unmute))
    app.add_handler(MessageHandler(filters.COMMAND, _env_pre_command), group=-1)
    app.add_handler(CallbackQueryHandler(_env_pre_callback), group=-1)
    app.add_handler(CommandHandler("cancel", cmd_cancel), group=0)
    app.add_handler(CallbackQueryHandler(cb_handler), group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_limit_or_broadcast_or_env, ), group=0)
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document), group=0)
    app.add_error_handler(admin_callback_error_handler)

    print("🤖 Hosting panel bot started.")
    app.run_polling()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
