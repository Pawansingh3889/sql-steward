"""Export the semantic layer as an Apache Ossie (OSI) document.

OSI is the Open Semantic Interchange format, the vendor-neutral semantic-model
interchange spec started by the Snowflake / dbt / Salesforce working group and
now incubating at the ASF as Apache Ossie. Exporting means the entities, joins
and metrics that govern an agent here can travel to any tool that reads OSI.

OSI has no governance vocabulary, so everything it cannot express rides along
in ``custom_extensions`` under ``vendor_name: SQL_STEWARD``, per the spec's
escape hatch: PII tags on fields, the policy block, metric allow-lists, checks,
and vector-search config. Nothing is dropped silently; anything that cannot be
represented at all is reported as an issue string.

Targets spec version 0.2.0.dev0. The version key is a ``const`` in the OSI
JSON schema, so documents must pin it exactly; expect to re-stamp when the
spec cuts a release.
"""
from __future__ import annotations

import json

import sqlglot
from sqlglot import exp

from sql_steward.semantic import EntityDef, JoinDef, MetricDef, SemanticLayer

OSI_VERSION = "0.2.0.dev0"
VENDOR = "SQL_STEWARD"

# steward dialects that exist in OSI's closed dialect enum. Everything else is
# emitted as ANSI_SQL, which is honest: exported expressions are bare columns
# and single aggregates, dialect-neutral by construction.
_NATIVE_DIALECTS = {"snowflake": "SNOWFLAKE", "bigquery": "BIGQUERY"}

_TIME_TYPES = {"date", "datetime", "timestamp"}

_AGGREGATE_SQL = {
    "sum": "SUM({ref})",
    "count": "COUNT({ref})",
    "count_distinct": "COUNT(DISTINCT {ref})",
    "avg": "AVG({ref})",
    "min": "MIN({ref})",
    "max": "MAX({ref})",
}


def _extension(payload: dict) -> dict:
    """One custom_extensions entry. OSI types `data` as a JSON *string*."""
    return {"vendor_name": VENDOR, "data": json.dumps(payload, sort_keys=True)}


def _expression(sql: str, dialect: str) -> dict:
    dialects = [{"dialect": "ANSI_SQL", "expression": sql}]
    native = _NATIVE_DIALECTS.get(dialect)
    if native:
        dialects.append({"dialect": native, "expression": sql})
    return {"dialects": dialects}


def _field_dict(entity: EntityDef, name: str, dialect: str) -> dict:
    fdef = entity.fields[name]
    out: dict = {"name": name, "expression": _expression(name, dialect)}
    if fdef.description:
        out["description"] = fdef.description
    if fdef.type in _TIME_TYPES:
        out["dimension"] = {"is_time": True}
    if fdef.pii:
        out["custom_extensions"] = [_extension({"pii": fdef.pii})]
    return out


def _dataset_dict(entity: EntityDef, dialect: str) -> dict:
    out: dict = {"name": entity.name, "source": entity.table}
    if entity.primary_key:
        out["primary_key"] = [entity.primary_key]
    if entity.description:
        out["description"] = entity.description
    if entity.fields:
        out["fields"] = [_field_dict(entity, name, dialect) for name in entity.fields]
    if entity.search:
        out["custom_extensions"] = [
            _extension(
                {
                    "vector_search": {
                        "vector_column": entity.search.vector_column,
                        "dim": entity.search.dim,
                        "returns": list(entity.search.returns),
                    }
                }
            )
        ]
    return out


def _equality_pairs(join: JoinDef, layer: SemanticLayer) -> list[tuple[str, str, str, str]]:
    """Parse a join's raw `on` condition into (entity, column, entity, column) pairs.

    Only conjunctions of qualified equalities are representable in OSI. Anything
    else makes the caller skip the relationship with an issue.
    """
    tree = sqlglot.parse_one(join.on, read=layer.dialect)
    conditions = list(tree.flatten()) if isinstance(tree, exp.And) else [tree]
    pairs: list[tuple[str, str, str, str]] = []
    for node in conditions:
        if not isinstance(node, exp.EQ):
            raise ValueError(f"not an equality: {node.sql()}")
        left, right = node.this, node.expression
        if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
            raise ValueError(f"not column = column: {node.sql()}")
        if not (left.table and right.table):
            raise ValueError(f"columns must be entity-qualified: {node.sql()}")
        pairs.append((left.table, left.name, right.table, right.name))
    return pairs


def _relationship_dict(join: JoinDef, layer: SemanticLayer, issues: list[str]) -> dict | None:
    try:
        pairs = _equality_pairs(join, layer)
    except (ValueError, sqlglot.errors.ParseError) as e:
        issues.append(
            f"join {join.left}/{join.right} skipped: OSI relationships need "
            f"'a.x = b.y' equalities ({e})"
        )
        return None

    sides: dict[str, list[str]] = {}
    for ent_a, col_a, ent_b, col_b in pairs:
        sides.setdefault(ent_a, []).append(col_a)
        sides.setdefault(ent_b, []).append(col_b)
    if set(sides) != {join.left, join.right}:
        issues.append(
            f"join {join.left}/{join.right} skipped: condition references "
            f"other entities ({sorted(sides)})"
        )
        return None

    # OSI's `from` is the many side, `to` the one side. steward does not declare
    # cardinality, so infer it: the side whose join columns are its primary key
    # is the one side. If neither side's key matches, keep left->right and say so.
    def is_one_side(entity_name: str) -> bool:
        pk = layer.entities[entity_name].primary_key
        return pk is not None and sides[entity_name] == [pk]

    if is_one_side(join.right):
        from_side, to_side = join.left, join.right
    elif is_one_side(join.left):
        from_side, to_side = join.right, join.left
    else:
        from_side, to_side = join.left, join.right
        issues.append(
            f"join {join.left}/{join.right}: cardinality unknown, assumed "
            f"{from_side} many-to-one {to_side}"
        )

    from_columns = [c_a if e_a == from_side else c_b for e_a, c_a, e_b, c_b in pairs]
    to_columns = [c_b if e_a == from_side else c_a for e_a, c_a, e_b, c_b in pairs]
    return {
        "name": f"{from_side}_to_{to_side}",
        "from": from_side,
        "to": to_side,
        "from_columns": from_columns,
        "to_columns": to_columns,
    }


def _metric_dict(metric: MetricDef, layer: SemanticLayer, issues: list[str]) -> dict:
    if metric.field == "*":
        ref = "*"
        if metric.aggregate != "count":
            issues.append(f"metric {metric.name}: '*' with {metric.aggregate} is unusual")
    else:
        ref = f"{metric.entity}.{metric.field}"
    sql = _AGGREGATE_SQL[metric.aggregate].format(ref=ref)
    out: dict = {"name": metric.name, "expression": _expression(sql, layer.dialect)}
    if metric.description:
        out["description"] = metric.description
    # The allow-lists are the governance half of a steward metric: which
    # dimensions and filters an agent may use. OSI has no home for that.
    out["custom_extensions"] = [
        _extension(
            {
                "entity": metric.entity,
                "aggregate": metric.aggregate,
                "field": metric.field,
                "dimensions_allowed": list(metric.dimensions_allowed),
                "filters_allowed": list(metric.filters_allowed),
            }
        )
    ]
    return out


def to_osi(layer: SemanticLayer, model_name: str = "sql_steward_model") -> tuple[dict, list[str]]:
    """Build an OSI document dict from a semantic layer.

    Returns (document, issues). Issues are human-readable notes about anything
    OSI could not express natively; the export never drops data silently.
    """
    issues: list[str] = []

    datasets = [_dataset_dict(e, layer.dialect) for e in layer.entities.values()]

    relationships = []
    for join in layer.joins:
        rel = _relationship_dict(join, layer, issues)
        if rel:
            relationships.append(rel)

    metrics = [_metric_dict(m, layer, issues) for m in layer.metrics.values()]

    model_extension: dict = {
        "dialect": layer.dialect,
        "policy": {
            "block_pii": sorted(layer.policy.block_pii),
            "max_rows": layer.policy.max_rows,
        },
    }
    if layer.checks:
        model_extension["checks"] = {
            name: {
                k: v
                for k, v in {
                    "entity": c.entity,
                    "kind": c.kind,
                    "field": c.field,
                    "min": c.min,
                    "max": c.max,
                    "values": list(c.values) if c.values else None,
                    "severity": c.severity,
                }.items()
                if v is not None
            }
            for name, c in layer.checks.items()
        }

    model: dict = {"name": model_name, "datasets": datasets}
    if relationships:
        model["relationships"] = relationships
    if metrics:
        model["metrics"] = metrics
    model["custom_extensions"] = [_extension(model_extension)]

    return {"version": OSI_VERSION, "semantic_model": [model]}, issues


def to_osi_yaml(layer: SemanticLayer, model_name: str = "sql_steward_model") -> tuple[str, list[str]]:
    import yaml

    doc, issues = to_osi(layer, model_name=model_name)
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), issues
