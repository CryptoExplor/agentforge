from __future__ import annotations

import os


class Settings:
    environment: str = os.getenv("AGENTFORGE_ENV", "development").lower()
    database_url: str = os.getenv("AGENTFORGE_DATABASE_URL", "sqlite:///./agentforge.db")
    # Development/test convenience only. Production must run Alembic first.
    auto_create_schema: bool = os.getenv(
        "AGENTFORGE_AUTO_CREATE_SCHEMA",
        "false" if environment == "production" else "true",
    ).lower() in {"1", "true", "yes"}
    challenge_ttl_seconds: int = int(os.getenv("AGENTFORGE_CHALLENGE_TTL_SECONDS", "300"))
    request_clock_skew_seconds: int = int(os.getenv("AGENTFORGE_REQUEST_CLOCK_SKEW_SECONDS", "300"))
    # Secure default: hosted deployments should explicitly opt into the local-only faucet.
    enable_mock_faucet: bool = os.getenv("AGENTFORGE_ENABLE_MOCK_FAUCET", "false").lower() in {"1", "true", "yes"}
    technocore_base_url: str = os.getenv("AGENTFORGE_TECHNOCORE_BASE_URL", "https://technocore.chat").rstrip("/")
    server_name: str = os.getenv("AGENTFORGE_SERVER_NAME", "AgentForge Reference Exchange")


settings = Settings()
