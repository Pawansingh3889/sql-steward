"""Compile a typed request into a read-only SQL statement.

There is no path from an agent prompt to raw SQL here. The agent supplies an
entity or metric name plus dimensions/filters chosen from the semantic layer's
allow-lists; this module turns that into a single SELECT, emitted for the target
dialect via sqlglot. It can only ever build a SELECT -- that is the read-only
guarantee, enforced by construction rather than by hoping a connection is
configured correctly.

Two refusals can happen before anything runs:
  * pii_blocked      -- a referenced field carries a PII tag the policy blocks
  * unreachable_entity -- a reference needs a join that the layer does not define
"""
from __future__ import annotations

from dataclasses import dataclass

import sqlglot

from sql_steward.semantic import FieldDef, SemanticLayer

# Comparison operators an agent may use in a filter. Anything else is rejected.
_OPERATORS = {
    "=": "{c} = {p}",
    "!=": "{c} <> {p}",
    "<": "{c} < {p}",
    "<=": "{c} <= {p}",
    ">": "{c} > {p}",
    ">=": "{c} >= {p}",
    "like": "{c} LIKE {p}",
    "in": "{c} IN ({p})",
    "not in": "{c} NOT IN ({p})",
    "is null": "{c} IS NULL",
    "is not null": "{c} IS NOT NULL",
}
_NO_VALUE_OPS = {"is null", "is not null"}


class Refusal(Exception):
    """A request the layer deliberately declines. Carries a structured payload
    the server hands back to the agent so it can adapt instead of guessing."""

    def __init__(self, kind: str, detail: str, recovery: dict | None = None):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail
        self.recovery = recovery or {}

    def as_dict(self) -> dict:
        return {"refused": True, "kind": self.kind, "detail": self.detail,
                "recovery": self.recovery}


@dataclass
class Compiled:
    sql: str
    params: dict
    dialect: str
    entities: tuple[str, ...]  # entities actually touched, for auditing


def _check_pii(layer: SemanticLayer, entity: str, fdef: FieldDef) -> None:
    if fdef.pii and fdef.pii in layer.policy.block_pii:
        raise Refusal(
            kind="pii_blocked",
            detail=(
                f"Field '{entity}.{fdef.name}' is tagged {fdef.pii}, which this "
                f"policy refuses. Ask for a non-PII field or an aggregate instead."
            ),
            recovery={"blocked_category": fdef.pii, "field": f"{entity}.{fdef.name}"},
        )


class _Where:
    """Accumulates filter conditions and their bound parameters."""

    def __init__(self) -> None:
        self.conditions: list[str] = []
        self.params: dict = {}
        self._n = 0

    def add(self, qualified: str, op: str, value) -> None:
        op = op.strip().lower()
        if op not in _OPERATORS:
            raise Refusal(
                kind="bad_operator",
                detail=f"Operator '{op}' is not allowed.",
                recovery={"allowed": sorted(_OPERATORS)},
            )
        if op in _NO_VALUE_OPS:
            self.conditions.append(_OPERATORS[op].format(c=qualified))
            return
        if op in ("in", "not in"):
            values = value if isinstance(value, (list, tuple)) else [value]
            names = []
            for v in values:
                key = f"p{self._n}"
                self._n += 1
                self.params[key] = v
                names.append(f":{key}")
            self.conditions.append(_OPERATORS[op].format(c=qualified, p=", ".join(names)))
            return
        key = f"p{self._n}"
        self._n += 1
        self.params[key] = value
        self.conditions.append(_OPERATORS[op].format(c=qualified, p=f":{key}"))


def _from_clause(layer: SemanticLayer, entity_name: str) -> str:
    ent = layer.get_entity(entity_name)
    return f"{ent.table} AS {ent.name}" if ent.table != ent.name else ent.table


def _join_or_refuse(layer: SemanticLayer, base: str, other: str) -> str:
    join = layer.find_join(base, other)
    if join is None:
        raise Refusal(
            kind="unreachable_entity",
            detail=(
                f"No join is defined between '{base}' and '{other}', so they "
                f"cannot be combined. Define a join in the semantic layer if "
                f"this relationship is real."
            ),
            recovery={"from": base, "to": other},
        )
    return join.on


def _emit(query: sqlglot.exp.Select, dialect: str) -> str:
    # Re-parse through sqlglot so the output is dialect-correct (e.g. LIMIT
    # becomes TOP on T-SQL) and so anything that isn't a SELECT would blow up
    # here rather than reach a database.
    sql = query.sql(dialect=dialect)
    parsed = sqlglot.parse_one(sql, read=dialect)
    if not isinstance(parsed, sqlglot.exp.Select):
        raise Refusal(
            kind="not_a_select",
            detail="Compiler produced a non-SELECT statement; refusing to run it.",
        )
    return sql


def compile_records(
    layer: SemanticLayer,
    entity: str,
    fields: list[str] | None = None,
    filters: list[dict] | None = None,
    order_by: str | None = None,
    limit: int | None = None,
    dialect: str | None = None,
) -> Compiled:
    """Compile a SELECT of raw rows from a single entity."""
    dialect = dialect or layer.dialect
    ent = layer.get_entity(entity)

    selected = list(fields) if fields else list(ent.fields)
    for f in selected:
        _check_pii(layer, entity, ent.get_field(f))
    select_exprs = [f"{entity}.{f}" for f in selected]

    where = _Where()
    for flt in filters or []:
        ref = flt["field"]
        _, fdef = layer.resolve_ref(ref, entity)
        # records are single-entity; a cross-entity filter is unreachable
        ref_entity = ref.split(".")[0] if "." in ref else entity
        if ref_entity != entity:
            _join_or_refuse(layer, entity, ref_entity)  # records stay single-entity
        _check_pii(layer, ref_entity, fdef)
        where.add(f"{ref_entity}.{fdef.name}", flt.get("op", "="), flt.get("value"))

    query = sqlglot.select(*select_exprs).from_(_from_clause(layer, entity))
    for cond in where.conditions:
        query = query.where(cond)
    if order_by:
        _, _ = layer.resolve_ref(order_by, entity)  # validate it's a real field
        query = query.order_by(order_by)
    query = query.limit(_clamp_limit(layer, limit))

    return Compiled(_emit(query, dialect), where.params, dialect, (entity,))


def compile_metric(
    layer: SemanticLayer,
    metric: str,
    dimensions: list[str] | None = None,
    filters: list[dict] | None = None,
    limit: int | None = None,
    dialect: str | None = None,
) -> Compiled:
    """Compile a pre-approved aggregate, grouped by allowed dimensions."""
    dialect = dialect or layer.dialect
    m = layer.get_metric(metric)
    base = m.entity
    touched = {base}

    dims = list(dimensions or [])
    for d in dims:
        if d not in m.dimensions_allowed:
            raise Refusal(
                kind="dimension_not_allowed",
                detail=f"Metric '{metric}' cannot be grouped by '{d}'.",
                recovery={"allowed": list(m.dimensions_allowed)},
            )

    where = _Where()
    for flt in filters or []:
        ref = flt["field"]
        if ref not in m.filters_allowed:
            raise Refusal(
                kind="filter_not_allowed",
                detail=f"Metric '{metric}' cannot be filtered by '{ref}'.",
                recovery={"allowed": list(m.filters_allowed)},
            )

    # Aggregate expression
    if m.aggregate == "count_distinct":
        agg_sql = f"COUNT(DISTINCT {base}.{m.field})"
    elif m.field == "*":
        agg_sql = f"{m.aggregate.upper()}(*)"
    else:
        agg_sql = f"{m.aggregate.upper()}({base}.{m.field})"
    select_exprs = [f"{agg_sql} AS {metric}"]

    joins: dict[str, str] = {}
    group_exprs: list[str] = []
    for d in dims:
        ent_name, fdef = layer.resolve_ref(d, base)
        _check_pii(layer, ent_name, fdef)
        if ent_name != base:
            joins[ent_name] = _join_or_refuse(layer, base, ent_name)
            touched.add(ent_name)
        qualified = f"{ent_name}.{fdef.name}"
        select_exprs.insert(-1, qualified)
        group_exprs.append(qualified)

    for flt in filters or []:
        ref = flt["field"]
        ent_name, fdef = layer.resolve_ref(ref, base)
        _check_pii(layer, ent_name, fdef)
        if ent_name != base:
            joins[ent_name] = _join_or_refuse(layer, base, ent_name)
            touched.add(ent_name)
        where.add(f"{ent_name}.{fdef.name}", flt.get("op", "="), flt.get("value"))

    query = sqlglot.select(*select_exprs).from_(_from_clause(layer, base))
    for ent_name, on in joins.items():
        query = query.join(_from_clause(layer, ent_name), on=on, join_type="inner")
    for cond in where.conditions:
        query = query.where(cond)
    if group_exprs:
        query = query.group_by(*group_exprs)
    query = query.limit(_clamp_limit(layer, limit))

    return Compiled(_emit(query, dialect), where.params, dialect, tuple(sorted(touched)))


def compile_vector_search(
    layer: SemanticLayer,
    entity: str,
    query_embedding: list[float],
    fields: list[str] | None = None,
    filters: list[dict] | None = None,
    k: int = 10,
    dialect: str | None = None,
) -> Compiled:
    """Compile a pgvector nearest-neighbour search over an entity's embedding column.

    The query embedding is bound as a parameter; the embedding column is never
    returned. PostgreSQL-only (pgvector). Same PII refusal as everything else.
    """
    dialect = dialect or layer.dialect
    if dialect not in ("postgres", "postgresql"):
        raise Refusal(
            kind="vector_unsupported_dialect",
            detail="Semantic search needs pgvector, which is PostgreSQL-only.",
            recovery={"dialect": dialect},
        )
    ent = layer.get_entity(entity)
    if ent.search is None:
        raise Refusal(
            kind="no_vector_search",
            detail=f"Entity '{entity}' has no semantic-search (vector) configuration.",
        )

    if fields:
        selected = list(fields)
    elif ent.search.returns:
        selected = list(ent.search.returns)
    else:
        selected = [f for f in ent.fields if f != ent.search.vector_column]
    for f in selected:
        _check_pii(layer, entity, ent.get_field(f))

    where = _Where()
    for flt in filters or []:
        ref = flt["field"]
        ref_entity = ref.split(".")[0] if "." in ref else entity
        if ref_entity != entity:
            raise Refusal(
                kind="unreachable_entity",
                detail="Semantic-search filters must be on the same entity.",
                recovery={"entity": entity},
            )
        _, fdef = layer.resolve_ref(ref, entity)
        _check_pii(layer, ref_entity, fdef)
        where.add(f"{entity}.{fdef.name}", flt.get("op", "="), flt.get("value"))

    # Bind the embedding as a text vector literal cast to pgvector. Building the
    # SQL directly (not via sqlglot) because `<=>` is a pgvector operator.
    vec = "[" + ",".join(repr(float(x)) for x in query_embedding) + "]"
    params = {"qvec": vec, **where.params}
    cols = ", ".join(f"{entity}.{f}" for f in selected)
    distance = f"{entity}.{ent.search.vector_column} <=> CAST(:qvec AS vector)"
    where_sql = (" WHERE " + " AND ".join(where.conditions)) if where.conditions else ""
    limit = _clamp_limit(layer, k)
    sql = (
        f"SELECT {cols}, {distance} AS distance "
        f"FROM {_from_clause(layer, entity)}{where_sql} "
        f"ORDER BY distance LIMIT {limit}"
    )
    return Compiled(sql=sql, params=params, dialect="postgres", entities=(entity,))


def _clamp_limit(layer: SemanticLayer, limit: int | None) -> int:
    cap = layer.policy.max_rows
    if limit is None:
        return cap
    return max(1, min(int(limit), cap))
