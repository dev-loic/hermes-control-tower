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
    lines = [f"title: {title or '(sans titre)'} | du {_day(started)} au {_day(last)}"]
    if chat_topic:
        lines.append(f"(conversation ouverte depuis Hermes Control Tower pour le sujet {chat_topic})")
    for u in users:
        lines.append(store.USER_NAME.upper() + ": " + _clip(u, 350))
    if assistants:
        if len(assistants) > 1:
            lines.append("HERMES (avant-dernière réponse): " + _clip(assistants[-2], 300))
        lines.append("HERMES (dernière réponse): " + _clip(assistants[-1], 900))
    return "\n".join(lines)


PROMPT = """Tu indexes les conversations entre {user} et toi (Hermes) en SUJETS, pour Hermes Control Tower, l'appli de suivi de {user}.
Un sujet = une affaire suivie dans le temps (ex : « Renouvellement du passeport », « Déménagement », « Recherche d'emploi », « Réclamation auprès d'une compagnie aérienne »). Plusieurs conversations peuvent appartenir au même sujet ; une conversation peut toucher 2 sujets au maximum.
N'UTILISE AUCUN OUTIL. Réponds uniquement avec un objet JSON valide, sans texte autour, sans bloc de code.

Date du jour : {today}

SUJETS EXISTANTS (rattache-y les conversations quand c'est le même sujet, plutôt que d'en créer un nouveau) :
{topics}

CONVERSATIONS À INDEXER :
{sessions}

Règles :
- Les questions ponctuelles sans suite (une info rapide, une curiosité, un test) vont dans "ignored", sauf si elles complètent un sujet existant.
- Titres courts en français (max 60 caractères), concrets, sans emoji dans le titre ; un emoji pertinent à part.
- summary : 1 à 3 phrases, en français, simple, où on en est concrètement (faits, dates, montants utiles).
- next_step : la prochaine action concrète, en une phrase ; vide si rien.
- owner : qui doit agir maintenant — "user" ({user} doit faire/répondre/décider), "hermes" (une question ou une action de Hermes est en attente), "waiting" (on attend un tiers), "none" (rien à faire).
- status : "open" ou "done" (done seulement si c'est clairement terminé).
- Pour un sujet existant, renvoie les champs mis à jour en tenant compte de son résumé actuel et des nouvelles conversations.
- last_activity : date (YYYY-MM-DD) de la dernière conversation du sujet.

Format exact :
{{"topics": [{{"id": "<id existant ou new_1, new_2…>", "title": "...", "emoji": "🏝️", "summary": "...", "next_step": "...", "owner": "user", "status": "open", "last_activity": "YYYY-MM-DD", "sessions": ["S1", "S4"]}}], "ignored": ["S2"]}}
Ne renvoie dans "topics" que les sujets touchés par les conversations ci-dessus.
"""


def _topics_block(conn):
    rows = conn.execute("SELECT * FROM topics ORDER BY coalesce(last_activity, updated_at) DESC").fetchall()
    if not rows:
        return "(aucun)"
    out = []
    for t in rows:
        out.append(json.dumps({"id": t["id"], "title": t["title"], "status": t["status"], "owner": t["owner"],
                               "summary": t["summary"], "next_step": t["next_step"],
                               "last_activity": _day(t["last_activity"])}, ensure_ascii=False))
    return "\n".join(out)


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
            progress("Rien de nouveau")
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
            progress(f"Analyse {i}/{len(batches)} ({len(batch)} conversations)")
            smap = {f"S{j}": c["id"] for j, (c, _) in enumerate(batch, 1)}
            block = "\n\n".join(f"[S{j}]\n{d}" for j, (_, d) in enumerate(batch, 1))
            prompt = PROMPT.format(user=store.USER_NAME, today=datetime.now().strftime("%Y-%m-%d"), topics=_topics_block(conn), sessions=block)
            result = ask_hermes(prompt, conn)
            apply_result(conn, result, smap, {c["id"]: c for c, _ in batch})
            done += len(batch)
        store.set_meta(conn, "last_refresh", time.time())
        progress(f"{done} conversations analysées")
        return {"indexed": done}
    finally:
        conn.close()


if __name__ == "__main__":
    print(refresh(progress=print))
