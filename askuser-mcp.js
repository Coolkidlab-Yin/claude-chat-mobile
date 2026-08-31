#!/usr/bin/env node
/**
 * ask_user MCP server for claude-chat-mobile.
 *
 * `claude -p` (the mode this app uses to drive sessions) does not expose the
 * built-in AskUserQuestion tool at all, so the model has no way to ask the
 * user a multiple-choice question. This tiny stdio MCP server fills the gap:
 * the model calls `mcp__chat__ask_user`, we forward the questions to the
 * claude-chat server (which renders tappable option cards on the phone),
 * wait for the user's choice, and return it as the tool result.
 *
 * Spawned automatically by server.py via --mcp-config; reads the run identity
 * from CLAUDE_CHAT_RUN_ID / CLAUDE_CHAT_PORT in the environment. Never run
 * or registered globally — desktop app and plain CLI are unaffected.
 *
 * Protocol: newline-delimited JSON-RPC 2.0 on stdio (MCP stdio transport).
 * stdout carries protocol messages ONLY; diagnostics go to stderr.
 */
"use strict";

const readline = require("readline");

const RUN_ID = process.env.CLAUDE_CHAT_RUN_ID || "";
const PORT = process.env.CLAUDE_CHAT_PORT || "8899";
const BASE = "http://127.0.0.1:" + PORT;
// claude 那邊的 MCP_TOOL_TIMEOUT 設 10 分鐘，這裡要比它先收手
const WAIT_TOTAL_MS = 9.5 * 60 * 1000;

const TOOL = {
  name: "ask_user",
  description:
    "在使用者的手機聊天介面上提出選擇題。手機會顯示可點選的選項卡（含自由輸入與跳過），" +
    "並把使用者的選擇當成工具結果回傳。需要使用者做決定、釐清模糊需求、或在多個做法中" +
    "挑一個時使用；一次可以問 1-4 題。",
  inputSchema: {
    type: "object",
    properties: {
      questions: {
        type: "array",
        minItems: 1,
        maxItems: 4,
        description: "要問使用者的選擇題（1-4 題）",
        items: {
          type: "object",
          properties: {
            question: {
              type: "string",
              description: "完整的問題，清楚具體，以問號結尾",
            },
            header: {
              type: "string",
              description: "問題的短標籤（12 字內），顯示在選項卡角落",
            },
            multiSelect: {
              type: "boolean",
              description: "true 表示可以複選",
            },
            options: {
              type: "array",
              minItems: 2,
              maxItems: 4,
              description: "給使用者點選的選項",
              items: {
                type: "object",
                properties: {
                  label: { type: "string", description: "選項文字，簡短" },
                  description: {
                    type: "string",
                    description: "白話解釋這個選項是什麼、選了會怎樣",
                  },
                },
                required: ["label"],
              },
            },
          },
          required: ["question", "options"],
        },
      },
    },
    required: ["questions"],
  },
};

function send(msg) {
  process.stdout.write(JSON.stringify(msg) + "\n");
}

async function askUser(args) {
  if (!RUN_ID) {
    return (
      "提問通道沒有啟用（不是手機介面開的工作）。" +
      "請自行採用最合理的預設繼續，並在回覆裡註明你採用了什麼假設。"
    );
  }

  const r = await fetch(BASE + "/api/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ run_id: RUN_ID, tool_input: args || {} }),
  });
  if (!r.ok) throw new Error("HTTP " + r.status);
  const askId = (await r.json()).ask_id;

  // 長輪詢等使用者在手機上點選（伺服器端每輪 hold ~20 秒）
  const deadline = Date.now() + WAIT_TOTAL_MS;
  let answer = null;
  while (Date.now() < deadline) {
    const p = await fetch(BASE + "/api/ask/" + askId);
    if (!p.ok) break;
    const d = await p.json();
    if (d.answer) {
      answer = d.answer;
      break;
    }
  }

  if (!answer || answer.skipped) {
    return (
      "使用者這次沒有回答這個提問（已跳過或逾時）。不要重問同一題，" +
      "請自行採用最合理的預設繼續做，並在回覆裡註明你採用了什麼假設。"
    );
  }
  const parts = [];
  for (const [q, a] of Object.entries(answer.answers || {})) {
    parts.push(`「${q}」→ ${a}`);
  }
  if (answer.free_text) parts.push(`補充說明：${answer.free_text}`);
  return (
    "使用者已經在手機上回答（這就是他的答案，不要再問同一題）：" +
    (parts.join("；") || "（空白）") +
    "。請按照這個選擇繼續。"
  );
}

const rl = readline.createInterface({ input: process.stdin });
rl.on("line", async (line) => {
  let msg;
  try {
    msg = JSON.parse(line);
  } catch {
    return;
  }
  const { id, method, params } = msg || {};
  try {
    if (method === "initialize") {
      send({
        jsonrpc: "2.0",
        id,
        result: {
          protocolVersion: (params && params.protocolVersion) || "2024-11-05",
          capabilities: { tools: {} },
          serverInfo: { name: "claude-chat-ask", version: "1.0.0" },
        },
      });
    } else if (method === "tools/list") {
      send({ jsonrpc: "2.0", id, result: { tools: [TOOL] } });
    } else if (method === "tools/call") {
      const name = params && params.name;
      if (name !== TOOL.name) {
        send({ jsonrpc: "2.0", id, error: { code: -32602, message: "unknown tool: " + name } });
        return;
      }
      let text;
      try {
        text = await askUser((params && params.arguments) || {});
      } catch (e) {
        // 通道故障就退化成今天的行為：讓模型自己決定，但講清楚原因
        text =
          "提問通道故障（" + (e && e.message ? e.message : "未知錯誤") + "）。" +
          "請自行採用最合理的預設繼續，並在回覆裡註明你採用了什麼假設。";
      }
      send({ jsonrpc: "2.0", id, result: { content: [{ type: "text", text }] } });
    } else if (method === "ping") {
      send({ jsonrpc: "2.0", id, result: {} });
    } else if (id !== undefined && id !== null) {
      send({ jsonrpc: "2.0", id, error: { code: -32601, message: "method not found: " + method } });
    }
    // 通知（notifications/*）不用回
  } catch (e) {
    process.stderr.write("askuser-mcp error: " + String(e) + "\n");
  }
});
rl.on("close", () => process.exit(0));
