"""Hermes Control Tower — local web app to browse, discuss and close the topics you have open with Hermes.

Run: ~/.hermes/hermes-agent/venv/bin/python server.py   (http://127.0.0.1:4747)
"""
import asyncio
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from aiohttp import web

import indexer
import store

HOST, PORT = "127.0.0.1", int(os.environ.get("SUJETS_PORT", "4747"))
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

PREAMBLE = """[Message envoyé depuis Hermes Control Tower — on parle d'un sujet précis]
Sujet : {emoji} {title}
Statut : {status} · À qui la main : {owner}
Où on en est : {summary}
Prochaine étape : {next_step}
Conversations passées sur ce sujet (lis-les avec session_search session_id=… si tu as besoin du détail) :
{sessions}

Message de {user} :
{message}"""

OWNER_FR = {"user": store.USER_NAME, "hermes": "Hermes", "waiting": "on attend un tiers", "none": "personne"}
STATUS_FR = {"open": "en cours", "snoozed": "en pause", "done": "fait", "dropped": "droppé"}


def _build_query(conn, topic, message):
    if topic["chat_session_id"]:
        return message
    sids = [r[0] for r in conn.execute("SELECT session_id FROM topic_sessions WHERE topic_id=?", (topic["id"],))]
    meta = store.sessions_meta(sids)
    lines = []
    for sid in sorted(sids, key=lambda s: meta.get(s, {}).get("started_at") or 0):
        m = meta.get(sid)
        if m:
            day = datetime.fromtimestamp(m["started_at"]).strftime("%d/%m/%Y")
            lines.append(f"- {day} · {m['title'] or '(sans titre)'} · {sid}")
    return PREAMBLE.format(user=store.USER_NAME, emoji=topic["emoji"], title=topic["title"], status=STATUS_FR.get(topic["status"], topic["status"]),
                           owner=OWNER_FR.get(topic["owner"], topic["owner"]), summary=topic["summary"] or "—",
                           next_step=topic["next_step"] or "—", sessions="\n".join(lines) or "(aucune)", message=message)


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
        await run.push({"type": "status", "text": "Hermes réfléchit…"})
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
            content = "⚠️ Pas de réponse de Hermes." + (f"\n\n```\n{err[-800:]}\n```" if err else "")
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
            await asyncio.create_subprocess_exec(HERMES_BIN, "sessions", "rename", session_id, f"Sujet · {title}"[:90],
                                                 stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await run.push({"type": "final", "text": content, "tools": tools})
    except Exception as exc:  # surface any failure in the UI
        await run.push({"type": "final", "text": f"⚠️ Erreur : {exc}", "tools": tools})
    finally:
        run.done = True
        await run.push({"type": "end"})


# ---------------------------------------------------------------- refresh
REFRESH = {"running": False, "message": "", "error": None}


def _refresh_thread():
    REFRESH.update(running=True, message="Démarrage…", error=None)
    try:
        indexer.refresh(progress=lambda m: REFRESH.update(message=m))
    except Exception as exc:
        REFRESH.update(error=str(exc), message="Échec de l'analyse")
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
        rows = conn.execute("SELECT * FROM topics ORDER BY pinned DESC, coalesce(last_activity, updated_at) DESC").fetchall()
        topics = [store.topic_dict(conn, r) for r in rows]
        for t in topics:
            t["running"] = t["id"] in RUNS and not RUNS[t["id"]].done
        return j({"topics": topics, "last_refresh": store.get_meta(conn, "last_refresh"), "refresh": REFRESH})
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
        return j({"error": "Hermes répond déjà sur ce sujet"}, 409)
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
    app.router.add_get("/api/sessions/{sid}", session_view)
    app.router.add_post("/api/refresh", refresh_post)
    app.router.add_get("/api/refresh", refresh_get)
    app.router.add_static("/static", STATIC)
    app.cleanup_ctx.append(nightly)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host=HOST, port=PORT, print=lambda *_: print(f"Hermes Control Tower → http://{HOST}:{PORT}"))
