# Changelog

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
