# Changelog

## [0.3.1] - 2026-07-07

### Fixed
- `--version` reported a stale hardcoded number; `__version__` now reads the
  installed package metadata, so it can never drift from pyproject again.

## [0.3.0] - 2026-07-07

First PyPI release: `pip install sql-steward`. The `rbac` and `audit` extras
are parked until query-warden and agent-blackbox reach PyPI themselves; their
lazy imports in safety.py pick up GitHub installs of those in the meantime.

### Added
- **`init --from-db`: draft a semantic layer from a live database.** Reflects a
  schema over SQLAlchemy (SQL Server, Postgres, SQLite), maps column types,
  proposes PII tags from column-name heuristics (biased toward over-tagging),
  and infers joins from foreign keys. Emits a draft that loads and validates as
  written, with a review header. `--include`/`--exclude` draft only a slice.
  This is the answer to authoring a layer for a large schema: bootstrap a
  reviewable file instead of a blank page.
- **Declarative data-quality checks.** A `checks:` block in the semantic layer
  declares assertions (`not_null`, `unique`, `range`, `accepted_values`,
  `row_count_min`) with `error` or `warn` severity. Two tools expose them:
  `list_checks` and `run_checks`. Each check compiles to a read-only violation
  count (the rule lives in the layer, so the agent runs the declared checks but
  cannot invent new ones), and `run_checks` returns a readiness score and an
  overall status (`ok` / `degraded` / `failing`).

## [0.2.0] - 2026-06-18

### Added
- **`semantic_search` tool (pgvector).** Nearest-neighbour search over an entity's
  embedding column, governed like every other tool: PII refusal, result masking
  and audit all apply, and the embedding column is never returned. Configure per
  entity with a `search:` block. Query text is embedded locally via
  `SQL_STEWARD_EMBED_URL` (Ollama by default), so nothing leaves the building.
  PostgreSQL only.
- **Per-role query budgets.** Set `SQL_STEWARD_QUERY_BUDGET` for a hard cap on
  queries per role per session, refused with `budget_exceeded`. A simple,
  on-prem take on gateway-style runtime spend caps.
- `describe_entity` now reports whether an entity supports semantic search.

## [0.1.0] - 2026-06-18

### Added
- Initial release. Semantic-layer SQL compiler where the agent never writes SQL,
  read-only by construction, with PII refusal and unreachable-join refusal,
  emitted multi-dialect (SQL Server / Postgres / SQLite) via sqlglot. FastMCP
  server (`list_entities`, `describe_entity`, `list_metrics`, `get_records`,
  `get_metric`, `audit_verify`) and CLI (`serve`, `validate`, `demo`,
  `audit-verify`). Optional, graceful query-warden (RBAC), pii-veil (masking)
  and agent-blackbox (audit) integrations.
