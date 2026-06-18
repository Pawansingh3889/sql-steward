"""Optional, graceful wiring to the rest of the governed-agent-stack.

The semantic layer is the primary control. These integrations are extra
defence and are all opt-in via environment variables; if a library isn't
installed or a variable isn't set, the matching step is a no-op.

  RBAC    query-warden    SQL_STEWARD_POLICY (+ SQL_STEWARD_ROLE)
  Masking pii-veil        SQL_STEWARD_MASK=1 (+ SQL_STEWARD_MASK_COLUMNS)
  Audit   agent-blackbox  on by default if installed; SQL_STEWARD_AUDIT=0 to disable,
                          SQL_STEWARD_AUDIT_DB to choose the path.
"""
from __future__ import annotations

import os

from sql_steward.compiler import Refusal

_ON = {"1", "true", "yes", "on"}


# --- RBAC via query-warden -------------------------------------------------

_warden = None
_warden_resolved = False


def _get_warden():
    global _warden, _warden_resolved
    if _warden_resolved:
        return _warden
    _warden_resolved = True
    path = os.environ.get("SQL_STEWARD_POLICY")
    if not path:
        return None
    try:
        from query_warden import Policy, Warden

        _warden = Warden(Policy.from_yaml(path))
    except Exception:
        _warden = None
    return _warden


def enforce_rbac(sql: str, dialect: str) -> None:
    """Second-pass role check on the compiled SQL. Raises Refusal if blocked."""
    warden = _get_warden()
    if warden is None:
        return
    role = os.environ.get("SQL_STEWARD_ROLE")
    try:
        decision = warden.check(sql, role=role, dialect=dialect)
    except Exception:
        # The semantic layer already whitelisted this query; don't fail a
        # legitimate read because the optional integration hiccupped.
        return
    if not decision.allowed:
        raise Refusal(
            kind="rbac_denied",
            detail="; ".join(getattr(decision, "violations", []))
            or "blocked by role policy",
            recovery={"role": role},
        )


# --- result masking via pii-veil -------------------------------------------

_veil = None
_veil_resolved = False


def _get_veil():
    global _veil, _veil_resolved
    if _veil_resolved:
        return _veil
    _veil_resolved = True
    if (os.environ.get("SQL_STEWARD_MASK") or "").strip().lower() not in _ON:
        return None
    try:
        from pii_veil import Veil

        _veil = Veil()
    except Exception:
        _veil = None
    return _veil


def mask_rows(rows: list[dict]) -> list[dict]:
    veil = _get_veil()
    if veil is None or not rows:
        return rows
    cols = os.environ.get("SQL_STEWARD_MASK_COLUMNS")
    columns = [c.strip() for c in cols.split(",") if c.strip()] if cols else None
    try:
        return veil.scrub_rows(rows, columns=columns)
    except Exception:
        return rows


# --- audit via agent-blackbox ----------------------------------------------

_ledger = None
_ledger_resolved = False


def _get_ledger():
    global _ledger, _ledger_resolved
    if _ledger_resolved:
        return _ledger
    _ledger_resolved = True
    if (os.environ.get("SQL_STEWARD_AUDIT", "1")).strip().lower() not in _ON:
        return None
    try:
        from agent_blackbox import Ledger

        path = os.environ.get("SQL_STEWARD_AUDIT_DB", "logs/steward.db")
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        _ledger = Ledger(path)
    except Exception:
        _ledger = None
    return _ledger


def audit(action: str, target=None, payload=None, meta=None, outcome=None) -> None:
    ledger = _get_ledger()
    if ledger is None:
        return
    try:
        ledger.record(
            actor=os.environ.get("SQL_STEWARD_ACTOR", "sql-steward"),
            action=action,
            target=target,
            payload=payload,
            meta=meta,
            outcome=outcome,
        )
    except Exception:
        pass


def audit_status() -> dict:
    ledger = _get_ledger()
    if ledger is None:
        return {"enabled": False,
                "detail": "audit disabled, or agent-blackbox not installed"}
    try:
        v = ledger.verify()
        return {"enabled": True, "ok": bool(v), "entries": ledger.count()}
    except Exception as exc:
        return {"enabled": True, "ok": False, "error": str(exc)[:200]}
