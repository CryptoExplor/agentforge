"""Fail-closed provider resolution, including already initialized singletons.

These test AgentForge's mock-only boundary, not the external Activity Engine's
credential registry, execution modes, or vLLM endpoint classifier.
"""
import pytest

from agentforge_server import settlement
from agentforge_server.adapters.mock_settlement import MockSettlementProvider
from agentforge_server.providers import get_provider, ProviderUnavailable
from agentforge_server.settings import settings


@pytest.fixture
def isolated_provider(monkeypatch):
    monkeypatch.setattr(settlement, "_provider", None)
    monkeypatch.setattr(settings, "settlement_provider", "mock")


@pytest.mark.parametrize("warm_cache", [False, True])
@pytest.mark.parametrize("configured", ["unsupported-rail", "", "   ", None])
def test_invalid_configuration_fails_even_with_cached_provider(isolated_provider, monkeypatch, warm_cache, configured):
    if warm_cache:
        assert isinstance(settlement.get_settlement_provider(), MockSettlementProvider)
    monkeypatch.setattr(settings, "settlement_provider", configured)
    with pytest.raises(RuntimeError, match="not enabled in MVP"):
        settlement.get_settlement_provider()


def test_explicit_override_does_not_bypass_configuration(isolated_provider, monkeypatch):
    settlement.set_settlement_provider(MockSettlementProvider())
    monkeypatch.setattr(settings, "settlement_provider", "unsupported-rail")
    with pytest.raises(RuntimeError, match="not enabled in MVP"):
        settlement.get_settlement_provider()


def test_supported_aliases_preserve_cached_instance(isolated_provider, monkeypatch):
    initial = settlement.get_settlement_provider()
    for alias in ("MOCK", "local", "LOCAL", "mock"):
        monkeypatch.setattr(settings, "settlement_provider", alias)
        assert settlement.get_settlement_provider() is initial


def test_valid_override_and_reset_remain_supported(isolated_provider):
    injected = MockSettlementProvider()
    settlement.set_settlement_provider(injected)
    assert settlement.get_settlement_provider() is injected
    settlement.reset_settlement_provider()
    resolved = settlement.get_settlement_provider()
    assert isinstance(resolved, MockSettlementProvider)
    assert resolved is not injected


@pytest.mark.parametrize("name", ["nvidia", "openrouter", "flop", "vllm", "unknown"])
def test_inference_mock_cache_never_masks_unsupported_name(name):
    assert get_provider("local") is get_provider("mock")
    with pytest.raises(ProviderUnavailable):
        get_provider(name)
