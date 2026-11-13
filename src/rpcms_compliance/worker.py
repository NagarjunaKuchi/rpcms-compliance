"""Run: rpcms-compliance-worker   (forwards queued AMP / NIMES submissions with retry)."""

from __future__ import annotations

import logging
import time

from .api import load_clients, make_session_factory
from .config import Settings
from .service import process_due

log = logging.getLogger("rpcms.compliance.worker")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    sf, clients = make_session_factory(settings), load_clients(settings)
    while True:
        try:
            with sf() as s:
                counts = process_due(s, settings, clients)
            if counts:
                log.info("processed: %s", counts)
                continue
        except Exception:
            log.exception("worker cycle failed")
        time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    main()
