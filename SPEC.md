# pi-causal-guard specification (v0.2.0)

## Goal

On each user question, pi follows one fixed causal flow:

1. Decide the domain of the question.
2. Gather existing information (an optional git repository, earlier sessions).
3. Find the causal model for the domain. Create one if none exists. Use it if one exists.
4. After the answer, record (a) new facts that update the model and (b) if the answer relies on causal knowledge,
   the checked results.

Code does every step that can be deterministic. The model does only: domain choice when no keyword matches,
graph content, fact extraction.

Out of scope:

- Effect estimation. The checks are structure checks only.
- Writes to the search repository. It is read with `git grep` on one ref only.
- Domain-specific check logic. The checker is generic.

v0.2.0 scope: optional variable `role` (fixed list), optional edge `type` (`bidirected` for an unmeasured common
cause), the latent-conditioning rule, and the `export` subcommand (DAGitty, DOT). Reason: DAGitty roles, ADMG graphs
(Ananke, pywhy-graphs), and DoWhy DOT input. Not in v0.2.0: a new extension tool, import from DAGitty or DOT, PAG or
CPDAG edges, a new `schema_version` (it stays 2), and automatic role migration. Breaking change: a model with a
`role` value outside the list fails validation.

## Components

- C-1 `bin/causal_store.py`: the model store helper (Python 3, `networkx`, `PyYAML`).
- C-2 `extensions/causal-guard.ts`: the pi extension (preflight, tools, gate, command).

## C-1 Store helper

The helper prints exactly one JSON object on stdout. Exit codes: 0 success, 2 validation error, 1 other failure.
`networkx` loads only for graph commands, so `lookup` stays fast.

### Store layout

Store root: env `PI_CAUSAL_STORE`, default `~/.pi/agent/causal-models`. On first use the helper creates the folder,
runs `git init`, and writes the default `config.yaml` and `registry.yaml`.

- `config.yaml`: see Config.
- `registry.yaml`: `domains: {<id>: {keywords: [...], models: [...], search_paths?: [...]}}`. A new store has only
  the domain `general` (no keywords).
- `models/<id>.yaml`: one model per file (schema v2).
- `checks.jsonl`: one line per checked claim.
- `log.jsonl`: one line per update (with the facts).

Models and the registry are committed to the store git. `checks.jsonl` and `log.jsonl` are not committed.

### Config

| Key | Default | Meaning |
|---|---|---|
| `search_repo` | none | Git repository that `lookup` searches. When it is not set, `lookup` does no repository search. |
| `search_paths` | `["."]` | Pathspecs searched in `search_repo`. A domain's `search_paths` replace them for that domain. |
| `search_ref` | `origin/main` | Ref searched in `search_repo`. |
| `session_search_command` | none | Command the agent runs to search earlier sessions. The extension appends the prompt keywords. |
| `stale_days` | `7` | A model is stale when `last_gathered` is older than this. |

### Subcommands

- `lookup --prompt-file F` → `{domain, domain_source: "keyword"|"none", domains: [ids], models: [{id, path, revision,
  last_gathered, stale}], repo_hits: [paths, at most 10], repo_error?, session_search_command: string|null}`.
  - Domain: the registry domain with the most keyword hits in the prompt (case-insensitive, whole word).
    A tie or 0 hits gives `domain: null`, `domain_source: "none"`.
  - `stale`: `last_gathered` is older than `stale_days`, or not a valid date.
  - Repository hits (only when `search_repo` is set): `git -C <search_repo> grep -l -i -E <terms> <search_ref> --
    <paths>` with a 6 s limit. Terms: up to 6 prompt words of 4 or more letters that are not in a stop list,
    regex-escaped. A grep failure or timeout gives `repo_hits: []` and `repo_error`; `lookup` still exits 0.
- `check --model ID` (stdin `{claims: [...]}`) → `{model, revision, results: [{check_id, claim_id, verdict, problems,
  notes, risky_edges}]}`. Each result is appended to `checks.jsonl`.
- `save --model ID` (stdin: the model as JSON) → validates, sets `revision` (old + 1, or 1), writes the YAML, adds the
  model to its domain in the registry, and commits. Optional `keywords` in the model object create a new domain with
  these keywords; they are removed before the model is written. Output `{id, revision, path, created}`.
- `update --model ID` (stdin `{facts: [{text, source}], add_edges, change_edges: [{id, ...fields}], remove_edges: [ids],
  add_variables, add_sources: {...}}`) → applies, validates, revision + 1, sets `last_gathered` to today when `facts`
  is not empty, commits, and appends the request and diff to `log.jsonl`. Output `{id, revision, diff: [lines]}`.
- `validate --model ID` → `{ok, errors}` (exit 2 when not ok).
- `export --model ID --format dagitty|dot` → `{model, revision, format, text}`. Read-only: no write, no commit.
  An invalid model gives exit 2. An unknown format is a usage error (exit 1). See Export.
- `has-check --ids a,b` → `{missing: [ids not in checks.jsonl]}`.

### Model schema v2

YAML. Unknown extra keys are kept.

- Required: `schema_version: 2`, `id` (slug), `domain` (slug), `title`, `revision` (positive int),
  `status` (`hypothesis`|`reviewed`), `last_gathered` (YYYY-MM-DD), `variables` [{`id`, `label`, `definition`}],
  `edges` [{`id`, `from`, `to`, `status` (`known`|`assumed`|`unknown`), `reason`, `sources` [source keys]}],
  `sources` {key: {`ref`, `use`}}.
- Optional variable `role` (added in v0.2.0): one of `exposure`, `outcome`, `mediator`, `context`, `selection`,
  `measurement`, `latent`, `other`. A missing `role` is valid. Any other value is a validation error.
  `latent` means the variable is not measured.
- Optional edge `type` (added in v0.2.0): `directed` (the default when missing) or `bidirected`. A bidirected edge
  `a <-> b` means an unmeasured common cause of `a` and `b` (an ADMG edge). The order of `from` and `to` has no
  meaning for a bidirected edge.
- Rules: unique variable ids and edge ids; no duplicate directed `from`→`to`; no two bidirected edges on the same
  unordered pair; a directed edge and a bidirected edge on the same pair are allowed; no edge from a variable to
  itself; no dangling edge; no directed cycle (bidirected edges are not part of the cycle test); each edge source
  key exists; a `known` edge has at least 1 source. The same `status`, `reason`, and `sources` rules apply to both
  edge types.
- `status` and `sources` are independent. A source does not make an edge `known`, and an edge without textual
  support can still be correct. Give an edge that an LLM proposed the status `assumed`, and give a model built
  from such edges the status `hypothesis`, until a person or data confirms it.
- `update` validates `add_edges`, `change_edges`, and `add_variables` with the same rules.

### Claim format

`{id, text, cause, effect, claimed_paths: [[variable ids]], conditioned_on: [ids], ruled_out: [[ids]],
design: "randomized"|"observational"}`. Claim ids match `^[A-Za-z0-9_.-]+$` (no comma, so `has-check` can split them).

### Check rules

Per claim:

1. Each claimed path is a directed path in the model from `cause` to `effect`. Otherwise → `does_not_follow`.
2. Backdoor (only `observational`): remove the outgoing edges of `cause`. If `cause` and `effect` are not
   d-separated given `conditioned_on` → `does_not_follow`. The problem names the shortest open path found in a search
   limit, or says "open backdoor (path not listed)".
3. Alternatives: each other directed path `cause`→`effect` that is not claimed and not in `ruled_out` → `partly`.
   At most 50 paths are listed, then a note "... and more (truncated)".
4. Risky edges: claimed-path edges with status `assumed` or `unknown` are listed. An `unknown` edge on a claimed
   path → at most `partly`.
5. All designs: `conditioned_on` contains a descendant of `cause` → `does_not_follow` (overcontrol or collider bias).
   This is stricter than the textbook rule: it also flags a descendant with no path to `effect`.

6. All designs: `conditioned_on` contains a variable with role `latent` → `does_not_follow` (an unmeasured variable
   cannot be conditioned on).

Otherwise the verdict is `follows`. Unknown variables in a claim → `does_not_follow`.

Bidirected edges: the checker replaces each bidirected edge `e` between `a` and `b` with a hidden node and the
directed edges hidden→`a` and hidden→`b`. Rules 2, 3, and 5 use this graph. A hidden node is not a
variable: a claim that names it has an unknown variable. Rule 1 accepts only directed edges, so a claimed path that
uses a bidirected edge → `does_not_follow`. Rule 4 lists only directed edges. In an open backdoor path, a
bidirected step shows as `<-> b`. A model without bidirected edges gives the same results and `check_id`s as in
v0.1.0.

### Export

The YAML model stays the source of truth. The export is one-way and drops `reason`, `sources`, and facts.

- `dagitty`: `dag {`, then one line per variable in model order (`id` plus ` [exposure]`, ` [outcome]`, or
  ` [latent]` for these roles; no attribute for other roles), then one line per edge in model order (`a -> b` or
  `a <-> b`), then `}`. An id that does not match `^[A-Za-z_][A-Za-z0-9_]*$` is written in double quotes, with `\`
  and `"` escaped.
- `dot`: `digraph "<model id>" {`, then one node line per variable `"<id>" [label="<label>"]`, then one line per
  edge `"a" -> "b" [style=solid|dashed|dotted]` for status `known`|`assumed`|`unknown`; a bidirected edge adds
  `dir=both` after the style: `[style=dashed, dir=both]`. All ids and labels are quoted, with `\` and `"` escaped. Then `}`.
- Both formats: no indentation, lines joined with `\n`, and `text` ends with `}` (no trailing newline).
- An unknown `--format` value exits 1 with one JSON error object. A missing `--format` is an argparse usage error,
  as for the other subcommands.

`check_id` = `<model>@r<revision>#<claim id>-<first 6 hex of sha1(canonical claim JSON)>`.

## C-2 Extension

- Off when `PI_CAUSAL_GUARD=off`, when `PI_SUBAGENT_CHILD` is set, or after `/causal-guard off` in the session.
- Helper: `python` on Windows, `python3` on other systems (env `PI_CAUSAL_PYTHON` overrides), runs `bin/causal_store.py` (env `PI_CAUSAL_STORE_SCRIPT`), without a shell.

### Preflight (`before_agent_start`, user messages only)

- Skip when the prompt has fewer than 4 words, or when a prompt of at most 80 characters is only short-reply tokens
  (yes, ok, lgtm, A1, 2, do it, ...).
- Else run `lookup` (8 s limit) and inject one hidden custom message (`customType: "causal-guard"`) with the domain
  or "none", the known domains, the models (path, revision, stale), the repository hits, and the required steps:
  1. If the domain is none: pick one of the listed domains or a new slug.
  2. Read the relevant repository hits.
  3. If there is no model or it is stale: run `session_search_command` with the prompt keywords, when it is set;
     else "search earlier sessions with the tool you have, if any". Then call `causal_model_save` or
     `causal_model_update`.
  4. If the answer makes a causal claim: call `causal_check` and cite the `check_id`s.
  5. At the end: call `causal_record`.
- A lookup failure (or a temp-file error) injects the error and the same steps. It does not block the run.

### Tools

Each tool calls the helper. A non-zero exit returns the error text as the tool result, marked as an error.

- `causal_check {model, claims}` (missing `conditioned_on` and `ruled_out` default to `[]`).
- `causal_model_save {model}` (the model JSON; optional `keywords` for a new domain). The tool description names
  the optional variable `role` and edge `type`.
- `causal_model_update {model, facts, add_edges, change_edges, remove_edges, add_variables, add_sources}`.
- `causal_record {domain, model?, causal, check_ids?, update: "none"|"applied"|"proposed", note?}`.
  `causal: true` needs at least one `check_id` that `has-check` finds, or a note that starts with `unchecked:`.
  When `has-check` fails to run, a note that starts with `unchecked:` still passes.

### Gate (`agent_before_settle`)

Only for completed runs where the preflight message was injected for the last user message.

- G1: no successful `causal_record` since the last user message.
- G2: only when `session_search_command` is set. `causal_model_save` created a new model in this run, and no tool
  call in this run contains the command name. The name is the first path segment of the command that ends in `.py`,
  `.js`, `.mjs`, or `.sh`, else the first word.
- An open rule adds a visible note (`customType: "causal-guard-gate"`) and continues the run.
  At most 2 continuations per user prompt; then the gate logs `budget-exhausted` and shows a warning.

### Command and log

- `/causal-guard [status|off|on]`: show the last lookup and record, or switch the guard for the session.
- Log: `~/.pi/agent/logs/causal-guard.jsonl` (lookup, record, gate decisions, toggles).

## Acceptance criteria

- AC1: `python -m pytest tests` passes: schema validation (cycle, dangling edge, duplicates, missing source);
  the check rules on textbook graphs (confounder observational → `does_not_follow`; confounder conditioned →
  `follows`; randomized → `follows`; unclaimed mediator → `partly`; collider X<-A->C<-B->Y plus X->Y conditioned
  on C → `does_not_follow`; conditioning on a mediator → `does_not_follow`); `check_id` stability; `save` revision
  bump and commit; `update` diff and `last_gathered`; `lookup` domain match, tie → none, repository hits,
  no repository search without `search_repo`; `has-check`; a new store has only the domain `general`.
- AC2: `node --test tests/` passes with a real pi session, fake model output, and no network:
  (a) a question without `causal_record` gets exactly 1 gate continuation, then settles after the record;
  (b) a short reply gets no injection and no gate; (c) with `PI_SUBAGENT_CHILD=1` nothing happens;
  (d) `causal_record {causal: true}` with an unknown check id returns an error; G2 with and without
  `session_search_command`; the continuation budget.
- AC3: the extension loads in pi's `DefaultResourceLoader` with 0 errors and registers the 4 tools.
- AC4: an end-to-end test with the real helper and a temporary store (save, session search, check, record) passes
  without a gate note.

v0.2.0 additions (all in `python -m pytest tests`, except AC9 and AC10):

- AC5: role validation: each listed role passes; a missing role passes; an unlisted role fails; `update` with
  `add_variables` that has an unlisted role is rejected and changes nothing.
- AC6: edge type validation: a missing type is `directed`; an unknown type fails; a bidirected self edge fails;
  two bidirected edges on one pair (either order) fail; a directed edge plus a bidirected edge on one pair pass;
  bidirected edges `a <-> b` plus `b -> a` do not count as a cycle; a directed cycle still fails.
- AC7: checks with bidirected edges: X->Y with X<->Y, observational → `does_not_follow` with `<-> Y` in the
  problem; randomized → `follows`; X->M->Y with X<->Y, M not conditioned, front-door style claim through M
  observational → `does_not_follow`; X->Y with X<->Z and Z->Y, conditioned on Z → `follows`; a claimed path over a
  bidirected edge → `does_not_follow`; conditioning on a `latent` variable → `does_not_follow`.
- AC8: export: `dagitty` and `dot` output for a fixed model equals the expected text exactly (roles, bidirected
  edges, quoting of an id with a space or a quote, status styles); `export` does not change the store git; an
  unknown format exits 1 with one JSON object.
- AC9: backward compatibility: every existing v0.1.0 test passes unchanged; `node --test tests/` passes.
- AC10 (coordinator, not in the repo tests): the DAGitty parser `r/inst/js/dagitty-alg.js` from
  `jtextor/dagitty` (pinned commit) parses the `dagitty` export of a model with roles, a bidirected edge, and a
  quoted id, and gives the same nodes, edges, and roles.
