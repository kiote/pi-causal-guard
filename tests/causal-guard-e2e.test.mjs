// End-to-end: real Pi session + real bin/causal_store.py + a temp store. Fake model output, no network.
// Flow: preflight lookup -> causal_model_save (new model) -> session search -> causal_check -> causal_record.
// Run: node --test tests/causal-guard-e2e.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, readFileSync, rmSync, existsSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import { piSdkUrl } from './pi-sdk.mjs';
const { createAgentSession, DefaultResourceLoader, ModelRuntime, SessionManager, SettingsManager } = await import(piSdkUrl('dist', 'bundle', 'index.js'));
const { createAssistantMessageEventStream } = await import(piSdkUrl('node_modules', '@earendil-works', 'pi-ai', 'dist', 'index.js'));

const EXT = fileURLToPath(new URL('../extensions/causal-guard.ts', import.meta.url));

const MODEL = {
  schema_version: 2, id: 'e2e-confounder', domain: 'e2e-domain', title: 'E2E confounder', revision: 0,
  status: 'hypothesis', last_gathered: '2026-11-13', keywords: ['widget'],
  variables: [
    { id: 'X', label: 'Treatment', definition: 'Feature on or off' },
    { id: 'Y', label: 'Outcome', definition: 'Metric' },
    { id: 'U', label: 'User mix', definition: 'Confounder' },
  ],
  edges: [
    { id: 'x_y', from: 'X', to: 'Y', status: 'assumed', reason: 'feature changes metric', sources: ['s1'] },
    { id: 'u_x', from: 'U', to: 'X', status: 'assumed', reason: 'mix drives opt-in', sources: ['s1'] },
    { id: 'u_y', from: 'U', to: 'Y', status: 'assumed', reason: 'mix drives metric', sources: ['s1'] },
  ],
  sources: { s1: { ref: 'test', use: 'e2e' } },
};
const CLAIM = { id: 'c1', text: 'X raised Y', cause: 'X', effect: 'Y', claimed_paths: [['X', 'Y']], design: 'observational' };

test('e2e: real store, new model, check, record; gate passes without a note', { timeout: 120000 }, async (t) => {
  const dir = mkdtempSync(join(tmpdir(), 'causal-guard-e2e-'));
  const store = join(dir, 'store');
  // A tiny repository with an origin/main ref, used as search_repo.
  const repo = join(dir, 'docs-repo');
  execFileSync('git', ['init', '-q', repo]);
  writeFileSync(join(repo, 'widget.md'), 'widget notes\n');
  execFileSync('git', ['-C', repo, 'add', '-A']);
  execFileSync('git', ['-C', repo, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'x']);
  execFileSync('git', ['-C', repo, 'update-ref', 'refs/remotes/origin/main', 'HEAD']);
  mkdirSync(store, { recursive: true });
  writeFileSync(join(store, 'config.yaml'),
    `search_repo: ${repo.replace(/\\/g, '/')}\nsearch_paths: ['.']\nsession_search_command: python tools/session_search.py --recent\nstale_days: 7\n`);
  const saved = {};
  const vars = { PI_CAUSAL_STORE: store, USERPROFILE: dir, HOME: dir, PI_CAUSAL_GUARD: undefined, PI_SUBAGENT_CHILD: undefined, PI_CAUSAL_STORE_SCRIPT: undefined };
  for (const [k, v] of Object.entries(vars)) { saved[k] = process.env[k]; if (v === undefined) delete process.env[k]; else process.env[k] = v; }
  const runtime = await ModelRuntime.create({ authPath: join(dir, 'auth.json'), modelsPath: null, refreshOnCreate: false, allowModelNetwork: false });
  const model = runtime.getModels('openai').find((m) => m.api === 'openai-completions') || runtime.getModels('openai')[0];
  await runtime.setRuntimeApiKey(model.provider, 'fake-never-sent');
  const settingsManager = SettingsManager.inMemory({ retry: { enabled: false }, compaction: { enabled: false } });
  const loader = new DefaultResourceLoader({ cwd: dir, agentDir: join(dir, 'agent'), settingsManager, noExtensions: true, additionalExtensionPaths: [EXT], noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true });
  await loader.reload();
  assert.deepEqual(loader.getExtensions().errors, []);
  const { session } = await createAgentSession({ cwd: dir, agentDir: join(dir, 'agent'), modelRuntime: runtime, model, resourceLoader: loader, settingsManager, sessionManager: SessionManager.inMemory(dir) });
  const errors = [];
  await session.bindExtensions({ mode: 'json', onError: (e) => errors.push(e) });
  t.after(async () => {
    await session.abort(); session.dispose();
    for (const [k, v] of Object.entries(saved)) { if (v === undefined) delete process.env[k]; else process.env[k] = v; }
    rmSync(dir, { recursive: true, force: true });
  });
  let n = 0, checkId = null;
  const contexts = [];
  runtime.streamSimple = () => { throw new Error('Network forbidden'); };
  session.agent.streamFunction = (_m, context) => {
    contexts.push(JSON.stringify(context.messages));
    n++;
    if (n === 4) {
      const m = /"check_id":\s*"([^"]+)"/.exec(contexts[3]);
      checkId = m && m[1];
    }
    const steps = [
      { tool: 'causal_model_save', args: { model: MODEL } },
      { tool: 'bash', args: { command: 'echo tools/session_search.py --recent widget' } },
      { tool: 'causal_check', args: { model: 'e2e-confounder', claims: [CLAIM] } },
      () => ({ tool: 'causal_record', args: { domain: 'e2e-domain', model: 'e2e-confounder', causal: true, check_ids: [checkId], update: 'none', note: 'checked' } }),
      `X -> Y does not follow: open backdoor via U (${checkId}).`,
    ];
    let r = steps[n - 1];
    assert.ok(r, `unexpected request ${n}`);
    if (typeof r === 'function') r = r();
    const tool = typeof r !== 'string';
    const message = { role: 'assistant', api: model.api, provider: model.provider, model: model.id, timestamp: Date.now(),
      usage: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
      stopReason: tool ? 'toolUse' : 'stop',
      content: tool ? [{ type: 'toolCall', id: `call-${n}`, name: r.tool, arguments: r.args }] : [{ type: 'text', text: r }] };
    const stream = createAssistantMessageEventStream();
    queueMicrotask(() => { stream.push({ type: 'start', partial: message }); stream.push({ type: 'done', reason: message.stopReason, message }); });
    return stream;
  };
  await session.prompt('Did the new widget feature raise the conversion metric last week?');
  assert.deepEqual(errors, []);
  assert.equal(n, 5, 'no gate continuation');
  assert.match(contexts[0], /causal-guard preflight/);
  assert.match(contexts[0], /widget\.md/, 'repository hit from the temp repo');
  assert.match(contexts[0], /python tools\/session_search\.py --recent/);
  const results = session.messages.filter((m) => m.role === 'toolResult');
  const byName = (name) => results.filter((m) => m.toolName === name);
  assert.equal(byName('causal_model_save')[0].isError, false, byName('causal_model_save')[0].content[0].text);
  assert.match(byName('causal_check')[0].content[0].text, /does_not_follow/);
  assert.ok(checkId && checkId.startsWith('e2e-confounder@r1#c1-'), String(checkId));
  assert.equal(byName('causal_record')[0].isError, false, byName('causal_record')[0].content[0].text);
  assert.equal(session.messages.filter((m) => m.role === 'custom' && m.customType === 'causal-guard-gate').length, 0);
  assert.ok(existsSync(join(store, 'models', 'e2e-confounder.yaml')));
  const reg = readFileSync(join(store, 'registry.yaml'), 'utf8');
  assert.match(reg, /e2e-domain/);
  assert.match(reg, /widget/);
  const log = execFileSync('git', ['-C', store, 'log', '--oneline']).toString();
  assert.match(log, /e2e-confounder/);
});
