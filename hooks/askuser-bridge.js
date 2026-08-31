#!/usr/bin/env node
/**
 * AskUserQuestion bridge for claude-chat-mobile.
 *
 * `claude -p` has no UI, so the AskUserQuestion tool normally just fails and
 * the model guesses. This PreToolUse hook intercepts the call, forwards the
 * question to the claude-chat server (which renders tappable options on the
 * phone), waits for the user's choice, and hands it back to the model as the
 * deny reason — the model reads it as the user's answer and continues.
 *
 * Safe by construction:
 * - Without CLAUDE_CHAT_RUN_ID in the environment (i.e. any session NOT
 *   started by claude-chat-mobile: desktop app, plain CLI) it exits 0
 *   immediately and changes nothing.
 * - On any error it also exits 0, degrading to today's behaviour.
 *
 * Register in ~/.claude/settings.json under hooks.PreToolUse with matcher
 * "AskUserQuestion" and a generous timeout (e.g. 600 seconds) — see README.
 */
"use strict";

const RUN_ID = process.env.CLAUDE_CHAT_RUN_ID;
const PORT = process.env.CLAUDE_CHAT_PORT || "8899";
if (!RUN_ID) process.exit(0);

const BASE = "http://127.0.0.1:" + PORT;
const WAIT_TOTAL_MS = 9 * 60 * 1000; // stay under the hook timeout

function readStdin() {
  return new Promise((resolve) => {
    let buf = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (d) => (buf += d));
    process.stdin.on("end", () => resolve(buf));
    setTimeout(() => resolve(buf), 5000);
  });
}

async function main() {
  const raw = await readStdin();
  let payload;
  try {
    payload = JSON.parse(raw);
  } catch {
    process.exit(0);
  }
  if (payload.tool_name !== "AskUserQuestion") process.exit(0);

  // Open the question on the server
  let askId;
  try {
    const r = await fetch(BASE + "/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_id: RUN_ID, tool_input: payload.tool_input || {} }),
    });
    if (!r.ok) process.exit(0);
    askId = (await r.json()).ask_id;
  } catch {
    process.exit(0);
  }

  // Long-poll for the user's answer
  const deadline = Date.now() + WAIT_TOTAL_MS;
  let answer = null;
  while (Date.now() < deadline) {
    try {
      const r = await fetch(BASE + "/api/ask/" + askId);
      if (!r.ok) break;
      const d = await r.json();
      if (d.answer) {
        answer = d.answer;
        break;
      }
    } catch {
      break;
    }
  }

  let reason;
  if (!answer || answer.skipped) {
    reason =
      "使用者這次沒有回答這個提問（已跳過或逾時）。不要重新呼叫 AskUserQuestion，" +
      "請自行採用最合理的預設繼續做，並在回覆裡註明你採用了什麼假設。";
  } else {
    const parts = [];
    for (const [q, a] of Object.entries(answer.answers || {})) {
      parts.push(`「${q}」→ ${a}`);
    }
    if (answer.free_text) parts.push(`補充說明：${answer.free_text}`);
    reason =
      "使用者已經在手機上回答了這個提問（這就是他的答案，不要再問一次）：" +
      (parts.join("；") || "（空白）") +
      "。請按照這個選擇繼續。";
  }

  process.stdout.write(
    JSON.stringify({
      hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: "deny",
        permissionDecisionReason: reason,
      },
    })
  );
  process.exit(0);
}

main().catch(() => process.exit(0));
