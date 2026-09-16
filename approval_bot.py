#!/usr/bin/env python3
"""
🛡️ Approval Bot — Admin interface for pending deploys.
Only touches shared DB. User DMs handled by hosting bot's notification worker.
"""
from __future__ import annotations
import os, sys, shutil, subprocess, sqlite3, errno, traceback, re, html, time, asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict

try:
    import psutil
    HAS_PSUTIL=True
except ImportError:
    psutil=None; HAS_PSUTIL=False

import aiosqlite
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter
from core import rich_message as rm

from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
)

# ================== CONFIG ==================
APPROVAL_BOT_TOKEN = os.getenv("APPROVAL_BOT_TOKEN", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID           = 5628671567
ADMIN_IDS          = [5628671567]

# MIGRATED: this bot authenticates as ITS OWN bot (APPROVAL_BOT_TOKEN, not
# BOT_TOKEN) — guarded the same way as hosting_panel_bot.py so importing
# without a real token set doesn't crash at import time.
rich_client = rm.RichClient(token=APPROVAL_BOT_TOKEN) if APPROVAL_BOT_TOKEN else None
# FEATURE3: Shared credit displayed on approval bot start/admin screens.
CREDIT_OWNER_USERNAME = "OG_SAGAR_ddos"  # no @

# UI: Approval bot uses the same SaaS-lite visual language.
class UI:
    DIV = "━━━━━━━━━━━━━━━━━━"
    SUCCESS = "✅"; ERROR = "❌"; INFO = "ℹ️"; LOADING = "⏳"
    APPROVAL = "🛡️"; BROADCAST = "📢"; AUDIT = "📋"; BACK = "🔙"
    GITHUB = "🐙"; ADMIN = "🔧"

def credit_footer() -> str:
    # FEATURE3: Centralized credit footer.
    username = re.sub(r"[^A-Za-z0-9_]", "", CREDIT_OWNER_USERNAME or "")
    if not username: return ""
    # FIX_9: Use Telegram-friendly double-quoted HTML href in the credit footer.
    # FIX_RICH_26: plain text only — no <a href> and no t.me/ URL, so Telegram does not render a clickable link or a link-preview card.
    return f"\n\n👑 Bot by @{username}"

HOSTED_BOTS_DIR = Path("/root/hosted_bots")
DATABASE_PATH   = HOSTED_BOTS_DIR / "inf" / "bot_data.db"
LOGS_DIR        = Path("/root/hosted_bots_logs")
MUTE_HOURS      = 2
LIMIT_DEFAULT_MEMORY_MB = 256
LIMIT_MAX_MEMORY_MB = 2048
LIMIT_SMALL_VPS_MAX_MB = 512
BROADCAST_RATE_PER_SECOND = 30
AUDIT_PAGE_SIZE = 20
FEATURE2_READY = False

LOGS_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

# ================== ENV MANAGER ==================
ENV_KEY_FILE = Path("/root/HostBotv3/.env_encryption_key")
ENV_KEY_VERSION = 1
ENV_MAX_KEYS = 50
ENV_MAX_VALUE_BYTES = 4096
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
ENV_SECRET_KEY_RE = re.compile(r"^.*(TOKEN|SECRET|KEY|PASSWORD|PASS|PWD|CREDENTIAL).*$", re.IGNORECASE)
ENV_MIXED_ALNUM_RE = re.compile(r"^(?=.*[A-Za-z])(?=.*[0-9])[A-Za-z0-9]+$")
_env_fernet = None
_env_schema_ready = False
try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:
    Fernet = None
    InvalidToken = Exception

def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()

def init_env_crypto():
    global _env_fernet
    _env_fernet = None
    if Fernet is None:
        log("Environment encryption unavailable: cryptography missing")
        return False
    try:
        override = os.getenv("ENV_ENCRYPTION_KEY")
        if override:
            key = override.strip().encode("ascii")
        else:
            if not ENV_KEY_FILE.exists():
                log("Environment encryption unavailable: shared key file missing")
                return False
            key = ENV_KEY_FILE.read_bytes().strip()
            os.chmod(ENV_KEY_FILE, 0o600)
        _env_fernet = Fernet(key)
        return True
    except Exception:
        log("Environment encryption unavailable", traceback.format_exc())
        return False

def env_available():
    """Return current encryption/schema availability.

    Approval bot never migrates the schema, but it may re-check the hosting
    bot's migration lock lazily if startup happened before migration completed.
    """
    global _env_schema_ready
    if _env_fernet is None:
        return False
    if not _env_schema_ready:
        try:
            _env_schema_ready = check_env_schema()
        except Exception:
            _env_schema_ready = False
    return bool(_env_schema_ready)

def encrypt_env_value(value):
    if not env_available(): raise RuntimeError("Encryption unavailable")
    return f"v{ENV_KEY_VERSION}:" + _env_fernet.encrypt(value.encode("utf-8")).decode("ascii")

def decrypt_env_value(value):
    if not env_available(): raise RuntimeError("Encryption unavailable")
    if value.startswith("v") and ":" in value:
        version_text, value = value.split(":", 1)
        if version_text != f"v{ENV_KEY_VERSION}": raise InvalidToken
    return _env_fernet.decrypt(value.encode("ascii")).decode("utf-8")

def validate_env_key(key):
    if not isinstance(key, str) or not ENV_KEY_RE.fullmatch(key): raise ValueError("Invalid environment key")

def validate_env_value(value):
    if not isinstance(value, str) or value == "": raise ValueError("Empty value not allowed")
    if "\x00" in value: raise ValueError("Null bytes not allowed")
    if "\n" in value or "\r" in value: raise ValueError("Multi-line values not allowed")
    if len(value.encode("utf-8")) > ENV_MAX_VALUE_BYTES: raise ValueError("Value too large (max 4096 bytes)")

def is_secret_env(key, value):
    return bool(ENV_SECRET_KEY_RE.fullmatch(key) or (len(value) >= 20 and ENV_MIXED_ALNUM_RE.fullmatch(value)))

def quote_env_value(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

def parse_env_line(line):
    raw = line.strip()
    if not raw or raw.startswith("#"): return None
    if raw.startswith("export "): raw = raw[7:].lstrip()
    if "=" not in raw: raise ValueError("Malformed env line")
    key, value = raw.split("=", 1); key = key.strip(); validate_env_key(key)
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        quote = value[0]; value = value[1:-1]
        if quote == '"': value = value.replace('\\"', '"').replace('\\\\', '\\')
    validate_env_value(value)
    return key, value

def prepare_env_for_bot(bot_id, bot_dir):
    """Import only bot_dir/.env once, then atomically regenerate .env from encrypted DB."""
    if not env_available(): return False, "Encryption unavailable"
    bdir = Path(bot_dir).resolve(); conn = None
    try:
        conn = sqlite3.connect(DATABASE_PATH, timeout=10); conn.execute("PRAGMA busy_timeout=10000")
        count = int(conn.execute("SELECT COUNT(*) FROM bot_env_vars WHERE bot_id=?", (bot_id,)).fetchone()[0])
        env_path = bdir / ".env"
        if count == 0 and env_path.is_file() and not env_path.is_symlink():
            imported = []
            for line_no, line in enumerate(env_path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                try:
                    item = parse_env_line(line)
                    if item: imported.append(item)
                except Exception as e:
                    log(f"env import malformed line {line_no} for {bot_id}: {type(e).__name__}")
            imported = imported[:ENV_MAX_KEYS]
            conn.execute("BEGIN IMMEDIATE"); now = utc_now_iso()
            for key, value in imported:
                conn.execute("INSERT OR IGNORE INTO bot_env_vars(bot_id,env_key,encrypted_value,enc_version,is_secret,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (bot_id,key,encrypt_env_value(value),ENV_KEY_VERSION,int(is_secret_env(key,value)),now,now))
            conn.commit()
            try:
                os.replace(env_path, bdir / ".env.imported"); os.chmod(bdir / ".env.imported", 0o600)
            except Exception:
                log(f"env import rename failed for {bot_id}", traceback.format_exc())
        rows = conn.execute("SELECT env_key,encrypted_value,is_secret FROM bot_env_vars WHERE bot_id=? ORDER BY env_key COLLATE NOCASE", (bot_id,)).fetchall()
        items = [(k, decrypt_env_value(enc), bool(sec)) for k, enc, sec in rows]
        conn.close(); conn = None
        tmp = bdir / ".env.tmp"; out = bdir / ".env"
        content = "".join(f"{k}={quote_env_value(v)}\n" for k,v,_ in items)
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fp:
                fd = None; fp.write(content); fp.flush(); os.fsync(fp.fileno())
        finally:
            if fd is not None: os.close(fd)
        os.chmod(tmp, 0o600); os.replace(tmp, out); os.chmod(out, 0o600)
        return True, None
    except Exception as e:
        if conn is not None:
            try: conn.rollback(); conn.close()
            except Exception: pass
        try:
            tmp = bdir / ".env.tmp"
            if tmp.exists(): tmp.unlink()
        except Exception: pass
        log(f"prepare_env_for_bot {bot_id}: {type(e).__name__}", traceback.format_exc())
        return False, str(e)

# Never inherit the complete approval-panel environment into a user bot.
# The panel environment contains approval/hosting Telegram tokens and the
# environment-encryption key, which must never cross the trust boundary.
BOT_SAFE_INHERITED_ENV = {
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
    "LC_MESSAGES", "TZ", "TMPDIR", "PM2_HOME", "XDG_RUNTIME_DIR",
}

def get_safe_bot_base_env(bot_dir=None):
    """Return only a small, non-secret inherited environment for the bot."""
    env = {k: v for k, v in os.environ.items() if k in BOT_SAFE_INHERITED_ENV}
    if bot_dir:
        env["PWD"] = str(Path(bot_dir).resolve())
    return env

def get_env_process_env(bot_id, bot_dir=None):
    """Return safe base environment plus this bot's decrypted env variables."""
    env = get_safe_bot_base_env(bot_dir)
    if not env_available():
        return env
    with sqlite3.connect(DATABASE_PATH, timeout=10) as conn:
        conn.execute("PRAGMA busy_timeout=10000")
        for key, encrypted in conn.execute("SELECT env_key,encrypted_value FROM bot_env_vars WHERE bot_id=?", (bot_id,)).fetchall():
            env[key] = decrypt_env_value(encrypted)
    return env

def check_env_schema():
    global _env_schema_ready
    try:
        with sqlite3.connect(DATABASE_PATH, timeout=10) as conn:
            conn.execute("PRAGMA busy_timeout=10000")
            _env_schema_ready = int(conn.execute("PRAGMA user_version").fetchone()[0]) >= 1
            return _env_schema_ready
    except Exception:
        _env_schema_ready = False; log("Environment schema check failed", traceback.format_exc()); return False

async def ensure_schema():
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
            CREATE TABLE IF NOT EXISTS muted_users (
                user_id INTEGER PRIMARY KEY,
                mute_until TEXT NOT NULL,
                reason TEXT
            );
        """)
        await conn.commit()

def log(msg, extra=None):
    try:
        with open(LOGS_DIR / "approval_bot.log", "a") as f:
            f.write(f"[{datetime.now()}] {msg}\n")
            if extra: f.write(f"EXTRA: {extra}\n")
    except Exception:
        pass


def log_err(msg, extra=None):
    # FIX_RICH_1: Keep rich-message diagnostics on the approval bot's existing log sink.
    log(msg, extra)

# ================== DB ==================
async def get_pending(bot_id: str) -> Optional[Dict]:
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM pending_deploys WHERE bot_id=?", (bot_id,))
        r = await cur.fetchone()
        return dict(r) if r else None

async def set_pending(bot_id: str, status: str, admin_id: int, reason: str = None):
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        await conn.execute(
            "UPDATE pending_deploys SET status=?,reviewed_by=?,reviewed_at=?,reason=? WHERE bot_id=?",
            (status, admin_id, datetime.now().isoformat(), reason, bot_id)
        )
        await conn.commit()

async def add_bot(bot_id, uid, name, bdir, btype):
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO bot_registry VALUES (?,?,?,?,?,?,?)",
            (bot_id, uid, name, bdir, btype, datetime.now().isoformat(), None)
        )
        await conn.commit()

async def mute_user(uid, hours, reason):
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO muted_users VALUES (?,?,?)",
            (uid, (datetime.now() + timedelta(hours=hours)).isoformat(), reason)
        )
        await conn.commit()

# ================== FEATURE 2 READ-ONLY / ADMIN HELPERS ==================
def admin_only(uid): return uid in ADMIN_IDS or uid==OWNER_ID

def get_bot_limits_sync(bot_id):
    now=utc_now_iso()
    keys=["bot_id","memory_limit_mb","cpu_limit_percent","restart_on_exceed","last_exceeded_at","exceed_count","created_at","updated_at"]
    try:
        with sqlite3.connect(DATABASE_PATH,timeout=10) as conn:
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("BEGIN IMMEDIATE")
            row=conn.execute("SELECT bot_id,memory_limit_mb,cpu_limit_percent,restart_on_exceed,last_exceeded_at,exceed_count,created_at,updated_at FROM bot_limits WHERE bot_id=?",(bot_id,)).fetchone()
            if not row:
                default=256; maxv=2048
                try:
                    r=conn.execute("SELECT value FROM app_settings WHERE key='default_memory_mb'").fetchone(); default=int(r[0]) if r else 256
                    r=conn.execute("SELECT value FROM app_settings WHERE key='max_memory_per_bot_mb'").fetchone(); maxv=int(r[0]) if r else 2048
                except Exception: pass
                if HAS_PSUTIL and psutil.virtual_memory().total < 2*1024**3: maxv=min(maxv,512)
                mem=max(64,min(default,maxv))
                conn.execute("INSERT INTO bot_limits(bot_id,memory_limit_mb,cpu_limit_percent,restart_on_exceed,created_at,updated_at) VALUES(?,?,?,?,?,?)",(bot_id,mem,100,1,now,now))
                row=conn.execute("SELECT bot_id,memory_limit_mb,cpu_limit_percent,restart_on_exceed,last_exceeded_at,exceed_count,created_at,updated_at FROM bot_limits WHERE bot_id=?",(bot_id,)).fetchone()
            conn.commit()
            return dict(zip(keys,row))
    except sqlite3.OperationalError:
        return dict(zip(keys,(bot_id,256,100,1,None,0,now,now)))


async def write_audit(admin_id,action,target=None,details=None):
    # BUGFIX 6: Do not attempt Feature 2 writes until migration is available.
    if not feature2_ready():
        log("audit unavailable: Feature 2 migration pending")
        return
    try:
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            await conn.execute("INSERT INTO admin_audit(admin_id,action,target,details,created_at) VALUES(?,?,?,?,?)",(admin_id,action,target,details,utc_now_iso())); await conn.commit()
    except Exception as e:
        log(f"audit unavailable: {type(e).__name__}")

async def show_admin_home(update,ctx):
    # UI: Approval bot intentionally exposes only Broadcast and Audit Log.
    await update.message.reply_text(f"{UI.ADMIN} <b>Admin Panel</b>\n{UI.DIV}\nApproval bot supports Broadcast and Audit Log.{credit_footer()}",
                                  parse_mode="HTML",reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton(f"{UI.BROADCAST} Broadcast",callback_data="adm_broadcast"),
                                       InlineKeyboardButton(f"{UI.AUDIT} Audit Log",callback_data="adm_audit:0")]]))

async def show_audit(update,ctx,page=0):
    q=update.callback_query
    # BUGFIX 9: Guard the audit view when Feature 2 migration has not landed.
    if not feature2_ready():
        # FIX_RICH_18: route through safe_edit so missing q.message cannot crash.
        await safe_edit(
            q,
            "❌ Audit not available yet (migration pending).",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Admin",callback_data="adm_home")
            ]]),
            parse_mode=None,
        )
        return
    try:
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            conn.row_factory=aiosqlite.Row
            rows=await (await conn.execute(
                "SELECT * FROM admin_audit ORDER BY id DESC LIMIT ? OFFSET ?",
                (AUDIT_PAGE_SIZE,page*AUDIT_PAGE_SIZE)
            )).fetchall()
    except sqlite3.OperationalError:
        # FIX_RICH_18: route through safe_edit so missing q.message cannot crash.
        await safe_edit(
            q,
            "❌ Audit table missing.",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Admin",callback_data="adm_home")
            ]]),
            parse_mode=None,
        )
        return
    lines=["📋 <b>Audit Log</b>","━━━━━━━━━━━━━━━━━━"]
    for r in rows:
        lines.append(
            f"{html.escape(str(r['created_at'])[:19])} | "
            f"admin:<code>{r['admin_id']}</code> | "
            f"{html.escape(str(r['action']))} | "
            f"{html.escape(str(r['target'] or '-'))}"
        )
    nav=[]
    if page>0: nav.append(InlineKeyboardButton("⬅️ Prev",callback_data=f"adm_audit:{page-1}"))
    if len(rows)==AUDIT_PAGE_SIZE: nav.append(InlineKeyboardButton("➡️ Next",callback_data=f"adm_audit:{page+1}"))
    kb=[]
    if nav: kb.append(nav)
    kb.append([InlineKeyboardButton("🔙 Admin",callback_data="adm_home")])
    # FIX_RICH_23: route success-path render through safe_edit
    await safe_edit(q, "\n".join(lines), InlineKeyboardMarkup(kb), parse_mode="HTML")

async def cmd_admin(update,ctx):
    u=update.effective_user
    if not u: return
    if not admin_only(u.id):
        await update.message.reply_text(f"🔒 Admin only\nYour ID: <code>{u.id}</code>",parse_mode="HTML"); return
    await show_admin_home(update,ctx)

async def broadcast_text_input(update,ctx):
    st=ctx.user_data.get("approval_broadcast")
    if not st: return False
    if time.time()>st["expires_at"]: ctx.user_data.pop("approval_broadcast",None); await update.message.reply_text("⏱️ Timed out"); return True
    if not admin_only(update.effective_user.id): ctx.user_data.pop("approval_broadcast",None); return True
    text=update.message.text or ""
    if len(text)>4000: await update.message.reply_text("❌ Broadcast is too long (max 4000 characters)."); return True
    ctx.user_data["approval_broadcast_confirm"]={"text":text}; ctx.user_data.pop("approval_broadcast",None)
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        ids=[int(r[0]) for r in await (await conn.execute("SELECT DISTINCT user_id FROM bot_registry ORDER BY user_id")).fetchall()]
    ctx.user_data["approval_broadcast_confirm"]["ids"]=ids
    await update.message.reply_text(f"📢 Send to {len(ids)} users?",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Send",callback_data="adm_broadcast_send"),InlineKeyboardButton("❌ Cancel",callback_data="adm_home")]]))
    return True

# ================== PM2 START ==================
def start_pm2(bot_id, bot_dir, bot_type):
    svc = f"hosted-bot-{bot_id}"
    try:
        lim=get_bot_limits_sync(bot_id)
        resource_args=["--max-memory-restart",f"{lim['memory_limit_mb']}M"] + (["--no-autorestart"] if not lim["restart_on_exceed"] else [])
        env_ok, env_err = prepare_env_for_bot(bot_id, bot_dir)
        if not env_ok:
            log(f"env generation warning for {bot_id}: {env_err}")
        pm2_env = get_env_process_env(bot_id, bot_dir) if env_ok else get_safe_bot_base_env(bot_dir)
        if bot_type in ("nodejs", "whatsapp"):
            target = None
            for f in os.listdir(bot_dir):
                if f.endswith(".js"):
                    target = os.path.join(bot_dir, f); break
            if not target: return False
            r = subprocess.run(["pm2", "start", target, "--name", svc] + resource_args,
                               capture_output=True, text=True, timeout=25, cwd=bot_dir, env=pm2_env)
        else:
            main = None
            for mf in ("main.py", "bot.py", "app.py", "run.py", "simple.py"):
                if os.path.exists(os.path.join(bot_dir, mf)):
                    main = mf; break
            if not main:
                pyfiles = [f for f in os.listdir(bot_dir) if f.endswith(".py")]
                if pyfiles:
                    for pref in ("main", "bot", "app", "run", "simple"):
                        for f in pyfiles:
                            if f.lower().startswith(pref):
                                main = f; break
                        if main: break
                    if not main:
                        main = pyfiles[0]
            if not main:
                log(f"no .py file found for {bot_id}")
                return False
            target = os.path.join(bot_dir, main)
            r = subprocess.run(
                ["pm2", "start", target, "--name", svc] + resource_args + [
                 "--interpreter", "python3", "--cwd", bot_dir],
                capture_output=True, text=True, timeout=25, env=pm2_env
            )
        subprocess.run(["pm2", "save"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception as e:
        log(f"pm2 start {bot_id}: {e}")
        return False

# ================== APPROVE / REJECT ==================
async def claim_pending(bot_id: str) -> Optional[Dict]:
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

async def get_bot_registry_entry(bot_id: str) -> Optional[Dict]:
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM bot_registry WHERE bot_id=?", (bot_id,))
        r = await cur.fetchone()
        return dict(r) if r else None

async def execute_approval(bot_id: str, admin_id: int, approve: bool) -> bool:
    p = await claim_pending(bot_id)
    if not p:
        return False
    staging = Path(p["staging_dir"])

    try:
        if not approve:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            await set_pending(bot_id, "rejected", admin_id, "admin rejected after review")
            await write_audit(admin_id,"bot_reject",bot_id,"flagged upload")
            await mute_user(p["user_id"], MUTE_HOURS, "flagged upload")
            return True

        if not staging.exists():
            if await get_bot_registry_entry(bot_id):
                await set_pending(bot_id, "approved", admin_id, "admin approved (recovered)")
                return True
            await set_pending(bot_id, "rejected", admin_id, "staging missing")
            return False

        final_dir = HOSTED_BOTS_DIR / bot_id
        shutil.move(str(staging), str(final_dir))
        await add_bot(bot_id, p["user_id"], p["name"], str(final_dir), p["bot_type"])
        # FIX_RICH_22: pm2 subprocess is blocking; run off the event loop.
        started = await asyncio.to_thread(start_pm2, bot_id, str(final_dir), p["bot_type"])
        reason = "admin approved" if started else "admin approved (pm2 start failed — check logs)"
        await set_pending(bot_id, "approved", admin_id, reason)
        await write_audit(admin_id,"bot_approve",bot_id,reason)
        return True
    except Exception as e:
        log(f"execute_approval: {e}")
        await set_pending(bot_id, "error", admin_id, f"exception: {e}"[:200])
        return False

# ================== HANDLERS ==================
async def safe_edit(q, text, markup=None, parse_mode="HTML"):
    # UI: Centralized HTML rendering with plain-text fallback.
    # FIX_RICH_BONUS: defensive target check in approval safe_edit
    target = getattr(q, "message", None) if hasattr(q, "message") else q
    if target is None or not hasattr(target, "edit_text"):
        log_err("safe_edit: invalid target", traceback.format_exc())
        return
    try:
        await target.edit_text(text, parse_mode=parse_mode, reply_markup=markup)
    except Exception as e:
        if "Message is not modified" in str(e):
            return
        if parse_mode == "HTML":
            try:
                await target.edit_text(re.sub(r"<[^>]+>", "", text), reply_markup=markup)
                return
            except Exception:
                pass
        raise

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u=update.effective_user
    if not u: return
    if u.id not in ADMIN_IDS:
        await update.message.reply_text(f"{UI.ERROR} Admin only.\nYour ID: <code>{u.id}</code>{credit_footer()}",parse_mode="HTML")
        return
    blocks=[
        rm.heading("🛡️ Approval Bot"),
        rm.paragraph("Flagged uploads appear here for review."),
        rm.compact_table(["Command", "Purpose"], [
            ["/pending", "List pending deploys"],
            ["/admin", "Admin tools"],
            ["/whoami", "Your Telegram ID"],
        ]),
    ]
    try:
        # RICH: Approval welcome uses the same Bot API rich card, with the exact existing HTML message as fallback.
        # MIGRATED: rich_client.send() is already async internally — awaited
        # directly, not wrapped in asyncio.to_thread (see rich_message.py's
        # migration note for why that would silently do nothing).
        if rich_client is None:
            raise rm.RichMessageError("APPROVAL_BOT_TOKEN is not configured")
        await rich_client.send(update.effective_chat.id, blocks)
        return
    except rm.RichMessageError:
        log_err("RICH: cmd_start rich send failed, falling back", traceback.format_exc())
    await update.message.reply_text(
        f"{UI.APPROVAL} <b>Approval Bot</b>\n{UI.DIV}\n"
        "Flagged uploads appear here for review.\n\n"
        "<b>Commands</b>\n<code>/pending</code> — list pending\n"
        "<code>/admin</code> — approval admin tools\n"
        "<code>/whoami</code> — your ID"
        f"{credit_footer()}",
        parse_mode="HTML"
    )

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    if update.message: await update.message.reply_text("✅ Cancelled")

async def cmd_whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if u:
        await update.message.reply_text(f"Your ID: <code>{u.id}</code>{credit_footer()}", parse_mode="HTML")

async def cmd_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u or u.id not in ADMIN_IDS: return
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM pending_deploys WHERE status='pending' ORDER BY created_at DESC LIMIT 50"
        )
        rows = await cur.fetchall()
    if not rows:
        await update.message.reply_text("✅ No pending deploys.")
        return
    for r in rows:
        txt = (
            f"⏳ <b>Pending</b>\n"
            f"👤 User: <code>{r['user_id']}</code>\n"
            f"🤖 Bot: <code>{html.escape(str(r['name']), quote=False)}</code> ({html.escape(str(r['bot_type']), quote=False)})\n"
            f"🆔 ID: <code>{html.escape(str(r['bot_id']), quote=False)}</code>"
        )
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"approve:{r['bot_id']}"),
            InlineKeyboardButton("❌ Reject",  callback_data=f"reject:{r['bot_id']}"),
        ]])
        await update.message.reply_text(txt, parse_mode="HTML", reply_markup=markup)

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
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        cur = await conn.execute("DELETE FROM muted_users WHERE user_id=?", (tid,))
        await conn.commit()
        changed = cur.rowcount > 0
    await update.message.reply_text(
        f"{'✅' if changed else '❌'} {'Unmuted' if changed else 'Not muted'}: <code>{tid}</code>{credit_footer()}", parse_mode="HTML")

async def cb_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; u=update.effective_user
    if not u or not admin_only(u.id):
        try: await q.answer("Admin only",show_alert=True)
        except Exception: pass
        return
    try: await q.answer()
    except Exception: pass
    data=q.data or ""
    if data=="adm_home":
        # FIX_RICH_10: safe_edit takes positional markup, not reply_markup kwarg
        await safe_edit(
            q,
            f"{UI.ADMIN} <b>Admin Panel</b>\n{UI.DIV}\nApproval bot supports Broadcast and Audit Log.{credit_footer()}",
            InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{UI.BROADCAST} Broadcast",callback_data="adm_broadcast"),
                 InlineKeyboardButton(f"{UI.AUDIT} Audit Log",callback_data="adm_audit:0")]]),
            parse_mode="HTML"
        ); return
    if data=="adm_broadcast":
        ctx.user_data["approval_broadcast"]={"expires_at":time.time()+120}
        # FIX_RICH_19: route through safe_edit so missing q.message cannot crash.
        await safe_edit(
            q,
            "📢 Send the broadcast message now. /cancel to cancel.",
            InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel",callback_data="adm_home")]]),
            parse_mode=None,
        ); return
    if data=="adm_broadcast_send":
        st=ctx.user_data.pop("approval_broadcast_confirm",None)
        if not st: await q.answer("Broadcast expired",show_alert=True); return
        # FIX_RICH_15: guard q.message in approval broadcast
        msg_src = q.message
        if msg_src is None:
            log_err("RICH: adm_broadcast_send missing q.message", traceback.format_exc())
            try: await q.answer("Message expired, please retry", show_alert=True)
            except Exception: pass
            return
        sent=failed=0; ids=st["ids"]; progress=await msg_src.chat.send_message(f"📢 Broadcast started: 0/{len(ids)}")
        for i,uid in enumerate(ids,1):
            try:
                await ctx.bot.send_message(uid,st["text"])
                sent+=1
            except RetryAfter as e:
                # BUGFIX 11: Respect Telegram's RetryAfter and retry once.
                retry_after = max(0.0, float(getattr(e, "retry_after", 1)))
                log(f"broadcast rate limited user {uid}: retry_after={retry_after}")
                await asyncio.sleep(retry_after)
                try:
                    await ctx.bot.send_message(uid,st["text"])
                    sent+=1
                except Exception as retry_exc:
                    failed+=1
                    log(f"broadcast retry failed user {uid}: {type(retry_exc).__name__}")
            except Exception as e:
                failed+=1
                log(f"broadcast failed user {uid}: {type(e).__name__}")
            if i%20==0:
                try: await progress.edit_text(f"📢 Broadcast progress: {i}/{len(ids)}\\nSent: {sent} | Failed: {failed}")
                except Exception: pass
            await asyncio.sleep(1.5/BROADCAST_RATE_PER_SECOND)
        try: await progress.edit_text(f"📢 Broadcast complete\nSent: {sent} | Failed: {failed}")
        except Exception: pass
        await write_audit(u.id,"broadcast",None,f"sent={sent},failed={failed}"); return
    if data.startswith("adm_audit:"):
        try: page=max(0,int(data.split(":",1)[1]))
        except Exception: page=0
        await show_audit(update,ctx,page); return
    parts=data.split(":",1); action=parts[0]; bot_id=parts[1] if len(parts)>1 else ""
    if action not in ("approve","reject"): return
    ok=await execute_approval(bot_id,u.id,approve=(action=="approve"))
    # FIX_RICH_8: Surface execute_approval failure instead of reporting false success.
    if ok:
        icon = "✅ Approved: " if action=="approve" else "❌ Rejected: "
    else:
        icon = "⚠️ Failed to process: "
    try:
        # FIX_RICH_23: route through safe_edit for consistency with other paths.
        await safe_edit(q, icon + f"<code>{html.escape(bot_id)}</code>", None, parse_mode="HTML")
    except Exception:
        log("RICH: approval result message edit failed", traceback.format_exc())


# ================== MAIN ==================
def feature2_ready() -> bool:
    # BUGFIX 6: Re-check the migration lazily if approval bot started before
    # the hosting bot completed Feature 2 migration.
    global FEATURE2_READY
    if not FEATURE2_READY:
        try:
            with sqlite3.connect(DATABASE_PATH, timeout=10) as conn:
                FEATURE2_READY = int(conn.execute("PRAGMA user_version").fetchone()[0]) >= 2
        except Exception:
            FEATURE2_READY = False
    return FEATURE2_READY

async def _post_init(app):
    """Approval bot never migrates; it only checks the hosting bot migration lock."""
    await ensure_schema()
    check_env_schema()
    feature2_ready()
    init_env_crypto()
    try:
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            await conn.execute("INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",("approval_started_at",utc_now_iso(),utc_now_iso())); await conn.commit()
    except Exception: pass

def main():
    if not APPROVAL_BOT_TOKEN:
        print("No approval token"); sys.exit(1)

    app = (
        Application.builder()
        .token(APPROVAL_BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("unmute", cmd_unmute))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_text_input))
    app.add_handler(CallbackQueryHandler(cb_handler))

    print("🛡️ Approval bot started.")
    app.run_polling()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
