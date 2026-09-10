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

async function postAnswer(permId, decision, by) {
  try {
    await fetch(BASE + "/api/perm/" + permId + "/answer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision, by: by || "" }),
    });
  } catch {
    /* ignore */
  }
}

/**
 * Ask the phone. Never throws.
 * - Phone-started run (CLAUDE_CHAT_RUN_ID set): the card belongs to that run and decides.
 * - Otherwise pass `session_id` (from the hook's stdin payload): the card is registered for that
 *   desktop/CLI session (list banner + inside the room). With `notifyOnly` the call returns "ask"
 *   right away so the desktop app shows its own dialog too; the card stays on the phone and the
 *   claude-chat server answers the desktop dialog on the user's behalf when they tap it.
 * Resolves to "allow" | "deny" | "ask" | "timeout" | "unavailable".
 */
async function askPhone({ tool_name, tool_input, reason, session_id, cwd, waitMs, notifyOnly }) {
  if (!RUN_ID && !session_id) return "unavailable";
  let permId;
  try {
    const r = await fetch(BASE + "/api/perm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        run_id: RUN_ID || "",
        session_id: RUN_ID ? "" : session_id || "",
        cwd: cwd || "",
        tool_name: tool_name || "",
        tool_input: tool_input || {},
        reason: reason || "",
        notify_only: !!notifyOnly,
      }),
    });
    if (!r.ok) return "unavailable";
    const d = await r.json();
    if (d.decision === "allow") return "allow";
    permId = d.perm_id;
    if (!permId) return "unavailable";
  } catch {
    return "unavailable";
  }
  if (notifyOnly) return "ask";

  const deadline = Date.now() + (waitMs || WAIT_TOTAL_MS);
  while (Date.now() < deadline) {
    try {
      const r = await fetch(BASE + "/api/perm/" + permId);
      if (!r.ok) return "unavailable";
      const d = await r.json();
      if (d.decision === "allow" || d.decision === "deny") return d.decision;
      if (d.decision === "ask") return "timeout";
    } catch {
      return "unavailable";
    }
  }
  await postAnswer(permId, "ask", "timeout");
  return "timeout";
}

/**
 * PermissionRequest hook (desktop / CLI sessions). The CLI runs this CONCURRENTLY with the
 * desktop app's own permission dialog and takes whichever answers first, so: register the
 * question on the phone (or attach to the card the dangerous-command hook already opened),
 * wait for the tap, and return the decision. If the desk answers first the CLI discards us.
 * No output = no opinion (the dialog just stays up).
 */
async function permissionRequest(payload) {
  const tool = payload.tool_name || "";
  const input = payload.tool_input || {};
  let permId;
  try {
    const r = await fetch(BASE + "/api/perm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: payload.session_id || "",
        cwd: payload.cwd || "",
        tool_name: tool,
        tool_input: input,
        reason: "",
        event: "PermissionRequest",
      }),
    });
    if (!r.ok) process.exit(0);
    const d = await r.json();
    permId = d.perm_id;
    if (!permId) process.exit(0);
  } catch {
    process.exit(0);
  }
  const deadline = Date.now() + WAIT_TOTAL_MS;
  while (Date.now() < deadline) {
    let d;
    try {
      const r = await fetch(BASE + "/api/perm/" + permId);
      if (!r.ok) process.exit(0);
      d = await r.json();
    } catch {
      process.exit(0);
    }
    if (d.decision === "allow" || d.decision === "deny") {
      let decision;
      if (tool === "AskUserQuestion") {
        // 桌面 app 回答選擇題的方式就是在確認框裡把 answers 回填進工具輸入；手機答案走同一條
        if (d.decision === "allow") {
          const answers = Object.assign({}, d.answers || {});
          const free = (d.free_text || "").trim();
          if (free) for (const q of input.questions || []) if (!answers[q.question]) answers[q.question] = free;
          decision = { behavior: "allow", updatedInput: Object.assign({}, input, { answers }) };
        } else {
          decision = { behavior: "deny", message: "使用者在手機上跳過了這個問題，請你自行判斷後繼續，不要再問同一題。" };
        }
      } else {
        decision =
          d.decision === "allow"
            ? { behavior: "allow" }
            : { behavior: "deny", message: "使用者在手機上拒絕了這個動作。不要重試同一個動作；換個做法或說明原因後停下來。" };
      }
      process.stdout.write(JSON.stringify({ hookSpecificOutput: { hookEventName: "PermissionRequest", decision } }));
      process.exit(0);
    }
    if (d.decision === "ask") process.exit(0);
  }
  await postAnswer(permId, "ask", "timeout");
  process.exit(0);
}

async function main() {
  let payload;
  try {
    payload = JSON.parse(await readStdin());
  } catch {
    process.exit(0);
  }
  if (payload.hook_event_name === "PermissionRequest") {
    if (RUN_ID) process.exit(0); // 手機發起的 run 由 PreToolUse 那條處理，不重複問
    await permissionRequest(payload);
    return;
  }
  if (!RUN_ID) process.exit(0);
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
