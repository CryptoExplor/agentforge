"""Isolate operator-managed authorization state between tests."""
import pytest
from agentforge_server.settings import settings


@pytest.fixture(autouse=True)
def isolated_validator_policy(monkeypatch):
    monkeypatch.setattr(settings, "trusted_validator_dids", frozenset())
