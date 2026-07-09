"""Tests for the optional safety layers (persistent, windowed query budget)."""
import pytest

from sql_steward import safety
from sql_steward.compiler import Refusal


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Each test gets a fresh persistent store and a clean in-memory fallback."""
    safety._budget_counts.clear()
    monkeypatch.setenv("SQL_STEWARD_BUDGET_DB", str(tmp_path / "budget.db"))
    monkeypatch.delenv("SQL_STEWARD_BUDGET_WINDOW", raising=False)
    yield
    safety._budget_counts.clear()


def test_query_budget_refuses_when_exhausted(monkeypatch):
    monkeypatch.setenv("SQL_STEWARD_QUERY_BUDGET", "2")
    monkeypatch.setenv("SQL_STEWARD_ROLE", "analyst")
    safety.enforce_budget()   # 1
    safety.enforce_budget()   # 2
    with pytest.raises(Refusal) as e:
        safety.enforce_budget()   # 3 -> over the cap
    assert e.value.kind == "budget_exceeded"
    assert e.value.recovery["budget"] == 2


def test_budget_is_per_role(monkeypatch):
    monkeypatch.setenv("SQL_STEWARD_QUERY_BUDGET", "1")
    monkeypatch.setenv("SQL_STEWARD_ROLE", "role_a")
    safety.enforce_budget()
    monkeypatch.setenv("SQL_STEWARD_ROLE", "role_b")
    safety.enforce_budget()   # different role, its own allowance
    monkeypatch.setenv("SQL_STEWARD_ROLE", "role_a")
    with pytest.raises(Refusal):
        safety.enforce_budget()


def test_no_budget_set_is_noop(monkeypatch):
    monkeypatch.delenv("SQL_STEWARD_QUERY_BUDGET", raising=False)
    for _ in range(50):
        safety.enforce_budget()   # no cap -> never raises


def test_budget_persists_across_restart(monkeypatch):
    """The cap survives a process restart: clearing in-memory state does not
    reset a caller's spend, because usage is stored on disk."""
    monkeypatch.setenv("SQL_STEWARD_QUERY_BUDGET", "2")
    monkeypatch.setenv("SQL_STEWARD_ROLE", "analyst")
    safety.enforce_budget()
    safety.enforce_budget()
    safety._budget_counts.clear()          # simulate a fresh process
    with pytest.raises(Refusal):
        safety.enforce_budget()            # persisted rows still count


def test_budget_window_slides(monkeypatch):
    """With a window set, usage older than the window stops counting."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(safety.time, "time", lambda: clock["t"])
    monkeypatch.setenv("SQL_STEWARD_QUERY_BUDGET", "2")
    monkeypatch.setenv("SQL_STEWARD_BUDGET_WINDOW", "60")
    monkeypatch.setenv("SQL_STEWARD_ROLE", "analyst")
    safety.enforce_budget()   # t=1000
    safety.enforce_budget()   # t=1000
    with pytest.raises(Refusal):
        safety.enforce_budget()   # third within the window -> refused
    clock["t"] = 1000 + 61        # advance past the window
    safety.enforce_budget()       # old usage pruned -> allowed again
