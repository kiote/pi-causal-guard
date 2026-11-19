# pi-causal-guard

A [pi](https://www.npmjs.com/package/@earendil-works/pi-coding-agent) package. On each user question, pi follows one
fixed causal flow: find the domain, gather repository and session context, use or create a causal model, check causal
claims against it, and record model updates. The full rules are in [`SPEC.md`](SPEC.md).

## Install

```bash
pi install git:github.com/kiote/pi-causal-guard@v0.2.0
```

To try it in one session without installing:

```bash
pi -e /path/to/pi-causal-guard
```

Requirements: Python 3 with `networkx` 3.3 or later and `PyYAML` (`pip install networkx pyyaml`), and `git`.

## How the flow works

1. **Preflight.** Before the agent starts on a user question, the extension runs `causal_store.py lookup`
   (8 s limit). Prompts with fewer than 4 words and short replies (`yes`, `ok`, `lgtm`, `A1`, `2`, `do it`, ...)
   are skipped. Lookup picks the domain by keyword match, lists the domain's models (with a stale flag), and greps
   the configured repository. The result goes into one hidden `causal-guard` message with the required steps:
   1. If the domain is none: pick a listed domain or a new slug.
   2. Read the relevant repository hits.
   3. If there is no model or it is stale: search earlier sessions (with `session_search_command` when it is set),
      then call `causal_model_save` or `causal_model_update`.
   4. If the answer makes a causal claim: call `causal_check` and cite the `check_id`s.
   5. At the end: call `causal_record`.

   A lookup failure is injected as text with the same steps. It does not block the run.
2. **Tools.** `causal_check`, `causal_model_save`, `causal_model_update`, `causal_record`. Each calls
   `bin/causal_store.py`. A failure or validation error is returned as the tool result text, marked as an error.
   `causal_record` with `causal: true` needs a check id that exists in `checks.jsonl`, or a note that starts with
   `unchecked:`.
3. **Gate.** When a run with a preflight completes, the gate checks:
   - G1: no successful `causal_record` since the last user message.
   - G2 (only when `session_search_command` is set): `causal_model_save` created a new model, and no tool call in
     the run contains the command name (the first path segment ending in `.py`, `.js`, `.mjs`, or `.sh`, else the
     first word).

   An open rule adds a visible note and continues the run, at most 2 times per user prompt. Then the gate logs
   `budget-exhausted` and shows a warning.
4. **Command.** `/causal-guard [status|off|on]` shows the last lookup and record, or switches the guard for the session.

The checks are structure checks on the model graph (directed paths, backdoor d-separation for observational claims,
unclaimed alternative paths, risky edges, conditioning on a descendant of the cause, conditioning on a `latent`
variable). See `SPEC.md` for the rules.

## Store and config

The store is a local git repository at `~/.pi/agent/causal-models` (env `PI_CAUSAL_STORE`). It is created on first
use with `config.yaml`, `registry.yaml` (only the domain `general`), and `models/`. `checks.jsonl` and `log.jsonl`
are written there too and are not committed.

Example `config.yaml`:

```yaml
search_repo: /home/me/src/team-docs   # git repository to grep (absolute path); leave it out for no repository search
search_paths: [docs, notes]           # pathspecs in search_repo (default ["."])
search_ref: origin/main               # ref to grep (default origin/main)
session_search_command: python ~/tools/session_search.py --days 30   # prompt keywords are appended
stale_days: 7
```

Example `registry.yaml`:

```yaml
domains:
  general:
    keywords: []
    models: []
  checkout:
    keywords: [checkout, cart, conversion]
    search_paths: [docs/checkout]     # optional; replaces search_paths for this domain
    models: [checkout-funnel]
```

`causal_model_save` with `keywords` in the model object creates a new domain with these keywords.

### External model files

A model can live in another repository (for example a shared team repository). `model_paths` in `registry.yaml`
maps a model id to its file; the path is absolute or starts with `~`:

```yaml
model_paths:
  checkout-funnel: ~/src/team-models/checkout-funnel.yaml
```

All subcommands read and write that file. `save` and `update` do not commit it (it belongs to the other
repository); they commit `registry.yaml` only when it changed. `link` validates an existing file, adds it to
`model_paths` and to its domain, and removes the store copy:

```
python bin/causal_store.py link --model checkout-funnel --path ~/src/team-models/checkout-funnel.yaml
```

## Model format

Each model is one YAML file `models/<id>.yaml` (schema v2). See `SPEC.md` for the required keys and rules.

- Variable `role` (optional): one of `exposure`, `outcome`, `mediator`, `context`, `selection`, `measurement`,
  `latent`, `other`. `latent` means the variable is not measured. Any other value fails validation.
- Edge `type` (optional): `directed` (the default) or `bidirected`. A bidirected edge `a <-> b` means an unmeasured
  common cause of `a` and `b`. The order of `from` and `to` has no meaning for a bidirected edge.
- Edge `status` and `sources` are independent. A source does not make an edge `known`. Give an edge that an LLM
  proposed the status `assumed`, and give the model the status `hypothesis`, until a person or data confirms it.

Part of a model:

```yaml
variables:
  - id: discount
    label: Discount shown
    definition: The checkout page shows a discount banner.
    role: exposure
  - id: conversion
    label: Conversion
    definition: The user completes the order.
    role: outcome
edges:
  - id: e1
    from: discount
    to: conversion
    status: assumed
    reason: A discount can make the order cheaper.
    sources: []
  - id: e2
    from: discount
    to: conversion
    type: bidirected
    status: assumed
    reason: Unmeasured purchase intent can affect both.
    sources: []
```

## Environment

| Variable | Effect |
|---|---|
| `PI_CAUSAL_GUARD=off` | Extension off (no preflight, gate, tools, or command). |
| `PI_SUBAGENT_CHILD` (any non-empty value) | Extension off in subagent children. |
| `PI_CAUSAL_STORE` | Store root. Default: `~/.pi/agent/causal-models`. |
| `PI_CAUSAL_STORE_SCRIPT` | Path of the store helper. Default: `<package root>/bin/causal_store.py`. |
| `PI_CAUSAL_PYTHON` | Python executable. Default: `python` on Windows, `python3` on other systems. |

Log: `~/.pi/agent/logs/causal-guard.jsonl` (lookup, record, gate decisions, toggles).

## Python helper

`bin/causal_store.py` prints one JSON object on stdout. Exit codes: 0 success, 2 validation error, 1 other failure.

```
python bin/causal_store.py lookup --prompt-file F
python bin/causal_store.py check --model ID        < {"claims": [...]}
python bin/causal_store.py save --model ID         < model JSON (optional "keywords" for a new domain)
python bin/causal_store.py update --model ID       < {"facts": [...], "add_edges": [...], ...}
python bin/causal_store.py validate --model ID
python bin/causal_store.py has-check --ids a,b
python bin/causal_store.py export --model ID --format dagitty
```

`export` writes the model as DAGitty (`--format dagitty`) or DOT (`--format dot`) text in the `text` field. It is
read-only. The YAML model stays the source of truth. The export is one-way and drops `reason`, `sources`, and facts.

## Tests

```bash
python -m pytest tests -q   # store helper; uses a temporary store, never ~/.pi/agent/causal-models
node --test tests/          # extension: unit tests, real pi session tests, end-to-end test (fake model, no network)
```

The Node tests load pi from env `PI_SDK_ROOT` (the `@earendil-works/pi-coding-agent` package folder), else from
`npm root -g`. They use `tests/fake_causal_store.py` as the store helper (except the end-to-end test) and a temporary
home folder for the log. They need Node 22.15 or later (they import the `.ts` extension directly and use
`module.registerHooks`). The tests were run on Windows only.

## Background

- DAGitty: https://arxiv.org/abs/1508.04633
- Ananke (ADMG): https://arxiv.org/abs/2301.11477
- pywhy-graphs: https://github.com/py-why/pywhy-graphs
- DoWhy: https://arxiv.org/abs/2011.04216
- Provenance gap: https://arxiv.org/abs/2606.15833

## License

MIT. See [`LICENSE`](LICENSE).
