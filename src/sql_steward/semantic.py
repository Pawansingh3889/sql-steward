"""The semantic layer: the curated set of definitions the agent is allowed to query.

This is the whole point of sql-steward. An agent never sees a connection string
and never writes SQL. It can only ask for things defined here: entities (tables
it may read), the fields on them, the joins that are allowed between them, and
named metrics (pre-approved aggregates). Every field can carry a PII tag, and a
policy decides which tag categories are refused before a query ever runs.

The layer is plain YAML so it lives in version control next to your schema and
is reviewed like any other code.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Aggregates a metric is allowed to use. Deliberately small.
ALLOWED_AGGREGATES = {"sum", "count", "count_distinct", "avg", "min", "max"}
ALLOWED_CHECKS = {"not_null", "unique", "range", "accepted_values", "row_count_min"}


class SemanticError(ValueError):
    """The semantic layer is malformed, or a caller named something it doesn't have.

    Unknown-name lookups (entity, metric, field) also carry a machine-readable
    `kind` and a `recovery` dict listing what IS available -- the same idea as
    compiler.Refusal -- so the MCP server can hand an agent something it can
    act on instead of a bare string. The agent's most common mistake is a
    misspelled name; with the available list (and closest spellings) in the
    error itself, it corrects in the same turn rather than spending a second
    round-trip on list_metrics / describe_entity. Layer-authoring errors
    (bad YAML, missing keys) leave both unset.
    """

    def __init__(self, message: str, kind: str | None = None, recovery: dict | None = None):
        super().__init__(message)
        self.kind = kind
        self.recovery = recovery or {}


def _suggest(name: str, candidates) -> dict:
    """Recovery payload for an unknown-name error: everything that exists,
    plus the closest spellings when the name looks like a typo."""
    recovery: dict = {"available": sorted(candidates)}
    close = difflib.get_close_matches(name, list(candidates), n=3, cutoff=0.6)
    if close:
        recovery["did_you_mean"] = close
    return recovery


def _join_on(join: dict) -> str:
    """Read a join's `on` condition.

    PyYAML follows YAML 1.1, where a bare ``on:`` key is parsed as the boolean
    ``True`` (same for off/yes/no). Accept either form so authors can write the
    natural ``on:`` without quoting.
    """
    if "on" in join:
        return join["on"]
    if True in join:
        return join[True]
    raise SemanticError(f"Join {join!r} is missing an 'on' condition")


@dataclass(frozen=True)
class FieldDef:
    """A single column the agent may reference, with an optional PII tag."""

    name: str
    type: str = "text"
    pii: str | None = None  # e.g. EMAIL_ADDRESS, PERSON, CREDIT_CARD
    description: str = ""


@dataclass(frozen=True)
class VectorSearch:
    """pgvector semantic-search config for an entity.

    `vector_column` is the entity field holding the embedding; `returns` are the
    default fields a search yields (the embedding itself is never returned).
    """

    vector_column: str
    dim: int = 0
    returns: tuple[str, ...] = ()


@dataclass(frozen=True)
class EntityDef:
    """A table the agent may read, exposed under a stable logical name."""

    name: str
    table: str
    primary_key: str | None = None
    description: str = ""
    fields: dict[str, FieldDef] = field(default_factory=dict)
    search: "VectorSearch | None" = None

    def get_field(self, field_name: str) -> FieldDef:
        if field_name not in self.fields:
            raise SemanticError(
                f"Entity '{self.name}' has no field '{field_name}'",
                kind="unknown_field",
                recovery={"entity": self.name, **_suggest(field_name, self.fields)},
            )
        return self.fields[field_name]


@dataclass(frozen=True)
class JoinDef:
    """An allowed join between two entities. If a join is not listed here, the
    agent cannot reach across those entities -- it gets a refusal, not an
    invented relationship."""

    left: str
    right: str
    on: str  # raw condition over logical entity names, e.g. "orders.customer_id = customers.id"

    def connects(self, a: str, b: str) -> bool:
        return {self.left, self.right} == {a, b}


@dataclass(frozen=True)
class MetricDef:
    """A pre-approved aggregate. The agent picks dimensions and filters from a
    fixed allow-list; it cannot invent the aggregation."""

    name: str
    entity: str
    aggregate: str
    field: str
    description: str = ""
    dimensions_allowed: tuple[str, ...] = ()  # references, optionally "entity.field"
    filters_allowed: tuple[str, ...] = ()


@dataclass(frozen=True)
class CheckDef:
    """A declared data-quality assertion. It compiles to a query that counts
    violations; zero violations passes. Like metrics, the rule is fixed in the
    layer -- the agent runs the declared checks, it cannot invent new ones.

    kinds: not_null | unique | range | accepted_values | row_count_min
    """

    name: str
    entity: str
    kind: str
    field: str | None = None
    min: float | None = None
    max: float | None = None
    values: tuple = ()
    severity: str = "error"  # error | warn
    description: str = ""


@dataclass(frozen=True)
class Policy:
    """What the layer refuses. PII categories listed here are blocked at the
    tool boundary, before any SQL is compiled or run."""

    block_pii: frozenset[str] = frozenset()
    max_rows: int = 1000


@dataclass(frozen=True)
class SemanticLayer:
    dialect: str
    entities: dict[str, EntityDef]
    joins: tuple[JoinDef, ...]
    metrics: dict[str, MetricDef]
    policy: Policy
    checks: dict[str, "CheckDef"] = field(default_factory=dict)

    # -- lookups -------------------------------------------------------------

    def get_entity(self, name: str) -> EntityDef:
        if name not in self.entities:
            raise SemanticError(
                f"Unknown entity '{name}'",
                kind="unknown_entity",
                recovery=_suggest(name, self.entities),
            )
        return self.entities[name]

    def get_metric(self, name: str) -> MetricDef:
        if name not in self.metrics:
            raise SemanticError(
                f"Unknown metric '{name}'",
                kind="unknown_metric",
                recovery=_suggest(name, self.metrics),
            )
        return self.metrics[name]

    def find_join(self, a: str, b: str) -> JoinDef | None:
        for j in self.joins:
            if j.connects(a, b):
                return j
        return None

    def resolve_ref(self, ref: str, default_entity: str) -> tuple[str, FieldDef]:
        """Turn a reference like "field" or "entity.field" into (entity, FieldDef)."""
        if "." in ref:
            ent_name, field_name = ref.split(".", 1)
        else:
            ent_name, field_name = default_entity, ref
        entity = self.get_entity(ent_name)
        return ent_name, entity.get_field(field_name)

    # -- construction --------------------------------------------------------

    @classmethod
    def from_dict(cls, data: dict) -> SemanticLayer:
        if not isinstance(data, dict):
            raise SemanticError("Semantic layer must be a mapping")
        dialect = str(data.get("dialect", "")).strip().lower()
        if not dialect:
            raise SemanticError("Semantic layer must declare a 'dialect'")

        entities: dict[str, EntityDef] = {}
        for name, raw in (data.get("entities") or {}).items():
            raw = raw or {}
            fields = {
                fname: FieldDef(
                    name=fname,
                    type=str((fraw or {}).get("type", "text")),
                    pii=(fraw or {}).get("pii"),
                    description=str((fraw or {}).get("description", "")),
                )
                for fname, fraw in (raw.get("fields") or {}).items()
            }
            raw_search = raw.get("search")
            search = None
            if raw_search:
                search = VectorSearch(
                    vector_column=raw_search["vector_column"],
                    dim=int(raw_search.get("dim", 0)),
                    returns=tuple(raw_search.get("returns") or ()),
                )
            entities[name] = EntityDef(
                name=name,
                table=str(raw.get("table", name)),
                primary_key=raw.get("primary_key"),
                description=str(raw.get("description", "")),
                fields=fields,
                search=search,
            )

        joins = tuple(
            JoinDef(left=j["left"], right=j["right"], on=_join_on(j))
            for j in (data.get("joins") or [])
        )

        metrics: dict[str, MetricDef] = {}
        for name, raw in (data.get("metrics") or {}).items():
            raw = raw or {}
            agg = str(raw.get("aggregate", "")).strip().lower()
            if agg not in ALLOWED_AGGREGATES:
                raise SemanticError(
                    f"Metric '{name}' uses unsupported aggregate '{agg}'. "
                    f"Allowed: {sorted(ALLOWED_AGGREGATES)}"
                )
            metrics[name] = MetricDef(
                name=name,
                entity=raw["entity"],
                aggregate=agg,
                field=str(raw.get("field", "*")),
                description=str(raw.get("description", "")),
                dimensions_allowed=tuple(raw.get("dimensions_allowed") or ()),
                filters_allowed=tuple(raw.get("filters_allowed") or ()),
            )

        checks: dict[str, CheckDef] = {}
        for name, raw in (data.get("checks") or {}).items():
            raw = raw or {}
            kind = str(raw.get("kind", "")).strip().lower()
            if kind not in ALLOWED_CHECKS:
                raise SemanticError(
                    f"Check '{name}' uses unsupported kind '{kind}'. "
                    f"Allowed: {sorted(ALLOWED_CHECKS)}"
                )
            checks[name] = CheckDef(
                name=name,
                entity=raw["entity"],
                kind=kind,
                field=raw.get("field"),
                min=raw.get("min"),
                max=raw.get("max"),
                values=tuple(raw.get("values") or ()),
                severity=str(raw.get("severity", "error")).strip().lower(),
                description=str(raw.get("description", "")),
            )

        pol_raw = data.get("policy") or {}
        policy = Policy(
            block_pii=frozenset(pol_raw.get("block_pii") or ()),
            max_rows=int(pol_raw.get("max_rows", 1000)),
        )

        layer = cls(
            dialect=dialect,
            entities=entities,
            joins=joins,
            metrics=metrics,
            policy=policy,
            checks=checks,
        )
        layer.validate()
        return layer

    @classmethod
    def from_yaml(cls, path: str | Path) -> SemanticLayer:
        text = Path(path).read_text(encoding="utf-8")
        return cls.from_dict(yaml.safe_load(text))

    def validate(self) -> None:
        """Fail fast on references that point at things that do not exist."""
        for j in self.joins:
            self.get_entity(j.left)
            self.get_entity(j.right)
        for ent in self.entities.values():
            if ent.search is not None:
                ent.get_field(ent.search.vector_column)
                for ref in ent.search.returns:
                    ent.get_field(ref)
        for m in self.metrics.values():
            entity = self.get_entity(m.entity)
            if m.field != "*":
                # count over * is allowed; everything else must name a real field
                entity.get_field(m.field)
            for ref in (*m.dimensions_allowed, *m.filters_allowed):
                self.resolve_ref(ref, m.entity)
        for c in self.checks.values():
            entity = self.get_entity(c.entity)
            if c.kind != "row_count_min":
                if not c.field:
                    raise SemanticError(f"Check '{c.name}' ({c.kind}) needs a 'field'")
                entity.get_field(c.field)
            if c.kind == "range" and c.min is None and c.max is None:
                raise SemanticError(f"Check '{c.name}' (range) needs 'min' or 'max'")
            if c.kind == "accepted_values" and not c.values:
                raise SemanticError(f"Check '{c.name}' (accepted_values) needs 'values'")
            if c.kind == "row_count_min" and c.min is None:
                raise SemanticError(f"Check '{c.name}' (row_count_min) needs 'min'")
