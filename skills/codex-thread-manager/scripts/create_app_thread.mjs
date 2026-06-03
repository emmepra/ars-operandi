#!/usr/bin/env node
import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import readline from "node:readline";

function usage() {
  console.log(`Usage:
  create_app_thread.mjs --cwd PATH --title TITLE (--prompt TEXT | --prompt-file FILE) [options]

Options:
  --effort VALUE       Reasoning effort, default: xhigh
  --sandbox VALUE      read-only | workspace-write | danger-full-access, default: workspace-write
  --approval VALUE     never | on-request | untrusted, default: on-request
  --timeout-ms VALUE   Wait timeout, default: 1800000
`);
}

function parseArgs(argv) {
  const args = {
    effort: "xhigh",
    sandbox: "workspace-write",
    approval: "on-request",
    timeoutMs: 30 * 60 * 1000,
  };
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i];
    const value = argv[i + 1];
    if (key === "--help" || key === "-h") {
      args.help = true;
    } else if (key === "--cwd") {
      args.cwd = value; i += 1;
    } else if (key === "--title") {
      args.title = value; i += 1;
    } else if (key === "--prompt") {
      args.prompt = value; i += 1;
    } else if (key === "--prompt-file") {
      args.promptFile = value; i += 1;
    } else if (key === "--effort") {
      args.effort = value; i += 1;
    } else if (key === "--sandbox") {
      args.sandbox = value; i += 1;
    } else if (key === "--approval") {
      args.approval = value; i += 1;
    } else if (key === "--timeout-ms") {
      args.timeoutMs = Number(value); i += 1;
    } else {
      throw new Error(`Unknown argument: ${key}`);
    }
  }
  return args;
}

function sandboxPolicy(mode, cwd) {
  if (mode === "read-only") return { type: "readOnly", networkAccess: true };
  if (mode === "workspace-write") {
    return {
      type: "workspaceWrite",
      writableRoots: [cwd],
      networkAccess: true,
      excludeTmpdirEnvVar: false,
      excludeSlashTmp: false,
    };
  }
  if (mode === "danger-full-access") return { type: "dangerFullAccess" };
  throw new Error(`Unsupported sandbox: ${mode}`);
}

function requireArgs(args) {
  if (args.help) return;
  for (const key of ["cwd", "title"]) {
    if (!args[key]) throw new Error(`Missing --${key}`);
  }
  if (!args.prompt && !args.promptFile) {
    throw new Error("Pass --prompt or --prompt-file");
  }
}

const args = parseArgs(process.argv.slice(2));
if (args.help) {
  usage();
  process.exit(0);
}
requireArgs(args);

const prompt = args.promptFile ? readFileSync(args.promptFile, "utf8") : args.prompt;
const proc = spawn("codex", ["app-server", "--listen", "stdio://"], {
  stdio: ["pipe", "pipe", "pipe"],
});
const rl = readline.createInterface({ input: proc.stdout });
const stderr = [];
let threadId = null;
let completed = false;
let verified = false;
let finalText = "";
let commandCount = 0;
let fileChangeCount = 0;

function send(message) {
  proc.stdin.write(`${JSON.stringify(message)}\n`);
}

function finish(code) {
  clearTimeout(timer);
  console.log(JSON.stringify({
    threadId,
    title: args.title,
    cwd: args.cwd,
    completed,
    verified,
    commandCount,
    fileChangeCount,
    finalText: finalText.trim(),
  }));
  proc.stdin.end();
  proc.kill();
  process.exit(code);
}

const timer = setTimeout(() => {
  console.error(`Timed out after ${args.timeoutMs}ms`);
  if (stderr.length) console.error(stderr.join(""));
  finish(2);
}, args.timeoutMs);

proc.stderr.on("data", (chunk) => stderr.push(chunk.toString()));

rl.on("line", (line) => {
  let message;
  try {
    message = JSON.parse(line);
  } catch {
    return;
  }

  if (message.id === 1 && message.result?.thread?.id && !threadId) {
    threadId = message.result.thread.id;
    console.log(JSON.stringify({ event: "thread_started", threadId, cwd: message.result.cwd }));
    send({ method: "thread/name/set", id: 2, params: { threadId, name: args.title } });
    send({
      method: "turn/start",
      id: 3,
      params: {
        threadId,
        input: [{ type: "text", text: prompt, text_elements: [] }],
        cwd: args.cwd,
        runtimeWorkspaceRoots: [args.cwd],
        approvalPolicy: args.approval,
        sandboxPolicy: sandboxPolicy(args.sandbox, args.cwd),
        effort: args.effort,
      },
    });
    return;
  }

  if (message.id === 2) {
    console.log(JSON.stringify({ event: "name_set", name: message.result?.thread?.name ?? args.title }));
    return;
  }

  if (message.id === 3) {
    console.log(JSON.stringify({ event: "turn_started", ok: Boolean(message.result), error: message.error ?? null }));
    return;
  }

  if (message.method === "item/completed") {
    const item = message.params?.item;
    if (item?.type === "agentMessage" && item.text) {
      finalText = item.text;
      console.log(JSON.stringify({ event: "agent_message", chars: item.text.length }));
    } else if (item?.type === "commandExecution") {
      commandCount += 1;
      console.log(JSON.stringify({ event: "command", status: item.status ?? null, exitCode: item.exitCode ?? null }));
    } else if (item?.type === "fileChange") {
      fileChangeCount += 1;
      console.log(JSON.stringify({ event: "file_change", status: item.status ?? null }));
    }
    return;
  }

  if (message.method === "turn/completed") {
    completed = true;
    send({
      method: "thread/list",
      id: 4,
      params: {
        limit: 10,
        sourceKinds: ["cli", "vscode", "appServer"],
        archived: false,
        searchTerm: args.title,
      },
    });
    return;
  }

  if (message.id === 4) {
    verified = Array.isArray(message.result?.data)
      && message.result.data.some((thread) => thread.id === threadId && thread.name === args.title);
    finish(verified ? 0 : 3);
  }
});

send({
  method: "initialize",
  id: 0,
  params: {
    clientInfo: {
      name: "codex_thread_manager",
      title: "Codex Thread Manager",
      version: "0.1.0",
    },
    capabilities: { experimentalApi: true, requestAttestation: false },
  },
});
send({ method: "initialized", params: {} });
send({
  method: "thread/start",
  id: 1,
  params: {
    cwd: args.cwd,
    approvalPolicy: args.approval,
    sandbox: args.sandbox,
    threadSource: "user",
  },
});
