"""Small local outbox worker entry point."""

from __future__ import annotations

import os
import time

from .adapters.technocore import TechnocoreAdapter
from .db import SessionLocal, init_db
from .outbox import drain_once
from .services import reap_expired_claims


def main() -> None:
    init_db()
    adapter = TechnocoreAdapter(os.getenv("AGENTFORGE_TECHNOCORE_BASE_URL", "https://technocore.chat"))
    interval = float(os.getenv("AGENTFORGE_OUTBOX_INTERVAL_SECONDS", "5"))
    while True:
        with SessionLocal() as db:
            reap_expired_claims(db)
            drain_once(db, adapter)
        time.sleep(interval)


if __name__ == "__main__":
    main()
