"""FastMCP server. Six tools, none of which can write.

There is deliberately no `run_sql` or `query` tool. The agent discovers what it
can read (list_entities / describe_entity / list_metrics) and then asks for it
(get_records / get_metric). Every read is compiled from the semantic layer,
checked, run, optionally masked, and audited.

Configuration (environment):
  SQL_STEWARD_LAYER    path to the semantic layer YAML (default: semantic.yaml)
  SQL_STEWARD_DB_URL   SQLAlchemy URL for the database to read
"""
from __future__ import annotations

import os

from dotenv import load_dotenv
from fastmcp import FastMCP

from sql_steward import __version__
from sql_steward.compiler import (
    Refusal,
    compile_check,
    compile_metric,
    compile_records,
    compile_vector_search,
)
from sql_steward.embeddings import embed
from sql_steward.engine import Engine
from sql_steward.safety import audit, audit_status, enforce_budget, enforce_rbac, mask_rows
from sql_steward.semantic import SemanticError, SemanticLayer

load_dotenv()
mcp = FastMCP("sql-steward")

_layer: SemanticLayer | None = None
_engine: Engine | None = None


def get_layer() -> SemanticLayer:
    global _layer
    if _layer is None:
        _layer = SemanticLayer.from_yaml(os.environ.get("SQL_STEWARD_LAYER", "semantic.yaml"))
    return _layer


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        url = os.environ.get("SQL_STEWARD_DB_URL")
        if not url:
            raise RuntimeError("SQL_STEWARD_DB_URL is not set")
        _engine = Engine(url)
    return _engine


@mcp.tool()
def list_entities() -> dict:
    """List the entities (tables) you may read, plus the available metrics.

    Call this first. You never write SQL -- you pass names from here to
    get_records / get_metric and sql-steward compiles the query for you.
    """
    layer = get_layer()
    return {
        "dialect": layer.dialect,
        "entities": [
            {"name": e.name, "description": e.description, "fields": list(e.fields)}
            for e in layer.entities.values()
        ],
        "metrics": list(layer.metrics),
        "version": __version__,
    }


@mcp.tool()
def describe_entity(entity: str) -> dict:
    """Show one entity's fields, types and PII tags.

    Fields tagged with a blocked PII category are marked `blocked: true`; asking
    for them in get_records is refused before any query runs.
    """
    layer = get_layer()
    try:
        e = layer.get_entity(entity)
    except SemanticError as ex:
        return _semantic_error(ex)
    block = layer.policy.block_pii
    return {
        "entity": e.name,
        "table": e.table,
        "description": e.description,
        "semantic_search": e.search is not None,
        "fields": [
            {
                "name": f.name,
                "type": f.type,
                "pii": f.pii,
                "blocked": bool(f.pii and f.pii in block),
                "description": f.description,
            }
            for f in e.fields.values()
        ],
    }


@mcp.tool()
def list_metrics() -> dict:
    """List pre-approved metrics and the dimensions/filters each one allows."""
    layer = get_layer()
    return {
        "metrics": [
            {
                "name": m.name,
                "description": m.description,
                "aggregate": m.aggregate,
                "dimensions_allowed": list(m.dimensions_allowed),
                "filters_allowed": list(m.filters_allowed),
            }
            for m in layer.metrics.values()
        ]
    }


@mcp.tool()
def get_records(
    entity: str,
    fields: list[str] | None = None,
    filters: list[dict] | None = None,
    order_by: str | None = None,
    limit: int | None = None,
) -> dict:
    """Read rows from one entity.

    `filters` is a list of {field, op, value}. Operators: =, !=, <, <=, >, >=,
    like, in, not in, is null, is not null. Blocked-PII fields are refused
    before anything runs; cross-entity references without a defined join are
    refused as unreachable.
    """
    return _run(
        lambda layer: compile_records(layer, entity, fields, filters, order_by, limit),
        target=entity,
        kind="records",
    )


@mcp.tool()
def get_metric(
    metric: str,
    dimensions: list[str] | None = None,
    filters: list[dict] | None = None,
    limit: int | None = None,
) -> dict:
    """Compute a pre-approved metric, optionally grouped/filtered by allowed
    dimensions. The aggregation itself is fixed by the semantic layer.
    """
    return _run(
        lambda layer: compile_metric(layer, metric, dimensions, filters, limit),
        target=metric,
        kind="metric",
    )


@mcp.tool()
def semantic_search(
    entity: str,
    query: str,
    k: int = 10,
    filters: list[dict] | None = None,
) -> dict:
    """Vector similarity search over an entity's embedding column (pgvector).

    `query` is embedded locally and matched against the entity's configured
    embedding; the closest rows are returned (the embedding itself never is).
    Requires the entity to have a `search` config and a local embedding model
    (SQL_STEWARD_EMBED_URL). PostgreSQL-only. Same PII refusal as everything else.
    """
    vec = embed(query)
    if vec is None:
        return {
            "error": "Semantic search needs a local embedding model. "
            "Set SQL_STEWARD_EMBED_URL (and SQL_STEWARD_EMBED_MODEL)."
        }
    return _run(
        lambda layer: compile_vector_search(layer, entity, vec, filters=filters, k=k),
        target=entity,
        kind="semantic_search",
    )


@mcp.tool()
def list_checks() -> dict:
    """List the declared data-quality checks the layer can run."""
    layer = get_layer()
    return {
        "checks": [
            {"name": c.name, "entity": c.entity, "kind": c.kind, "field": c.field,
             "severity": c.severity, "description": c.description}
            for c in layer.checks.values()
        ]
    }


@mcp.tool()
def run_checks() -> dict:
    """Run the declared data-quality checks and return a readiness summary.

    Each check compiles to a read-only violation count; zero violations passes.
    Returns a readiness score (percent of checks passing), an overall status, and
    a per-check breakdown. An 'error'-severity failure makes the status 'failing';
    a 'warn'-severity failure makes it 'degraded'.
    """
    try:
        layer = get_layer()
        engine = get_engine()
    except SemanticError as ex:
        return _semantic_error(ex)
    except RuntimeError as ex:
        return {"error": str(ex)}

    results = []
    passed = 0
    error_failures = 0
    warn_failures = 0
    for c in layer.checks.values():
        try:
            compiled = compile_check(layer, c)
            rows = engine.run(compiled)
            violations = int(next(iter(rows[0].values()))) if rows else 0
        except Exception as exc:  # surface a broken check without failing the rest
            results.append({"name": c.name, "error": str(exc)[:200]})
            error_failures += 1
            continue
        ok = violations == 0
        passed += int(ok)
        if not ok and c.severity == "error":
            error_failures += 1
        elif not ok:
            warn_failures += 1
        results.append({
            "name": c.name, "entity": c.entity, "kind": c.kind, "field": c.field,
            "severity": c.severity, "violations": violations, "passed": ok,
        })

    total = len(layer.checks)
    score = round(100 * passed / total) if total else 100
    status = "ok" if error_failures == 0 and warn_failures == 0 else ("failing" if error_failures else "degraded")
    audit(action="run_checks", target="data_quality", meta=f"{passed}/{total}", outcome=status)
    return {"readiness": score, "status": status, "passed": passed, "total": total, "checks": results}


@mcp.tool()
def audit_verify() -> dict:
    """Verify the tamper-evident audit chain (agent-blackbox), if enabled.

    Reports whether any previously recorded call was altered after the fact.
    """
    return audit_status()


def _semantic_error(ex: SemanticError) -> dict:
    """Serialize a SemanticError for the agent.

    Unknown-name lookups carry `kind` and `recovery` (what IS available, plus
    closest spellings) so the agent can correct itself in the same turn --
    the same envelope idea as Refusal.as_dict(). The `error` key stays for
    anything already keying on it.
    """
    out: dict = {"error": str(ex)}
    if ex.kind:
        out["kind"] = ex.kind
    if ex.recovery:
        out["recovery"] = ex.recovery
    return out


def _run(compile_fn, target: str, kind: str) -> dict:
    try:
        layer = get_layer()
        compiled = compile_fn(layer)
    except Refusal as r:
        audit(action=f"{kind}_refused", target=target, meta=r.kind, outcome="refused")
        return r.as_dict()
    except SemanticError as ex:
        return _semantic_error(ex)

    try:
        enforce_rbac(compiled.sql, compiled.dialect)
        enforce_budget()
    except Refusal as r:
        audit(action=f"{kind}_refused", target=target, payload=compiled.sql,
              meta=r.kind, outcome="refused")
        return r.as_dict()

    try:
        rows = get_engine().run(compiled)
    except Exception as exc:
        audit(action=f"{kind}_error", target=target, payload=compiled.sql,
              meta=str(exc)[:200], outcome="error")
        return {"error": str(exc)[:300], "sql": compiled.sql}

    rows = mask_rows(rows)
    audit(action=kind, target=target, payload=compiled.sql,
          meta={"rows": len(rows)}, outcome="ok")
    return {"rows": rows, "rowcount": len(rows), "sql": compiled.sql,
            "dialect": compiled.dialect}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
