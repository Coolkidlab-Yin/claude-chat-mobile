#!/usr/bin/env node
/**
 * Per-tool permission bridge for claude-chat-mobile ("ask me first" mode).
 *
 * In `-p` mode with the default permission mode there is no UI, so any tool
 * that needs approval (Bash, Edit, Write, WebFetch, MCP tools…) is simply
 * denied. This PreToolUse hook forwards the request to the claude-chat server,
 * which shows an allow / deny card on the phone, and returns the user's
 * decision to Claude Code as the hook result.
 *
 * Only active for runs started by claude-chat-mobile: server.py passes this
 * file through `--settings` solely in "ask" mode, and the hook exits 0 (no
 * opinion) unless CLAUDE_CHAT_RUN_ID is set. Desktop app / plain CLI unaffected.
 */
"use strict";

const RUN_ID = process.env.CLAUDE_CHAT_RUN_ID;
const PORT = process.env.CLAUDE_CHAT_PORT || "8899";
if (!RUN_ID) process.exit(0);

const BASE = "http://127.0.0.1:" + PORT;
const WAIT_TOTAL_MS = 9.5 * 60 * 1000; // stay under the hook timeout (600s)
// tools that are always fine without asking
const AUTO_ALLOW = new Set(["mcp__chat__ask_user"]);

function readStdin() {
  return new Promise((resolve) => {
    let buf = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (d) => (buf += d));
    process.stdin.on("end", () => resolve(buf));
    setTimeout(() => resolve(buf), 5000);
  });
}

function reply(decision, reason) {
  process.stdout.write(
    JSON.stringify({
      hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: decision,
        permissionDecisionReason: reason,
      },
    })
  );
}

async function main() {
  let payload;
  try {
    payload = JSON.parse(await readStdin());
  } catch {
    process.exit(0);
  }
  const tool = payload.tool_name || "";
  if (AUTO_ALLOW.has(tool)) {
    reply("allow", "claude-chat 內建通道");
    process.exit(0);
  }

  let permId;
  try {
    const r = await fetch(BASE + "/api/perm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_id: RUN_ID, tool_name: tool, tool_input: payload.tool_input || {} }),
    });
    if (!r.ok) process.exit(0); // server unreachable → no opinion (Claude Code's default applies)
    const d = await r.json();
    if (d.decision === "allow") {
      reply("allow", "使用者已選擇「這次工作全部允許」");
      process.exit(0);
    }
    permId = d.perm_id;
  } catch {
    process.exit(0);
  }

  const deadline = Date.now() + WAIT_TOTAL_MS;
  let decision = null;
  while (Date.now() < deadline) {
    try {
      const r = await fetch(BASE + "/api/perm/" + permId);
      if (!r.ok) break;
      const d = await r.json();
      if (d.decision) {
        decision = d.decision;
        break;
      }
    } catch {
      break;
    }
  }

  if (decision === "allow") {
    reply("allow", "使用者在手機上允許了這個動作");
  } else {
    reply(
      "deny",
      decision === "deny"
        ? "使用者在手機上拒絕了這個動作。不要重試同一個動作；換個做法或說明你需要它的原因，然後停下來等使用者。"
        : "使用者沒有在時限內回應授權。不要重試同一個動作；先把目前進度說清楚再停下來。"
    );
  }
  process.exit(0);
}

main().catch(() => process.exit(0));
