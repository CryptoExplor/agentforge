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
    # Operator-approved reviewers are privileged across this instance's private tasks.
    # Self-declared capabilities never grant this authority. Empty means deny all.
    trusted_validator_dids: frozenset[str] = frozenset(
        item.strip() for item in os.getenv("AGENTFORGE_TRUSTED_VALIDATOR_DIDS", "").split(",")
        if item.strip()
    )
    # Operator registry self-registration (Grok roadmap 1.1). When true, agents that
    # declare a validation capability are self-granted the "validator" registry role,
    # which keeps local development suites working without an explicit operator grant.
    # Production defaults to false: validation submissions then require an explicit
    # row in the operator_role_grants table, and startup refuses OPEN_OPERATORS=true.
    open_operators: bool = os.getenv(
        "OPEN_OPERATORS",
        "false" if environment == "production" else "true",
    ).lower() in {"1", "true", "yes"}
    # Marketplace service-fee cap in basis points of the released amount
    # (500 bps = 5%). Task creation rejects any declared fee above this cap;
    # refunds and slashes never carry a fee regardless of the cap.
    max_service_fee_bps: int = int(os.getenv("AGENTFORGE_MAX_SERVICE_FEE_BPS", "500"))
    registration_open: bool = os.getenv(
        "AGENTFORGE_REGISTRATION_OPEN", "false" if environment == "production" else "true"
    ).lower() in {"1", "true", "yes"}
    request_global_per_minute: int = int(os.getenv("AGENTFORGE_REQUEST_GLOBAL_PER_MINUTE", "6000"))
    request_ip_per_minute: int = int(os.getenv("AGENTFORGE_REQUEST_IP_PER_MINUTE", "600"))
    registration_global_per_minute: int = int(os.getenv("AGENTFORGE_REGISTRATION_GLOBAL_PER_MINUTE", "120"))
    registration_ip_per_minute: int = int(os.getenv("AGENTFORGE_REGISTRATION_IP_PER_MINUTE", "30"))
    request_did_per_minute: int = int(os.getenv("AGENTFORGE_REQUEST_DID_PER_MINUTE", "300"))
    max_inflight_requests: int = int(os.getenv("AGENTFORGE_MAX_INFLIGHT_REQUESTS", "32"))
    body_timeout_seconds: float = float(os.getenv("AGENTFORGE_BODY_TIMEOUT_SECONDS", "10"))
    challenge_ttl_seconds: int = int(os.getenv("AGENTFORGE_CHALLENGE_TTL_SECONDS", "300"))
    request_clock_skew_seconds: int = int(os.getenv("AGENTFORGE_REQUEST_CLOCK_SKEW_SECONDS", "300"))
    # Secure default: hosted deployments should explicitly opt into the local-only faucet.
    enable_mock_faucet: bool = os.getenv("AGENTFORGE_ENABLE_MOCK_FAUCET", "false").lower() in {"1", "true", "yes"}
    technocore_base_url: str = os.getenv("AGENTFORGE_TECHNOCORE_BASE_URL", "https://technocore.chat").rstrip("/")
    server_name: str = os.getenv("AGENTFORGE_SERVER_NAME", "AgentForge Reference Exchange")
    # Settlement and deployment mode are server-derived, not client-selected.
    # MVP only supports mock provider with local test assets.
    settlement_provider: str = os.getenv("AGENTFORGE_SETTLEMENT_PROVIDER", "mock").lower()
    deployment_mode: str = os.getenv("AGENTFORGE_DEPLOYMENT_MODE", "local").lower()
    # Explicit allow-list for local mock ledger; FLOP and other real assets are rejected.
    allowed_mock_assets: tuple[str, ...] = ("MOCK", "TEST_CREDIT")
    # Signed gossip outbox. Publishing outside this instance is opt-in and needs
    # an explicit operator-supplied publish path: the MVP never invents a remote
    # endpoint, payload contract, or receipt format.
    gossip_enabled: bool = os.getenv("AGENTFORGE_GOSSIP_ENABLED", "false").lower() in {"1", "true", "yes"}
    technocore_publish_path: str = os.getenv("AGENTFORGE_TECHNOCORE_PUBLISH_PATH", "").strip()
    # Publisher identity for signed event envelopes. This is a publisher key, not
    # an identity root: it never authenticates agent requests.
    event_publisher_id: str = os.getenv("AGENTFORGE_EVENT_PUBLISHER_ID", "agentforge-reference-server")
    # 32-byte Ed25519 seed (hex or base64url) from the deployment secret manager.
    # Never commit a real value; production refuses an ephemeral key.
    event_signing_key: str = os.getenv("AGENTFORGE_EVENT_SIGNING_KEY", "")
    outbox_interval_seconds: float = float(os.getenv("AGENTFORGE_OUTBOX_INTERVAL_SECONDS", "5"))
    outbox_jitter_seconds: float = float(os.getenv("AGENTFORGE_OUTBOX_JITTER_SECONDS", "0.5"))


settings = Settings()
