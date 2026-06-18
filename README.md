# sql-steward

[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)

> Part of the [Governed Agent Stack](https://github.com/Pawansingh3889/governed-agent-stack): free, on-prem building blocks for an AI agent you can point at a real database and audit.

A governed SQL gateway for AI agents, exposed over the Model Context Protocol. The agent never gets a connection string and never writes SQL. It calls typed tools; sql-steward compiles every query from a semantic layer you control, refuses blocked PII before the query runs, and returns rows. Same tools across **SQL Server, Postgres and SQLite**.

Most SQL MCP servers hand the model a `run_sql` tool and try to catch the bad queries on the way out. sql-steward removes the tool. There is no path from a prompt to raw SQL at your database, because the only thing the agent can do is name an entity or a metric and pick from allow-lists you wrote.

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

Ask for a join that isn't defined and you get `unreachable_entity`, not an invented relationship. Ask to group a metric by a dimension that isn't listed and you get `dimension_not_allowed`.

## Tools exposed to the agent

| Tool | Purpose |
|---|---|
| `list_entities()` | What can be read, plus the available metrics |
| `describe_entity(entity)` | Fields, types and PII tags (blocked ones flagged) |
| `list_metrics()` | Metrics and the dimensions/filters each allows |
| `get_records(entity, fields, filters, order_by, limit)` | Read rows from one entity |
| `get_metric(metric, dimensions, filters, limit)` | Compute a pre-approved aggregate |
| `audit_verify()` | Verify the tamper-evident audit chain |

Filters are `{field, op, value}`; operators are `=, !=, <, <=, >, >=, like, in, not in, is null, is not null`. Values are always bound parameters, never inlined.

## Wire it into an MCP client

`servers.yaml` lives wherever you point `SQL_STEWARD_LAYER`. Claude Desktop (`claude_desktop_config.json`):

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
```

- [query-warden](https://github.com/Pawansingh3889/query-warden) re-checks the compiled SQL against a role policy.
- [pii-veil](https://github.com/Pawansingh3889/pii-veil) masks any PII that survives into result rows.
- [agent-blackbox](https://github.com/Pawansingh3889/agent-blackbox) records every call in a hash-chained ledger; `sql-steward audit-verify` checks it.

## How this is different

A typical SQL MCP validates arbitrary SQL the model wrote (a blocklist: catch what's bad). sql-steward compiles SQL from definitions you wrote (an allow-list: only what's described exists). The read-only and PII guarantees hold by construction rather than by inspection, and the query surface is the same across three engines.

## Develop

```bash
git clone https://github.com/Pawansingh3889/sql-steward
cd sql-steward
pip install -e ".[dev]"
pytest -q
```

## License

MIT
