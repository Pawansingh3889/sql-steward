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
import sqlite3
import time

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


# --- persistent, windowed per-role query budget ----------------------------

_budget_counts: dict = {}  # in-memory fallback if the persistent store is unusable


def _budget_db_path() -> str:
    return os.environ.get("SQL_STEWARD_BUDGET_DB", "logs/steward-budget.db")


def enforce_budget() -> None:
    """Rate-limit queries per role, persistently.

    Set ``SQL_STEWARD_QUERY_BUDGET`` to an integer cap. By default it is a
    persistent lifetime limit per role, stored in SQLite so a caller cannot reset
    it by reconnecting. Set ``SQL_STEWARD_BUDGET_WINDOW`` (seconds) to make it a
    sliding-window rate limit instead: at most N queries per role in any window.
    ``SQL_STEWARD_BUDGET_DB`` chooses the store (default logs/steward-budget.db).
    No-op if the cap is unset; falls back to an in-memory session cap only if the
    store cannot be opened.
    """
    cap = os.environ.get("SQL_STEWARD_QUERY_BUDGET")
    if not cap:
        return
    try:
        cap_n = int(cap)
    except ValueError:
        return
    role = os.environ.get("SQL_STEWARD_ROLE", "_default")

    window_s = None
    raw_window = os.environ.get("SQL_STEWARD_BUDGET_WINDOW")
    if raw_window:
        try:
            w = float(raw_window)
            window_s = w if w > 0 else None
        except ValueError:
            window_s = None

    path = _budget_db_path()
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(path)
    except Exception:
        # Persistent store unavailable: degrade to an in-memory session cap
        # rather than silently dropping the budget entirely.
        _enforce_budget_memory(cap_n, role)
        return

    try:
        now = time.time()
        conn.execute("CREATE TABLE IF NOT EXISTS budget (role TEXT NOT NULL, ts REAL NOT NULL)")
        if window_s is not None:
            cutoff = now - window_s
            conn.execute("DELETE FROM budget WHERE ts < ?", (cutoff,))
            (used,) = conn.execute(
                "SELECT COUNT(*) FROM budget WHERE role = ? AND ts >= ?",
                (role, cutoff),
            ).fetchone()
        else:
            (used,) = conn.execute(
                "SELECT COUNT(*) FROM budget WHERE role = ?", (role,)
            ).fetchone()
        if used >= cap_n:
            conn.commit()
            raise Refusal(
                kind="budget_exceeded",
                detail=(
                    f"Query budget of {cap_n} for role '{role}' is exhausted"
                    + (f" in the last {int(window_s)}s." if window_s is not None
                       else " (persistent cap).")
                ),
                recovery={"budget": cap_n, "role": role, "window_seconds": window_s},
            )
        conn.execute("INSERT INTO budget (role, ts) VALUES (?, ?)", (role, now))
        conn.commit()
    finally:
        conn.close()


def _enforce_budget_memory(cap_n: int, role: str) -> None:
    used = _budget_counts.get(role, 0)
    if used >= cap_n:
        raise Refusal(
            kind="budget_exceeded",
            detail=f"Query budget of {cap_n} for role '{role}' is exhausted for this session.",
            recovery={"budget": cap_n, "role": role, "window_seconds": None},
        )
    _budget_counts[role] = used + 1


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
