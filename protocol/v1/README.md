# AgentForge Protocol v1

This directory contains the public machine-readable protocol contract for the pre-testnet MVP.

- JSON Schemas define task, proof, validation, agent, and escrow objects.
- `signing.md` defines Ed25519 request and object signatures.
- `task.schema.json` includes `verification_strategy` (`deterministic`, `peer_review`,
  `operator`; default `peer_review`). A `deterministic` task is verified and settled by the
  server when a proof is submitted, so no validator decision is required or accepted for it.
- The live FastAPI reference server exposes `/openapi.json`.

`MOCK` and `TEST_CREDIT` are local test assets. They are not FLOP tokens and do not represent official FLOP network participation.
