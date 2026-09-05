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
 *
 * Also usable as a library: other hooks (e.g. a dangerous-command guard that
 * would normally answer "ask") can `require()` this file and call
 * `askPhone({tool_name, tool_input, reason})` to turn their "ask" into a card
 * on the phone. Resolves to "allow" | "deny" | "timeout" | "unavailable".
 */
"use strict";

const RUN_ID = process.env.CLAUDE_CHAT_RUN_ID;
const PORT = process.env.CLAUDE_CHAT_PORT || "8899";
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

/** Ask the phone. Never throws; "unavailable" when this is not a phone run or the server is unreachable. */
async function askPhone({ tool_name, tool_input, reason }) {
  if (!RUN_ID) return "unavailable";
  let permId;
  try {
    const r = await fetch(BASE + "/api/perm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_id: RUN_ID, tool_name: tool_name || "", tool_input: tool_input || {}, reason: reason || "" }),
    });
    if (!r.ok) return "unavailable";
    const d = await r.json();
    if (d.decision === "allow") return "allow";
    permId = d.perm_id;
  } catch {
    return "unavailable";
  }
  const deadline = Date.now() + WAIT_TOTAL_MS;
  while (Date.now() < deadline) {
    try {
      const r = await fetch(BASE + "/api/perm/" + permId);
      if (!r.ok) return "unavailable";
      const d = await r.json();
      if (d.decision === "allow" || d.decision === "deny") return d.decision;
    } catch {
      return "unavailable";
    }
  }
  return "timeout";
}

async function main() {
  if (!RUN_ID) process.exit(0);
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
  const decision = await askPhone({ tool_name: tool, tool_input: payload.tool_input || {} });
  if (decision === "unavailable") process.exit(0); // no opinion → Claude Code's default applies
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

module.exports = { askPhone };

if (require.main === module) {
  main().catch(() => process.exit(0));
}
