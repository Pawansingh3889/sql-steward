"""Tests for the optional safety layers (query budget)."""
import pytest

from sql_steward import safety
from sql_steward.compiler import Refusal


def test_query_budget_refuses_when_exhausted(monkeypatch):
    safety._budget_counts.clear()
    monkeypatch.setenv("SQL_STEWARD_QUERY_BUDGET", "2")
    monkeypatch.setenv("SQL_STEWARD_ROLE", "analyst")
    safety.enforce_budget()   # 1
    safety.enforce_budget()   # 2
    with pytest.raises(Refusal) as e:
        safety.enforce_budget()   # 3 -> over the cap
    assert e.value.kind == "budget_exceeded"
    assert e.value.recovery["budget"] == 2
    safety._budget_counts.clear()


def test_budget_is_per_role(monkeypatch):
    safety._budget_counts.clear()
    monkeypatch.setenv("SQL_STEWARD_QUERY_BUDGET", "1")
    monkeypatch.setenv("SQL_STEWARD_ROLE", "role_a")
    safety.enforce_budget()
    monkeypatch.setenv("SQL_STEWARD_ROLE", "role_b")
    safety.enforce_budget()   # different role, own allowance
    monkeypatch.setenv("SQL_STEWARD_ROLE", "role_a")
    with pytest.raises(Refusal):
        safety.enforce_budget()
    safety._budget_counts.clear()


def test_no_budget_set_is_noop(monkeypatch):
    safety._budget_counts.clear()
    monkeypatch.delenv("SQL_STEWARD_QUERY_BUDGET", raising=False)
    for _ in range(50):
        safety.enforce_budget()   # no cap -> never raises
