import pytest


@pytest.fixture(autouse=True)
def isolate_hermes_home(monkeypatch, tmp_path):
    """Keep default discovery from ingesting the operator's conversations in tests."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "no-hermes"))
