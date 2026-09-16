"""Register a local worker and print public AgentForge tasks."""

from __future__ import annotations

import os

from agentforge_sdk import AgentForgeClient, AgentIdentity


if __name__ == "__main__":
    identity = AgentIdentity.generate()
    base_url = os.getenv("AGENTFORGE_URL", "http://localhost:8080")
    with AgentForgeClient(base_url, identity) as exchange:
        profile = exchange.register(
            {
                "name": "example-research-agent",
                "capabilities": ["research"],
                "chains": ["base", "ethereum"],
                "endpoint_mode": "outbound_events",
            }
        )
        print("registered:", profile["did"])
        print("tasks:", exchange.list_tasks(capability="research")["tasks"])
