"""Persistence for Hermes Control Tower: topics, their linked Hermes sessions, and per-topic chat transcripts."""
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
DATA_DIR = Path(os.environ.get("HCT_DATA_DIR", HERMES_HOME / "control-tower"))
DB_PATH = DATA_DIR / "control-tower.db"
USER_NAME = os.environ.get("HCT_USER_NAME", "the user")
CONTENT_LANG = os.environ.get("HCT_LANGUAGE", "English")
STATE_DB = HERMES_HOME / "state.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  emoji TEXT DEFAULT '💬',
  status TEXT NOT NULL DEFAULT 'open',      -- open | snoozed | done | dropped
  owner TEXT NOT NULL DEFAULT 'user',       -- user | hermes | waiting | none
  summary TEXT DEFAULT '',
  next_step TEXT DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  last_activity REAL,
  status_changed_at REAL,
  snooze_until REAL,
  chat_session_id TEXT,
  pinned INTEGER NOT NULL DEFAULT 0,
  manual INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS topic_sessions (
  topic_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  PRIMARY KEY (topic_id, session_id)
);
CREATE INDEX IF NOT EXISTS idx_ts_session ON topic_sessions(session_id);
CREATE TABLE IF NOT EXISTS indexed_sessions (
  session_id TEXT PRIMARY KEY,
  msg_count INTEGER NOT NULL,
  ignored INTEGER NOT NULL DEFAULT 0,
  indexed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  topic_id TEXT NOT NULL,
  role TEXT NOT NULL,            -- user | assistant | event
  content TEXT NOT NULL,
  tools TEXT,                    -- json list of tool names used during the turn
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_topic ON chat_messages(topic_id, id);
CREATE TABLE IF NOT EXISTS folders (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  emoji TEXT DEFAULT '📁',
  position INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(topics)")}
    if "cron_job_id" not in cols:
        conn.execute("ALTER TABLE topics ADD COLUMN cron_job_id TEXT")
        conn.commit()
    if "folder_id" not in cols:
        conn.execute("ALTER TABLE topics ADD COLUMN folder_id TEXT")
        conn.commit()
    return conn


# ---------------------------------------------------------------- routines (Hermes cron jobs)
CRON_JOBS = HERMES_HOME / "cron" / "jobs.json"
DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def cron_jobs() -> dict:
    try:
        data = json.loads(CRON_JOBS.read_text())
    except (OSError, ValueError):
        return {}
    jobs = data.get("jobs", data) if isinstance(data, dict) else data
    jobs = jobs.values() if isinstance(jobs, dict) else jobs
    return {j["id"]: j for j in jobs if isinstance(j, dict) and j.get("id")}


def schedule_text(job) -> str:
    s = job.get("schedule") or {}
    if s.get("kind") == "interval" and s.get("minutes"):
        m = int(s["minutes"])
        return f"Every {m} min" if m < 60 or m % 60 else ("Every hour" if m == 60 else f"Every {m // 60} h")
    expr = (s.get("expr") or "").split()
    if len(expr) == 5:
        mi, h, dom, mon, dow = expr
        if mi.isdigit() and h.isdigit():
            at = f"{int(h):02d}:{int(mi):02d}"
            if dom == "*" and mon == "*" and dow == "*":
                return f"Every day at {at}"
            if dom == "*" and mon == "*" and dow.isdigit():
                return f"Every {DAYS[int(dow)]} at {at}"
            if dom == "*" and mon == "*" and dow in ("1-5", "mon-fri"):
                return f"Weekdays at {at}"
            if dom.isdigit() and mon == "*" and dow == "*":
                return f"Monthly on day {dom} at {at}"
    return s.get("display") or "Scheduled"


def cron_info(job) -> dict:
    return {"id": job["id"], "name": job.get("name"), "schedule": schedule_text(job),
            "enabled": bool(job.get("enabled", True)) and job.get("state") != "paused",
            "state": job.get("state"), "next_run_at": job.get("next_run_at"), "last_run_at": job.get("last_run_at"),
            "last_status": job.get("last_status"), "last_error": job.get("last_error"),
            "deliver": job.get("deliver"), "failure_streak": job.get("failure_streak") or 0}


def sync_routines(conn):
    """One routine (topic row with cron_job_id) per Hermes cron job; a removed job closes its routine."""
    jobs = cron_jobs()
    known = {r["cron_job_id"]: r["id"] for r in conn.execute("SELECT id, cron_job_id FROM topics WHERE cron_job_id IS NOT NULL")}
    now = time.time()
    for jid, job in jobs.items():
        if jid not in known:
            conn.execute("INSERT INTO topics(id,title,emoji,status,owner,summary,next_step,created_at,updated_at,last_activity,"
                         "status_changed_at,cron_job_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (new_topic_id(), (job.get("name") or "Routine")[:80], "🔁", "open", "none", "", "", now, now,
                          job.get("created_at") and _iso_ts(job.get("created_at")) or now, now, jid))
    for jid, tid in known.items():
        if jid not in jobs:
            conn.execute("UPDATE topics SET status='done', status_changed_at=?, cron_job_id=NULL, "
                         "summary = summary || ' (routine deleted)' WHERE id=?", (now, tid))
    conn.commit()
    return jobs


def _iso_ts(v):
    try:
        from datetime import datetime
        return datetime.fromisoformat(v).timestamp() if isinstance(v, str) else float(v)
    except (TypeError, ValueError):
        return None


def state_db() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_meta(conn, key, value):
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value)))
    conn.commit()


def new_topic_id() -> str:
    return "t_" + uuid.uuid4().hex[:10]


def topic_dict(conn, row) -> dict:
    d = dict(row)
    sids = [r["session_id"] for r in conn.execute(
        "SELECT session_id FROM topic_sessions WHERE topic_id=?", (d["id"],))]
    d["session_ids"] = sids
    d["chat_count"] = conn.execute(
        "SELECT count(*) FROM chat_messages WHERE topic_id=? AND role='user'", (d["id"],)).fetchone()[0]
    return d


def wake_snoozed(conn):
    now = time.time()
    conn.execute("UPDATE topics SET status='open', snooze_until=NULL, status_changed_at=? "
                 "WHERE status='snoozed' AND snooze_until IS NOT NULL AND snooze_until <= ?", (now, now))
    conn.commit()


def sessions_meta(session_ids):
    """Title/date/source for Hermes sessions (read-only on state.db)."""
    if not session_ids:
        return {}
    sdb = state_db()
    try:
        q = ",".join("?" * len(session_ids))
        rows = sdb.execute(
            f"SELECT id, title, source, started_at, coalesce(last_activity_at, started_at) AS last, message_count "
            f"FROM sessions WHERE id IN ({q})", list(session_ids)).fetchall()
        return {r["id"]: dict(r) for r in rows}
    finally:
        sdb.close()


def session_transcript(session_id, limit=200):
    sdb = state_db()
    try:
        rows = sdb.execute(
            "SELECT role, content, timestamp FROM messages WHERE session_id=? AND role IN ('user','assistant') "
            "AND content IS NOT NULL AND length(trim(content))>0 AND coalesce(display_kind,'')<>'hidden' "
            "ORDER BY id LIMIT ?", (session_id, limit)).fetchall()
        meta = sdb.execute("SELECT title, source, started_at FROM sessions WHERE id=?", (session_id,)).fetchone()
        return {"meta": dict(meta) if meta else None, "messages": [dict(r) for r in rows]}
    finally:
        sdb.close()
