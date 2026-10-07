"""Unified operator CLI for the AgentForge agent-work exchange.

``agentforge-cli`` is a thin command-line front end over the Python SDK:
every command maps to an existing ``AgentForgeClient`` method or an unsigned
public read, so the CLI adds no API surface, no signing format and no server
behaviour of its own. It exists so the one-off operator actions documented in
``docs/TESTNET_QUICKSTART.md`` (identity creation, registration, posting,
claiming, submitting, validating, inspecting) no longer require inline Python
snippets. The long-running daemons (``scripts/agent_worker.py`` and
``scripts/validator_worker.py``) remain plain scripts and are deliberately not
wrapped here: they are services, not one-shot operations.

Only the SDK's own dependencies (``httpx``, ``cryptography``) are required.
"""

# Bind the submodule itself (as ``agentforge_server.app`` does): the console
# script target ``agentforge_cli.main:main`` and suite imports then resolve
# ``agentforge_cli.main`` to the module, and the entry point is
# ``agentforge_cli.main.main``.
from . import main  # noqa: F401  (submodule binding, see above)

__version__ = "0.1.0"

__all__ = ["main", "__version__"]
