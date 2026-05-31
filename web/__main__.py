"""`python -m web` — run the Helmsman console with uvicorn.

Single worker: the RunSupervisor + EventBus are in-process singletons; multiple
workers would each spawn their own and fight over the one serial run. Bound to
127.0.0.1 (the SSH tunnel is the auth boundary — see web/config.py).
"""

from __future__ import annotations

import uvicorn

from web.config import WebConfig

if __name__ == "__main__":
    uvicorn.run(
        "web.app:app",
        host=WebConfig.HOST,
        port=WebConfig.PORT,
        workers=1,
        log_level="info",
    )
