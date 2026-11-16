// Real Pi loader/session tests for extensions/causal-guard.ts. Fake model output and a fake store script;
// no provider calls, no network, no user credentials. Covers the SPEC.md acceptance criteria for the gate, the loader, and the tool list.
// Run: node --test tests/
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { piSdkUrl } from './pi-sdk.mjs';
const { createAgentSession, DefaultResourceLoader, ModelRuntime, SessionManager, SettingsManager } = await import(piSdkUrl('dist', 'bundle', 'index.js'));
const { createAssistantMessageEventStream } = await import(piSdkUrl('node_modules', '@earendil-works', 'pi-ai', 'dist', 'index.js'));

const EXT = fileURLToPath(new URL('../extensions/causal-guard.ts', import.meta.url));
const FAKE_STORE = fileURLToPath(new URL('./fake_causal_store.py', import.meta.url));
const TOOLS = ['causal_check', 'causal_model_save', 'causal_model_update', 'causal_record'];
const QUESTION = 'Why did checkout conversion drop after the widget change?';

const readJsonl = (f) => (existsSync(f) ? readFileSync(f, 'utf8').trim().split('\n').filter(Boolean).map((l) => JSON.parse(l)) : []);

// Builds a real Pi session with only this extension. `replies` is the scripted model output, one item per request:
// a string (final text) or {tool, args} (one tool call).
async function setup(t, replies, env = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'causal-guard-runtime-'));
  const saved = {};
  const vars = {
    PI_CAUSAL_STORE_SCRIPT: FAKE_STORE,
    FAKE_CAUSAL_STORE_LOG: join(dir, 'store-calls.jsonl'),
    USERPROFILE: dir, // keeps ~/.pi/agent/logs/causal-guard.jsonl inside the temp folder
    HOME: dir,
    PI_CAUSAL_GUARD: undefined,
    PI_SUBAGENT_CHILD: undefined,
    ...env,
  };
  for (const [k, v] of Object.entries(vars)) {
    saved[k] = process.env[k];
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
  const runtime = await ModelRuntime.create({ authPath: join(dir, 'auth.json'), modelsPath: null, refreshOnCreate: false, allowModelNetwork: false });
  const model = runtime.getModels('openai').find((m) => m.api === 'openai-completions') || runtime.getModels('openai')[0];
  assert.ok(model);
  await runtime.setRuntimeApiKey(model.provider, 'fake-never-sent');
  const settingsManager = SettingsManager.inMemory({ retry: { enabled: false }, compaction: { enabled: false } });
  const loader = new DefaultResourceLoader({ cwd: dir, agentDir: join(dir, 'agent'), settingsManager,
    noExtensions: true, additionalExtensionPaths: [EXT],
    noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true });
  await loader.reload();
  assert.deepEqual(loader.getExtensions().errors, []);
  const manager = SessionManager.inMemory(dir);
  const { session } = await createAgentSession({ cwd: dir, agentDir: join(dir, 'agent'), modelRuntime: runtime, model, resourceLoader: loader, settingsManager, sessionManager: manager });
  const errors = [];
  await session.bindExtensions({ mode: 'json', onError: (e) => errors.push(e) });
  t.after(async () => {
    await session.abort();
    session.dispose();
    for (const [k, v] of Object.entries(saved)) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
    rmSync(dir, { recursive: true, force: true });
  });
  const contexts = [];
  runtime.streamSimple = () => { throw new Error('Network forbidden'); };
  session.agent.streamFunction = (_model, context) => {
    contexts.push(JSON.parse(JSON.stringify(context.messages)));
    const i = contexts.length - 1;
    assert.ok(i < replies.length, `unexpected model request ${i + 1}`);
    const r = replies[i];
    const tool = typeof r !== 'string';
    const message = { role: 'assistant', api: model.api, provider: model.provider, model: model.id, timestamp: Date.now(),
      usage: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
      stopReason: tool ? 'toolUse' : 'stop',
      content: tool ? [{ type: 'toolCall', id: `call-${i}`, name: r.tool, arguments: r.args }] : [{ type: 'text', text: r }] };
    const stream = createAssistantMessageEventStream();
    queueMicrotask(() => { stream.push({ type: 'start', partial: message }); stream.push({ type: 'done', reason: message.stopReason, message }); });
    return stream;
  };
  const customs = (type) => session.messages.filter((m) => m.role === 'custom' && m.customType === type);
  const toolResults = (name) => session.messages.filter((m) => m.role === 'toolResult' && m.toolName === name);
  const storeCalls = () => readJsonl(join(dir, 'store-calls.jsonl'));
  const guardLog = () => readJsonl(join(dir, '.pi', 'agent', 'logs', 'causal-guard.jsonl'));
  return { session, errors, contexts, customs, toolResults, storeCalls, guardLog };
}

test('tools: the extension registers the 4 causal tools in a real session', { timeout: 30000 }, async (t) => {
  const { session } = await setup(t, []);
  const all = session.getAllTools().map((x) => x.name);
  const active = session.getActiveToolNames();
  for (const name of TOOLS) {
    assert.ok(all.includes(name), `${name} registered`);
    assert.ok(active.includes(name), `${name} active`);
  }
});

test('gate (a): a question without causal_record gets exactly 1 gate continuation, then settles after the record', { timeout: 30000 }, async (t) => {
  const s = await setup(t, [
    'Conversion dropped because of the widget change.',
    { tool: 'causal_record', args: { domain: 'checkout', model: 'fake-model', causal: false, update: 'none', note: 'no causal claim' } },
    'Recorded.',
  ]);
  await s.session.prompt(QUESTION);
  assert.deepEqual(s.errors, []);
  assert.equal(s.contexts.length, 3);
  // The preflight ran once (lookup) and was injected before the first model request.
  assert.deepEqual(s.storeCalls().map((c) => c.cmd), ['lookup']);
  assert.equal(s.customs('causal-guard').length, 1);
  const first = JSON.stringify(s.contexts[0]);
  assert.match(first, /causal-guard preflight/);
  assert.match(first, /Domain: checkout/);
  assert.match(first, /docs\/checkout\/notes\.md/);
  assert.match(first, /python tools\/session_search\.py --recent checkout/);
  // Exactly one gate note, seen by the second request.
  assert.equal(s.customs('causal-guard-gate').length, 1);
  assert.match(JSON.stringify(s.contexts[1]), /Call causal_record now/);
  const rec = s.toolResults('causal_record');
  assert.equal(rec.length, 1);
  assert.equal(rec[0].isError, false);
  const gates = s.guardLog().filter((e) => e.event === 'gate').map((e) => e.decision);
  assert.deepEqual(gates, ['continue', 'pass']);
});

test('gate (b): a short reply ("yes") gets no injection and no gate', { timeout: 30000 }, async (t) => {
  const s = await setup(t, ['Done.']);
  await s.session.prompt('yes');
  assert.deepEqual(s.errors, []);
  assert.equal(s.contexts.length, 1);
  assert.deepEqual(s.storeCalls(), []);
  assert.equal(s.customs('causal-guard').length, 0);
  assert.equal(s.customs('causal-guard-gate').length, 0);
});

test('gate (c): with PI_SUBAGENT_CHILD=1 nothing happens', { timeout: 30000 }, async (t) => {
  const s = await setup(t, ['Conversion dropped because of the widget change.'], { PI_SUBAGENT_CHILD: '1' });
  await s.session.prompt(QUESTION);
  assert.deepEqual(s.errors, []);
  assert.equal(s.contexts.length, 1);
  assert.deepEqual(s.storeCalls(), []);
  assert.equal(s.session.messages.filter((m) => m.role === 'custom').length, 0);
  const all = s.session.getAllTools().map((x) => x.name);
  assert.ok(!TOOLS.some((n) => all.includes(n)));
  assert.deepEqual(s.guardLog(), []);
});

test('gate (d): causal_record {causal:true} with an unknown check id returns an error; a check_id from causal_check passes', { timeout: 30000 }, async (t) => {
  const claim = { id: 'c1', text: 'widget change lowered conversion', cause: 'widget', effect: 'conversion', claimed_paths: [['widget', 'conversion']], design: 'randomized' };
  const s = await setup(t, [
    { tool: 'causal_check', args: { model: 'fake-model', claims: [claim] } },
    { tool: 'causal_record', args: { domain: 'checkout', model: 'fake-model', causal: true, check_ids: ['fake-model@r9#nope-000000'], update: 'none', note: 'cites a wrong id' } },
    { tool: 'causal_record', args: { domain: 'checkout', model: 'fake-model', causal: true, check_ids: ['fake-model@r1#c1-abcdef'], update: 'none', note: 'checked' } },
    'The widget change lowered conversion (check fake-model@r1#c1-abcdef).',
  ]);
  await s.session.prompt(QUESTION);
  assert.deepEqual(s.errors, []);
  assert.equal(s.contexts.length, 4);
  const check = s.toolResults('causal_check');
  assert.equal(check[0].isError, false);
  assert.equal(check[0].details.result.results[0].check_id, 'fake-model@r1#c1-abcdef');
  const checkCall = s.storeCalls().find((c) => c.cmd === 'check');
  assert.deepEqual(checkCall.stdin.claims[0].conditioned_on, []);
  const rec = s.toolResults('causal_record');
  assert.equal(rec.length, 2);
  assert.equal(rec[0].isError, true);
  assert.match(rec[0].content[0].text, /not in checks\.jsonl: fake-model@r9#nope-000000/);
  assert.equal(rec[1].isError, false);
  assert.equal(s.customs('causal-guard-gate').length, 0);
  const records = s.guardLog().filter((e) => e.event === 'record');
  assert.equal(records.length, 1);
  assert.deepEqual(records[0].check_ids, ['fake-model@r1#c1-abcdef']);
});

test('G2 and budget: a new model without session search gets 2 continuations, then budget-exhausted', { timeout: 30000 }, async (t) => {
  const s = await setup(t, [
    { tool: 'causal_model_save', args: { model: { id: 'new-model', domain: 'checkout', keywords: ['widget'] } } },
    'Answer without a record.',
    'Still no record.',
    'Still no record.',
  ]);
  await s.session.prompt(QUESTION);
  assert.deepEqual(s.errors, []);
  assert.equal(s.contexts.length, 4);
  assert.equal(s.toolResults('causal_model_save')[0].details.result.created, true);
  const notes = s.customs('causal-guard-gate');
  assert.equal(notes.length, 2);
  assert.match(notes[0].content, /without a session search/);
  assert.match(notes[0].content, /python tools\/session_search\.py --recent new-model/);
  const gates = s.guardLog().filter((e) => e.event === 'gate');
  assert.deepEqual(gates.map((e) => e.decision), ['continue', 'continue', 'budget-exhausted']);
  assert.deepEqual(gates[0].reasons, ['G1', 'G2']);
});

test('G2: a tool call that contains the configured command name satisfies it', { timeout: 30000 }, async (t) => {
  const s = await setup(t, [
    { tool: 'causal_model_save', args: { model: { id: 'new-model', domain: 'checkout' } } },
    { tool: 'causal_record', args: { domain: 'checkout', model: 'new-model', causal: false, update: 'applied' } },
    'Answer.',
    { tool: 'bash', args: { command: 'echo tools/session_search.py --recent widget' } },
    'Answer after the search.',
  ]);
  await s.session.prompt(QUESTION);
  assert.deepEqual(s.errors, []);
  const notes = s.customs('causal-guard-gate');
  assert.equal(notes.length, 1);
  assert.match(notes[0].content, /without a session search/);
  assert.doesNotMatch(notes[0].content, /Call causal_record now/);
  const gates = s.guardLog().filter((e) => e.event === 'gate');
  assert.deepEqual(gates.map((e) => e.decision), ['continue', 'pass']);
  assert.deepEqual(gates[0].reasons, ['G2']);
});

test('G2 is off when no session search command is configured', { timeout: 30000 }, async (t) => {
  const s = await setup(t, [
    { tool: 'causal_model_save', args: { model: { id: 'new-model', domain: 'checkout' } } },
    { tool: 'causal_record', args: { domain: 'checkout', model: 'new-model', causal: false, update: 'applied' } },
    'Answer.',
  ]);
  await s.session.prompt('NO_SEARCH_COMMAND why did checkout conversion drop?');
  assert.deepEqual(s.errors, []);
  assert.equal(s.contexts.length, 3);
  assert.match(JSON.stringify(s.contexts[0]), /search earlier sessions with the tool you have, if any/);
  assert.equal(s.customs('causal-guard-gate').length, 0);
  assert.deepEqual(s.guardLog().filter((e) => e.event === 'gate').map((e) => e.decision), ['pass']);
});

test('save validation error (exit 2) is returned as the tool result text', { timeout: 30000 }, async (t) => {
  const s = await setup(t, [
    { tool: 'causal_model_save', args: { model: { id: 'bad', domain: 'checkout', invalid: true } } },
    { tool: 'causal_record', args: { domain: 'checkout', causal: false, update: 'none' } },
    'Done.',
  ]);
  await s.session.prompt(QUESTION);
  const r = s.toolResults('causal_model_save')[0];
  assert.equal(r.isError, true);
  assert.match(r.content[0].text, /validation: edge e1 is dangling/);
});

test('lookup failure injects the error and the steps, and does not block', { timeout: 30000 }, async (t) => {
  const s = await setup(t, [
    { tool: 'causal_record', args: { domain: 'general', causal: false, update: 'none' } },
    'Answer.',
  ]);
  await s.session.prompt('FAIL_LOOKUP why did the metric drop last week?');
  assert.deepEqual(s.errors, []);
  assert.equal(s.contexts.length, 2);
  const first = JSON.stringify(s.contexts[0]);
  assert.match(first, /lookup failed/);
  assert.match(first, /fake lookup failure/);
  assert.match(first, /At the end: call causal_record/);
});

test('/causal-guard off switches the guard off for the session; on switches it back', { timeout: 30000 }, async (t) => {
  const s = await setup(t, ['Answer.']);
  await s.session.prompt('/causal-guard off');
  await s.session.prompt(QUESTION);
  assert.deepEqual(s.errors, []);
  assert.equal(s.contexts.length, 1);
  assert.deepEqual(s.storeCalls(), []);
  assert.equal(s.customs('causal-guard').length, 0);
  await s.session.prompt('/causal-guard on');
  await s.session.prompt('/causal-guard status');
  assert.deepEqual(s.guardLog().map((e) => e.state ?? e.event), ['off', 'on']);
});
