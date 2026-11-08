import pytest


@pytest.fixture(autouse=True)
def no_real_processes(monkeypatch):
    """Guarantee tests can never start ssh, ping or poweroff."""

    def refuse(argv, timeout):
        raise AssertionError(f"test tried to run a real process: {argv}")

    monkeypatch.setattr("app.shutdown._spawn", refuse)
