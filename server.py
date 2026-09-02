# -*- coding: utf-8 -*-
"""claude-chat — 用聊天室介面操作 Claude Code sessions（手機經 Tailscale 使用）。

聊天室 = ~/.claude/projects/<slug>/<session-id>.jsonl
送訊息 = 對該 session 跑 `claude -p --resume <sid>`，stream-json 事件經 SSE 推給前端。
只綁 127.0.0.1 與 Tailscale IP，家用區網與外網碰不到。
"""
import asyncio
import ctypes
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

HOME = Path.home()
PROJECTS_DIR = HOME / ".claude" / "projects"
LIVE_DIR = HOME / ".claude" / "sessions"
BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
LOG_FILE = BASE / "logs" / "server.log"
PORT = 8899
TAILSCALE_EXE = r"C:\Program Files\Tailscale\tailscale.exe"
TAILSCALE_WAIT = 120   # 開機時最多等 Tailscale 幾秒


log = logging.getLogger("claude-chat")

CONFIG_FILE = BASE / "config.json"

# 預設一律走最保守的設定：只綁 127.0.0.1、AI 不能自己動手、只讀自己產生的檔案。
# 要放寬就在 config.json 明確打開（見 README 的 Configuration）。
CONFIG_DEFAULTS = {
    "bind_tailscale": False,   # True = 也綁 Tailscale IP，手機才連得到
    "auth_token": "",          # 綁網路時強烈建議設；空字串 = 不驗證
    "default_mode": "plan",    # plan / edits / auto，auto 等於讓 AI 免詢問執行指令
    "extra_file_roots": [],    # /api/file 額外開放的資料夾，預設只開上傳目錄
    "allow_home_reads": False, # True = /api/file 可讀整個家目錄（含憑證，危險）
    "desktop_sync": "manual",  # auto = 手機開的新對話做完第一輪就自動登錄進桌面 app；manual = 只在按鈕按下時；off = 不用
}


def load_config():
    cfg = dict(CONFIG_DEFAULTS)
    try:
        user = json.loads(CONFIG_FILE.read_text("utf-8"))
        if isinstance(user, dict):
            cfg.update({k: v for k, v in user.items() if k in CONFIG_DEFAULTS})
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("config.json 讀不到或格式壞掉，改用預設值：%s", e)
    env_token = os.environ.get("CLAUDE_CHAT_TOKEN", "").strip()
    if env_token:
        cfg["auth_token"] = env_token
    return cfg


CONFIG = load_config()


def bind_hosts():
    """預設只綁 127.0.0.1。要讓手機連得到才打開 bind_tailscale。"""
    hosts = ["127.0.0.1"]
    if not CONFIG["bind_tailscale"]:
        return hosts
    # 登入時排程比 Tailscale 先起來，第一次查會查不到 → 最多等 2 分鐘再放棄
    # （09-02 中招：18:40 重新登入後只綁了 127.0.0.1，手機整晚連不上）
    deadline = time.time() + TAILSCALE_WAIT
    while True:
        try:
            out = subprocess.run([TAILSCALE_EXE, "ip", "-4"], capture_output=True,
                                 text=True, timeout=10,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
            ip = out.stdout.strip().splitlines()[0].strip() if out.stdout.strip() else ""
            if ip.startswith("100."):
                hosts.append(ip)
                return hosts
        except Exception as e:
            log.warning("查 Tailscale IP 失敗：%s", e)
        if time.time() >= deadline:
            log.warning("等了 %d 秒還是找不到 Tailscale IP，只綁 127.0.0.1", TAILSCALE_WAIT)
            return hosts
        time.sleep(3)
MAX_ROOMS = 250
MAX_CONCURRENT_RUNS = 4
HEAD_BYTES = 128 * 1024
TAIL_BYTES = 256 * 1024
MAX_HISTORY_BYTES = 64 * 1024 * 1024



# ---------- claude 執行檔解析 ----------

def resolve_claude_cmd():
    """優先直接用 node + cli.js（避開 .cmd 的 cmd.exe 引號地雷）。"""
    npm = HOME / "AppData" / "Roaming" / "npm"
    exe = npm / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    if exe.exists():
        return [str(exe)]
    cli = npm / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
    node = shutil.which("node")
    if not node:
        pf = Path(r"C:\Program Files\nodejs\node.exe")
        node = str(pf) if pf.exists() else None
    if node and cli.exists():
        return [node, str(cli)]
    cmd = shutil.which("claude.cmd") or str(npm / "claude.cmd")
    return ["cmd.exe", "/d", "/c", cmd]


CLAUDE_ARGV = resolve_claude_cmd()

# ---------- AskUserQuestion 手機橋接 ----------
# claude -p 的工具清單裡「沒有」內建 AskUserQuestion，模型根本無從問使用者選擇題。
# 解法：每個手機 run 掛一個自製 MCP 工具 mcp__chat__ask_user 當提問通道——
# 模型呼叫它 → askuser-mcp.js POST /api/ask → 手機顯示選項卡 → 答案當工具結果回去。
ASK_MCP_JS = BASE / "askuser-mcp.js"
ASK_MCP_CONFIG = BASE / "ask-mcp-config.json"
ASK_SYSTEM_PROMPT = (
    "你正在使用者的手機聊天介面（claude-chat）裡執行。需要問使用者選擇題"
    "（釐清模糊需求、做決定、在多個做法中挑一個）時，呼叫 mcp__chat__ask_user 工具："
    "手機會顯示可點選的選項卡，使用者的選擇會當成工具結果回傳給你。"
    "不要呼叫 AskUserQuestion（這個環境沒有那個工具），"
    "也不要用純文字列出選項乾等回覆。"
)


def _write_ask_mcp_config():
    """把 MCP 設定檔落地（node 路徑因機器而異，啟動時現算）。"""
    if not ASK_MCP_JS.exists():
        return False
    node = shutil.which("node")
    if not node:
        pf = Path(r"C:\Program Files\nodejs\node.exe")
        node = str(pf) if pf.exists() else "node"
    cfg = {"mcpServers": {"chat": {"command": node, "args": [str(ASK_MCP_JS)]}}}
    ASK_MCP_CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


ASK_MCP_READY = _write_ask_mcp_config()

# ---------- 逐項授權（「先問我」模式）手機橋接 ----------
# -p 在 default 權限模式下，沒被允許的工具會直接被拒。掛一個 PreToolUse hook：
# 要動手（Bash/Edit/Write…）前先 POST /api/perm → 手機跳「允許／拒絕」卡 → 決定當 hook 結果。
# 這份 settings 只在 ask 模式用 --settings 帶進去，桌面 app 與一般 CLI 完全不受影響。
PERM_BRIDGE_JS = BASE / "perm-bridge.js"
PERM_SETTINGS = BASE / "perm-settings.json"
PERM_MATCHER = "Bash|Edit|Write|MultiEdit|NotebookEdit|WebFetch|mcp__.*"


def _write_perm_settings():
    if not PERM_BRIDGE_JS.exists():
        return False
    node = shutil.which("node")
    if not node:
        pf = Path(r"C:\Program Files\nodejs\node.exe")
        node = str(pf) if pf.exists() else "node"
    cfg = {"hooks": {"PreToolUse": [{
        "matcher": PERM_MATCHER,
        "hooks": [{"type": "command", "command": f'"{node}" "{PERM_BRIDGE_JS}"', "timeout": 600}],
    }]}}
    PERM_SETTINGS.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


PERM_READY = _write_perm_settings()

# ---------- 桌面 app 的 session 登錄 ----------
# 桌面 app 把每個 session 存成一個 json（Windows 在 %APPDATA%\Claude\claude-code-sessions\<帳號>\<組織>\local_<id>.json），
# 裡面的 cliSessionId 就是 ~/.claude/projects 那個 jsonl 的檔名 —— 兩邊靠這個對齊，不用再猜時間。
# 手機開的對話要進桌面 app：桌面 app 有註冊 claude:// 協定，claude://resume?session=<cli sid> 會把
# 磁碟上的 jsonl 匯進登錄（同一份紀錄，不複製），之後兩邊都寫同一個檔。直接寫登錄 json 桌面 app 不會即時吃到，實測過。
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_desktop_reg_cache = {"at": 0.0, "data": {}}
DESKTOP_REG_TTL = 5.0


def _desktop_reg_dirs():
    cands = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        cands.append(Path(appdata) / "Claude" / "claude-code-sessions")
    cands.append(HOME / "Library" / "Application Support" / "Claude" / "claude-code-sessions")
    return [p for p in cands if p.is_dir()]


def desktop_registry():
    """cli session id -> {local_id, title, archived, cwd, last(epoch 秒)}；沒有桌面 app 就是空 dict。"""
    now = time.time()
    if now - _desktop_reg_cache["at"] < DESKTOP_REG_TTL:
        return _desktop_reg_cache["data"]
    out = {}
    for root in _desktop_reg_dirs():
        for f in root.glob("*/*/local_*.json"):
            try:
                d = json.loads(f.read_text("utf-8"))
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            local_id = d.get("sessionId") or f.stem
            cli = d.get("cliSessionId") or local_id.replace("local_", "", 1)
            last = d.get("lastActivityAt") or d.get("createdAt") or 0
            out[cli] = {
                "local_id": local_id,
                "title": _clean_title(d.get("title") or ""),
                "archived": bool(d.get("isArchived")),
                "cwd": d.get("cwd") or "",
                "last": (last / 1000.0) if isinstance(last, (int, float)) else 0,
            }
    _desktop_reg_cache.update(at=now, data=out)
    return out


def desktop_app_available():
    """桌面 app 有沒有裝（看 claude:// 協定有沒有註冊）。"""
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\claude\shell\open\command"):
                return True
        except OSError:
            return False
    if sys.platform == "darwin":
        return (Path("/Applications/Claude.app").exists()
                or (HOME / "Applications" / "Claude.app").exists())
    return False


def desktop_open(sid):
    """把一個 CLI session 匯進桌面 app 並切過去；已經在登錄裡的就只是切過去。
    回 'imported' / 'opened' / 'unavailable'。"""
    if not sid or not _UUID_RE.match(sid):
        raise ValueError("session id 格式不對")
    if not desktop_app_available():
        return "unavailable"
    reg = desktop_registry().get(sid)
    if reg:
        url = f"claude://code/continue?session={reg['local_id']}"
    else:
        url = f"claude://resume?session={sid}"
    if sys.platform == "win32":
        os.startfile(url)  # noqa: S606 - 交給系統的協定處理器（桌面 app）
    else:
        subprocess.Popen(["open", url])
    _desktop_reg_cache["at"] = 0.0
    return "opened" if reg else "imported"

CODEX_EXE = shutil.which("codex.cmd") or str(HOME / "AppData" / "Roaming" / "npm" / "codex.cmd")
CODEX_SESSIONS = HOME / ".codex" / "sessions"
API_CHATS = BASE / "api-chats"
KEYS_FILE = BASE / "api-keys.json"

# 引擎：cli = 本機 agent（能讀寫檔案跑指令）；api = 純聊天（貼 key 就能用）
ENGINES = {
    "claude": {"label": "Claude Code", "kind": "cli", "icon": "✳", "note": "能改檔案、跑指令"},
    "codex":  {"label": "Codex", "kind": "cli", "icon": "◆", "note": "OpenAI 的 agent，能改檔案、跑指令"},
    "grok":   {"label": "Grok", "kind": "api", "icon": "𝕏", "base": "https://api.x.ai/v1",
               "model": "grok-4-latest", "note": "純聊天，需要 x.ai 的 API key"},
    "gemini": {"label": "Gemini", "kind": "api", "icon": "✦",
               "base": "https://generativelanguage.googleapis.com/v1beta/openai",
               "model": "gemini-2.5-pro", "note": "純聊天，需要 Google AI Studio 的 API key"},
    "openai": {"label": "ChatGPT", "kind": "api", "icon": "◎", "base": "https://api.openai.com/v1",
               "model": "gpt-5", "note": "純聊天，需要 OpenAI 的 API key"},
    "deepseek": {"label": "DeepSeek", "kind": "api", "icon": "◇", "base": "https://api.deepseek.com/v1",
                 "model": "deepseek-chat", "note": "純聊天，需要 DeepSeek 的 API key"},
    "openrouter": {"label": "OpenRouter", "kind": "api", "icon": "⇄", "base": "https://openrouter.ai/api/v1",
                   "model": "openai/gpt-5", "note": "一把 key 通多家模型，型號可自己填"},
}
API_SLUG = {e: "api-" + e for e, d in ENGINES.items() if d["kind"] == "api"}
SLUG_ENGINE = {v: k for k, v in API_SLUG.items()}
SLUG_ENGINE["codex"] = "codex"


def load_keys():
    try:
        return json.loads(KEYS_FILE.read_text("utf-8"))
    except Exception:
        return {}


def save_keys(d):
    KEYS_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")


PERMISSION_FLAGS = {
    "auto": ["--dangerously-skip-permissions"],
    "edits": ["--permission-mode", "acceptEdits"],
    "plan": ["--permission-mode", "plan"],
    "ask": [],
}


# ---------- jsonl 解析 ----------

META_PREFIXES = (
    "<local-command", "<command-name", "Caveat:", "<system-reminder",
    "<task-notification", "[Request interrupted",
)


def _loads(line):
    try:
        return json.loads(line)
    except Exception:
        return None


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
        return "\n".join(parts)
    return ""


def _is_meta_user(rec, text):
    if rec.get("isMeta") or rec.get("isCompactSummary"):
        return True
    t = text.lstrip()
    if not t:
        return True
    return t.startswith(META_PREFIXES) or t.startswith("This session is being continued")


def _tool_detail(name, inp):
    if not isinstance(inp, dict):
        return ""
    for k in ("command", "file_path", "pattern", "description", "prompt",
              "url", "query", "skill", "title"):
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().replace("\n", " ")[:160]
    return ""


def _clean_title(t):
    t = re.sub(r"\s+", " ", t or "").strip()
    return t[:64] if t else ""


def items_from_message(role, message, ts=None, tool_status=None):
    """jsonl 記錄與 stream-json 事件共用的正規化。"""
    items = []
    content = (message or {}).get("content")
    if isinstance(content, str):
        if content.strip():
            items.append({"role": role, "kind": "text", "text": content, "ts": ts})
        return items
    if not isinstance(content, list):
        return items
    for b in content:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "text":
            if (b.get("text") or "").strip():
                items.append({"role": role, "kind": "text", "text": b["text"], "ts": ts})
        elif bt == "tool_use":
            name = b.get("name", "?")
            if name.startswith("mcp__"):
                name = name.split("__")[-1]
            item = {"role": "assistant", "kind": "tool", "tool": name,
                    "detail": _tool_detail(b.get("name"), b.get("input")),
                    "tool_use_id": b.get("id"), "ok": None, "ts": ts}
            if tool_status is not None and b.get("id") in tool_status:
                item["ok"] = tool_status[b.get("id")]
            items.append(item)
    return items


def _iter_tail_lines(path, max_bytes):
    size = path.stat().st_size
    with open(path, "rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
            f.readline()  # 丟掉可能被切半的行
        data = f.read()
    return data.decode("utf-8", "replace").splitlines()


def _head_info(path):
    """讀檔頭：cwd、entrypoint、標題、是否為 compact 後的世代。

    標題優先用 `custom-title`（就是桌面 app 顯示的那個名字，寫在檔案第一行）。
    退回「第一句提問」只在沒有 custom-title 時 —— 對 compact 後產生的檔，
    第一句提問是使用者當下講的那句話，拿來當標題會變成滿滿的自言自語。
    """
    info = {"cwd": None, "title": "", "entry": None,
            "custom_title": "", "compacted": False}
    try:
        with open(path, "rb") as f:
            data = f.read(HEAD_BYTES)
        lines = data.decode("utf-8", "replace").splitlines()
    except OSError:
        return info
    first_q = first_user = ""
    for line in lines:
        rec = _loads(line)
        if not rec:
            continue
        if info["cwd"] is None and isinstance(rec.get("cwd"), str):
            info["cwd"] = rec["cwd"]
        if info["entry"] is None and isinstance(rec.get("entrypoint"), str):
            info["entry"] = rec["entrypoint"]
        if not info["custom_title"] and rec.get("type") == "custom-title":
            ct = rec.get("customTitle")
            if isinstance(ct, str) and ct.strip():
                info["custom_title"] = _clean_title(ct)
        if not info["compacted"] and rec.get("compactMetadata"):
            info["compacted"] = True
        if not first_q and rec.get("type") == "queue-operation":
            c = rec.get("content")
            if isinstance(c, str) and c.strip():
                first_q = _clean_title(c)
        if not first_user and rec.get("type") == "user":
            t = _text_of((rec.get("message") or {}).get("content"))
            if t and not _is_meta_user(rec, t):
                first_user = _clean_title(t)
        if info["cwd"] and info["entry"] and info["custom_title"] and info["compacted"]:
            break
    info["title"] = info["custom_title"] or first_q or first_user
    return info


def _tail_info(path):
    """讀檔尾：最後訊息預覽、最後時間。"""
    info = {"preview": "", "ts": None}
    try:
        lines = _iter_tail_lines(path, TAIL_BYTES)
    except OSError:
        return info
    for line in reversed(lines):
        rec = _loads(line)
        if not rec:
            continue
        if info["ts"] is None and isinstance(rec.get("timestamp"), str):
            info["ts"] = rec["timestamp"]
        if rec.get("isSidechain"):
            continue
        rt = rec.get("type")
        if rt == "assistant":
            t = _text_of((rec.get("message") or {}).get("content"))
            if t.strip():
                info["preview"] = _clean_title(t)
                break
        elif rt == "user":
            t = _text_of((rec.get("message") or {}).get("content"))
            if t.strip() and not _is_meta_user(rec, t):
                info["preview"] = "你：" + _clean_title(t)
                break
    return info


# ---------- 房間列表（含快取） ----------

_room_cache = {}  # str(path) -> (mtime_ns, size, room_dict)


def _pid_alive(pid):
    try:
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
    except Exception:
        pass
    return False


def live_session_ids():
    out = set()
    try:
        for p in LIVE_DIR.glob("*.json"):
            try:
                d = json.loads(p.read_text("utf-8"))
            except Exception:
                continue
            sid, pid = d.get("sessionId"), d.get("pid")
            if sid and pid and _pid_alive(pid):
                out.add(sid)
    except OSError:
        pass
    return out


SNAP_FILE = BASE / "desktop-sessions.json"
MATCH_TOLERANCE = 180.0
_snap_cache = {"mtime": None, "data": None}


def _iso_epoch(iso):
    if not iso:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _norm_cwd(p):
    return (p or "").replace("/", "\\").rstrip("\\").casefold()


def _slug_encode(p):
    return re.sub(r"[^A-Za-z0-9]", "-", p or "")


def _project_name(cwd, slug):
    """worktree 顯示上層專案名；沒 cwd 時退回 slug 尾段。"""
    if not cwd:
        return slug.split("-")[-1] if slug else "?"
    parts = cwd.rstrip("\\/").replace("/", "\\").split("\\")
    if ".claude" in parts:
        i = parts.index(".claude")
        if 0 < i and i + 1 < len(parts) and parts[i + 1] == "worktrees":
            return parts[i - 1]
    return parts[-1]


WEB_ARCHIVE = BASE / "web-archive.json"
TRASH_DIR = BASE / "trash"
APP_SESSIONS = BASE / "app-sessions.json"
_overlay_cache = {"mtime": None, "data": {}}
_app_sids_cache = {"mtime": None, "data": set()}


def load_app_sids():
    """這個 App 自己開過的對話 id。

    從這裡送出的訊息是用 `claude -p` 跑的，Claude Code 記下來的 entrypoint 會是
    sdk-cli — 跟排程機器人同一類。少了這份名單，使用者在手機上開的新對話
    會被 sdk-cli 那條過濾規則一起藏掉，開完就從列表上消失。
    """
    try:
        m = APP_SESSIONS.stat().st_mtime
    except OSError:
        return set()
    if _app_sids_cache["mtime"] != m:
        try:
            data = json.loads(APP_SESSIONS.read_text("utf-8"))
            _app_sids_cache["data"] = set(data) if isinstance(data, list) else set()
            _app_sids_cache["mtime"] = m
        except Exception:
            return set()
    return _app_sids_cache["data"]


def remember_app_sid(sid):
    if not sid:
        return
    sids = set(load_app_sids())
    if sid in sids:
        return
    sids.add(sid)
    tmp = APP_SESSIONS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sorted(sids), indent=1), "utf-8")
    os.replace(tmp, APP_SESSIONS)
    _app_sids_cache["mtime"] = None


TITLES_FILE = BASE / "web-titles.json"   # 手機上改的聊天室名字 {sid: title}
_titles_cache = {"mtime": None, "data": {}}


def load_titles():
    try:
        m = TITLES_FILE.stat().st_mtime
    except OSError:
        return {}
    if _titles_cache["mtime"] != m:
        try:
            d = json.loads(TITLES_FILE.read_text("utf-8"))
            _titles_cache["data"] = d if isinstance(d, dict) else {}
            _titles_cache["mtime"] = m
        except Exception:
            return {}
    return _titles_cache["data"]


def save_title(sid, title):
    d = dict(load_titles())
    if title:
        d[sid] = title
    else:
        d.pop(sid, None)
    tmp = TITLES_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")
    os.replace(tmp, TITLES_FILE)
    _titles_cache["mtime"] = None


def load_overlay():
    """網頁端的封存標記：{sid: "archived"|"active"}，疊在桌面快照之上。"""
    try:
        m = WEB_ARCHIVE.stat().st_mtime
    except OSError:
        return {}
    if _overlay_cache["mtime"] != m:
        try:
            _overlay_cache["data"] = json.loads(WEB_ARCHIVE.read_text("utf-8"))
            _overlay_cache["mtime"] = m
        except Exception:
            return {}
    return _overlay_cache["data"] or {}


def save_overlay(d):
    # 先寫暫存再原子替換：中途斷電也不會留下半個檔案讓下次讀取整份失效
    tmp = WEB_ARCHIVE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")
    os.replace(tmp, WEB_ARCHIVE)
    _overlay_cache["mtime"] = None


def load_snapshot():
    """桌面 app 的 session 登錄（標題 + 封存狀態）。

    有桌面 app 的機器直接讀它的登錄 json（即時、用 cliSessionId 精準對齊）；
    沒有才退回手動匯出的 desktop-sessions.json 快照。
    """
    reg = desktop_registry()
    if reg:
        return {"generated_at": time.time(), "live": True,
                "sessions": {sid: {"title": r["title"], "archived": r["archived"],
                                   "cwd": r["cwd"], "last": r["last"]}
                             for sid, r in reg.items()}}
    empty = {"generated_at": 0, "sessions": {}}
    try:
        m = SNAP_FILE.stat().st_mtime
    except OSError:
        return empty
    if _snap_cache["mtime"] != m:
        try:
            _snap_cache["data"] = json.loads(SNAP_FILE.read_text("utf-8"))
            _snap_cache["mtime"] = m
        except Exception:
            log.warning("snapshot unreadable")
            return empty
    return _snap_cache["data"] or empty


def _file_infos():
    infos = []
    try:
        for proj in PROJECTS_DIR.iterdir():
            if not proj.is_dir():
                continue
            for f in proj.glob("*.jsonl"):
                if f.name.startswith("agent-"):
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                if st.st_size < 200:
                    continue
                key = str(f)
                cached = _room_cache.get(key)
                if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
                    info = dict(cached[2])
                else:
                    head = _head_info(f)
                    tail = _tail_info(f)
                    cwd = head["cwd"] or ""
                    info = {
                        "slug": proj.name,
                        "sid": f.stem,
                        "project": cwd,
                        "project_name": (cwd.rstrip("\\/").split("\\")[-1] if cwd else proj.name),
                        "title": head["title"] or "（沒有標題）",
                        "entry": head["entry"] or "cli",
                        "custom_title": head["custom_title"],
                        "compacted": head["compacted"],
                        "preview": tail["preview"],
                        "ts": tail["ts"],
                        "last_epoch": _iso_epoch(tail["ts"]) or st.st_mtime,
                        "mtime": st.st_mtime,
                    }
                    _room_cache[key] = (st.st_mtime_ns, st.st_size, dict(info))
                infos.append(info)
    except OSError:
        pass
    return infos


_codex_cache = {}


def _codex_info(f):
    """從 codex rollout jsonl 抽 cwd/標題/預覽（只讀 event_msg 層，跳過系統提示）。"""
    info = {"cwd": "", "title": "", "preview": "", "ts": None, "sid": ""}
    try:
        lines = f.read_bytes().decode("utf-8", "replace").splitlines()
    except OSError:
        return info
    last_user = last_ai = ""
    for line in lines:
        rec = _loads(line)
        if not rec:
            continue
        pl = rec.get("payload") or {}
        t = rec.get("type")
        if t == "session_meta":
            info["cwd"] = pl.get("cwd") or ""
            info["sid"] = pl.get("session_id") or pl.get("id") or ""
        elif t == "event_msg":
            pt = pl.get("type")
            if pt == "user_message":
                m = (pl.get("message") or "").strip()
                if m and not m.startswith("<"):
                    if not info["title"]:
                        info["title"] = _clean_title(m)
                    last_user = m
                    last_ai = ""
            elif pt == "agent_message":
                last_ai = (pl.get("message") or "").strip()
        if rec.get("timestamp"):
            info["ts"] = rec["timestamp"]
    info["preview"] = _clean_title(last_ai) if last_ai else ("你：" + _clean_title(last_user) if last_user else "")
    return info


def codex_rooms():
    rooms = []
    if not CODEX_SESSIONS.exists():
        return rooms
    for f in CODEX_SESSIONS.rglob("rollout-*.jsonl"):
        try:
            st = f.stat()
        except OSError:
            continue
        if st.st_size < 400:
            continue
        key = str(f)
        cached = _codex_cache.get(key)
        if not (cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size):
            info = _codex_info(f)
            if not info["sid"] or not info["title"]:
                info = None
            cached = (st.st_mtime_ns, st.st_size, info)
            _codex_cache[key] = cached
        info = cached[2]
        if not info:
            continue
        cwd = info["cwd"]
        rooms.append({
            "engine": "codex", "slug": "codex", "sid": info["sid"], "path": str(f),
            "project": cwd, "project_name": _project_name(cwd, "codex"),
            "title": info["title"], "preview": info["preview"], "ts": info["ts"],
            "last_epoch": _iso_epoch(info["ts"]) or st.st_mtime, "mtime": st.st_mtime,
            "archived": False, "live": False, "entry": "codex",
        })
    return rooms


def api_rooms():
    rooms = []
    if not API_CHATS.exists():
        return rooms
    for eng_dir in API_CHATS.iterdir():
        eng = eng_dir.name
        if eng not in ENGINES or not eng_dir.is_dir():
            continue
        for f in eng_dir.glob("*.jsonl"):
            try:
                st = f.stat()
            except OSError:
                continue
            title = preview = ""
            ts = None
            model = ENGINES[eng]["model"]
            try:
                for line in f.read_bytes().decode("utf-8", "replace").splitlines():
                    r = _loads(line)
                    if not r:
                        continue
                    ts = r.get("ts") or ts
                    model = r.get("model") or model
                    txt = (r.get("content") or "").strip()
                    if not txt:
                        continue
                    if r.get("role") == "user":
                        if not title:
                            title = _clean_title(txt)
                        preview = "你：" + _clean_title(txt)
                    else:
                        preview = _clean_title(txt)
            except OSError:
                continue
            if not title:
                continue
            rooms.append({
                "engine": eng, "slug": API_SLUG[eng], "sid": f.stem, "path": str(f),
                "project": "", "project_name": ENGINES[eng]["label"], "model": model,
                "title": title, "preview": preview, "ts": ts,
                "last_epoch": _iso_epoch(ts) or st.st_mtime, "mtime": st.st_mtime,
                "archived": False, "live": False, "entry": "api",
            })
    return rooms


def _merge_compact_generations(infos):
    """把同一場對話的多個 compact 世代併成一列。

    每次 /compact（或自動壓縮）Claude Code 都會換一個新的 session id、
    另開一個 jsonl，把壓縮後的完整歷史複製進去。不合併的話，一場聊了整天、
    compact 過五次的對話會在列表上長成五間，而且每一間的標題都是使用者
    當時講的第一句話 —— 看起來就像「每講一句話就開一間」。

    合併鍵用 (custom-title, cwd)：custom-title 是桌面 app 顯示的名字，同一場
    對話的所有世代都一樣。沒有 custom-title 的檔（早期版本、CLI 開的）維持獨立，
    不靠猜的欄位去併，寧可少併也不要把兩場不同的對話併成一場。
    """
    groups = {}
    out = []
    for fi in infos:
        ct = fi.get("custom_title")
        if not ct:
            out.append(fi)
            continue
        groups.setdefault((ct, _norm_cwd(fi.get("project"))), []).append(fi)

    for gen_list in groups.values():
        if len(gen_list) == 1:
            out.append(gen_list[0])
            continue
        # 最新世代代表這場對話：點進去接續的必須是它，接到舊世代等於回到 compact 前
        gen_list.sort(key=lambda x: x["last_epoch"], reverse=True)
        newest = dict(gen_list[0])
        newest["generations"] = len(gen_list)
        newest["gen_sids"] = [g["sid"] for g in gen_list]
        out.append(newest)
    return out


def scan_rooms(show_all=False):
    """檔案 × 桌面登錄 對齊：
    - 檔名直接命中登錄 id → 綁定
    - 否則用 (專案路徑, 最後活動時間±180s) 最近鄰配對
    可見 = 桌面未封存 + 一般 CLI 對話 + 快照之後的新桌面對話；
    隱藏 = 已封存、排程機器人(sdk-cli)、舊桌面殘檔。
    """
    infos = _merge_compact_generations(_file_infos())
    snap = load_snapshot()
    sessions = snap.get("sessions", {})
    gen_at = snap.get("generated_at", 0)

    # slug 反查 cwd（有些檔頭讀不到 cwd）
    slug_map = {}
    for reg in sessions.values():
        if reg.get("cwd"):
            slug_map.setdefault(_slug_encode(reg["cwd"]), reg["cwd"])
    for fi in infos:
        if fi["project"]:
            slug_map.setdefault(_slug_encode(fi["project"]), fi["project"])
    for fi in infos:
        if not fi["project"] and fi["slug"] in slug_map:
            fi["project"] = slug_map[fi["slug"]]
        fi["project_name"] = _project_name(fi["project"], fi["slug"])

    assigned = {}          # file sid -> registry uuid
    used_reg = set()
    for fi in infos:
        if fi["sid"] in sessions:
            assigned[fi["sid"]] = fi["sid"]
            used_reg.add(fi["sid"])

    cands = []
    for fi in infos:
        if fi["sid"] in assigned or not fi["last_epoch"]:
            continue
        ncwd = _norm_cwd(fi["project"])
        for rid, reg in sessions.items():
            if rid in used_reg or _norm_cwd(reg.get("cwd")) != ncwd:
                continue
            d = abs(fi["last_epoch"] - reg.get("last", 0))
            if d <= MATCH_TOLERANCE:
                cands.append((d, fi["sid"], rid))
    cands.sort()
    for d, fsid, rid in cands:
        if fsid in assigned or rid in used_reg:
            continue
        assigned[fsid] = rid
        used_reg.add(rid)

    live = live_session_ids()
    overlay = load_overlay()
    app_sids = load_app_sids()
    rooms = []
    for fi in infos:
        room = dict(fi)
        # 合併過的列要用整組世代來判斷，不能只看代表那一代的 sid
        sids = fi.get("gen_sids") or [fi["sid"]]
        rid = next((assigned[x] for x in sids if x in assigned), None)
        reg = sessions.get(rid) if rid else None
        archived = bool(reg and reg.get("archived"))
        if reg and reg.get("title") and not fi.get("custom_title"):
            # custom-title 是桌面 app 當下顯示的名字，比快照裡的舊標題新
            room["title"] = reg["title"]
        # 來源規則（先不管封存）
        if any(x in app_sids for x in sids):
            # 這個 App 自己開的對話。它的 entrypoint 也是 sdk-cli，
            # 所以要排在那條過濾規則前面，否則使用者一開新對話就看不到了
            base = True
        elif reg:
            base = True
        elif fi["entry"] == "sdk-cli":
            base = False
        elif fi["entry"] == "claude-desktop":
            base = fi["last_epoch"] > gen_at - 3600
        else:
            base = True
        # 網頁端封存標記覆蓋桌面快照（任一世代被標記就算數）
        ov = next((overlay[x] for x in sids if x in overlay), None)
        if ov == "archived":
            archived = True
        elif ov == "active":
            # 明確解封＝使用者說「我要看到這個」，連上面的來源規則一起蓋掉。
            # 這是被自動規則藏錯的對話唯一的救回方式。
            archived = False
            base = True
        room["archived"] = archived
        visible = base and not archived
        if not visible and not show_all:
            continue
        room["hidden"] = not visible
        room["live"] = any(x in live for x in sids)
        room["engine"] = "claude"
        room["desktop"] = bool(reg)                      # 桌面 app 登錄裡有它
        room["app"] = any(x in app_sids for x in sids)   # 是從手機開的
        rooms.append(room)

    for extra in codex_rooms() + api_rooms():
        ov = overlay.get(extra["sid"])
        extra["archived"] = ov == "archived"
        if extra["archived"] and not show_all:
            continue
        rooms.append(extra)

    titles = load_titles()
    for room in rooms:
        room["running"] = room["sid"] in BY_SESSION
        t = titles.get(room["sid"])
        if t:
            room["title"] = t
    rooms.sort(key=lambda r: r["last_epoch"], reverse=True)
    return rooms[: (900 if show_all else MAX_ROOMS + 120)]


def _safe_name(v, pattern):
    """單一路徑片段的檢查：形狀對，而且不是 . 或 ..（會跳出目錄）。"""
    return bool(re.fullmatch(pattern, v or "")) and v not in (".", "..")


def find_room_file(slug, sid):
    if not _safe_name(slug, r"[A-Za-z0-9._-]+") or not _safe_name(sid, r"[A-Za-z0-9-]+"):
        raise HTTPException(400, "壞掉的參數")
    eng = SLUG_ENGINE.get(slug)
    if eng == "codex":
        for r in codex_rooms():
            if r["sid"] == sid:
                return Path(r["path"])
        raise HTTPException(404, "找不到這個 Codex 對話")
    if eng:
        f = API_CHATS / eng / (sid + ".jsonl")
        if not f.exists():
            raise HTTPException(404, "找不到這個聊天室")
        return f
    f = PROJECTS_DIR / slug / (sid + ".jsonl")
    if not f.exists():
        raise HTTPException(404, "找不到這個聊天室")
    return f


def codex_history(path, limit=120):
    """Codex rollout → 聊天泡泡（走 event_msg 層）。"""
    items = []
    ctx = None
    try:
        lines = path.read_bytes().decode("utf-8", "replace").splitlines()
    except OSError:
        return {"items": [], "oldest": 0, "more": False, "context": None}
    win = 0
    for idx, line in enumerate(lines):
        rec = _loads(line)
        if not rec or rec.get("type") != "event_msg":
            continue
        pl = rec.get("payload") or {}
        pt = pl.get("type")
        ts = rec.get("timestamp")
        if pt == "user_message":
            m = (pl.get("message") or "").strip()
            if m and not m.startswith("<"):
                items.append({"i": idx, "role": "user", "kind": "text", "text": m, "ts": ts})
        elif pt == "agent_message":
            m = (pl.get("message") or "").strip()
            if m:
                items.append({"i": idx, "role": "assistant", "kind": "text", "text": m, "ts": ts})
        elif pt == "agent_reasoning" or pt == "exec_command_begin":
            cmd = pl.get("command") or pl.get("text") or ""
            if pt == "exec_command_begin" and cmd:
                items.append({"i": idx, "role": "assistant", "kind": "tool", "tool": "Shell",
                              "detail": (" ".join(cmd) if isinstance(cmd, list) else str(cmd))[:160],
                              "ok": True, "ts": ts})
        elif pt == "task_started":
            win = pl.get("model_context_window") or win
        elif pt == "token_count":
            info = pl.get("info") or {}
            tot = info.get("total_token_usage") or {}
            tok = (tot.get("input_tokens") or 0) + (tot.get("cached_input_tokens") or 0)
            if tok and win:
                ctx = {"tokens": tok, "window": win, "pct": round(tok * 100 / win)}
    more = len(items) > limit
    return {"items": items[-limit:], "oldest": 0, "more": more, "context": ctx}


def api_history(path, limit=200):
    items = []
    try:
        lines = path.read_bytes().decode("utf-8", "replace").splitlines()
    except OSError:
        lines = []
    for idx, line in enumerate(lines):
        r = _loads(line)
        if not r or not (r.get("content") or "").strip():
            continue
        items.append({"i": idx, "role": r.get("role", "assistant"), "kind": "text",
                      "text": r["content"], "ts": r.get("ts")})
    return {"items": items[-limit:], "oldest": 0, "more": len(items) > limit, "context": None}


# ---------- 歷史訊息 ----------

def load_history(path, before=None, limit=120):
    size = path.stat().st_size
    with open(path, "rb") as f:
        if size > MAX_HISTORY_BYTES:
            f.seek(size - MAX_HISTORY_BYTES)
            f.readline()
        lines = f.read().decode("utf-8", "replace").splitlines()

    tool_status = {}
    items = []
    context = None
    reached_start = True
    for idx in range(len(lines) - 1, -1, -1):
        if before is not None and idx >= before:
            continue
        rec = _loads(lines[idx])
        if not rec or rec.get("isSidechain"):
            continue
        rt = rec.get("type")
        ts = rec.get("timestamp")
        if rt == "user":
            msg = rec.get("message") or {}
            content = msg.get("content")
            # 先收集 tool_result 狀態
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        tool_status[b.get("tool_use_id")] = not b.get("is_error", False)
            text = _text_of(content)
            if rec.get("isCompactSummary"):
                items.append({"i": idx, "role": "user", "kind": "info",
                              "text": text, "label": "前情摘要", "ts": ts})
                continue
            if text.strip() and not _is_meta_user(rec, text):
                items.append({"i": idx, "role": "user", "kind": "text", "text": text, "ts": ts})
        elif rt == "assistant":
            msg = rec.get("message") or {}
            if context is None and isinstance(msg.get("usage"), dict):
                u = msg["usage"]
                tok = ((u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
                       + (u.get("cache_creation_input_tokens") or 0))
                if tok > 0:
                    win = _ctx_window(msg.get("model"), tok)
                    context = {"tokens": tok, "window": win,
                               "pct": round(tok * 100 / win)}
            got = items_from_message("assistant", msg, ts, tool_status)
            for it in reversed(got):
                it["i"] = idx
                items.append(it)
        if len(items) >= limit:
            reached_start = False
            break
    items.reverse()
    return {"items": items, "oldest": (items[0]["i"] if items else 0),
            "more": not reached_start, "context": context}


# ---------- 執行 claude ----------

async def run_codex(run, text, mode):
    """Codex：codex exec --json（新對話）或 codex exec resume <id>（續聊）。"""
    argv = [CODEX_EXE, "exec", "--json", "--skip-git-repo-check", "-C", run.cwd]
    argv += ["-s", "danger-full-access" if mode == "auto" else "read-only"]
    if mode == "auto":
        argv.append("--dangerously-bypass-approvals-and-sandbox")
    if run.sid:
        argv += ["resume", run.sid, "-"]
    else:
        argv.append("-")
    stderr_tail = ""
    done_sent = False
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=run.cwd, limit=16 * 1024 * 1024,
            creationflags=subprocess.CREATE_NO_WINDOW)
        run.proc = proc
        proc.stdin.write(text.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

        async def rerr():
            nonlocal stderr_tail
            stderr_tail = (await proc.stderr.read()).decode("utf-8", "replace")[-1500:]
        err_task = asyncio.create_task(rerr())

        win = 0
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            ev = _loads(line.decode("utf-8", "replace"))
            if not ev:
                continue
            et = ev.get("type")
            if et == "thread.started":
                if not run.sid and ev.get("thread_id"):
                    run.sid = ev["thread_id"]
                    BY_SESSION[run.sid] = run.id
                await _emit(run, {"kind": "init", "sid": run.sid})
            elif et == "item.completed":
                item = ev.get("item") or {}
                it = item.get("type")
                if it == "agent_message" and (item.get("text") or "").strip():
                    await _emit(run, {"role": "assistant", "kind": "text", "text": item["text"]})
                elif it == "command_execution":
                    await _emit(run, {"role": "assistant", "kind": "tool", "tool": "Shell",
                                      "detail": str(item.get("command", ""))[:160],
                                      "ok": item.get("exit_code", 0) == 0})
                elif it in ("file_change", "patch_apply"):
                    await _emit(run, {"role": "assistant", "kind": "tool", "tool": "改檔案",
                                      "detail": str(item.get("path", ""))[:160], "ok": True})
                elif it == "error" and item.get("message"):
                    log.info("codex note: %s", item["message"][:200])
            elif et == "turn.started":
                win = ev.get("model_context_window") or win
            elif et == "turn.completed":
                u = ev.get("usage") or {}
                tok = (u.get("input_tokens") or 0) + (u.get("cached_input_tokens") or 0)
                if tok:
                    w = win or 258400
                    await _emit(run, {"kind": "ctx", "tokens": tok, "window": w,
                                      "pct": round(tok * 100 / w)})
                await _emit(run, {"kind": "done", "ok": True, "sid": run.sid, "error": ""})
                done_sent = True
            elif et == "turn.failed":
                err = ((ev.get("error") or {}).get("message") or "Codex 這一輪失敗")[:400]
                await _emit(run, {"kind": "done", "ok": False, "sid": run.sid, "error": err})
                done_sent = True
        await proc.wait()
        await err_task
        if not done_sent:
            msg = stderr_tail.strip() or f"Codex 結束但沒有回覆（exit {proc.returncode}）"
            await _emit(run, {"kind": "done", "ok": False, "sid": run.sid, "error": msg[-400:]})
    except Exception as e:
        log.exception("run_codex failed")
        await _emit(run, {"kind": "done", "ok": False, "sid": run.sid,
                          "error": f"{type(e).__name__}: {e}"})
    finally:
        if run.sid and BY_SESSION.get(run.sid) == run.id:
            BY_SESSION.pop(run.sid, None)
        asyncio.get_event_loop().create_task(_gc_run(run.id))


def _api_append(eng, sid, role, content, model=None):
    d = API_CHATS / eng
    d.mkdir(parents=True, exist_ok=True)
    rec = {"role": role, "content": content,
           "ts": __import__("datetime").datetime.now().astimezone().isoformat()}
    if model:
        rec["model"] = model
    with open(d / (sid + ".jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


async def run_api(run, text, engine, model):
    """OpenAI 相容的 API 引擎（Grok/Gemini/ChatGPT/DeepSeek/OpenRouter），串流回覆。"""
    import urllib.error
    import urllib.request
    spec = ENGINES[engine]
    key = load_keys().get(engine, {}).get("key", "")
    model = model or load_keys().get(engine, {}).get("model") or spec["model"]
    if not key:
        await _emit(run, {"kind": "done", "ok": False, "sid": run.sid,
                          "error": "還沒設定 " + spec["label"] + " 的 API key（設定 → 其他 AI）"})
        return
    if not run.sid:
        run.sid = uuid.uuid4().hex[:16]
        BY_SESSION[run.sid] = run.id
        await _emit(run, {"kind": "init", "sid": run.sid})

    msgs = []
    f = API_CHATS / engine / (run.sid + ".jsonl")
    if f.exists():
        for line in f.read_bytes().decode("utf-8", "replace").splitlines():
            r = _loads(line)
            if r and r.get("content"):
                msgs.append({"role": r.get("role", "user"), "content": r["content"]})
    msgs = msgs[-30:] + [{"role": "user", "content": text}]
    _api_append(engine, run.sid, "user", text, model)

    body = json.dumps({"model": model, "messages": msgs, "stream": True}).encode()
    req = urllib.request.Request(
        spec["base"].rstrip("/") + "/chat/completions", data=body,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})

    acc = []
    loop = asyncio.get_event_loop()
    try:
        def pump():
            out = []
            with urllib.request.urlopen(req, timeout=180) as resp:
                for raw in resp:
                    s = raw.decode("utf-8", "replace").strip()
                    if not s.startswith("data:"):
                        continue
                    payload = s[5:].strip()
                    if payload == "[DONE]":
                        break
                    d = _loads(payload)
                    if not d:
                        continue
                    for ch in d.get("choices") or []:
                        piece = (ch.get("delta") or {}).get("content")
                        if piece:
                            out.append(piece)
            return "".join(out)

        full = await loop.run_in_executor(None, pump)
        acc.append(full)
        if full.strip():
            await _emit(run, {"role": "assistant", "kind": "text", "text": full})
            _api_append(engine, run.sid, "assistant", full, model)
        await _emit(run, {"kind": "done", "ok": True, "sid": run.sid, "error": ""})
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        msg = ("API key 被拒絕，檢查一下是不是貼錯或過期" if e.code in (401, 403)
               else f"HTTP {e.code}：{detail}")
        await _emit(run, {"kind": "done", "ok": False, "sid": run.sid, "error": msg})
    except Exception as e:
        await _emit(run, {"kind": "done", "ok": False, "sid": run.sid,
                          "error": f"{type(e).__name__}: {e}"})
    finally:
        if run.sid and BY_SESSION.get(run.sid) == run.id:
            BY_SESSION.pop(run.sid, None)
        asyncio.get_event_loop().create_task(_gc_run(run.id))


class Run:
    def __init__(self, run_id, slug, sid, cwd):
        self.id = run_id
        self.slug = slug
        self.sid = sid
        self.cwd = cwd
        self.events = []
        self.cond = asyncio.Condition()
        self.done = False
        self.proc = None
        self.started = time.time()
        self.is_new = sid is None     # 這一輪是不是從手機開的新對話（做完要不要登錄進桌面 app）
        self.allow_all = False        # 「先問我」模式下使用者按了「這次工作全部允許」


RUNS = {}          # run_id -> Run
BY_SESSION = {}    # sid -> run_id


async def _emit(run, ev):
    async with run.cond:
        run.events.append(ev)
        if ev.get("kind") == "done":
            run.done = True
        run.cond.notify_all()


async def _gc_run(run_id, delay=900):
    await asyncio.sleep(delay)
    RUNS.pop(run_id, None)


async def run_claude(run, text, mode, extra_args=()):
    argv = CLAUDE_ARGV + ["-p", "--output-format", "stream-json", "--verbose"]
    # 送訊息的入口已經擋過未知模式，這裡的預設值只是雙保險：退到最安全的那個
    argv += PERMISSION_FLAGS.get(mode, PERMISSION_FLAGS["plan"])
    if ASK_MCP_READY:
        # 提問通道要在任何權限模式下都免詢問（-p 沒有 UI，詢問等於直接被拒）
        argv += [
            "--mcp-config", str(ASK_MCP_CONFIG),
            "--allowedTools", "mcp__chat__ask_user",
            "--append-system-prompt", ASK_SYSTEM_PROMPT,
        ]
    if mode == "ask" and PERM_READY:
        # 逐項授權：default 權限模式 + 手機橋接 hook（沒有 hook 的話 -p 會把每個工具直接拒掉）
        argv += ["--settings", str(PERM_SETTINGS)]
    argv += list(extra_args)
    if run.sid:
        argv += ["--resume", run.sid]
    stderr_tail = ""
    got_done = False
    try:
        # 讓 AskUserQuestion 橋接 hook 認得這是本 App 開的 run（hook 沒讀到這兩個
        # 變數會直接放行，桌面與一般 CLI 完全不受影響）
        child_env = dict(os.environ)
        child_env["CLAUDE_CHAT_RUN_ID"] = run.id
        child_env["CLAUDE_CHAT_PORT"] = str(PORT)
        # ask_user 要等真人在手機上點選，工具呼叫逾時放寬到 10 分鐘
        child_env["MCP_TOOL_TIMEOUT"] = "600000"
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=run.cwd,
            env=child_env,
            limit=16 * 1024 * 1024,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        run.proc = proc
        proc.stdin.write(text.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

        async def read_stderr():
            nonlocal stderr_tail
            data = await proc.stderr.read()
            stderr_tail = data.decode("utf-8", "replace")[-2000:]

        err_task = asyncio.create_task(read_stderr())

        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            ev = _loads(line.decode("utf-8", "replace"))
            if not ev:
                continue
            et = ev.get("type")
            if et == "system" and ev.get("subtype") == "init":
                if not run.sid and ev.get("session_id"):
                    run.sid = ev["session_id"]
                    BY_SESSION[run.sid] = run.id
                # 記下來，不然這場新對話會被 sdk-cli 的過濾規則藏掉
                try:
                    remember_app_sid(run.sid)
                except Exception as e:
                    log.warning("記錄 app session id 失敗：%s", e)
                await _emit(run, {"kind": "init", "sid": run.sid})
            elif et == "assistant":
                msg = ev.get("message") or {}
                for it in items_from_message("assistant", msg):
                    await _emit(run, it)
                u = msg.get("usage")
                if isinstance(u, dict):
                    tok = ((u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
                           + (u.get("cache_creation_input_tokens") or 0))
                    if tok > 0:
                        win = _ctx_window(msg.get("model"), tok)
                        await _emit(run, {"kind": "ctx", "tokens": tok, "window": win,
                                          "pct": round(tok * 100 / win)})
            elif et == "user":
                content = (ev.get("message") or {}).get("content")
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            await _emit(run, {"kind": "tool_ok",
                                              "tool_use_id": b.get("tool_use_id"),
                                              "ok": not b.get("is_error", False)})
            elif et == "result":
                got_done = True
                await _emit(run, {
                    "kind": "done",
                    "ok": ev.get("subtype") == "success",
                    "sid": ev.get("session_id") or run.sid,
                    "error": (ev.get("result") or "")[:500] if ev.get("is_error") else "",
                    "duration_ms": ev.get("duration_ms"),
                })
        await proc.wait()
        await err_task
        if not got_done:
            msg = stderr_tail.strip() or f"claude 結束了但沒有回傳結果（exit {proc.returncode}）"
            await _emit(run, {"kind": "done", "ok": False, "sid": run.sid, "error": msg[-500:]})
        elif run.is_new and run.sid and CONFIG["desktop_sync"] == "auto":
            # 手機開的新對話做完第一輪就登錄進桌面 app（桌面 app 會切到這個 session，一次而已）
            try:
                res = await asyncio.to_thread(desktop_open, run.sid)
                log.info("desktop sync %s -> %s", run.sid, res)
            except Exception as e:
                log.warning("desktop sync failed for %s: %s", run.sid, e)
    except Exception as e:
        log.exception("run_claude failed")
        await _emit(run, {"kind": "done", "ok": False, "sid": run.sid,
                          "error": f"{type(e).__name__}: {e}"})
    finally:
        if run.sid and BY_SESSION.get(run.sid) == run.id:
            BY_SESSION.pop(run.sid, None)
        asyncio.get_event_loop().create_task(_gc_run(run.id))


# ---------- API ----------

app = FastAPI(title="claude-chat")


@app.middleware("http")
async def require_token(request: Request, call_next):
    """設了 auth_token 就每個請求都要帶。來自本機的連線放行（本機＝已經坐在電腦前）。"""
    token = CONFIG["auth_token"]
    if token:
        client = request.client.host if request.client else ""
        if client not in ("127.0.0.1", "::1"):
            given = (request.headers.get("x-auth-token")
                     or request.query_params.get("token") or "")
            if not secrets.compare_digest(given, token):
                return JSONResponse({"detail": "沒有權限"}, status_code=401)
    return await call_next(request)


MODELS = {
    "fable": "claude-fable-5",
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5-20251001",
}
EFFORTS = {"low", "medium", "high", "max"}


class SendBody(BaseModel):
    text: str
    slug: str | None = None
    sid: str | None = None
    project: str | None = None
    mode: str = ""          # 空字串 = 用 config.json 的 default_mode
    model: str | None = None
    effort: str | None = None
    engine: str | None = None


@app.get("/api/health")
def health():
    return {"ok": True, "claude": CLAUDE_ARGV[0], "projects": PROJECTS_DIR.exists(),
            "desktop_app": desktop_app_available(),
            "desktop_registry": bool(desktop_registry()),
            "desktop_sync": CONFIG["desktop_sync"],
            "perm_ready": PERM_READY}


class SettingsBody(BaseModel):
    desktop_sync: str | None = None


@app.post("/api/settings")
def set_settings(body: SettingsBody):
    """手機上能改的伺服器設定（目前只有桌面同步方式），寫回 config.json。"""
    if body.desktop_sync is not None:
        if body.desktop_sync not in ("auto", "manual", "off"):
            raise HTTPException(400, "desktop_sync 只能是 auto / manual / off")
        CONFIG["desktop_sync"] = body.desktop_sync
        try:
            user = json.loads(CONFIG_FILE.read_text("utf-8"))
            if not isinstance(user, dict):
                user = {}
        except Exception:
            user = {}
        user["desktop_sync"] = body.desktop_sync
        tmp = CONFIG_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(user, ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, CONFIG_FILE)
    return {"ok": True, "desktop_sync": CONFIG["desktop_sync"]}


UPLOAD_DIR = BASE / "uploads"
UPLOAD_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp",
               # 桌面 app 能附 PDF/文件，手機也開放這幾種（AI 用 Read 工具讀）
               ".pdf", ".txt", ".md", ".csv", ".json"}
DOC_EXTS = {".pdf", ".txt", ".md", ".csv", ".json"}
UPLOAD_MAX = 25 * 1024 * 1024
UPLOAD_QUOTA = 2 * 1024 * 1024 * 1024   # 上傳資料夾總量上限
UPLOAD_CHUNK = 256 * 1024

# 圖片的檔頭魔術位元組：只看副檔名擋不住把任意檔案改名成 .png
IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"RIFF")

# 這些副檔名交給瀏覽器 inline 顯示會變成「以本服務的身分執行的網頁」，一律當附件下載
ACTIVE_CONTENT_EXTS = {".html", ".htm", ".svg", ".xhtml", ".xml", ".mhtml", ".js", ".mjs"}
INLINE_OK_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".mov", ".webm",
                  ".m4v", ".mp3", ".wav", ".m4a", ".pdf", ".txt", ".md", ".csv", ".json"}


def _build_allowed_roots():
    """預設只開放本程式自己的上傳目錄。家目錄要明確在 config 打開才給。"""
    roots = [str(UPLOAD_DIR.resolve()).casefold().rstrip("\\") + "\\"]
    if CONFIG["allow_home_reads"]:
        roots.append(str(HOME).casefold().rstrip("\\") + "\\")
    for p in CONFIG["extra_file_roots"]:
        try:
            roots.append(str(Path(p).resolve()).casefold().rstrip("\\") + "\\")
        except Exception:
            log.warning("extra_file_roots 裡有無效路徑，略過：%s", p)
    return tuple(dict.fromkeys(roots))


ALLOWED_FILE_ROOTS = _build_allowed_roots()


def _upload_dir_size():
    try:
        return sum(f.stat().st_size for f in UPLOAD_DIR.glob("*") if f.is_file())
    except OSError:
        return 0


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    """手機上傳截圖/照片，回傳本機路徑（訊息裡引用，AI 用 Read 看圖）。"""
    ext = Path(file.filename or "").suffix.lower()
    if ext not in UPLOAD_EXTS:
        raise HTTPException(400, "只收圖片（png/jpg/gif/webp）或文件（pdf/txt/md/csv/json）")
    UPLOAD_DIR.mkdir(exist_ok=True)
    if _upload_dir_size() >= UPLOAD_QUOTA:
        raise HTTPException(507, "上傳資料夾已滿，先清一下 uploads/")

    name = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:12] + ext
    dest = UPLOAD_DIR / name
    total = 0
    head = b""
    try:
        # 邊收邊寫邊算，不要先整包讀進記憶體再檢查大小
        with open(dest, "wb") as out:
            while True:
                chunk = await file.read(UPLOAD_CHUNK)
                if not chunk:
                    break
                if not head:
                    head = chunk[:16]
                total += len(chunk)
                if total > UPLOAD_MAX:
                    raise HTTPException(413, "圖片太大（上限 25MB）")
                out.write(chunk)
        if total == 0:
            raise HTTPException(400, "空檔案")
        if ext == ".pdf":
            if not head.startswith(b"%PDF"):
                raise HTTPException(400, "這個檔案的內容不是 PDF")
        elif ext in DOC_EXTS:
            if b"\x00" in head:
                raise HTTPException(400, "這個檔案不是純文字")
        elif not head.startswith(IMAGE_MAGIC):
            raise HTTPException(400, "這個檔案的內容不是圖片")
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    except Exception:
        dest.unlink(missing_ok=True)
        log.exception("upload failed")
        raise HTTPException(500, "存檔失敗")
    return {"path": str(dest), "name": name, "size": total}


@app.get("/api/file")
def serve_file(path: str):
    """把本機檔案端給手機看（影片/圖片預覽用）。開放範圍由 config 決定。"""
    p = Path(path)
    if not p.is_absolute():
        raise HTTPException(400, "要用完整路徑")
    try:
        rp = p.resolve(strict=True)
    except OSError:
        raise HTTPException(404, "檔案不存在")
    low = str(rp).casefold()
    if not any(low.startswith(root) for root in ALLOWED_FILE_ROOTS):
        raise HTTPException(403, "這個位置不開放")
    if not rp.is_file():
        raise HTTPException(404, "不是檔案")

    ext = rp.suffix.lower()
    if ext in ACTIVE_CONTENT_EXTS or ext not in INLINE_OK_EXTS:
        # filename 一設，Starlette 會加 Content-Disposition: attachment，
        # 瀏覽器就不會把它當同源網頁執行
        return FileResponse(str(rp), filename=rp.name,
                            media_type="application/octet-stream")
    return FileResponse(str(rp))


@app.get("/api/rooms")
def rooms(all: int = 0):
    return {"rooms": scan_rooms(show_all=bool(all))}


@app.get("/api/projects")
def projects():
    seen = {}
    for r in scan_rooms():
        p = r.get("project")
        if p and p not in seen and Path(p).is_dir():
            seen[p] = {"path": p, "name": r["project_name"], "slug": r["slug"]}
    return {"projects": list(seen.values())}


@app.get("/api/engines")
def engines():
    keys = load_keys()
    out = []
    for eid, spec in ENGINES.items():
        row = {"id": eid, "label": spec["label"], "kind": spec["kind"],
               "icon": spec["icon"], "note": spec["note"]}
        if spec["kind"] == "api":
            row["has_key"] = bool(keys.get(eid, {}).get("key"))
            row["model"] = keys.get(eid, {}).get("model") or spec["model"]
        else:
            row["ready"] = True if eid == "claude" else Path(CODEX_EXE).exists()
        out.append(row)
    return {"engines": out}


class KeyBody(BaseModel):
    key: str | None = None
    model: str | None = None


@app.post("/api/engines/{eid}/key")
def set_key(eid: str, body: KeyBody):
    if eid not in ENGINES or ENGINES[eid]["kind"] != "api":
        raise HTTPException(400, "沒有這個引擎")
    keys = load_keys()
    cur = dict(keys.get(eid) or {})
    if body.key is not None:
        k = body.key.strip()
        if k:
            cur["key"] = k
        else:
            cur.pop("key", None)
    if body.model is not None:
        m = body.model.strip()
        if m:
            cur["model"] = m
        else:
            cur.pop("model", None)
    keys[eid] = cur
    save_keys(keys)
    return {"ok": True, "has_key": bool(cur.get("key")),
            "model": cur.get("model") or ENGINES[eid]["model"]}


@app.get("/api/history/{slug}/{sid}")
def history(slug: str, sid: str, before: int | None = None, limit: int = 120):
    f = find_room_file(slug, sid)
    eng = SLUG_ENGINE.get(slug)
    if eng == "codex":
        out = codex_history(f, limit=min(limit, 400))
    elif eng:
        out = api_history(f)
    else:
        out = load_history(f, before=before, limit=min(limit, 400))
    out["running_run_id"] = BY_SESSION.get(sid)
    run = RUNS.get(out["running_run_id"]) if out["running_run_id"] else None
    out["n_events"] = len(run.events) if run else 0
    return out


def _ctx_window(model, tokens):
    """上下文視窗估算：Fable 走 1M，其他 200k；超過 190k 一律視為 1M。"""
    if "fable" in (model or "").lower() or tokens > 190_000:
        return 1_000_000
    return 200_000


_limits_cache = {"ts": 0.0, "data": None}
LIMIT_LABELS = {"session": "5 小時限額", "weekly_all": "每週 · 全模型"}


@app.get("/api/limits")
def limits():
    """方案額度（跟桌面 app 同一個來源：Anthropic OAuth usage API），快取 60 秒。"""
    import urllib.error
    import urllib.request
    if _limits_cache["data"] and time.time() - _limits_cache["ts"] < 60:
        return _limits_cache["data"]
    try:
        cred = json.loads((HOME / ".claude" / ".credentials.json").read_text("utf-8"))
        tok = cred.get("claudeAiOauth", {}).get("accessToken", "")
        if not tok:
            return {"ok": False, "error": "本機沒有登入憑證"}
        req = urllib.request.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={"Authorization": "Bearer " + tok,
                     "anthropic-beta": "oauth-2025-04-20",
                     "Content-Type": "application/json",
                     "User-Agent": "claude-chat/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        msg = "憑證過期了，在電腦上用一次 claude 就會自動刷新" if e.code == 401 else f"HTTP {e.code}"
        return {"ok": False, "error": msg}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    rows = []
    for lim in raw.get("limits") or []:
        label = LIMIT_LABELS.get(lim.get("kind"))
        if not label:
            scope = ((lim.get("scope") or {}).get("model") or {}).get("display_name")
            label = "每週 · " + scope if scope else (lim.get("kind") or "?")
        rows.append({"label": label, "percent": lim.get("percent", 0),
                     "resets_at": lim.get("resets_at"),
                     "severity": lim.get("severity", "normal")})
    credits = None
    ex = raw.get("extra_usage") or {}
    if ex.get("is_enabled"):
        dp = 10 ** ex.get("decimal_places", 2)
        credits = {"used": (ex.get("used_credits") or 0) / dp,
                   "limit": (ex.get("monthly_limit") or 0) / dp,
                   "currency": ex.get("currency", "USD")}
    out = {"ok": True, "limits": rows, "credits": credits}
    _limits_cache.update(ts=time.time(), data=out)
    return out


MODEL_LABELS = {
    "claude-fable-5": "Fable 5",
    "claude-opus-5": "Opus 5",
    "claude-sonnet-5": "Sonnet 5",
    "claude-haiku-4-5-20251001": "Haiku 4.5",
}
_usage_cache = {}  # path -> (mtime_ns, size, {(day, model): [msgs, in, out, cr, cw]})


def _usage_of_file(f):
    from datetime import datetime
    agg = {}
    seen = set()
    try:
        lines = f.read_bytes().decode("utf-8", "replace").splitlines()
    except OSError:
        return agg
    for line in lines:
        rec = _loads(line)
        if not rec or rec.get("type") != "assistant":
            continue
        msg = rec.get("message") or {}
        u = msg.get("usage")
        if not isinstance(u, dict):
            continue
        mid = rec.get("requestId") or msg.get("id")
        if mid and mid in seen:
            continue
        if mid:
            seen.add(mid)
        ts = rec.get("timestamp")
        try:
            day = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%m-%d")
        except Exception:
            continue
        model = msg.get("model") or "?"
        model = MODEL_LABELS.get(model, model)
        k = (day, model)
        a = agg.setdefault(k, [0, 0, 0, 0, 0])
        a[0] += 1
        a[1] += u.get("input_tokens", 0) or 0
        a[2] += u.get("output_tokens", 0) or 0
        a[3] += u.get("cache_read_input_tokens", 0) or 0
        a[4] += u.get("cache_creation_input_tokens", 0) or 0
    return agg


@app.get("/api/usage")
def usage(days: int = 7):
    """從本機對話紀錄統計真實 token 用量（含排程與 subagent）。"""
    from datetime import datetime
    cutoff = time.time() - (min(days, 30) + 1) * 86400
    merged = {}
    try:
        for proj in PROJECTS_DIR.iterdir():
            if not proj.is_dir():
                continue
            for f in proj.glob("*.jsonl"):
                try:
                    st = f.stat()
                except OSError:
                    continue
                if st.st_mtime < cutoff or st.st_size < 200:
                    continue
                key = str(f)
                cached = _usage_cache.get(key)
                if not (cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size):
                    cached = (st.st_mtime_ns, st.st_size, _usage_of_file(f))
                    _usage_cache[key] = cached
                for k, v in cached[2].items():
                    a = merged.setdefault(k, [0, 0, 0, 0, 0])
                    for i in range(5):
                        a[i] += v[i]
    except OSError:
        pass

    today = datetime.now().strftime("%m-%d")
    day_series = {}
    model_totals = {}
    today_tot = [0, 0, 0, 0, 0]
    week_tot = [0, 0, 0, 0, 0]
    for (day, model), v in merged.items():
        d = day_series.setdefault(day, [0, 0, 0, 0, 0])
        m = model_totals.setdefault(model, [0, 0, 0, 0, 0])
        for i in range(5):
            d[i] += v[i]
            m[i] += v[i]
            week_tot[i] += v[i]
            if day == today:
                today_tot[i] += v[i]

    def pack(a):
        return {"msgs": a[0], "in": a[1], "out": a[2], "cache_read": a[3], "cache_write": a[4]}

    return {
        "today": pack(today_tot),
        "window": pack(week_tot),
        "days": sorted(({"date": d, **pack(v)} for d, v in day_series.items()),
                       key=lambda x: x["date"], reverse=True),
        "models": {m: pack(v) for m, v in
                   sorted(model_totals.items(), key=lambda kv: -kv[1][2])},
    }


# ---------- AskUserQuestion 橋接 ----------
# claude -p 沒有介面可以回答 AskUserQuestion，工具呼叫會直接失敗。
# 橋接法：PreToolUse hook 攔下它 → POST 到這裡 → SSE 推給手機出選項卡 →
# 使用者點選 → hook 長輪詢拿到答案 → 以 deny+reason 把選擇還給模型。

ASKS = {}  # ask_id -> {"run_id", "questions", "answer", "created"}


class AskBody(BaseModel):
    run_id: str
    tool_input: dict


class AskAnswerBody(BaseModel):
    answers: dict = {}
    free_text: str = ""
    skipped: bool = False


@app.post("/api/ask")
async def ask_open(body: AskBody):
    run = RUNS.get(body.run_id)
    if not run or run.done:
        raise HTTPException(404, "這個工作已經結束")
    ask_id = uuid.uuid4().hex[:12]
    questions = body.tool_input.get("questions") or []
    ASKS[ask_id] = {"run_id": body.run_id, "questions": questions,
                    "answer": None, "created": time.time()}
    await _emit(run, {"kind": "ask", "ask_id": ask_id, "questions": questions})
    return {"ask_id": ask_id}


@app.get("/api/ask/{ask_id}")
async def ask_poll(ask_id: str):
    """hook 的長輪詢端點：最多等 20 秒，拿到答案或先回 pending。"""
    a = ASKS.get(ask_id)
    if not a:
        raise HTTPException(404, "沒有這筆提問")
    for _ in range(40):
        if a["answer"] is not None:
            return {"answer": a["answer"]}
        run = RUNS.get(a["run_id"])
        if not run or run.done:
            return {"answer": {"skipped": True, "reason": "run_ended"}}
        await asyncio.sleep(0.5)
    return {"pending": True}


@app.post("/api/ask/{ask_id}/answer")
async def ask_answer(ask_id: str, body: AskAnswerBody):
    a = ASKS.get(ask_id)
    if not a:
        raise HTTPException(404, "沒有這筆提問")
    if a["answer"] is not None:
        return {"ok": True, "already": True}
    a["answer"] = {"answers": body.answers, "free_text": body.free_text,
                   "skipped": body.skipped}
    run = RUNS.get(a["run_id"])
    if run:
        await _emit(run, {"kind": "ask_done", "ask_id": ask_id})
    return {"ok": True}


class ArchiveBody(BaseModel):
    on: bool = True


@app.post("/api/room/{slug}/{sid}/archive")
def room_archive(slug: str, sid: str, body: ArchiveBody):
    find_room_file(slug, sid)
    d = dict(load_overlay())
    d[sid] = "archived" if body.on else "active"
    save_overlay(d)
    return {"ok": True, "archived": body.on}


@app.post("/api/room/{slug}/{sid}/delete")
def room_delete(slug: str, sid: str):
    """刪除＝移到 trash/ 資料夾（可手動救回），不直接銷毀。"""
    if sid in BY_SESSION:
        raise HTTPException(409, "這個聊天室還在忙，先停掉再刪")
    f = find_room_file(slug, sid)
    TRASH_DIR.mkdir(exist_ok=True)
    dest = TRASH_DIR / (slug + "__" + f.name)
    if dest.exists():
        dest = TRASH_DIR / (slug + "__" + uuid.uuid4().hex[:6] + "-" + f.name)
    shutil.move(str(f), str(dest))
    _room_cache.pop(str(f), None)
    d = dict(load_overlay())
    d.pop(sid, None)
    save_overlay(d)
    return {"ok": True, "trash": str(dest)}


@app.post("/api/room/{slug}/{sid}/desktop")
def room_desktop(slug: str, sid: str):
    """把這個聊天室登錄進桌面 app 並切過去（只有 Claude 對話才有 jsonl 可以匯）。"""
    if SLUG_ENGINE.get(slug):
        raise HTTPException(400, "只有 Claude Code 的對話能同步到桌面 app")
    find_room_file(slug, sid)
    try:
        res = desktop_open(sid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        log.exception("desktop_open failed")
        raise HTTPException(500, f"開不起來：{e}")
    if res == "unavailable":
        raise HTTPException(400, "這台電腦沒有裝 Claude 桌面 app")
    return {"ok": True, "result": res}


class TitleBody(BaseModel):
    title: str = ""


@app.post("/api/room/{slug}/{sid}/title")
def room_title(slug: str, sid: str, body: TitleBody):
    """改聊天室名字（存在 web-titles.json，空字串 = 還原）。"""
    find_room_file(slug, sid)
    t = _clean_title(body.title)
    save_title(sid, t)
    return {"ok": True, "title": t}


SEARCH_TAIL = 1_500_000   # 每個對話只翻最後 1.5MB
SEARCH_MAX_ROOMS = 40


def _search_file(path, q, engine):
    """在一個對話檔裡找 q（不分大小寫），回最多 3 段摘錄。"""
    hits = []
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > SEARCH_TAIL:
                f.seek(size - SEARCH_TAIL)
                f.readline()   # 丟掉切半的那行
            data = f.read()
    except OSError:
        return hits
    ql = q.lower()
    for line in data.decode("utf-8", "replace").splitlines():
        if ql not in line.lower():
            continue
        rec = _loads(line)
        if not rec:
            continue
        if engine == "codex":
            if rec.get("type") != "event_msg":
                continue
            p = rec.get("payload") or {}
            if p.get("type") not in ("user_message", "agent_message"):
                continue
            text = p.get("message") or ""
            role = "user" if p["type"] == "user_message" else "assistant"
        else:
            role = rec.get("type") if rec.get("type") in ("user", "assistant") else rec.get("role")
            if role not in ("user", "assistant"):
                continue
            msg = rec.get("message") or rec
            text = _text_of(msg.get("content"))
            if role == "user" and _is_meta_user(rec, text):
                continue
        i = text.lower().find(ql)
        if i < 0:
            continue
        start = max(0, i - 40)
        snippet = text[start:start + 120].replace("\n", " ")
        hits.append({"role": role, "snippet": ("…" if start else "") + snippet,
                     "ts": rec.get("timestamp")})
        if len(hits) >= 3:
            break
    return hits


@app.get("/api/search")
def search(q: str = "", all: int = 0):
    """全文搜尋聊天內容（桌面 app 的「搜尋 session 內容」）。"""
    q = (q or "").strip()
    if len(q) < 2:
        raise HTTPException(400, "至少兩個字")
    out = []
    for room in scan_rooms(show_all=bool(all)):
        eng = room.get("engine", "claude")
        if eng == "codex":
            path = Path(room["path"])
        elif eng != "claude":
            path = API_CHATS / eng / (room["sid"] + ".jsonl")
        else:
            path = PROJECTS_DIR / room["slug"] / (room["sid"] + ".jsonl")
        hits = _search_file(path, q, eng)
        if hits:
            out.append({"slug": room["slug"], "sid": room["sid"], "title": room["title"],
                        "project_name": room["project_name"], "engine": eng,
                        "ts": room["ts"], "hits": hits})
        if len(out) >= SEARCH_MAX_ROOMS:
            break
    return {"q": q, "rooms": out}


# ---------- 逐項授權（先問我模式） ----------
PERMS = {}  # perm_id -> {"run_id", "tool", "input", "answer", "created"}


class PermBody(BaseModel):
    run_id: str
    tool_name: str = ""
    tool_input: dict = {}


class PermAnswerBody(BaseModel):
    decision: str = "deny"   # allow / deny / allow_all


@app.post("/api/perm")
async def perm_open(body: PermBody):
    run = RUNS.get(body.run_id)
    if not run or run.done:
        raise HTTPException(404, "這個工作已經結束")
    if run.allow_all:
        return {"perm_id": None, "decision": "allow"}
    perm_id = uuid.uuid4().hex[:12]
    detail = _tool_detail(body.tool_name, body.tool_input)
    PERMS[perm_id] = {"run_id": body.run_id, "tool": body.tool_name, "answer": None,
                      "created": time.time()}
    preview = ""
    if isinstance(body.tool_input, dict):
        for k in ("command", "content", "new_string", "url"):
            v = body.tool_input.get(k)
            if isinstance(v, str) and v.strip():
                preview = v[:600]
                break
    await _emit(run, {"kind": "perm", "perm_id": perm_id, "tool": body.tool_name,
                      "detail": detail, "preview": preview})
    return {"perm_id": perm_id}


@app.get("/api/perm/{perm_id}")
async def perm_poll(perm_id: str):
    p = PERMS.get(perm_id)
    if not p:
        raise HTTPException(404, "沒有這筆授權")
    for _ in range(40):
        if p["answer"] is not None:
            return {"decision": p["answer"]}
        run = RUNS.get(p["run_id"])
        if not run or run.done:
            return {"decision": "deny", "reason": "run_ended"}
        if run.allow_all:
            return {"decision": "allow"}
        await asyncio.sleep(0.5)
    return {"pending": True}


@app.post("/api/perm/{perm_id}/answer")
async def perm_answer(perm_id: str, body: PermAnswerBody):
    p = PERMS.get(perm_id)
    if not p:
        raise HTTPException(404, "沒有這筆授權")
    if body.decision not in ("allow", "deny", "allow_all"):
        raise HTTPException(400, "decision 只能是 allow / deny / allow_all")
    if p["answer"] is not None:
        return {"ok": True, "already": True}
    run = RUNS.get(p["run_id"])
    if body.decision == "allow_all" and run:
        run.allow_all = True
    p["answer"] = "allow" if body.decision == "allow_all" else body.decision
    if run:
        await _emit(run, {"kind": "perm_done", "perm_id": perm_id, "decision": p["answer"]})
    return {"ok": True}


@app.get("/api/status")
def status():
    running = {}
    for sid, rid in BY_SESSION.items():
        run = RUNS.get(rid)
        running[sid] = {"run_id": rid, "n_events": len(run.events) if run else 0}
    return {"running": running}


@app.post("/api/send")
async def send(body: SendBody):
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "沒有內容")
    # 未知的權限模式一律拒絕，不能靜靜退回成「讓 AI 免詢問執行」
    mode = body.mode or CONFIG["default_mode"]
    if mode not in PERMISSION_FLAGS:
        raise HTTPException(400, "不認得這個權限模式")
    # 算真正還在跑的 run，不要算 BY_SESSION —— 新聊天室在拿到 session id 之前
    # 不在那張表裡，用它當上限等於沒有上限
    if sum(1 for r in RUNS.values() if not r.done) >= MAX_CONCURRENT_RUNS:
        raise HTTPException(429, "同時進行的工作太多，等一件做完再送")
    engine = body.engine or SLUG_ENGINE.get(body.slug or "") or "claude"
    if engine not in ENGINES:
        raise HTTPException(400, "沒有這個 AI")

    if body.sid:
        if body.sid in BY_SESSION:
            raise HTTPException(409, "這個聊天室還在忙，等它回完")
        f = find_room_file(body.slug, body.sid)
        if engine == "codex":
            cwd = _codex_info(f)["cwd"] or str(HOME)
        elif ENGINES[engine]["kind"] == "api":
            cwd = str(HOME)
        else:
            cwd = _head_info(f)["cwd"]
        if not cwd or not Path(cwd).is_dir():
            cwd = str(HOME)
    else:
        if ENGINES[engine]["kind"] == "api":
            cwd = str(HOME)
        elif not body.project or not Path(body.project).is_dir():
            raise HTTPException(400, "要先選一個專案資料夾")
        else:
            cwd = body.project

    if ENGINES[engine]["kind"] == "api":
        run = Run(uuid.uuid4().hex[:12], API_SLUG[engine], body.sid, cwd)
        RUNS[run.id] = run
        if body.sid:
            BY_SESSION[body.sid] = run.id
        asyncio.get_event_loop().create_task(run_api(run, text, engine, body.model))
        return {"run_id": run.id}

    if engine == "codex":
        if not Path(CODEX_EXE).exists():
            raise HTTPException(400, "這台電腦沒有安裝 Codex")
        run = Run(uuid.uuid4().hex[:12], "codex", body.sid, cwd)
        RUNS[run.id] = run
        if body.sid:
            BY_SESSION[body.sid] = run.id
        asyncio.get_event_loop().create_task(run_codex(run, text, mode))
        return {"run_id": run.id}

    extra = []
    if body.model in MODELS:
        extra += ["--model", MODELS[body.model]]
    if body.effort in EFFORTS:
        extra += ["--effort", body.effort]

    run = Run(uuid.uuid4().hex[:12], body.slug, body.sid, cwd)
    RUNS[run.id] = run
    if body.sid:
        BY_SESSION[body.sid] = run.id
    asyncio.get_event_loop().create_task(run_claude(run, text, mode, extra))
    return {"run_id": run.id}


@app.get("/api/run/{run_id}/events")
async def run_events(run_id: str, request: Request, start: int = 0):
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(404, "這個工作已經結束或不存在")

    async def gen():
        i = max(0, start)
        while True:
            if await request.is_disconnected():
                return
            while i < len(run.events):
                ev = run.events[i]
                yield f"id: {i}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
                i += 1
                if ev.get("kind") == "done":
                    return
            if run.done:
                return
            try:
                async with run.cond:
                    await asyncio.wait_for(run.cond.wait(), timeout=15)
            except asyncio.TimeoutError:
                yield ": ping\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.post("/api/run/{run_id}/stop")
async def stop_run(run_id: str):
    run = RUNS.get(run_id)
    if not run or run.done:
        return {"ok": False, "error": "這個工作已經結束了"}
    if not run.proc:
        # API 引擎跑在 executor 裡，沒有可以殺的子行程
        return {"ok": False, "error": "這種對話停不下來，等它回完"}
    try:
        r = subprocess.run(["taskkill", "/F", "/T", "/PID", str(run.proc.pid)],
                           capture_output=True, timeout=15,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        if r.returncode != 0:
            msg = (r.stderr or b"").decode("utf-8", "replace").strip()[:200]
            log.warning("taskkill rc=%s: %s", r.returncode, msg)
    except Exception as e:
        log.warning("taskkill failed: %s", e)
        return {"ok": False, "error": "停不掉，請看伺服器 log"}
    # 確認行程真的結束了才回報成功，不要按了停止卻還在背景跑
    try:
        await asyncio.wait_for(run.proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        log.warning("run %s: 行程沒有在 10 秒內結束", run_id)
        return {"ok": False, "error": "停止指令送出了，但行程還沒結束"}
    except Exception:
        pass
    return {"ok": True}


# ---------- 靜態頁 ----------

app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/apple-touch-icon.png")
@app.get("/apple-touch-icon-precomposed.png")
def touch_icon():
    return FileResponse(STATIC / "icon-180.png")


# ---------- 進入點 ----------

def main():
    LOG_FILE.parent.mkdir(exist_ok=True)
    logging.basicConfig(filename=str(LOG_FILE), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        encoding="utf-8")
    # pythonw 下 stdout/stderr 是 None，接到 log 檔避免噴錯
    if sys.stdout is None or sys.stderr is None:
        f = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = f
        if sys.stderr is None:
            sys.stderr = f
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    hosts = bind_hosts()
    log.info("claude-chat starting on %s:%s (claude=%s)", hosts, PORT, CLAUDE_ARGV)

    async def serve_all():
        servers = []
        for h in hosts:
            cfg = uvicorn.Config(app, host=h, port=PORT,
                                 log_config=None, access_log=False)
            srv = uvicorn.Server(cfg)
            servers.append(srv.serve())
        await asyncio.gather(*servers)

    try:
        asyncio.run(serve_all())
    except KeyboardInterrupt:
        pass
    except OSError as e:
        log.error("port busy or bind failed: %s", e)


if __name__ == "__main__":
    main()
