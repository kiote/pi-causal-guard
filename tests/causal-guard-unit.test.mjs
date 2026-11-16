// Unit tests for extensions/causal-guard.ts: short-reply skip and gate counting. Run: node --test tests/
import test from 'node:test';
import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
import { piSdkUrl } from './pi-sdk.mjs';

// Node runs the .ts file directly; map the host-provided 'typebox' import to Pi's copy.
const typeboxUrl = piSdkUrl('node_modules', 'typebox', 'build', 'index.mjs');
registerHooks({ resolve: (spec, ctx, next) => (spec === 'typebox' ? { url: typeboxUrl, shortCircuit: true } : next(spec, ctx)) });
const g = await import('../extensions/causal-guard.ts');

test('needsPreflight skips short replies and prompts under 4 words', () => {
  for (const p of ['yes', 'ok', 'lgtm', 'A1', '2', 'do it', 'yes do it now', 'ok, go ahead, thanks', 'yes yes ok lgtm', 'Option B please go ahead', '  ']) {
    assert.equal(g.needsPreflight(p), false, p);
  }
  for (const p of ['Why did the checkout conversion rate drop?', 'yes but why did the metric drop last week', 'Do it for the retention metric']) {
    assert.equal(g.needsPreflight(p), true, p);
  }
  assert.ok(g.SHORT_REPLY.test('A1'));
  assert.ok(!g.SHORT_REPLY.test('A1 caused the drop in clicks'));
});

test('needsPreflight is fast on number and hex runs (no exponential backtracking)', () => {
  const inputs = [
    '1234567 1234568 1234569 1234570 1234571 1234572 why did these fail',
    '1234567 1234568 1234569 1234570 why',
    'abcde0123456789abcde0123456789abcde01234 failed again today',
    '1234567890123456789012345678901234567890 x y z',
  ];
  for (const p of inputs) {
    const t = Date.now();
    assert.equal(g.needsPreflight(p), true, p);
    assert.ok(Date.now() - t < 50, `slow: ${p}`);
    const t2 = Date.now();
    g.SHORT_REPLY.test(p);
    assert.ok(Date.now() - t2 < 50, `slow regex: ${p}`);
  }
});

test('guardOffByEnv and storeScriptPath', () => {
  assert.equal(g.guardOffByEnv({ PI_CAUSAL_GUARD: 'off' }), true);
  assert.equal(g.guardOffByEnv({ PI_SUBAGENT_CHILD: '1' }), true);
  assert.equal(g.guardOffByEnv({}), false);
  assert.match(g.storeScriptPath({}).replace(/\\/g, '/'), /\/bin\/causal_store\.py$/);
  assert.equal(g.storeScriptPath({ PI_CAUSAL_STORE_SCRIPT: 'X.py' }), 'X.py');
});

const user = (text) => ({ role: 'user', content: text });
const pre = { role: 'custom', customType: g.TYPE, content: 'causal-guard preflight' };
const note = { role: 'custom', customType: g.GATE_TYPE, content: 'causal-guard: ...' };
const answer = { role: 'assistant', content: [{ type: 'text', text: 'answer' }] };
const record = (ok) => ({ role: 'toolResult', toolName: 'causal_record', isError: !ok, details: { ok } });
const call = (name, args) => ({ role: 'assistant', content: [{ type: 'toolCall', id: 'c', name, arguments: args }] });
const saved = { role: 'toolResult', toolName: 'causal_model_save', isError: false, details: { ok: true, result: { id: 'm1', created: true } } };

test('evaluateGate counts gate notes only since the last user message', () => {
  const old = [user('first question here please'), pre, note, note, answer];
  let d = g.evaluateGate([...old, user('second question about the metric'), pre, answer]);
  assert.deepEqual([d.preflight, d.continuations, d.reasons], [true, 0, ['G1']]);
  d = g.evaluateGate([...old, user('second question about the metric'), pre, answer, note, answer]);
  assert.equal(d.continuations, 1);
  d = g.evaluateGate([user('q one two three'), pre, answer, note, answer, note, answer]);
  assert.equal(d.continuations, g.MAX_CONTINUATIONS);
});

test('evaluateGate: no preflight, G1 satisfied only by a successful record, G2 needs the session search name', () => {
  assert.equal(g.evaluateGate([user('yes'), answer]).preflight, false);
  // A record from an earlier user message does not count.
  assert.deepEqual(g.evaluateGate([user('a b c d'), pre, record(true), user('e f g h'), pre, answer]).reasons, ['G1']);
  assert.deepEqual(g.evaluateGate([user('a b c d'), pre, record(false), answer]).reasons, ['G1']);
  assert.deepEqual(g.evaluateGate([user('a b c d'), pre, record(true), answer]).reasons, []);
  const d = g.evaluateGate([user('a b c d'), pre, saved, record(true), answer], 'session_search.py');
  assert.deepEqual([d.reasons, d.createdModels], [['G2'], ['m1']]);
  const searched = call('bash', { command: 'python tools/session_search.py widget --recent' });
  assert.deepEqual(g.evaluateGate([user('a b c d'), pre, searched, saved, record(true), answer], 'session_search.py').reasons, []);
  // No configured command: G2 is off.
  assert.deepEqual(g.evaluateGate([user('a b c d'), pre, saved, record(true), answer]).reasons, []);
  assert.deepEqual(g.evaluateGate([user('a b c d'), pre, saved, record(true), answer], null).reasons, []);
});

test('sessionSearchName: first path segment ending in .py/.js/.mjs/.sh, else the first word', () => {
  assert.equal(g.sessionSearchName('python ~/tools/session-search/search.py --recent'), 'search.py');
  assert.equal(g.sessionSearchName('node C:\\tools\\find-sessions.mjs --days 30'), 'find-sessions.mjs');
  assert.equal(g.sessionSearchName('bash "./scripts/sessions.sh"'), 'sessions.sh');
  assert.equal(g.sessionSearchName('node lib/search.js'), 'search.js');
  assert.equal(g.sessionSearchName('session-search --since 30'), 'session-search');
  assert.equal(g.sessionSearchName(''), null);
  assert.equal(g.sessionSearchName(null), null);
  assert.equal(g.sessionSearchName(undefined), null);
});

const lookupJson = (extra = {}) => ({ domain: null, domain_source: 'none', domains: ['a', 'b'], models: [], repo_hits: [], session_search_command: null, ...extra });

test('preflightMessage lists domain, models, repository hits, steps; lookup failure keeps the steps', () => {
  const ok = g.preflightMessage({ ok: true, code: 0, text: '', json: lookupJson({ repo_hits: ['docs/checkout.md'], repo_error: 'slow' }) }, 'why did the clicks drop');
  assert.match(ok, /Domain: none/);
  assert.match(ok, /Known domains: a, b/);
  assert.match(ok, /Repository hits:\n- docs\/checkout\.md/);
  assert.match(ok, /Repository error: slow/);
  assert.match(ok, /2\. Read the relevant repository hits/);
  assert.match(ok, /search earlier sessions with the tool you have, if any/);
  assert.match(ok, /causal_record/);
  const bad = g.preflightMessage({ ok: false, code: 1, text: 'boom' }, 'why did the clicks drop');
  assert.match(bad, /lookup failed/);
  assert.match(bad, /boom/);
  assert.match(bad, /search earlier sessions with the tool you have, if any/);
  assert.match(bad, /5\. At the end: call causal_record/);
});

test('preflightMessage step 3 shows the configured session search command with the prompt keywords', () => {
  const json = lookupJson({ domain: 'checkout', domain_source: 'keyword', session_search_command: 'python tools/session_search.py --recent' });
  const msg = g.preflightMessage({ ok: true, code: 0, text: '', json }, 'why did widget conversion drop');
  assert.match(msg, /run the session search first:\n {3}python tools\/session_search\.py --recent checkout widget conversion drop\n/);
  assert.doesNotMatch(msg, /with the tool you have/);
  assert.equal(g.searchCommand('tool --x', []), 'tool --x <topic keywords>');
  assert.equal(g.searchCommand('tool', ['two words', 'one']), 'tool "two words" one');
});
