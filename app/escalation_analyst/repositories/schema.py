"""Private schema v1; never alters AI Box tables."""

from __future__ import annotations

DDL = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS whatsapp_groups (group_id TEXT PRIMARY KEY, group_name TEXT, is_active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS group_name_overrides (group_id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS group_names_learned (group_id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, wa_id TEXT, group_id TEXT NOT NULL, group_name TEXT, author TEXT, author_name TEXT, body TEXT, msg_type TEXT, has_media INTEGER DEFAULT 0, timestamp BIGINT NOT NULL, created_at BIGINT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS idx_msg_waid ON messages(wa_id) WHERE wa_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_group_ts ON messages(group_id,timestamp,id);
CREATE TABLE IF NOT EXISTS clusters (id TEXT PRIMARY KEY, name TEXT NOT NULL, is_active INTEGER DEFAULT 1, created_at BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS group_clusters (group_id TEXT PRIMARY KEY, cluster_id TEXT NOT NULL, group_name TEXT, updated_at BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS schedules (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, time_hhmm TEXT NOT NULL, window_mode TEXT DEFAULT 'previous-day', enabled INTEGER DEFAULT 1, created_at BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS reports (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL, group_name TEXT, report TEXT NOT NULL, message_count INTEGER, window_label TEXT, period_start BIGINT, period_end BIGINT, model TEXT, trigger TEXT, scope TEXT DEFAULT 'group', metrics_json TEXT, cluster_id TEXT, base_report_id INTEGER, max_message_id BIGINT, created_at BIGINT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_rep_group ON reports(group_id,created_at);
CREATE INDEX IF NOT EXISTS idx_rep_scope ON reports(scope,period_start,period_end);
CREATE TABLE IF NOT EXISTS usage_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts BIGINT, scope TEXT, group_id TEXT, group_name TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL, cost_inr REAL);
CREATE TABLE IF NOT EXISTS group_activity_policy (id INTEGER PRIMARY KEY, days INTEGER NOT NULL, enabled INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, lock_key TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, request_key TEXT, request TEXT NOT NULL, result TEXT, error TEXT, created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS idx_active_job ON jobs(lock_key) WHERE status='running';
CREATE UNIQUE INDEX IF NOT EXISTS idx_request_job ON jobs(kind,request_key) WHERE request_key IS NOT NULL;
CREATE TABLE IF NOT EXISTS report_presets (id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id TEXT NOT NULL, name TEXT NOT NULL, config TEXT NOT NULL, created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS api_keys (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, key_hash TEXT UNIQUE NOT NULL, created_at BIGINT, last_used_at BIGINT, revoked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS legacy_records (table_name TEXT NOT NULL, record_id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(table_name,record_id));
CREATE TABLE IF NOT EXISTS legacy_imports (kind TEXT NOT NULL, legacy_id TEXT NOT NULL, target_id BIGINT NOT NULL, PRIMARY KEY(kind,legacy_id));
"""
