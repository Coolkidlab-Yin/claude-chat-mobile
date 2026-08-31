# claude-chat

A self-hosted, mobile-friendly web UI for driving your local coding agents like a chat app. Each chat room is one agent session, and opening a room resumes that conversation. Typical setup: run it on your desktop/workstation, reach it from your phone over [Tailscale](https://tailscale.com/) or another private VPN.

Supported engines:

| Engine | Type | Status |
|---|---|---|
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | local agent (reads/writes files, runs commands) | tested |
| [Codex](https://developers.openai.com/codex/cli) | local agent (reads/writes files, runs commands) | tested |
| Grok / Gemini / ChatGPT / DeepSeek / OpenRouter | chat only, via OpenAI-compatible API | **wired up but never exercised — see Limitations** |

Rooms for Claude Code and Codex read the transcripts those tools already write on disk, so your phone and your desktop see the same conversation.

## Security warning — read this before you run it

This tool can hand an AI agent full control of your computer. The defaults are deliberately locked down; **you** decide how far to open them.

**Defaults out of the box:** binds to `127.0.0.1` only (nothing else on your network can reach it), the agent runs in `plan` mode (read-only — it can look and talk, never change anything), and `GET /api/file` can only read the folder this app uploads into. In that state it is roughly as dangerous as a local text editor.

Everything below is about what happens when you loosen those.

1. **Private network only, never the public internet.** Setting `bind_tailscale` puts the server on your tailnet. That is fine for a VPN only you are on. Do not port-forward it, do not give it a public IP, do not run it on a cloud box with an open port, and do not put it behind Tailscale **Funnel** (Funnel publishes to the whole internet; plain `tailscale serve` stays inside your tailnet). Treat "someone can reach this port" as equivalent to "someone is sitting at your keyboard."
2. **Authentication is opt-in, and you should turn it on the moment you leave loopback.** Set `auth_token` in `config.json` (or the `CLAUDE_CHAT_TOKEN` environment variable). Requests from `127.0.0.1` skip the check; everything else must send the token as an `X-Auth-Token` header or a `?token=` query parameter. With no token set and `bind_tailscale` on, anyone who can reach the port has full access — they can read your history, send messages as you, and get the agent to run commands. Also note: behind a reverse proxy every request looks like it came from `127.0.0.1`, so the loopback exemption would let everyone through. Don't put this behind one.
3. **`auto` mode lets the agent act without asking.** In that mode Claude Code runs with `--dangerously-skip-permissions` and Codex with `--dangerously-bypass-approvals-and-sandbox`, so the agent reads files, writes files, and executes shell commands with no prompt. That is what makes "control your computer from your phone" actually work, and it is also how a malicious or careless message does real damage. The alternatives, selectable per room in the in-app Settings sheet: **`edits`** maps to Claude Code's `acceptEdits`, which auto-approves file edits and — per Anthropic's documentation — some filesystem commands too, so it is *not* a guarantee that nothing executes; **`plan`** is genuinely read-only, and is the default. Unknown mode values are rejected outright rather than falling back to something permissive.
4. **`/api/file` and `/api/upload` are only as safe as the folders you expose.** By default `/api/file` serves nothing but this app's own `uploads/` folder. Turning on `allow_home_reads` exposes your **entire home directory** to anyone who can reach the server — including `~/.claude/.credentials.json`, SSH keys, and every private document under it. That is a convenience trade-off, not a safe default; enable it only on a network where you are the only participant. HTML, SVG, XML and JavaScript are always sent as downloads rather than rendered, so a file the agent generated cannot execute as a page inside this app's origin.

If any of this is unacceptable for your situation, don't run this tool — it was built for a single trusted user on a single trusted private network.

## Screenshot

*(Add screenshots here, e.g. `docs/screenshot-list.png` for the chat list and `docs/screenshot-chat.png` for a conversation.)*

## Requirements

- **Windows.** The server currently relies on a few Windows-specific APIs (`ctypes.windll` for process-liveness checks, `taskkill` to stop a run, `CREATE_NO_WINDOW`, and an auto-detected Tailscale executable path). See [Limitations](#limitations) for what porting to macOS/Linux would involve.
- **Python 3.10+** (tested on 3.11).
- **Node.js + the Claude Code CLI**, installed and already logged in — i.e. running `claude` from a terminal works.
- **Tailscale (optional but recommended)** if you want to reach the server from your phone. Without it, the server only binds to `127.0.0.1` (local machine only).

## Installation

```bat
git clone <this-repo-url> claude-chat-mobile
cd claude-chat-mobile
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Regenerating the app icons is optional — the repo already ships with `static/icon-*.png` — but if you want to change the look:

```bat
.venv\Scripts\python make_icon.py
```

## Usage

**Start the server:**

```bat
.venv\Scripts\pythonw.exe server.py
```

or just double-click `start-server.cmd` (starts it hidden, in the background).

**Open it:**

- Locally: `http://127.0.0.1:8899`
- From your phone, over Tailscale: `http://<your-tailscale-ip>:8899` (find your Tailscale IPv4 address with `tailscale ip -4` on the machine running the server)

On iPhone Safari: Share → Add to Home Screen, and it behaves like a standalone app (icon, no browser chrome), backed by the included `manifest.webmanifest`.

**Keep it running:**

- The included `start-server.cmd` is the simplest way to start it manually.
- For "always on," register it as a Windows scheduled task (or any process supervisor you prefer) that runs `pythonw.exe server.py` at logon.
- To restart: find and kill the `pythonw.exe` process bound to port 8899 in Task Manager, then run `start-server.cmd` again (or re-trigger your scheduled task).
- Logs are written to `logs\server.log`.

**Chat rooms:**

The chat list is simply every `~/.claude/projects/*/*.jsonl` file Claude Code has already written to disk, sorted by last activity. Opening a room resumes that session (`claude -p --resume <session-id>`); tapping "+" starts a brand-new session in a project folder you pick.

**Archive & delete:**

Swipe left on a room (or long-press / right-click) to reveal **Archive** and **Delete**.

- *Archive* just hides it from the list — the transcript file is untouched. The mark is stored in `web-archive.json` (created automatically, gitignored). "Show all" in Settings brings archived rooms back into view.
- *Delete* moves the transcript into a local `trash\` folder (created automatically) instead of destroying it. To restore, move the file back into `~\.claude\projects\<project-slug>\`. A room that's currently running a message can't be deleted.

**File preview:**

Windows-style file paths that show up in a reply (e.g. `C:\Users\you\Desktop\screenshot.png`) are automatically turned into inline previews — images render inline, video and audio get a player. Anything that a browser could execute as a page (HTML, SVG, XML, JS) is always sent as a download instead, so a file the agent generated can't run as script inside this app's origin.

This is served by `GET /api/file?path=...`, which only reads from an allow-list:

- **By default the allow-list contains exactly one folder: this app's own `uploads/`.** Nothing else on your disk is reachable through this endpoint.
- To allow more folders, add absolute paths to `extra_file_roots` in `config.json`.
- Setting `allow_home_reads: true` adds your entire home directory. That is convenient — the agent can show you anything it produced anywhere — but it also exposes `~/.claude/.credentials.json`, SSH keys, and every private document under your home folder to whoever can reach the server. Only do this on a network where you are the only participant.

**Sending images to Claude:**

Tap "+" next to the composer to attach a photo (camera or library, multiple at once). It uploads to a local `uploads\` folder (created automatically) and the message text gets a `[phone photo, please use Read to view: <path>]` note appended so Claude knows to look at it. Accepts png/jpg/gif/webp, 25 MB max per file.

**Appearance & per-room settings:**

- Theme: Settings → Appearance (Auto / Light / Dark). All colors are CSS custom properties in `static/style.css` (`:root` for dark, `html[data-theme="light"]` for light).
- Each room has its own model/effort override (top-right pill button in a conversation), stored in `localStorage`; "Default" falls back to the global choice in Settings.
- Voice input: the microphone button uses the Web Speech API; unsupported browsers get a hint to use the keyboard's built-in dictation key instead.
- Usage panel (Settings → "plan limits & usage", collapsed by default): the top half shows your Anthropic plan limits (5-hour / weekly, same source as the desktop app's OAuth usage API, using the local `~/.claude/.credentials.json` token, cached 60s); the bottom half is real token usage computed by scanning your local `.jsonl` transcripts (today / last 7 days, deduplicated by request ID, includes scheduled/subagent runs).
- Context bar at the top of a conversation shows current context usage for that session (turns orange above 70%, red above 85%), estimated from the model name and the last reported token usage.

**Notes:**

- Don't type into the same session from your desktop Claude Code and this phone UI at the same time — the list shows a "desktop open" tag as a reminder.
- Locking your phone doesn't interrupt anything — the run keeps going on the server; reopen the room later to see the result.
- One room can only run one message at a time; the whole server allows at most 4 concurrent runs (`MAX_CONCURRENT_RUNS` in `server.py`).
- For an `https://` URL instead of `http://`, enable Tailscale **Serve** on your tailnet and run `tailscale serve --bg 8899` — optional, and it does not change who can reach the server (Serve stays inside your tailnet). Do not use Tailscale **Funnel**, which would publish it to the open internet.

### Optional: aligning with another session list

If a `desktop-sessions.json` file exists next to `server.py`, the server will use it to override room titles and archive-state, and to fold in rooms it wouldn't otherwise recognize. This is entirely optional — the app works fully from the raw `.jsonl` files alone. The expected shape, if you want to populate it yourself:

```json
{
  "generated_at": 1700000000,
  "sessions": {
    "<session-id>": { "title": "...", "archived": false, "cwd": "C:\\path\\to\\project", "last": 1700000000 }
  }
}
```

This file is git-ignored and never generated automatically by this repo.

## Configuration

Copy `config.example.json` to `config.json` (gitignored) and set what you need. Every key is optional; anything you leave out keeps the safe default.

| Key | Default | What it does |
|---|---|---|
| `bind_tailscale` | `false` | `true` also binds your Tailscale IP so your phone can reach it. Leave it off and only this machine can connect. |
| `auth_token` | `""` (off) | Require this token on every non-loopback request (`X-Auth-Token` header or `?token=`). **Set this whenever `bind_tailscale` is on.** The `CLAUDE_CHAT_TOKEN` environment variable overrides it. |
| `default_mode` | `"plan"` | Permission mode used when the client doesn't specify one: `plan` (read-only), `edits`, or `auto` (no prompts — see security warning 3). |
| `allow_home_reads` | `false` | `true` lets `GET /api/file` read anything under your home directory, credentials included. See security warning 4. |
| `extra_file_roots` | `[]` | Additional folders `GET /api/file` may read from. |

A few constants near the top of `server.py` are also worth knowing: `PORT` (8899), `MAX_CONCURRENT_RUNS` (4 simultaneous agent runs server-wide), `MAX_ROOMS` (250 rooms in the normal list view), and `TAILSCALE_EXE` (where to look for Tailscale when auto-detecting its IP).

## How it works

- **Backend:** a single-file FastAPI app (`server.py`). No database — the chat list is read straight off the `.jsonl` transcript files Claude Code already writes under `~/.claude/projects/`, with an in-memory cache keyed on file size/mtime so re-scanning is cheap.
- **Frontend:** plain HTML/CSS/JS in `static/` — no framework, no build step, no bundler.
- **Sending a message** shells out to `claude -p --resume <session-id> --output-format stream-json --verbose <permission-flags>` and streams the resulting JSON events back to the browser over Server-Sent Events (`GET /api/run/{run_id}/events`), so the UI updates live as Claude works.
- **Network exposure:** the server only binds to `127.0.0.1` plus (if detected) your machine's Tailscale IPv4 address — nothing else. It is not reachable from your regular home Wi-Fi/LAN or the public internet unless you explicitly change `bind_hosts()`.

### Question cards (AskUserQuestion on your phone)

`claude -p` — the mode this app drives sessions with — does **not** expose Claude Code's
built-in `AskUserQuestion` tool at all, so out of the box the model literally cannot ask
you a multiple-choice question and will just guess. This app fills the gap with a
question channel of its own:

1. Every Claude run is spawned with `--mcp-config ask-mcp-config.json`, which loads
   `askuser-mcp.js` — a tiny stdio MCP server exposing one tool, `mcp__chat__ask_user`
   (auto-allowed via `--allowedTools`, and a `--append-system-prompt` note tells the
   model to use it instead of `AskUserQuestion`).
2. When the model calls it, the MCP server POSTs the questions to `/api/ask` and the
   phone renders a tappable option card (single/multi select, free-text field, and a
   skip button) right in the chat.
3. Your tap is returned to the model as the tool result; if you skip or don't answer
   within ~9 minutes, the model is told to proceed with a sensible default and say what
   it assumed.

No setup needed — `ask-mcp-config.json` is regenerated on server start with your
machine's `node` path, and the channel only exists for runs this server spawns (the
desktop app and plain CLI are untouched).

Optional belt-and-suspenders: `hooks/askuser-bridge.js` is a `PreToolUse` hook that
bridges the *built-in* `AskUserQuestion` tool the same way, should a future Claude Code
version add it to `-p` mode. It refuses to run unless the `CLAUDE_CHAT_RUN_ID`
environment variable is set (only server-spawned runs have it), so registering it
globally is safe. To register, add to `hooks.PreToolUse` in `~/.claude/settings.json`:

```json
{
  "matcher": "AskUserQuestion",
  "hooks": [
    { "type": "command", "command": "node \"/path/to/hooks/askuser-bridge.js\"", "timeout": 600 }
  ]
}
```

### Troubleshooting

- **"claude" can't be found / server won't start a run:** the executable lookup logic lives in `resolve_claude_cmd()` near the top of `server.py`. It tries, in order: the Claude Code CLI's own `claude.exe`, then `node` + `cli.js` directly, then falls back to `claude.cmd` via `cmd.exe`. If a Claude Code / npm update moves things, start here.
- **Nothing shows up from your phone:** confirm Tailscale is installed, running, and logged into the same tailnet as your phone; confirm `tailscale ip -4` on the server machine returns a `100.x.x.x` address; confirm you're using that IP (not `127.0.0.1`) from the phone.

## Limitations

- **The five API-key engines have never actually been exercised.** Grok, Gemini, ChatGPT, DeepSeek and OpenRouter are wired up — the engine picker, the "no key set" gate, and the key/model settings UI all work — but no real request has ever been sent through `run_api()`. The `/chat/completions` streaming parse, its error handling, and the `api-chats/` transcript writing are correct on paper and untested in practice. Claude Code and Codex are the two that have actually been used. Bug reports welcome.
- **Windows only, as written.** Porting to macOS/Linux means replacing the `ctypes.windll`-based process-liveness check, the `taskkill` call used to stop a run, `CREATE_NO_WINDOW`, and the Tailscale executable auto-detection (or dropping/reimplementing that last one for the target platform).
- **Authentication is a single shared token, and it is off by default.** There are no accounts, no roles, and no per-user anything: either you know the token or you don't (and requests from `127.0.0.1` skip the check entirely). That is enough for one person on a private network and nowhere near enough for multi-user or internet-facing use.
- **No built-in HTTPS.** Use `tailscale serve` if you want a TLS-terminated URL.
- **Single machine, single user.** There's no concept of accounts; concurrency is capped globally (`MAX_CONCURRENT_RUNS`), not per-user.
- **Reads transcripts from disk on every poll.** Cached by file size/mtime, so it stays cheap up to a few hundred sessions, but it wasn't built to scale past that.

## License

MIT — see [LICENSE](LICENSE).
