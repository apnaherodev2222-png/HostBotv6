BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS bot_limits (
    bot_id TEXT PRIMARY KEY,
    memory_limit_mb INTEGER NOT NULL DEFAULT 256,
    cpu_limit_percent INTEGER NOT NULL DEFAULT 100,
    restart_on_exceed INTEGER NOT NULL DEFAULT 1,
    last_exceeded_at TEXT,
    exceed_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    details TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON admin_audit(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_admin ON admin_audit(admin_id);
CREATE TABLE IF NOT EXISTS user_bans (
    user_id INTEGER PRIMARY KEY,
    banned_by INTEGER NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);
INSERT OR IGNORE INTO app_settings(key,value,updated_at) VALUES
 ('maintenance_mode','0',CURRENT_TIMESTAMP),
 ('deploys_enabled','1',CURRENT_TIMESTAMP),
 ('default_memory_mb','256',CURRENT_TIMESTAMP),
 ('max_memory_per_bot_mb','2048',CURRENT_TIMESTAMP),
 ('vps_cap_percent','80',CURRENT_TIMESTAMP);
INSERT OR IGNORE INTO bot_limits(bot_id,memory_limit_mb,cpu_limit_percent,restart_on_exceed,last_exceeded_at,exceed_count,created_at,updated_at)
SELECT bot_id,256,100,1,NULL,0,COALESCE(created_at,CURRENT_TIMESTAMP),CURRENT_TIMESTAMP FROM bot_registry;
PRAGMA user_version = 2;
COMMIT;
