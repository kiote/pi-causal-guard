# Changelog

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
