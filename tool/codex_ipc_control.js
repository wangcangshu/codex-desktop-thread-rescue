#!/usr/bin/env node

const crypto = require("crypto");
const net = require("net");

const PIPE_PATH = "\\\\.\\pipe\\codex-ipc";
const INITIAL_CLIENT_ID = "initializing-client";
const VERSION_BY_METHOD = {
  "thread-follower-command-approval-decision": 1,
  "thread-follower-compact-thread": 1,
  "thread-follower-edit-last-user-turn": 1,
  "thread-follower-file-approval-decision": 1,
  "thread-follower-interrupt-turn": 1,
  "thread-follower-permissions-request-approval-response": 1,
  "thread-follower-set-collaboration-mode": 1,
  "thread-follower-set-model-and-reasoning": 1,
  "thread-follower-set-queued-follow-ups-state": 1,
  "thread-follower-start-turn": 1,
  "thread-follower-steer-turn": 1,
  "thread-follower-submit-mcp-server-elicitation-response": 1,
  "thread-follower-submit-user-input": 1,
};

function printJson(payload) {
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

function parseArgs(argv) {
  if (argv.length < 2) {
    throw new Error(
      "Usage: codex_ipc_control.js <interrupt|compact> <thread-id> [--timeout-ms <ms>]"
    );
  }

  const action = argv[0];
  const threadId = argv[1];
  let timeoutMs = 12000;

  for (let i = 2; i < argv.length; i += 1) {
    const token = argv[i];
    if (token === "--timeout-ms") {
      timeoutMs = Number(argv[i + 1]);
      i += 1;
      continue;
    }
    throw new Error(`Unknown argument: ${token}`);
  }

  if (!threadId || !threadId.trim()) {
    throw new Error("Thread id is required.");
  }
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
    throw new Error("Timeout must be a positive number.");
  }

  switch (action) {
    case "interrupt":
      return {
        action,
        method: "thread-follower-interrupt-turn",
        params: { conversationId: threadId },
        threadId,
        timeoutMs,
      };
    case "compact":
      return {
        action,
        method: "thread-follower-compact-thread",
        params: { conversationId: threadId },
        threadId,
        timeoutMs,
      };
    default:
      throw new Error(`Unsupported action: ${action}`);
  }
}

function encodeFrame(message) {
  const json = JSON.stringify(message);
  const buffer = Buffer.alloc(4 + Buffer.byteLength(json, "utf8"));
  buffer.writeUInt32LE(Buffer.byteLength(json, "utf8"), 0);
  buffer.write(json, 4, "utf8");
  return buffer;
}

function decodeFrames(state, chunk, onFrame) {
  state.buffer = Buffer.concat([state.buffer, chunk]);
  for (;;) {
    if (state.frameLength == null) {
      if (state.buffer.length < 4) {
        return;
      }
      state.frameLength = state.buffer.readUInt32LE(0);
      state.buffer = state.buffer.subarray(4);
    }

    if (state.buffer.length < state.frameLength) {
      return;
    }

    const frame = state.buffer.subarray(0, state.frameLength);
    state.buffer = state.buffer.subarray(state.frameLength);
    state.frameLength = null;
    onFrame(JSON.parse(frame.toString("utf8")));
  }
}

function buildPayload(base, extra) {
  return {
    timestamp: new Date().toISOString(),
    pipePath: PIPE_PATH,
    ...base,
    ...extra,
  };
}

async function main() {
  const parsed = parseArgs(process.argv.slice(2));
  const version = VERSION_BY_METHOD[parsed.method];
  if (version == null) {
    throw new Error(`No request version is known for ${parsed.method}`);
  }

  const socket = net.connect(PIPE_PATH);
  const decodeState = { buffer: Buffer.alloc(0), frameLength: null };
  let clientId = INITIAL_CLIENT_ID;
  let pendingRequestId = null;
  let done = false;

  const finish = (code, payload) => {
    if (done) {
      return;
    }
    done = true;
    clearTimeout(timeoutHandle);
    socket.removeAllListeners();
    try {
      socket.end();
    } catch (error) {
      // Ignore cleanup failures on exit.
    }
    printJson(payload);
    process.exitCode = code;
  };

  const timeoutHandle = setTimeout(() => {
    finish(
      11,
      buildPayload(parsed, {
        status: "timeout",
        request: {
          method: parsed.method,
          params: parsed.params,
          version,
        },
      })
    );
  }, parsed.timeoutMs);

  socket.on("connect", () => {
    const initializeMessage = {
      type: "request",
      requestId: `init-${crypto.randomUUID()}`,
      sourceClientId: clientId,
      version: 1,
      method: "initialize",
      params: {
        clientType: "external-codex-repair",
      },
    };
    socket.write(encodeFrame(initializeMessage));
  });

  socket.on("data", (chunk) => {
    decodeFrames(decodeState, chunk, (message) => {
      if (
        message.type === "response" &&
        message.method === "initialize" &&
        message.resultType === "success"
      ) {
        clientId = message.result.clientId;
        pendingRequestId = `request-${crypto.randomUUID()}`;
        socket.write(
          encodeFrame({
            type: "request",
            requestId: pendingRequestId,
            sourceClientId: clientId,
            version,
            method: parsed.method,
            params: parsed.params,
          })
        );
        return;
      }

      if (message.type !== "response" || message.requestId !== pendingRequestId) {
        return;
      }

      finish(
        message.resultType === "success" ? 0 : 10,
        buildPayload(parsed, {
          status: message.resultType === "success" ? "success" : "error",
          request: {
            method: parsed.method,
            params: parsed.params,
            version,
          },
          response: message,
        })
      );
    });
  });

  socket.on("error", (error) => {
    finish(
      12,
      buildPayload(parsed, {
        status: "socket_error",
        error: String(error),
        request: {
          method: parsed.method,
          params: parsed.params,
          version,
        },
      })
    );
  });

  socket.on("close", () => {
    if (!done) {
      finish(
        13,
        buildPayload(parsed, {
          status: "connection_closed",
          request: {
            method: parsed.method,
            params: parsed.params,
            version,
          },
        })
      );
    }
  });
}

main().catch((error) => {
  printJson({
    timestamp: new Date().toISOString(),
    status: "fatal",
    error: String(error),
  });
  process.exitCode = 1;
});
