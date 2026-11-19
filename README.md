# 🗼 Hermes Control Tower

A small local web app on top of [Hermes Agent](https://github.com/NousResearch/hermes-agent) that turns your scattered conversations with the agent into a **list of topics you can follow, discuss and close**.

When you use an agent every day, you open a lot of threads: a visa application, a flat move, a job search, a refund claim… Some need you, some wait on the agent, some are already done. Control Tower answers one question: *what is still open, and whose move is it?*

## What it does

- **Topic index**: reads your Hermes conversations (desktop, CLI/TUI, Telegram) from `~/.hermes/state.db` and asks Hermes itself to group them into topics. Each topic has a summary, a concrete next step, who has the ball (you, Hermes, a third party) and its linked conversations.
- **Talk about a topic**: every topic has its own chat. The first message gives Hermes the topic context (summary, next step, links to past sessions); follow-ups continue the same Hermes session, with all of its tools, memory and skills. The sessions also show up in Hermes, titled `Topic · …`.
- **Triage**: Done, Snooze (comes back by itself), Pin, Reopen, and **Drop**. Dropping can optionally ask Hermes to list what is still attached to the topic (tasks, cron jobs, drafts) and propose a cleanup — nothing is deleted without your confirmation.
- **Routines**: Hermes cron jobs (daily brief, weekly grocery list…) are not "open topics", so they live in their own section with their schedule, next/last run and last status (failures are flagged). Pause, resume, run now or delete them, and chat with Hermes to tweak one. Conversations about a routine are attached to it instead of creating a new topic.
- **Folders**: group topics by theme (Bali, a dismissal…). Create them from the sidebar, file a topic from its detail view. Hermes can do it too: `PATCH /api/topics/{id}` with `{"folder": "Bali"}` (created if missing), `GET /api/folders`. Deleting a folder never deletes topics. On refresh, new topics (and unfiled ones that get new conversations) are filed automatically into the best-fitting existing folder; a folder you set by hand is never changed.
- **Incremental refresh**: only new or grown conversations are re-read, nightly at 4 am or on demand. A dropped topic only comes back if you talk about it again.

## How it works

```
Browser ──HTTP/SSE──▶ server.py (aiohttp, 127.0.0.1:4747)
                         │
                         ├─ indexer.py ──▶ hermes chat -Q --source tool   (topic extraction, JSON out)
                         ├─ chat       ──▶ hermes chat --format stream-json [--resume <session>]
                         └─ store.py   ──▶ ~/.hermes/control-tower/control-tower.db  (topics)
                                          ~/.hermes/state.db (read-only, Hermes sessions)
```

No API key, no extra service: it drives the `hermes` CLI you already use, so it inherits your provider, model, tools and memory.

## Requirements

- Hermes Agent installed and configured (`hermes` on your `PATH`)
- Python with `aiohttp` (Hermes' own venv already has it: `~/.hermes/hermes-agent/venv/bin/python`)

## Run

```bash
git clone https://github.com/dev-loic/hermes-control-tower
cd hermes-control-tower
HCT_USER_NAME="Your name" ~/.hermes/hermes-agent/venv/bin/python indexer.py   # first index (a few minutes)
HCT_USER_NAME="Your name" ~/.hermes/hermes-agent/venv/bin/python server.py
open http://127.0.0.1:4747
```

### Start at login (macOS)

```bash
./deploy/install-macos.sh "Your name"
```

## Configuration

| Variable | Default | |
|---|---|---|
| `HCT_USER_NAME` | `the user` | Your first name, used in prompts |
| `HCT_LANGUAGE` | `English` | Language of generated titles and summaries |
| `HCT_PORT` | `4747` | HTTP port (bound to 127.0.0.1 only) |
| `HCT_DATA_DIR` | `~/.hermes/control-tower` | Topic database and logs |
| `HERMES_HOME` | `~/.hermes` | Hermes home (reads `state.db`) |
| `HERMES_BIN` | `hermes` on `PATH` | Hermes CLI |

## Limitations

- Chats run Hermes non-interactively: it cannot ask for approval of a dangerous command or show a multiple-choice question there. Use Hermes directly for those.
- Only the active Hermes profile is indexed.
- The server has no authentication and is meant to stay on `127.0.0.1`. Anyone who can reach it can make your agent run commands — do not expose it.

## License

MIT
