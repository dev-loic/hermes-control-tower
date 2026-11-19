"""Hermes Control Tower — builds/refreshes the topic list from Hermes conversations, using Hermes itself (oneshot CLI) as the LLM."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime

import store

SOURCES = ("local", "gui", "telegram", "tui", "desktop")
BATCH_CHARS = 45000
HERMES_BIN = os.environ.get("HERMES_BIN") or shutil.which("hermes") or str(
    store.HERMES_HOME / "hermes-agent/.hermes/bin/hermes")


def _clip(s, n):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _day(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "?"


def candidate_sessions(conn):
    """Sessions that are new or grew since the last indexing pass."""
    chat_sids = {r[0] for r in conn.execute("SELECT chat_session_id FROM topics WHERE chat_session_id IS NOT NULL")}
    indexed = {r["session_id"]: r["msg_count"] for r in conn.execute("SELECT session_id, msg_count FROM indexed_sessions")}
    own_index = set(store.get_meta(conn, "index_session_ids", []))
    sdb = store.state_db()
    try:
        rows = sdb.execute(
            "SELECT s.id, s.source, s.title, s.started_at, coalesce(s.last_activity_at, s.started_at) AS last, "
            "(SELECT count(*) FROM messages m WHERE m.session_id=s.id AND m.role='user') AS n_user, "
            "(SELECT count(*) FROM messages m WHERE m.session_id=s.id) AS n_all "
            "FROM sessions s ORDER BY s.started_at").fetchall()
    finally:
        sdb.close()
    out = []
    for r in rows:
        if r["id"] in own_index:
            continue
        if r["source"] not in SOURCES and r["id"] not in chat_sids:
            continue
        if r["n_user"] == 0:
            continue
        if indexed.get(r["id"]) == r["n_all"]:
            continue
        out.append(dict(r))
    return out


def session_digest(sid, title, started, last, chat_topic=None):
    sdb = store.state_db()
    try:
        users = [r["content"] for r in sdb.execute(
            "SELECT content FROM messages WHERE session_id=? AND role='user' AND content IS NOT NULL "
            "AND coalesce(display_kind,'')<>'hidden' ORDER BY id", (sid,))]
        assistants = [r["content"] for r in sdb.execute(
            "SELECT content FROM messages WHERE session_id=? AND role='assistant' AND content IS NOT NULL "
            "AND length(trim(content))>20 ORDER BY id", (sid,))]
    finally:
        sdb.close()
    users = [u for u in users if not u.startswith("[IMPORTANT: You are running as a scheduled cron job")]
    if len(users) > 10:
        users = users[:6] + ["…"] + users[-4:]
    lines = [f"title: {title or '(untitled)'} | from {_day(started)} to {_day(last)}"]
    if chat_topic:
        lines.append(f"(conversation opened from Hermes Control Tower for the topic {chat_topic})")
    for u in users:
        lines.append(store.USER_NAME.upper() + ": " + _clip(u, 350))
    if assistants:
        if len(assistants) > 1:
            lines.append("HERMES (previous answer): " + _clip(assistants[-2], 300))
        lines.append("HERMES (last answer): " + _clip(assistants[-1], 900))
    return "\n".join(lines)


PROMPT = """You index the conversations between {user} and you (Hermes) into TOPICS for Hermes Control Tower, {user}'s tracking app.
A topic is a matter followed over time (e.g. "Passport renewal", "Moving out", "Job search", "Refund claim with an airline"). Several conversations can belong to the same topic; one conversation can touch at most 2 topics.
DO NOT USE ANY TOOL. Answer only with a valid JSON object, no surrounding text, no code fence.

Today: {today}

EXISTING TOPICS (attach conversations to them when it is the same topic rather than creating a new one).
Entries marked "routine" are Hermes' recurring automated jobs (cron): any conversation that tunes, fixes or discusses a routine (its content, schedule, failures) belongs to that routine — NEVER create a separate topic for an existing routine:
{topics}

FOLDERS ({user} groups topics by theme):
{folders}

CONVERSATIONS TO INDEX:
{sessions}

Rules:
- Write titles, summaries and next steps in {language}. If an existing topic's title, summary or next step is in another language, return it translated.
- One-off questions with no follow-up (a quick fact, a curiosity, a test) go to "ignored", unless they add to an existing topic.
- Short, concrete titles (max 60 characters), no emoji in the title; one relevant emoji separately.
- summary: 1 to 3 plain sentences on where things concretely stand (useful facts, dates, amounts).
- next_step: the next concrete action in one sentence; empty if none.
- owner: who must act now — "user" ({user} must do/answer/decide), "hermes" (a question or action from Hermes is pending), "waiting" (waiting on a third party), "none" (nothing to do).
- status: "open" or "done" (done only if clearly finished). For a routine keep "open"; its summary describes what it does and its latest tweaks, its next_step a pending tweak (else empty), owner "none" unless someone must act.
- For an existing topic, return the updated fields taking its current summary and the new conversations into account.
- folder: for every returned topic, the id of the folder it belongs to (see FOLDERS), or null if none fits. Keep a topic's current folder unless it clearly no longer fits.
- last_activity: date (YYYY-MM-DD) of the topic's latest conversation.

Exact format:
{{"topics": [{{"id": "<existing id or new_1, new_2…>", "title": "...", "emoji": "🏝️", "summary": "...", "next_step": "...", "owner": "user", "status": "open", "last_activity": "YYYY-MM-DD", "folder": "f_xxx", "sessions": ["S1", "S4"]}}], "ignored": ["S2"]}}
Only return in "topics" the topics touched by the conversations above.
"""


def _topics_block(conn):
    jobs = store.sync_routines(conn)
    rows = conn.execute("SELECT * FROM topics ORDER BY coalesce(last_activity, updated_at) DESC").fetchall()
    if not rows:
        return "(none)"
    out = []
    for t in rows:
        item = {"id": t["id"], "title": t["title"], "status": t["status"], "owner": t["owner"],
                "summary": t["summary"], "next_step": t["next_step"], "last_activity": _day(t["last_activity"]),
                "folder": t["folder_id"]}
        if t["cron_job_id"]:
            job = jobs.get(t["cron_job_id"])
            item = {"routine": True, **item, "cron": (job or {}).get("name"),
                    "schedule": store.schedule_text(job) if job else None}
        out.append(json.dumps(item, ensure_ascii=False))
    return "\n".join(out)


def _folders_block(conn):
    rows = conn.execute("SELECT id, name, emoji FROM folders ORDER BY position").fetchall()
    out = []
    for f in rows:
        titles = [r[0] for r in conn.execute("SELECT title FROM topics WHERE folder_id=? ORDER BY last_activity DESC LIMIT 8", (f["id"],))]
        out.append(json.dumps({"id": f["id"], "name": f["name"], "examples": titles}, ensure_ascii=False))
    return "\n".join(out) or "(none)"


def _extract_json(text):
    text = text.strip()
    text = re.sub(r"\n*session_id:\s*\S+\s*$", "", text).strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON in output: " + text[:300])
    return json.loads(m.group(0))


def ask_hermes(prompt, conn, timeout=900):
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, dir=store.DATA_DIR) as f:
        f.write(prompt)
        path = f.name
    try:
        p = subprocess.run([HERMES_BIN, "chat", "--query-file", path, "-Q", "-t", "todo", "--max-turns", "3", "--source", "tool"],
                           capture_output=True, text=True, timeout=timeout)
    finally:
        os.unlink(path)
    out = p.stdout
    m = re.search(r"session_id:\s*(\S+)", p.stdout + "\n" + p.stderr)
    if m:  # keep the index runs out of the topic list and out of Hermes' history
        sid = m.group(1)
        ids = store.get_meta(conn, "index_session_ids", [])
        store.set_meta(conn, "index_session_ids", (ids + [sid])[-200:])
        subprocess.run([HERMES_BIN, "sessions", "delete", "-y", sid], capture_output=True, timeout=60)
    return _extract_json(out)


def apply_result(conn, result, smap, cand_by_sid):
    now = time.time()
    folder_ids = {r[0] for r in conn.execute("SELECT id FROM folders")}
    for t in result.get("topics", []):
        sids = [smap[s] for s in t.get("sessions", []) if s in smap]
        tid = t.get("id") or ""
        row = conn.execute("SELECT * FROM topics WHERE id=?", (tid,)).fetchone()
        last_ts = max([cand_by_sid[s]["last"] for s in sids] or [now])
        try:
            la = datetime.strptime(t.get("last_activity", ""), "%Y-%m-%d").timestamp()
            last_ts = max(last_ts, la) if sids else la
        except ValueError:
            pass
        owner = t.get("owner") if t.get("owner") in ("user", "hermes", "waiting", "none") else "user"
        status = "done" if t.get("status") == "done" else "open"
        if row is None:
            tid = store.new_topic_id()
            conn.execute(
                "INSERT INTO topics(id,title,emoji,status,owner,summary,next_step,created_at,updated_at,last_activity,status_changed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (tid, t.get("title", "Sujet")[:80], t.get("emoji") or "💬", status, owner, t.get("summary", ""),
                 t.get("next_step", ""), now, now, last_ts, now))
        else:
            new_status = row["status"]
            if row["cron_job_id"]:
                status = row["status"]  # routines follow their cron job, not the LLM
            fresh = [s for s in sids if cand_by_sid[s]["last"] > (row["status_changed_at"] or 0) + 60]
            if row["status"] == "open" and status == "done":
                new_status = "done"
            elif row["status"] in ("done", "dropped") and fresh and status == "open":
                # the user talked about it again after closing it -> reopen
                new_status = "open"
            title = row["title"] if row["manual"] else t.get("title", row["title"])[:80]
            conn.execute(
                "UPDATE topics SET title=?, emoji=?, summary=?, next_step=?, owner=?, status=?, updated_at=?, "
                "last_activity=max(coalesce(last_activity,0),?), "
                "status_changed_at=CASE WHEN status<>? THEN ? ELSE status_changed_at END WHERE id=?",
                (title, t.get("emoji") or row["emoji"], t.get("summary", row["summary"]),
                 t.get("next_step", row["next_step"]), owner, new_status, now, last_ts, new_status, now, tid))
        # auto-file: only topics without a folder (never override a folder set by the user)
        fid = t.get("folder")
        if fid in folder_ids and not (row is not None and row["folder_id"]) and not (row is not None and row["cron_job_id"]):
            conn.execute("UPDATE topics SET folder_id=? WHERE id=? AND folder_id IS NULL", (fid, tid))
        for s in sids:
            conn.execute("INSERT OR IGNORE INTO topic_sessions(topic_id, session_id) VALUES(?,?)", (tid, s))
    ignored = {smap[s] for s in result.get("ignored", []) if s in smap}
    for sid, c in cand_by_sid.items():
        conn.execute("INSERT INTO indexed_sessions(session_id,msg_count,ignored,indexed_at) VALUES(?,?,?,?) "
                     "ON CONFLICT(session_id) DO UPDATE SET msg_count=excluded.msg_count, ignored=excluded.ignored, "
                     "indexed_at=excluded.indexed_at", (sid, c["n_all"], int(sid in ignored), now))
    conn.commit()


def refresh(progress=lambda msg: None):
    conn = store.connect()
    try:
        cands = candidate_sessions(conn)
        chat_map = {r["chat_session_id"]: r["title"] for r in
                    conn.execute("SELECT chat_session_id, title FROM topics WHERE chat_session_id IS NOT NULL")}
        if not cands:
            progress("Nothing new")
            store.set_meta(conn, "last_refresh", time.time())
            return {"indexed": 0}
        digests = [(c, session_digest(c["id"], c["title"], c["started_at"], c["last"], chat_map.get(c["id"])))
                   for c in cands]
        batches, cur, size = [], [], 0
        for c, d in digests:
            if cur and size + len(d) > BATCH_CHARS:
                batches.append(cur)
                cur, size = [], 0
            cur.append((c, d))
            size += len(d)
        if cur:
            batches.append(cur)
        done = 0
        for i, batch in enumerate(batches, 1):
            progress(f"Batch {i}/{len(batches)} ({len(batch)} conversations)")
            smap = {f"S{j}": c["id"] for j, (c, _) in enumerate(batch, 1)}
            block = "\n\n".join(f"[S{j}]\n{d}" for j, (_, d) in enumerate(batch, 1))
            prompt = PROMPT.format(user=store.USER_NAME, language=store.CONTENT_LANG, today=datetime.now().strftime("%Y-%m-%d"), topics=_topics_block(conn), folders=_folders_block(conn), sessions=block)
            result = ask_hermes(prompt, conn)
            apply_result(conn, result, smap, {c["id"]: c for c, _ in batch})
            done += len(batch)
        store.set_meta(conn, "last_refresh", time.time())
        progress(f"{done} conversations analysed")
        return {"indexed": done}
    finally:
        conn.close()


if __name__ == "__main__":
    print(refresh(progress=print))
