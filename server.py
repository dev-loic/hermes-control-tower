"""Hermes Control Tower — local web app to browse, discuss and close the topics you have open with Hermes.

Run: ~/.hermes/hermes-agent/venv/bin/python server.py   (http://127.0.0.1:4747)
"""
import asyncio
import json
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from aiohttp import web

import indexer
import store

HOST, PORT = "127.0.0.1", int(os.environ.get("HCT_PORT", "4747"))
STATIC = Path(__file__).parent / "static"
HERMES_BIN = indexer.HERMES_BIN


# ---------------------------------------------------------------- chat runs
class ChatRun:
    def __init__(self, topic_id):
        self.topic_id = topic_id
        self.events = []
        self.done = False
        self.proc = None
        self.cond = asyncio.Condition()

    async def push(self, ev):
        async with self.cond:
            self.events.append(ev)
            self.cond.notify_all()


RUNS: dict[str, ChatRun] = {}

PREAMBLE = """[Sent from Hermes Control Tower — this conversation is about one specific topic. Reply in the language {user} writes in.]
Topic: {emoji} {title}{routine}
Folder: {folder}
Status: {status} · Whose move: {owner}
Where we are: {summary}
Next step: {next_step}
Past conversations about it (read them with session_search session_id=… if you need details):
{sessions}

(To file this topic in a folder: PATCH http://127.0.0.1:4747/api/topics/{topic_id} with {{"folder_id": "<id>"}} or {{"folder": "<name>"}} (created if missing); GET /api/folders lists them.)

Message from {user}:
{message}"""

OWNER_TXT = {"user": store.USER_NAME, "hermes": "Hermes", "waiting": "waiting on a third party", "none": "nobody"}
STATUS_TXT = {"open": "open", "snoozed": "snoozed", "done": "done", "dropped": "dropped"}


def _build_query(conn, topic, message):
    if topic["chat_session_id"]:
        return message
    sids = [r[0] for r in conn.execute("SELECT session_id FROM topic_sessions WHERE topic_id=?", (topic["id"],))]
    meta = store.sessions_meta(sids)
    lines = []
    for sid in sorted(sids, key=lambda s: meta.get(s, {}).get("started_at") or 0):
        m = meta.get(sid)
        if m:
            day = datetime.fromtimestamp(m["started_at"]).strftime("%Y-%m-%d")
            lines.append(f"- {day} · {m['title'] or '(untitled)'} · {sid}")
    routine = ""
    if topic["cron_job_id"]:
        job = store.cron_jobs().get(topic["cron_job_id"])
        if job:
            routine = (f"\nThis is an automated ROUTINE: your cron job \"{job.get('name')}\" (id {job['id']}, "
                       f"{store.schedule_text(job)}). To change it, use the cronjob tool on that id.")
    frow = conn.execute("SELECT name, emoji FROM folders WHERE id=?", (topic["folder_id"],)).fetchone() if topic["folder_id"] else None
    return PREAMBLE.format(folder=f"{frow['emoji']} {frow['name']}" if frow else "(none)", topic_id=topic["id"], user=store.USER_NAME, routine=routine, emoji=topic["emoji"], title=topic["title"], status=STATUS_TXT.get(topic["status"], topic["status"]),
                           owner=OWNER_TXT.get(topic["owner"], topic["owner"]), summary=topic["summary"] or "—",
                           next_step=topic["next_step"] or "—", sessions="\n".join(lines) or "(none)", message=message)


async def run_chat(run: ChatRun, query: str, resume: str | None, title: str):
    cmd = [HERMES_BIN, "chat", "--query-file", "-", "--format", "stream-json"]
    if resume:
        cmd += ["--resume", resume]
    text_parts, tools, session_id, final = [], [], resume, None
    try:
        run.proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(Path.home()), limit=16 * 1024 * 1024)
        run.proc.stdin.write(query.encode())
        await run.proc.stdin.drain()
        run.proc.stdin.close()
        await run.push({"type": "status", "text": "Hermes is thinking…"})
        while True:
            line = await run.proc.stdout.readline()
            if not line:
                break
            s = line.decode(errors="replace").strip()
            if not s:
                continue
            if s.startswith("Session:") or s.startswith("session_id:"):
                session_id = s.split(":", 1)[1].strip() or session_id
                continue
            try:
                ev = json.loads(s)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "text":
                text_parts.append(ev.get("text", ""))
                await run.push({"type": "delta", "text": ev.get("text", "")})
            elif t == "tool_use":
                name = ev.get("name") or "outil"
                tools.append(name)
                preview = ev.get("input") or {}
                if isinstance(preview, dict):
                    preview = next((str(v) for v in preview.values() if isinstance(v, (str, int))), "")
                await run.push({"type": "tool", "name": name, "preview": str(preview)[:140]})
                text_parts.append("\n\n")
            elif t == "tool_result":
                await run.push({"type": "tool_done", "name": ev.get("name"), "error": bool(ev.get("is_error"))})
            elif t == "result":
                session_id = ev.get("session_id") or session_id
                final = ev.get("text")
        await run.proc.wait()
        err = (await run.proc.stderr.read()).decode(errors="replace").strip()
        content = (final or "".join(text_parts)).strip()
        if not content:
            content = "⚠️ No answer from Hermes." + (f"\n\n```\n{err[-800:]}\n```" if err else "")
        conn = store.connect()
        try:
            now = time.time()
            conn.execute("INSERT INTO chat_messages(topic_id, role, content, tools, created_at) VALUES(?,?,?,?,?)",
                         (run.topic_id, "assistant", content, json.dumps(tools), now))
            if session_id:
                conn.execute("UPDATE topics SET chat_session_id=?, updated_at=?, last_activity=? WHERE id=?",
                             (session_id, now, now, run.topic_id))
                conn.execute("INSERT OR IGNORE INTO topic_sessions(topic_id, session_id) VALUES(?,?)",
                             (run.topic_id, session_id))
            conn.commit()
        finally:
            conn.close()
        if session_id and not resume:
            await asyncio.create_subprocess_exec(HERMES_BIN, "sessions", "rename", session_id, f"Topic · {title}"[:90],
                                                 stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await run.push({"type": "final", "text": content, "tools": tools})
    except Exception as exc:  # surface any failure in the UI
        await run.push({"type": "final", "text": f"⚠️ Error: {exc}", "tools": tools})
    finally:
        run.done = True
        await run.push({"type": "end"})


# ---------------------------------------------------------------- refresh
REFRESH = {"running": False, "message": "", "error": None}


def _refresh_thread():
    REFRESH.update(running=True, message="Starting…", error=None)
    try:
        indexer.refresh(progress=lambda m: REFRESH.update(message=m))
    except Exception as exc:
        REFRESH.update(error=str(exc), message="Analysis failed")
    finally:
        REFRESH["running"] = False


def start_refresh():
    if REFRESH["running"]:
        return False
    threading.Thread(target=_refresh_thread, daemon=True).start()
    return True


async def nightly(app):
    async def loop():
        while True:
            await asyncio.sleep(600)
            now = datetime.now()
            conn = store.connect()
            last = store.get_meta(conn, "last_refresh", 0)
            conn.close()
            if now.hour == 4 and time.time() - last > 20 * 3600:
                start_refresh()
    task = asyncio.create_task(loop())
    yield
    task.cancel()


# ---------------------------------------------------------------- handlers
def j(data, status=200):
    return web.json_response(data, status=status, dumps=lambda d: json.dumps(d, ensure_ascii=False))


async def list_topics(request):
    conn = store.connect()
    try:
        store.wake_snoozed(conn)
        jobs = store.sync_routines(conn)
        rows = conn.execute("SELECT * FROM topics ORDER BY pinned DESC, coalesce(last_activity, updated_at) DESC").fetchall()
        topics = [store.topic_dict(conn, r) for r in rows]
        for t in topics:
            t["running"] = t["id"] in RUNS and not RUNS[t["id"]].done
            t["cron"] = store.cron_info(jobs[t["cron_job_id"]]) if t.get("cron_job_id") in jobs else None
        folders = [dict(r) for r in conn.execute("SELECT * FROM folders ORDER BY position, created_at")]
        return j({"folders": folders, "topics": topics, "last_refresh": store.get_meta(conn, "last_refresh"), "refresh": REFRESH})
    finally:
        conn.close()


async def create_topic(request):
    body = await request.json()
    title = (body.get("title") or "").strip()
    if not title:
        return j({"error": "title required"}, 400)
    conn = store.connect()
    try:
        tid, now = store.new_topic_id(), time.time()
        conn.execute("INSERT INTO topics(id,title,emoji,status,owner,summary,next_step,created_at,updated_at,last_activity,"
                     "status_changed_at,manual) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
                     (tid, title[:80], body.get("emoji") or "✨", "open", "hermes", "", "", now, now, now, now))
        conn.commit()
        return j(store.topic_dict(conn, conn.execute("SELECT * FROM topics WHERE id=?", (tid,)).fetchone()))
    finally:
        conn.close()


async def get_topic(request):
    tid = request.match_info["id"]
    conn = store.connect()
    try:
        row = conn.execute("SELECT * FROM topics WHERE id=?", (tid,)).fetchone()
        if not row:
            return j({"error": "not found"}, 404)
        t = store.topic_dict(conn, row)
        job = store.cron_jobs().get(t.get("cron_job_id") or "")
        t["cron"] = store.cron_info(job) if job else None
        meta = store.sessions_meta(t["session_ids"])
        t["sessions"] = sorted([meta[s] for s in t["session_ids"] if s in meta],
                               key=lambda m: m["started_at"], reverse=True)
        t["messages"] = [dict(r) for r in conn.execute(
            "SELECT id, role, content, tools, created_at FROM chat_messages WHERE topic_id=? ORDER BY id", (tid,))]
        run = RUNS.get(tid)
        t["running"] = bool(run and not run.done)
        return j(t)
    finally:
        conn.close()


async def patch_topic(request):
    tid = request.match_info["id"]
    body = await request.json()
    conn = store.connect()
    try:
        row = conn.execute("SELECT * FROM topics WHERE id=?", (tid,)).fetchone()
        if not row:
            return j({"error": "not found"}, 404)
        now = time.time()
        sets, vals = ["updated_at=?"], [now]
        if "status" in body and body["status"] in ("open", "snoozed", "done", "dropped"):
            sets += ["status=?", "status_changed_at=?"]
            vals += [body["status"], now]
            if body["status"] == "snoozed":
                sets.append("snooze_until=?")
                vals.append(float(body.get("snooze_until") or now + 7 * 86400))
            else:
                sets.append("snooze_until=NULL")
        if "title" in body and body["title"].strip():
            sets += ["title=?", "manual=1"]
            vals.append(body["title"].strip()[:80])
        if "folder" in body and body["folder"] and str(body["folder"]).strip():
            body["folder_id"] = _folder_by_name(conn, str(body["folder"]).strip())
        if "folder_id" in body:
            fid = body["folder_id"] or None
            if fid and not conn.execute("SELECT 1 FROM folders WHERE id=?", (fid,)).fetchone():
                return j({"error": "unknown folder"}, 400)
            sets.append("folder_id=?")
            vals.append(fid)
        for k in ("owner", "emoji", "next_step", "summary"):
            if k in body:
                sets.append(f"{k}=?")
                vals.append(body[k])
        if "pinned" in body:
            sets.append("pinned=?")
            vals.append(1 if body["pinned"] else 0)
        conn.execute(f"UPDATE topics SET {', '.join(sets)} WHERE id=?", vals + [tid])
        conn.commit()
        return j(store.topic_dict(conn, conn.execute("SELECT * FROM topics WHERE id=?", (tid,)).fetchone()))
    finally:
        conn.close()


def _folder_by_name(conn, name, emoji=None):
    row = conn.execute("SELECT id FROM folders WHERE lower(name)=lower(?)", (name,)).fetchone()
    if row:
        return row["id"]
    fid = "f_" + uuid.uuid4().hex[:8]
    pos = conn.execute("SELECT coalesce(max(position),0)+1 FROM folders").fetchone()[0]
    conn.execute("INSERT INTO folders(id,name,emoji,position,created_at) VALUES(?,?,?,?,?)",
                 (fid, name[:60], emoji or "📁", pos, time.time()))
    return fid


async def list_folders(request):
    conn = store.connect()
    try:
        rows = conn.execute("SELECT f.*, (SELECT count(*) FROM topics t WHERE t.folder_id=f.id) AS topic_count "
                            "FROM folders f ORDER BY position, created_at").fetchall()
        return j({"folders": [dict(r) for r in rows]})
    finally:
        conn.close()


async def create_folder(request):
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return j({"error": "name required"}, 400)
    conn = store.connect()
    try:
        fid = _folder_by_name(conn, name, body.get("emoji"))
        conn.commit()
        return j(dict(conn.execute("SELECT * FROM folders WHERE id=?", (fid,)).fetchone()))
    finally:
        conn.close()


async def patch_folder(request):
    fid = request.match_info["id"]
    body = await request.json()
    conn = store.connect()
    try:
        if not conn.execute("SELECT 1 FROM folders WHERE id=?", (fid,)).fetchone():
            return j({"error": "not found"}, 404)
        for k in ("name", "emoji"):
            if (body.get(k) or "").strip():
                conn.execute(f"UPDATE folders SET {k}=? WHERE id=?", (body[k].strip()[:60], fid))
        conn.commit()
        return j(dict(conn.execute("SELECT * FROM folders WHERE id=?", (fid,)).fetchone()))
    finally:
        conn.close()


async def delete_folder(request):
    """Deleting a folder never deletes topics: they just become unfiled."""
    fid = request.match_info["id"]
    conn = store.connect()
    try:
        conn.execute("UPDATE topics SET folder_id=NULL WHERE folder_id=?", (fid,))
        conn.execute("DELETE FROM folders WHERE id=?", (fid,))
        conn.commit()
        return j({"ok": True})
    finally:
        conn.close()


async def delete_topic(request):
    tid = request.match_info["id"]
    conn = store.connect()
    try:
        conn.execute("DELETE FROM topics WHERE id=?", (tid,))
        conn.execute("DELETE FROM topic_sessions WHERE topic_id=?", (tid,))
        conn.execute("DELETE FROM chat_messages WHERE topic_id=?", (tid,))
        conn.commit()
        return j({"ok": True})
    finally:
        conn.close()


async def post_chat(request):
    tid = request.match_info["id"]
    body = await request.json()
    message = (body.get("message") or "").strip()
    if not message:
        return j({"error": "empty"}, 400)
    if tid in RUNS and not RUNS[tid].done:
        return j({"error": "Hermes is already answering on this topic"}, 409)
    conn = store.connect()
    try:
        topic = conn.execute("SELECT * FROM topics WHERE id=?", (tid,)).fetchone()
        if not topic:
            return j({"error": "not found"}, 404)
        query = _build_query(conn, topic, message)
        now = time.time()
        conn.execute("INSERT INTO chat_messages(topic_id, role, content, created_at) VALUES(?,?,?,?)",
                     (tid, "user", body.get("display") or message, now))
        conn.execute("UPDATE topics SET last_activity=?, updated_at=? WHERE id=?", (now, now, tid))
        conn.commit()
        run = ChatRun(tid)
        RUNS[tid] = run
        asyncio.create_task(run_chat(run, query, topic["chat_session_id"], topic["title"]))
        return j({"ok": True})
    finally:
        conn.close()


async def stream(request):
    tid = request.match_info["id"]
    run = RUNS.get(tid)
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
    await resp.prepare(request)
    if not run:
        await resp.write(b"data: {\"type\":\"end\"}\n\n")
        return resp
    i = 0
    try:
        while True:
            async with run.cond:
                if i >= len(run.events):
                    try:
                        await asyncio.wait_for(run.cond.wait(), timeout=15)
                    except asyncio.TimeoutError:
                        pass
                batch = run.events[i:]
            if not batch:
                await resp.write(b": keepalive\n\n")
                continue
            for ev in batch:
                await resp.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
            i += len(batch)
            if batch[-1].get("type") == "end":
                break
    except (ConnectionResetError, asyncio.CancelledError):
        pass  # the run keeps going; the page can reattach
    return resp


async def stop_chat(request):
    run = RUNS.get(request.match_info["id"])
    if run and run.proc and run.proc.returncode is None:
        run.proc.terminate()
    return j({"ok": True})


async def cron_action(request):
    tid = request.match_info["id"]
    action = (await request.json()).get("action")
    if action not in ("pause", "resume", "run", "remove"):
        return j({"error": "bad action"}, 400)
    conn = store.connect()
    try:
        row = conn.execute("SELECT cron_job_id FROM topics WHERE id=?", (tid,)).fetchone()
    finally:
        conn.close()
    if not row or not row["cron_job_id"]:
        return j({"error": "not a routine"}, 400)
    proc = await asyncio.create_subprocess_exec(HERMES_BIN, "cron", action, row["cron_job_id"],
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    text = out.decode(errors="replace").strip()
    if proc.returncode != 0:
        return j({"error": text[-300:] or "failed"}, 500)
    conn = store.connect()
    try:
        store.sync_routines(conn)
    finally:
        conn.close()
    return j({"ok": True, "output": text[-300:]})


async def session_view(request):
    return j(store.session_transcript(request.match_info["sid"]))


async def refresh_post(request):
    return j({"started": start_refresh(), "refresh": REFRESH})


async def refresh_get(request):
    return j(REFRESH)


async def index(request):
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------- local-only guard
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}


@web.middleware
async def local_only(request, handler):
    """The app can make the agent run commands: refuse DNS-rebinding (Host) and cross-site requests (Origin/CSRF)."""
    if request.host not in ALLOWED_HOSTS:
        return web.Response(status=403, text="forbidden host")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("Origin")
        if origin and origin not in {f"http://{h}" for h in ALLOWED_HOSTS}:
            return web.Response(status=403, text="forbidden origin")
        if request.content_type != "application/json":
            return web.Response(status=415, text="application/json required")
    return await handler(request)


def make_app():
    app = web.Application(middlewares=[local_only])
    app.router.add_get("/", index)
    app.router.add_get("/api/topics", list_topics)
    app.router.add_post("/api/topics", create_topic)
    app.router.add_get("/api/topics/{id}", get_topic)
    app.router.add_patch("/api/topics/{id}", patch_topic)
    app.router.add_delete("/api/topics/{id}", delete_topic)
    app.router.add_post("/api/topics/{id}/chat", post_chat)
    app.router.add_get("/api/topics/{id}/stream", stream)
    app.router.add_post("/api/topics/{id}/stop", stop_chat)
    app.router.add_post("/api/topics/{id}/cron", cron_action)
    app.router.add_get("/api/folders", list_folders)
    app.router.add_post("/api/folders", create_folder)
    app.router.add_patch("/api/folders/{id}", patch_folder)
    app.router.add_delete("/api/folders/{id}", delete_folder)
    app.router.add_get("/api/sessions/{sid}", session_view)
    app.router.add_post("/api/refresh", refresh_post)
    app.router.add_get("/api/refresh", refresh_get)
    app.router.add_static("/static", STATIC)
    app.cleanup_ctx.append(nightly)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host=HOST, port=PORT, print=lambda *_: print(f"Hermes Control Tower → http://{HOST}:{PORT}"))
