// causal-guard: on each user question, run the causal preflight (domain, repository hits, model lookup),
// give the agent 4 tools for the causal model store, and gate the end of the run on causal_record.
// Spec: SPEC.md, component C-2. Python helper: bin/causal_store.py (C-1).
// Off: PI_CAUSAL_GUARD=off, PI_SUBAGENT_CHILD set, or `/causal-guard off` for the session.
// Helper overrides: PI_CAUSAL_STORE_SCRIPT (script path), PI_CAUSAL_PYTHON (python executable).
// Log: ~/.pi/agent/logs/causal-guard.jsonl. Tests: node --test tests/
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { execFile } from "node:child_process";
import { appendFileSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

export const TYPE = "causal-guard";
export const GATE_TYPE = "causal-guard-gate";
export const MAX_CONTINUATIONS = 2;
const LOOKUP_TIMEOUT_MS = 8000;
const TOOL_TIMEOUT_MS = 60000;

// A prompt that is only short-reply tokens: "yes", "ok do it", "lgtm, go ahead", "A1", "2".
const SHORT_TOKEN =
  "(?:y|yes|yep|yeah|no|nope|ok|okay|k|sure|thanks|thank you|thx|ty|lgtm|go|go ahead|do it|do that|done|continue|proceed|next|now|please|pls|merge|ship it|sounds good|agreed|approved?|[a-e]\\d{0,2}|\\d{1,3}|option [a-e1-9]\\d?)";
export const SHORT_REPLY = new RegExp(`^\\s*(?:${SHORT_TOKEN}\\b[\\s,.!;:]*)+$`, "i");
const SHORT_REPLY_MAX_CHARS = 80;

// True when the prompt is a question that needs the preflight: 4 or more words and not a short reply.
// The regex runs only on short prompts, and tokens end at a word boundary (no exponential backtracking).
export function needsPreflight(prompt: string): boolean {
  const words = (prompt ?? "").trim().split(/\s+/).filter(Boolean);
  if (words.length < 4) return false;
  if (prompt.length > SHORT_REPLY_MAX_CHARS) return true;
  return !SHORT_REPLY.test(prompt);
}

export function guardOffByEnv(env: NodeJS.ProcessEnv = process.env): boolean {
  return env.PI_CAUSAL_GUARD === "off" || !!env.PI_SUBAGENT_CHILD;
}

export function storeScriptPath(env: NodeJS.ProcessEnv = process.env): string {
  return env.PI_CAUSAL_STORE_SCRIPT || fileURLToPath(new URL("../bin/causal_store.py", import.meta.url));
}

export type StoreResult = { ok: boolean; code: number | null; json?: any; text: string };

// Runs `python causal_store.py <args>` without a shell, writes `input` as JSON to stdin,
// and parses one JSON object from stdout. Exit code 2 is a validation error; its text goes to the tool result.
export function runStore(args: string[], input: unknown, timeoutMs: number): Promise<StoreResult> {
  const python = process.env.PI_CAUSAL_PYTHON || (process.platform === "win32" ? "python" : "python3");
  return new Promise((resolve) => {
    let child: ReturnType<typeof execFile>;
    try {
      child = execFile(
        python,
        [storeScriptPath(), ...args],
        { timeout: timeoutMs, windowsHide: true, maxBuffer: 16 * 1024 * 1024, encoding: "utf8", env: { ...process.env, PYTHONIOENCODING: "utf-8" } },
        (err: any, stdout: string, stderr: string) => {
          const out = (stdout ?? "").trim();
          const errText = (stderr ?? "").trim();
          if (err) {
            const code = typeof err.code === "number" ? err.code : null;
            const reason = err.killed
              ? `causal_store.py ${args[0]} timed out after ${timeoutMs} ms`
              : code === 2
                ? `causal_store.py ${args[0]} validation error`
                : `causal_store.py ${args[0]} failed (${code ?? err.code ?? err.message})`;
            resolve({ ok: false, code, text: [reason, errText, out].filter(Boolean).join("\n") });
            return;
          }
          try {
            resolve({ ok: true, code: 0, json: JSON.parse(out), text: out });
          } catch {
            resolve({ ok: false, code: 0, text: `causal_store.py ${args[0]} printed no JSON object:\n${out.slice(0, 2000)}\n${errText}`.trim() });
          }
        },
      );
    } catch (e: any) {
      resolve({ ok: false, code: null, text: `causal_store.py ${args[0]} could not start: ${e?.message ?? e}` });
      return;
    }
    child.stdin?.on("error", () => {});
    child.stdin?.end(input === undefined ? "" : JSON.stringify(input));
  });
}

type CtxMsg = { role?: string; customType?: string; toolName?: string; isError?: boolean; details?: any; content?: any };

const lastUserIndex = (msgs: CtxMsg[]) => msgs.findLastIndex((m) => m?.role === "user");

export type GateDecision = {
  preflight: boolean; // the preflight message was injected for the last user message
  reasons: ("G1" | "G2")[];
  continuations: number; // gate notes already sent for the last user message
  createdModels: string[];
};

// The name G2 looks for in tool calls: the first path segment of the command that ends in .py, .js, .mjs, or .sh,
// else the first word. Null when no session search command is configured (G2 off).
export function sessionSearchName(command: string | null | undefined): string | null {
  const words = (command ?? "").trim().split(/\s+/).filter(Boolean).map((w) => w.replace(/^["']|["']$/g, ""));
  if (!words.length) return null;
  for (const word of words) {
    const segment = word.split(/[\\/]/).find((s) => /\.(py|js|mjs|sh)$/i.test(s));
    if (segment) return segment;
  }
  return words[0] || null;
}

// Gate rules over the context messages since the last user message.
// G1: no successful causal_record. G2 (only when searchName is set): causal_model_save created a model,
// and no tool call contains searchName.
export function evaluateGate(messages: CtxMsg[], searchName?: string | null): GateDecision {
  const msgs = messages ?? [];
  const run = msgs.slice(lastUserIndex(msgs) + 1);
  const preflight = run.some((m) => m.role === "custom" && m.customType === TYPE);
  const continuations = run.filter((m) => m.role === "custom" && m.customType === GATE_TYPE).length;
  const recorded = run.some((m) => m.role === "toolResult" && m.toolName === "causal_record" && !m.isError && m.details?.ok === true);
  const createdModels = run
    .filter((m) => m.role === "toolResult" && m.toolName === "causal_model_save" && !m.isError && m.details?.result?.created === true)
    .map((m) => String(m.details.result.id ?? "?"));
  const searched =
    !!searchName &&
    run.some(
      (m) =>
        m.role === "assistant" &&
        Array.isArray(m.content) &&
        m.content.some((c: any) => c?.type === "toolCall" && JSON.stringify(c.arguments ?? {}).includes(searchName)),
    );
  const reasons: GateDecision["reasons"] = [];
  if (!recorded) reasons.push("G1");
  if (searchName && createdModels.length && !searched) reasons.push("G2");
  return { preflight, reasons, continuations, createdModels };
}

// The configured session search command with the keywords appended.
export function searchCommand(command: string, keywords: string[]): string {
  const terms = keywords.length ? keywords.map((k) => (/^[\w.-]+$/.test(k) ? k : `"${k}"`)).join(" ") : "<topic keywords>";
  return `${command.trim()} ${terms}`;
}

const STOP = new Set(
  "about above after again also because been before being between both could does doing down during each from have having here into just more most other over same should some such than that their them then there these they this those through under until very want what when where which while will with would your".split(" "),
);

export function promptKeywords(prompt: string, max = 4): string[] {
  const seen: string[] = [];
  for (const w of (prompt ?? "").toLowerCase().match(/[a-z][a-z0-9_-]{3,}/g) ?? []) {
    if (STOP.has(w) || seen.includes(w)) continue;
    seen.push(w);
    if (seen.length >= max) break;
  }
  return seen;
}

function steps(searchCmd: string | null): string {
  const search = searchCmd
    ? `run the session search first:\n   ${searchCmd}\n   Then read the relevant part of its output, and call`
    : "search earlier sessions with the tool you have, if any. Then call";
  return `Required steps (causal-guard):
1. If the domain is none: pick one of the listed domains, or a new slug for a new domain.
2. Read the relevant repository hits (read-only).
3. If there is no model for the domain, or the model is stale: ${search} causal_model_save (new model) or causal_model_update (new facts).
4. If the answer makes a causal claim: call causal_check on the model and cite the check_id of each claim.
5. At the end: call causal_record (causal: true needs a check_id from causal_check, or a note that starts with "unchecked:").`;
}

export function preflightMessage(lookup: StoreResult, prompt: string): string {
  if (!lookup.ok) {
    return `causal-guard preflight: lookup failed. Continue without it.\n${lookup.text.slice(0, 1500)}\n\n${steps(null)}`;
  }
  const j = lookup.json ?? {};
  const domain = j.domain ? `${j.domain} (${j.domain_source ?? "keyword"})` : "none";
  const models: any[] = Array.isArray(j.models) ? j.models : [];
  const hits: string[] = Array.isArray(j.repo_hits) ? j.repo_hits : [];
  const lines = [
    "causal-guard preflight",
    `Domain: ${domain}`,
    `Known domains: ${(j.domains ?? []).join(", ") || "(none)"}`,
    "Models:",
    ...(models.length
      ? models.map((m) => `- ${m.id} r${m.revision} ${m.path} (${m.stale ? "STALE" : "fresh"}, last gathered ${m.last_gathered})`)
      : ["- (no model for this domain)"]),
    "Repository hits:",
    ...(hits.length ? hits.map((h) => `- ${h}`) : ["- (none)"]),
  ];
  if (j.repo_error) lines.push(`Repository error: ${j.repo_error}`);
  const keywords = j.domain ? [String(j.domain), ...promptKeywords(prompt, 3)] : promptKeywords(prompt);
  const command = typeof j.session_search_command === "string" && j.session_search_command.trim() ? j.session_search_command : null;
  return `${lines.join("\n")}\n\n${steps(command ? searchCommand(command, keywords) : null)}`;
}

export function gateNote(d: GateDecision, sessionSearchCommand?: string | null): string {
  const parts = ["causal-guard: the causal flow for this question is not complete."];
  if (d.reasons.includes("G2") && sessionSearchCommand) {
    parts.push(
      `You created the model ${d.createdModels.join(", ")} in this run without a session search. Run the session search now:\n${searchCommand(sessionSearchCommand, d.createdModels)}\nThen call causal_model_update with any new facts from it.`,
    );
  }
  if (d.reasons.includes("G1")) {
    parts.push(
      'Call causal_record now: domain, model, causal (true if the answer relies on causal knowledge), check_ids from causal_check, update ("none", "applied", or "proposed"), and note. With causal: true and no check, start the note with "unchecked:".',
    );
  }
  return parts.join("\n");
}

function log(entry: Record<string, unknown>, cwd?: string, sessionId?: string) {
  try {
    const dir = join(homedir(), ".pi", "agent", "logs");
    mkdirSync(dir, { recursive: true });
    appendFileSync(join(dir, "causal-guard.jsonl"), JSON.stringify({ ts: new Date().toISOString(), cwd, sessionId, ...entry }) + "\n");
  } catch {}
}

const text = (t: string) => [{ type: "text" as const, text: t }];

const ClaimSchema = Type.Object({
  id: Type.String(),
  text: Type.Optional(Type.String()),
  cause: Type.String({ description: "variable id" }),
  effect: Type.String({ description: "variable id" }),
  claimed_paths: Type.Array(Type.Array(Type.String()), { description: "each path is a list of variable ids from cause to effect" }),
  conditioned_on: Type.Optional(Type.Array(Type.String())),
  ruled_out: Type.Optional(Type.Array(Type.Array(Type.String()))),
  design: Type.String({ enum: ["randomized", "observational"] }),
});

export default function (pi: ExtensionAPI) {
  if (guardOffByEnv()) return;
  let sessionOff = false;
  let lastLookup: { at: string; prompt: string; ok: boolean; domain?: string | null; models?: number; hits?: number; error?: string } | undefined;
  // From the last lookup; G2 is off while it is null.
  let sessionSearchCommand: string | null = null;
  let lastRecord: Record<string, unknown> | undefined;

  pi.on("before_agent_start", async (event, ctx) => {
    if (sessionOff || event.messageType !== "user") return;
    const prompt = event.prompt ?? "";
    if (!needsPreflight(prompt)) return;
    const started = Date.now();
    let lookup: StoreResult;
    let dir: string | undefined;
    try {
      dir = mkdtempSync(join(tmpdir(), "causal-guard-"));
      const file = join(dir, "prompt.txt");
      writeFileSync(file, prompt, "utf8");
      lookup = await runStore(["lookup", "--prompt-file", file], undefined, LOOKUP_TIMEOUT_MS);
    } catch (e) {
      lookup = { ok: false, code: null, text: `preflight temp file error: ${(e as Error)?.message ?? e}` };
    } finally {
      if (dir) rmSync(dir, { recursive: true, force: true });
    }
    lastLookup = {
      at: new Date().toISOString(),
      prompt: prompt.slice(0, 200),
      ok: lookup.ok,
      domain: lookup.json?.domain ?? null,
      models: lookup.json?.models?.length ?? 0,
      hits: lookup.json?.repo_hits?.length ?? 0,
      error: lookup.ok ? lookup.json?.repo_error : lookup.text.slice(0, 500),
    };
    const command = lookup.ok ? lookup.json?.session_search_command : null;
    sessionSearchCommand = typeof command === "string" && command.trim() ? command : null;
    log({ event: "lookup", ms: Date.now() - started, ...lastLookup }, ctx.cwd, ctx.sessionManager?.getSessionId?.());
    return { message: { customType: TYPE, content: preflightMessage(lookup, prompt), display: false } };
  });

  pi.on("agent_before_settle", async (event, ctx) => {
    if (sessionOff || event.outcome !== "completed") return;
    const d = evaluateGate((event.context?.contextMessages ?? []) as CtxMsg[], sessionSearchName(sessionSearchCommand));
    if (!d.preflight) return;
    const sessionId = ctx.sessionManager?.getSessionId?.();
    if (!d.reasons.length) {
      log({ event: "gate", decision: "pass", continuations: d.continuations }, ctx.cwd, sessionId);
      return;
    }
    if (d.continuations >= MAX_CONTINUATIONS) {
      log({ event: "gate", decision: "budget-exhausted", reasons: d.reasons, continuations: d.continuations }, ctx.cwd, sessionId);
      ctx.ui?.notify(`causal-guard reached its continuation limit (${d.reasons.join(", ")} still open).`, "warning");
      return;
    }
    log({ event: "gate", decision: "continue", reasons: d.reasons, continuations: d.continuations + 1 }, ctx.cwd, sessionId);
    return {
      entries: [...event.entries, { type: "custom_message", customType: GATE_TYPE, content: gateNote(d, sessionSearchCommand), display: true }],
      continue: true,
    };
  });

  const storeTool = async (args: string[], input: unknown) => {
    const r = await runStore(args, input, TOOL_TIMEOUT_MS);
    if (!r.ok) return { content: text(r.text), details: { ok: false, result: null }, isError: true };
    return { content: text(JSON.stringify(r.json, null, 2)), details: { ok: true, result: r.json } };
  };

  pi.registerTool({
    name: "causal_check",
    label: "Causal check",
    description:
      "Check causal claims against a causal model in the store (directed paths, backdoor d-separation for observational designs, unclaimed alternative paths, risky edges). Returns one verdict and check_id per claim; cite the check_ids.",
    promptSnippet: "Check causal claims against a stored causal model",
    parameters: Type.Object({ model: Type.String({ description: "model id" }), claims: Type.Array(ClaimSchema) }),
    async execute(_id, params) {
      const claims = params.claims.map((c: any) => ({ text: "", conditioned_on: [], ruled_out: [], ...c }));
      return storeTool(["check", "--model", params.model], { claims });
    },
  });

  pi.registerTool({
    name: "causal_model_save",
    label: "Causal model save",
    description:
      "Create or replace a causal model (schema v2: schema_version, id, domain, title, status, last_gathered, variables, edges, sources). Optional variable role: exposure, outcome, mediator, context, selection, measurement, latent (not measured), or other. Optional edge type: directed (default) or bidirected (an unmeasured common cause of from and to). Add keywords to the model for a new domain. The store validates, bumps the revision, and commits.",
    promptSnippet: "Create or replace a causal model in the store",
    parameters: Type.Object({
      model: Type.Object(
        { id: Type.String(), domain: Type.String(), keywords: Type.Optional(Type.Array(Type.String())) },
        { additionalProperties: true, description: "the full model as JSON" },
      ),
    }),
    async execute(_id, params) {
      return storeTool(["save", "--model", params.model.id], params.model);
    },
  });

  pi.registerTool({
    name: "causal_model_update",
    label: "Causal model update",
    description:
      "Apply new facts and edge or variable changes to a stored causal model. Facts set last_gathered to today. The store validates, bumps the revision, commits, and returns a diff.",
    promptSnippet: "Add facts and edge changes to a stored causal model",
    parameters: Type.Object({
      model: Type.String({ description: "model id" }),
      facts: Type.Optional(Type.Array(Type.Object({ text: Type.String(), source: Type.String() }))),
      add_edges: Type.Optional(Type.Array(Type.Object({}, { additionalProperties: true }))),
      change_edges: Type.Optional(Type.Array(Type.Object({ id: Type.String() }, { additionalProperties: true }))),
      remove_edges: Type.Optional(Type.Array(Type.String())),
      add_variables: Type.Optional(Type.Array(Type.Object({}, { additionalProperties: true }))),
      add_sources: Type.Optional(Type.Object({}, { additionalProperties: true })),
    }),
    async execute(_id, params) {
      const { model, ...changes } = params as any;
      return storeTool(["update", "--model", model], changes);
    },
  });

  pi.registerTool({
    name: "causal_record",
    label: "Causal record",
    description:
      'Record the causal flow for the current question: domain, model, whether the answer relies on causal knowledge, the check_ids from causal_check, and the model update state. causal: true needs at least one existing check_id, or a note that starts with "unchecked:".',
    promptSnippet: "Record the causal flow result at the end of an answer",
    parameters: Type.Object({
      domain: Type.String(),
      model: Type.Optional(Type.String()),
      causal: Type.Boolean(),
      check_ids: Type.Optional(Type.Array(Type.String())),
      update: Type.String({ enum: ["none", "applied", "proposed"] }),
      note: Type.Optional(Type.String()),
    }),
    async execute(_id, params, _signal, _onUpdate, ctx) {
      const checkIds = (params.check_ids ?? []).filter(Boolean);
      const note = (params.note ?? "").trim();
      let missing: string[] = [];
      let unverified = false;
      if (params.causal) {
        if (checkIds.length) {
          const r = await runStore(["has-check", "--ids", checkIds.join(",")], undefined, TOOL_TIMEOUT_MS);
          if (!r.ok && !note.startsWith("unchecked:")) return { content: text(r.text), details: { ok: false, result: null }, isError: true };
          missing = r.ok && Array.isArray(r.json?.missing) ? r.json.missing.map(String) : r.ok ? [] : [...checkIds];
          unverified = !r.ok;
        }
        const found = checkIds.filter((id) => !missing.includes(id));
        if (!found.length && !note.startsWith("unchecked:")) {
          const why = checkIds.length ? `These check_ids are not in checks.jsonl: ${missing.join(", ")}.` : "No check_ids were given.";
          return {
            content: text(`causal_record rejected: causal: true needs at least one existing check_id from causal_check, or a note that starts with "unchecked:". ${why}`),
            details: { ok: false, result: null },
            isError: true,
          };
        }
      }
      lastRecord = { at: new Date().toISOString(), ...params, check_ids: checkIds, missing_check_ids: missing };
      log({ event: "record", ...lastRecord }, ctx?.cwd, ctx?.sessionManager?.getSessionId?.());
      const warn = unverified
        ? ` has-check failed; check_ids not verified: ${checkIds.join(", ")}.`
        : missing.length ? ` Unknown check_ids ignored: ${missing.join(", ")}.` : "";
      return { content: text(`Recorded the causal flow for domain ${params.domain}.${warn}`), details: { ok: true, result: lastRecord } };
    },
  });

  pi.registerCommand("causal-guard", {
    description: "causal-guard: status (last lookup and record), off, or on for this session",
    handler: async (args, ctx) => {
      const arg = (args ?? "").trim().toLowerCase() || "status";
      if (arg === "off" || arg === "on") {
        sessionOff = arg === "off";
        log({ event: "toggle", state: arg }, ctx.cwd, ctx.sessionManager?.getSessionId?.());
        ctx.ui.notify(`causal-guard is ${arg} for this session.`, "info");
        return;
      }
      if (arg !== "status") {
        ctx.ui.notify("Usage: /causal-guard [status|off|on]", "warning");
        return;
      }
      ctx.ui.notify(
        [
          `causal-guard: ${sessionOff ? "off" : "on"} for this session`,
          `Last lookup: ${lastLookup ? JSON.stringify(lastLookup) : "(none)"}`,
          `Last record: ${lastRecord ? JSON.stringify(lastRecord) : "(none)"}`,
        ].join("\n"),
        "info",
      );
    },
  });
}
