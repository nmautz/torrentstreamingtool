# CLAUDE.md

Guidance for Claude Code working in this repo. This file is intentionally **terse**: the real documentation lives in `docs/`. Read the doc that matches your task before exploring source.

---

## ⚠️ Target platform priority — Windows first

**Windows is the primary deployment target. Linux is second. macOS is last (dev convenience only).** This ordering is non-negotiable and overrides any instinct to optimise for the Mac you may be developing on.

Concretely, for every change that touches the OS — launching processes, file paths, service/daemon install, firewall, browser/VLC launch, priorities, signals:

1. **Make it correct on Windows first.** A feature that works on macOS but not Windows is a **bug**, not a partial success. Verify the Windows code path (exe discovery incl. per-user `%LOCALAPPDATA%` installs and the registry, `creationflags`, backslash paths, no reliance on POSIX-only APIs) before considering the task done.
2. **Then Linux** (systemd, `nice`, `/usr/bin` paths, `start_new_session`).
3. **macOS last.** Don't let a macOS-only convenience (or a macOS limitation like the TCC HLS block) shape the design in a way that weakens Windows.

When a capability can't be identical across all three, Windows wins. Note any platform gaps explicitly in the relevant `docs/` file and `docs/GOTCHAS.md`.

---

## ⚠️ Keeping documentation current — read this first

This repo has **two** sources of truth you must keep current as the code changes:

1. **[PLAN.md](PLAN.md)** — the milestone-by-milestone roadmap. Every time you complete, defer, or add a task, update the corresponding checkbox and any inline notes. This is non-negotiable. If you ship a change that closes/opens a milestone, the very next thing you do is edit PLAN.md.
2. **`docs/*.md`** — topic-specific reference docs. When you change behaviour the docs describe (an endpoint signature, a state field, the skip algorithm, the auth flow, etc.), update the relevant doc in the same patch. If you introduce a new gotcha, add it to `docs/GOTCHAS.md`.

Default to editing existing docs. Only create a new `docs/<topic>.md` if a genuinely new subsystem appears that doesn't fit anywhere existing.

---

## Documentation index

Each entry is a short hook so future Claude instances can jump straight to the right file instead of grepping. **Don't explore the codebase before checking the relevant doc.**

| Doc | When to read it |
|-----|-----------------|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Always read this first if you're unfamiliar with the repo. Service topology, process model, code map, lifecycle, key invariants. |
| [docs/BACKEND.md](docs/BACKEND.md) | Working on `main.py`. Section map by line range, `AppState` field reference, background-task descriptions, pipeline flow, qBit/VLC client notes. |
| [docs/FRONTEND.md](docs/FRONTEND.md) | Working on `static/index.html` or `static/admin.html`. HTML section map, JS function list, SSE handlers, render functions, init flow. |
| [docs/API.md](docs/API.md) | Adding/modifying an endpoint, or building a new UI feature that calls one. Every route with method, path, request shape, notes. SSE event catalog. |
| [docs/LIBRARY_DATA.md](docs/LIBRARY_DATA.md) | Touching `library.json` schema (profiles, items, progress, skip_data, settings). Includes the migration logic. |
| [docs/SETUP.md](docs/SETUP.md) | Changing `setup.py` — venv, deps install, qBit ini, SSL cert, service registration. |
| [docs/RUNTIME.md](docs/RUNTIME.md) | Changing `run.py` — venv relaunch, service launchers, LAN/SSID detection, mDNS, firewall, dashboard launch (HTTP + HTTPS). |
| [docs/DAEMON_WATCHDOG.md](docs/DAEMON_WATCHDOG.md) | Working on `daemon.py` (system service install) or `watchdog.py` (crash supervisor + VPN-gated qBit). |
| `updater.py` (top-level) | Auto-updater. Async `git fetch / switch / reset` + non-interactive `setup.py` invoker + `service_is_installed()`. Triggers live in `main.py` (`updater_loop`, `/api/admin/updater/*`, `ENV_KEY_FEATURES`). See [docs/ADMIN.md § Updates](docs/ADMIN.md). |
| [docs/ANALYZER.md](docs/ANALYZER.md) | Touching Smart Skip — `analyzer.py`, the orchestrator in `main.py`, skip-offer UI logic, or the admin editor. Algorithm + thresholds + fallback chain. |
| [docs/ADMIN.md](docs/ADMIN.md) | Working on `/admin` panel — auth flow, HTTPS redirect, Jackett admin auth, the four tabs, content-lock semantics. |
| [docs/STREAMING.md](docs/STREAMING.md) | Working on Stream-to-Device — `/offline-prepare`, `.offline_cache/`, per-row Prep buttons, the local `<video>` player, progress sync. (Successor to the old `OFFLINE.md`.) |
| [docs/YOUTUBE.md](docs/YOUTUBE.md) | Working on YouTube-on-TV — `/api/youtube*`, the Chrome kiosk + `static/tv.html` IFrame player, the `yt_command` SSE relay, the dashboard control routing (`app.youtube_active`). |
| [docs/GOTCHAS.md](docs/GOTCHAS.md) | **Read before any non-trivial change.** VLC ES-ID quirks, qBit sequential-download traps, VPN dual-enforcement, Jackett `Category[]=0`, canonical path matching, etc. |

---

## Quick commands

```bash
python3 setup.py          # first-time configuration (or re-run to refresh)
python3 run.py            # launch all services + dashboard
make setup / make run     # shortcuts

python3 run.py --install  # register as a system service (delegates to daemon.py)
python3 run.py --status   # service status
```

Both `setup.py` and `run.py` must be invoked with the **system** Python — they use `from __future__ import annotations` for 3.9 compatibility. `run.py` `os.execv`s itself into `.venv/bin/python` so the rest of execution runs in the venv.

---

## Versioning — mandatory on every change

The version badge in `static/index.html` (bottom-right corner `<div>`) and the entry in `CHANGELOG.md` **must** be updated in the same patch as any code change.

Scheme: **x.y.z**

| Part | When to bump | Examples |
|------|--------------|---------|
| `x`  | Major feature — new top-level capability, architectural overhaul | new streaming mode, new admin tab |
| `y`  | Minor feature — new user-visible behaviour within an existing subsystem | hold-to-large-step vol, new skip threshold option |
| `z`  | Bug fix — correcting wrong behaviour, no new capability | off-by-one in seek, crash fix |

Current version lives in the `<div>` at the very bottom of `static/index.html`. After bumping, add a bullet to `CHANGELOG.md` under the new version heading.

---

## Style conventions

- **Metro UI** throughout the frontend — flat tiles, no rounded corners, bold uppercase typography, square status dots, no `backdrop-blur`. See [docs/FRONTEND.md](docs/FRONTEND.md).
- Backend uses `asyncio` everywhere — never `time.sleep` inside a request handler or background task. Use `await asyncio.sleep(...)`.
- Library access goes through `get_library()` / `put_library()` (both hold `_lib_lock`) — never read/write `library.json` raw outside that lock.
- VLC track IDs are **ES IDs** from the `"Stream N"` keys, not 1/2/3 counters. See [docs/GOTCHAS.md](docs/GOTCHAS.md).

---

## Working memory: where to put what

- **In-flight task plan / todos** → ephemeral, not persisted (use TodoWrite during work).
- **Roadmap of features** → [PLAN.md](PLAN.md). Update as you finish work.
- **Reference docs about how the system works** → `docs/*.md`. Update alongside code changes.
- **Non-obvious behaviours / footguns discovered during work** → `docs/GOTCHAS.md`.
- **README.md** → user-facing install/quickstart only. Don't put architecture details here; link to `docs/` if needed.

If something doesn't fit any of the above, ask before creating a new file at the repo root.
