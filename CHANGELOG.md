# Changelog

## [0.3.0] - 2026-11-19

### Added

- External model files: `registry.yaml` can map a model id to a file outside the store with
  `model_paths: {<model id>: <path>}` (absolute, or starting with `~`). `lookup`, `check`, `save`, `update`,
  `validate`, and `export` use that file. `save` and `update` write it and do not commit it; they commit
  `registry.yaml` only when it changed.
- `bin/causal_store.py link --model ID --path P`: validates the model in P, sets `model_paths[ID] = P`, adds the model
  to its domain, removes the store copy `models/<ID>.yaml` when it exists, and commits `link <ID>`.
- A missing external file gives `model not found: <id> (<path>)`; `lookup` skips the model.

## [0.2.0] - 2026-11-17

### Added

- Optional variable `role`: `exposure`, `outcome`, `mediator`, `context`, `selection`, `measurement`, `latent`,
  `other`. `latent` means the variable is not measured.
- Optional edge `type`: `directed` (default) or `bidirected`. A bidirected edge `a <-> b` means an unmeasured common
  cause of `a` and `b`. The checks replace it with a hidden common cause. A model without bidirected edges gives the
  same results and `check_id`s as in v0.1.0.
- Check rule: a claim that conditions on a `latent` variable gives `does_not_follow`.
- `bin/causal_store.py export --model ID --format dagitty|dot`: read-only, one-way export to DAGitty or DOT.

### Breaking

- A model with a `role` outside the list now fails validation. `schema_version` stays 2.
- An edge with a `type` other than `directed` or `bidirected` now fails validation. In v0.1.0, an unknown edge key
  `type` was kept.
- A model that already had `role: latent` can get new verdicts from the latent-conditioning rule.

## [0.1.0] - 2026-11-16

First public release.

- `bin/causal_store.py`: local causal-model store (git), model schema v2 validation, `lookup`, `check`, `save`,
  `update`, `validate`, `has-check`.
- Claim checks: directed claimed paths, backdoor d-separation for observational claims, unclaimed alternative paths,
  risky edges, conditioning on a descendant of the cause.
- `extensions/causal-guard.ts`: preflight message, `causal_check`, `causal_model_save`, `causal_model_update`,
  `causal_record` tools, end-of-run gate (G1 record, G2 session search), `/causal-guard` command.
- Config keys `search_repo`, `search_paths`, `search_ref`, `session_search_command`, `stale_days`. No repository
  search without `search_repo`; gate G2 is off without `session_search_command`. A new store has only the domain
  `general`.
