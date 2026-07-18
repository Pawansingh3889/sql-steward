# sql-steward

[![PyPI](https://img.shields.io/pypi/v/sql-steward)](https://pypi.org/project/sql-steward/) [![Downloads](https://static.pepy.tech/badge/sql-steward)](https://pepy.tech/projects/sql-steward)

<!-- mcp-name: io.github.Pawansingh3889/sql-steward -->

[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Discord](https://img.shields.io/badge/discord-join-5865F2?logo=discord&logoColor=white)](https://discord.gg/gBr77yYPkD)

> Part of the [Governed Agent Stack](https://github.com/Pawansingh3889/governed-agent-stack): free, on-prem building blocks for an AI agent you can point at a real database and audit.

A governed SQL gateway for AI agents, exposed over the Model Context Protocol. The agent never gets a connection string and never writes SQL. It calls typed tools; sql-steward compiles every query from a semantic layer you control, refuses blocked PII before the query runs, and returns rows. Same tools across **SQL Server, Postgres and SQLite**.

Most SQL MCP servers hand the model a `run_sql` tool and try to catch the bad queries on the way out. sql-steward removes the tool. There is no path from a prompt to raw SQL at your database, because the only thing the agent can do is name an entity or a metric and pick from allow-lists you wrote.

**See it defend a database live.** The [governed vs ungoverned demo](https://pawan-portfolio.pawankapkoti3889.workers.dev/governance-split.html) runs the same request through a naive `run_sql` agent, which leaks customer PII and empties a table, and through sql-steward, which refuses it at compile time.

## Three guarantees

1. **Read-only by construction.** There is no `run_sql`, `query`, or `execute` tool. The compiler can only ever build a `SELECT`, so a write isn't blocked, it's unrepresentable.
2. **PII refused before retrieval.** Every field can carry a PII tag. If a request touches a category your policy blocks, sql-steward refuses with a structured reason before any SQL is compiled or run.
3. **Auditable.** Every call, refusal and error can be recorded in a tamper-evident, hash-chained log via [agent-blackbox](https://github.com/Pawansingh3889/agent-blackbox), with `audit-verify` to prove nothing was rewritten.

## See it in 10 seconds

```bash
pip install sql-steward      # or: pipx install sql-steward
sql-steward demo            # zero config, no API key, no agent, SQLite
```

```
1) get_metric('mrr_total', dimensions=['plan'])  -> safe aggregate
   compiled: SELECT subscriptions.plan, SUM(subscriptions.mrr) AS mrr_total
             FROM subscriptions GROUP BY subscriptions.plan LIMIT 1000
   {'plan': 'pro', 'mrr_total': 297.0}
   {'plan': 'team', 'mrr_total': 598.0}

2) get_metric('mrr_total', dimensions=['customers.country'])  -> auto-join
   compiled: ... INNER JOIN customers ON subscriptions.customer_id = customers.id ...

3) get_records('customers', fields=['id','email'])  -> PII refusal
   refused: {"kind": "pii_blocked", "detail": "Field 'customers.email' is tagged
             EMAIL_ADDRESS, which this policy refuses."}
```

## The semantic layer

This YAML is the entire contract between the agent and your database. Review it like code.

```yaml
dialect: postgres

entities:
  customers:
    table: customers
    fields:
      id: {type: int}
      name: {type: text, pii: PERSON}
      email: {type: text, pii: EMAIL_ADDRESS}
      country: {type: text}
  subscriptions:
    table: subscriptions
    fields:
      customer_id: {type: int}
      plan: {type: text}
      mrr: {type: numeric}

joins:                              # nothing reachable that isn't listed here
  - left: subscriptions
    right: customers
    on: subscriptions.customer_id = customers.id

metrics:
  mrr_total:                        # the aggregation is fixed; the agent only
    entity: subscriptions           # chooses dimensions/filters from the lists
    aggregate: sum
    field: mrr
    dimensions_allowed: [plan, status, customers.country]
    filters_allowed: [status, customers.country]

policy:
  block_pii: [EMAIL_ADDRESS, CREDIT_CARD]
  max_rows: 1000
```

Ask for a join that isn't defined and you get `unreachable_entity`, not an invented relationship. Ask to group a metric by a dimension that isn't listed and you get `dimension_not_allowed`. Misspell a metric or field and the error lists what does exist, with the closest spellings, so an agent corrects itself in one turn instead of making a second discovery call.

## Tools exposed to the agent

| Tool | Purpose |
|---|---|
| `list_entities()` | What can be read, plus the available metrics |
| `describe_entity(entity)` | Fields, types and PII tags (blocked ones flagged) |
| `list_metrics()` | Metrics and the dimensions/filters each allows |
| `get_records(entity, fields, filters, order_by, limit)` | Read rows from one entity |
| `get_metric(metric, dimensions, filters, limit)` | Compute a pre-approved aggregate |
| `semantic_search(entity, query, k, filters)` | pgvector nearest-neighbour search over an entity's embedding column |
| `audit_verify()` | Verify the tamper-evident audit chain |

Filters are `{field, op, value}`; operators are `=, !=, <, <=, >, >=, like, in, not in, is null, is not null`. Values are always bound parameters, never inlined.

## Wire it into an MCP client

The semantic layer YAML lives wherever you point `SQL_STEWARD_LAYER`. Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "sql-steward": {
      "command": "sql-steward",
      "env": {
        "SQL_STEWARD_LAYER": "/full/path/to/semantic.yaml",
        "SQL_STEWARD_DB_URL": "postgresql+psycopg://readonly@db.internal/warehouse"
      }
    }
  }
}
```

`SQL_STEWARD_DB_URL` is a SQLAlchemy URL, so the same server reads SQL Server (`mssql+pyodbc://...`), Postgres (`postgresql+psycopg://...`) or SQLite (`sqlite:///path.db`). Install the matching driver with the extras: `pip install "sql-steward[postgres]"` or `"[mssql]"`.

## Optional: the rest of the stack

The semantic layer is the primary control. These are extra layers, all opt-in, and no-ops if the library isn't installed:

```bash
pip install "sql-steward[rbac,mask,audit]"

export SQL_STEWARD_POLICY=/path/to/policy.yaml   # query-warden second-pass role check
export SQL_STEWARD_ROLE=analyst
export SQL_STEWARD_MASK=1                         # pii-veil masks anything left in results
export SQL_STEWARD_AUDIT_DB=logs/steward.db       # agent-blackbox audit chain (on if installed)
export SQL_STEWARD_QUERY_BUDGET=200               # persistent per-role query cap (SQLite-backed)
export SQL_STEWARD_BUDGET_WINDOW=3600             # optional: make the cap a sliding window, in seconds
export SQL_STEWARD_EMBED_URL=http://localhost:11434/api/embeddings  # local embeddings for semantic_search
export SQL_STEWARD_EMBED_MODEL=nomic-embed-text
```

## Semantic search (pgvector)

Give an entity a `search` block pointing at a pgvector column and the agent gets a `semantic_search` tool, governed exactly like everything else (PII refused, results masked, calls audited):

```yaml
entities:
  documents:
    table: documents
    fields:
      id: {type: int}
      title: {type: text}
      embedding: {type: vector}
    search:
      vector_column: embedding
      dim: 768
      returns: [id, title]
```

The query text is embedded locally (set `SQL_STEWARD_EMBED_URL` to a local Ollama endpoint, so nothing leaves the building), and matched with pgvector's `<=>` operator. PostgreSQL only. The embedding column is never returned.

- [query-warden](https://github.com/Pawansingh3889/query-warden) re-checks the compiled SQL against a role policy.
- [pii-veil](https://github.com/Pawansingh3889/pii-veil) masks any PII that survives into result rows.
- [agent-blackbox](https://github.com/Pawansingh3889/agent-blackbox) records every call in a hash-chained ledger; `sql-steward audit-verify` checks it.

## Security model

sql-steward governs the path from the model to your database. It assumes the process that calls its tools is trusted. That boundary is the difference between deploying it safely and exposing it, so it is worth stating plainly.

**What it enforces, whatever the model asks.** Every call runs the same gate: read-only by construction, blocked PII refused before retrieval, an optional role check via query-warden, results masked by pii-veil, and a hash-chained audit of the call. A jailbroken model still cannot write, cannot read a blocked column, and cannot reach an entity the layer does not define. These hold by construction, not by trusting the model to behave.

**What it does not do.** sql-steward is an enforcement plane, not a front door.

- It does not authenticate the caller. `SQL_STEWARD_ROLE` is configuration, not a verified identity; sql-steward trusts that the role it is handed is the correct one.
- It does not resolve per-user permissions. If different users should see different data, the caller maps the user to a role and sets it before invoking. The role check enforces a policy, it does not decide who you are.
- It does not secure transport. Run it over stdio to a local host, or place it behind something that terminates TLS.
- It does not manage secrets. `SQL_STEWARD_DB_URL` and the audit key `AGENT_BLACKBOX_KEY` are read from the environment; supply them from a secret store, not a checked-in file.

**What you provide.** sql-steward is built to sit behind a trusted caller, whether that is an MCP host on the same machine or a gateway that has already authenticated the user.

- Invoke it from a trusted process: a subprocess of an MCP host you control (stdio), or a service behind a gateway that authenticates the request and maps the verified identity to a role before the call.
- Give it a least-privilege, read-only database account. Writes are unrepresentable in the compiler, but a read-only grant is defense in depth and the right blast radius if a dependency is ever wrong.
- Keep the database and any local embedding endpoint on a private network. Do not expose the server to untrusted callers.
- Treat the connection string and the audit key as secrets.

**The threat it is built for.** The design assumes a capable, possibly compromised model and a trusted operator. It bounds what the agent can do with your database. It does not replace your identity provider and is not meant to face an untrusted network alone. Point it at a real database from behind a host or gateway you trust, and the three guarantees are what the model is left with.

## How this is different

A typical SQL MCP validates arbitrary SQL the model wrote (a blocklist: catch what's bad). sql-steward compiles SQL from definitions you wrote (an allow-list: only what's described exists). The read-only and PII guarantees hold by construction rather than by inspection, and the query surface is the same across three engines.

## Versus a semantic layer (Cube, dbt Semantic Layer, Cortex Analyst, Genie)

The nearest tools are not other MCP servers, they are semantic layers. Cube and the dbt Semantic Layer already expose compiled, metric-only access, and Snowflake Cortex Analyst and Databricks Genie both answer natural-language questions over a governed model. If you run on their platform, use them. This project exists for the case they do not cover:

- **On-prem and air-gappable.** sql-steward is a pip install that talks to SQL Server, Postgres or SQLite with no account, no warehouse, and no data leaving the building. Cortex Analyst is Snowflake, Genie is Databricks, and Cube's cloud features assume their service. For a factory or a hospital that cannot send data to a vendor, that difference is the whole decision.
- **PII refused before retrieval, at the same chokepoint.** A blocked field is refused at compile time for every caller, so the model cannot read what policy forbids even to reason over. Semantic layers govern which metrics you can query; they are not built to guarantee a tagged column never reaches the model.
- **One tamper-evident audit for the whole agent, not just SQL.** The same gate pattern wraps KQL, document retrieval and agent memory in the [composed stack](https://github.com/Pawansingh3889/governed-agent-stack), under one hash-chained ledger. A warehouse semantic layer governs the warehouse; it does not govern the agent's other tools.

Short version: a semantic layer makes queries safe on its platform. sql-steward makes an agent safe on your infrastructure, across every surface it can reach.

That difference does not mean living on an island. The industry is converging on
[Apache Ossie](https://github.com/apache/ossie) (the Open Semantic Interchange
format, incubating at the ASF, started by the Snowflake, dbt and Salesforce
working group) as the neutral way to move semantic models between tools, and
sql-steward speaks it:

```bash
sql-steward export semantic.yaml --out model.osi.yaml
```

Entities, joins and metrics map to OSI datasets, relationships and metrics.
OSI has no governance vocabulary yet, so the parts that make the layer a
contract rather than a catalog, the PII tags, the policy block, metric
allow-lists and checks, travel in `custom_extensions` under
`vendor_name: SQL_STEWARD`; anything that cannot be represented is reported
as a note on stderr instead of dropped silently. The output passes Ossie's
own validator, so a layer written for sql-steward can be handed to anything
that reads the standard.

## Scaling to a real schema

The semantic layer is authored by hand on purpose, so it reads and reviews like code. That is the right default for tens of tables and the wrong one for thousands: nobody hand-writes a layer for a 10,000-table ERP, and returning the whole layer in one `list_entities` call would not fit an agent's context anyway. How that scales:

- **Bootstrap, then review.** `sql-steward init --from-db <url>` reflects a live schema, maps column types, proposes PII tags from column-name heuristics (biased toward over-tagging, so a leak is a review edit rather than a default), and infers joins from foreign keys. It emits a draft layer that loads and validates as-is, with a header that tells you what to narrow. The point is not to auto-expose everything; it is to remove the blank-page problem so a large schema starts as a reviewable file, not a hand-typed one.

  ```bash
  sql-steward init --from-db "postgresql+psycopg://readonly@db/warehouse" --out semantic.yaml
  # then delete entities you do not need, check the PII tags, add metrics
  ```

- **Scope beats size.** A governed layer should expose the handful of entities an agent actually needs, not the whole database. A 10,000-table schema still becomes a 20-entity contract; the discipline is deciding what belongs (use `--include`/`--exclude` to draft only a slice), which is a feature of the model, not a limit of the tool.
- **Discovery grows with the layer.** `list_entities`/`describe_entity` are browsed as the layer grows; search and paging on those responses is the next edge to close before this points at a very large single layer.

## Develop

```bash
git clone https://github.com/Pawansingh3889/sql-steward
cd sql-steward
pip install -e ".[dev]"
pytest -q
```

## License

MIT
