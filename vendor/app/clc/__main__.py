from __future__ import annotations

import uvicorn

from .config import get_settings


if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(
        "clc.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        # Browsers and installed PWAs can keep event WebSockets open while
        # suspended in the background. Bound the graceful wait so a routine
        # service update cannot be mistaken for a hung control plane and
        # trigger the host watchdog quarantine.
        timeout_graceful_shutdown=8,
    )
