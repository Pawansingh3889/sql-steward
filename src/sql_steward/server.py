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
from sql_steward.compiler import Refusal, compile_metric, compile_records
from sql_steward.engine import Engine
from sql_steward.safety import audit, audit_status, enforce_rbac, mask_rows
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
        return {"error": str(ex)}
    block = layer.policy.block_pii
    return {
        "entity": e.name,
        "table": e.table,
        "description": e.description,
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
def audit_verify() -> dict:
    """Verify the tamper-evident audit chain (agent-blackbox), if enabled.

    Reports whether any previously recorded call was altered after the fact.
    """
    return audit_status()


def _run(compile_fn, target: str, kind: str) -> dict:
    try:
        layer = get_layer()
        compiled = compile_fn(layer)
    except Refusal as r:
        audit(action=f"{kind}_refused", target=target, meta=r.kind, outcome="refused")
        return r.as_dict()
    except SemanticError as ex:
        return {"error": str(ex)}

    try:
        enforce_rbac(compiled.sql, compiled.dialect)
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
