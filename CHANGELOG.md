# Changelog

## [Unreleased]

### Added
- Unknown-name errors are now self-correcting for agents. Asking for a metric,
  entity or field that does not exist used to return a bare string; the reply
  now carries `kind` (`unknown_metric` / `unknown_entity` / `unknown_field`)
  and a `recovery` block listing everything that IS available, plus the
  closest spellings when the name looks like a typo. Same envelope idea as
  refusals, and additive: the `error` key is unchanged, so nothing keying on
  it breaks. An agent that misspells a name can fix its call in the same turn
  instead of spending a second round-trip on `list_metrics` or
  `describe_entity`.
- `sql-steward export`: emit the semantic layer as an Apache Ossie (OSI)
  document, the vendor-neutral semantic-model interchange format incubating
  at the ASF. Entities, joins and metrics map to OSI datasets, relationships
  and metrics; everything OSI cannot express (PII tags, the policy block,
  metric allow-lists, checks, vector-search config) rides in
  `custom_extensions` under `vendor_name: SQL_STEWARD`, and anything not
  representable is reported as a note rather than dropped silently. Output
  passes Ossie's own validator (spec 0.2.0.dev0).

## [0.3.3] - 2026-07-07

### Fixed
- MCP Registry name uses the case-exact GitHub namespace
  (`io.github.Pawansingh3889/sql-steward`); marker and server.json realigned.

## [0.3.2] - 2026-07-07

### Added
- Listed in the official MCP Registry as `io.github.pawansingh3889/sql-steward`:
  `server.json` at the repo root plus the registry's ownership marker in this
  README. No code changes.

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
