"""The standalone simulation drives a real TCP-served app through the SDK."""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import uvicorn

from agentforge_server import db
from agentforge_server.app import create_app
from agentforge_server.settings import settings

# pytest's configured import roots contain server/ and sdk/python/, not the
# repository root where standalone operator scripts live.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.simulate_marketplace import run_simulation  # noqa: E402


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_simulation_end_to_end_against_live_uvicorn(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "auto_create_schema", True)
    monkeypatch.setattr(settings, "registration_open", True)
    monkeypatch.setattr(settings, "enable_mock_faucet", True)
    monkeypatch.setattr(settings, "open_operators", True)
    monkeypatch.setattr(settings, "trusted_validator_dids", frozenset())
    db.configure_database(f"sqlite:///{tmp_path / 'simulation.db'}")

    port = unused_port()
    config = uvicorn.Config(
        create_app(), host="127.0.0.1", port=port, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{base_url}/health", timeout=0.5).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.05)
        else:
            raise AssertionError("uvicorn did not become healthy")

        result = run_simulation(base_url, tmp_path / "identities", timeout=5)
        assert result["ok"] is True
        assert result["strategy"] == "deterministic"
        assert result["escrow"]["platform_fee_amount"] == "0.5"
        assert result["escrow"]["released_amount"] == "9.5"
        assert result["balances"]["poster"] == {"before": "1000", "after": "990"}
        assert result["balances"]["executor"] == {"before": "1000", "after": "1009.5"}
        assert result["executor_reputation"]["overall"] == 1.0
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()
