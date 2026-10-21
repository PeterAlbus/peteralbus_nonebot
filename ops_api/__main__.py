from __future__ import annotations

from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from .app import create_app
from .settings import OpsSettings


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".ops-api.env", override=False)
    settings = OpsSettings.from_env(repo_root=root)
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.api_host,
        port=settings.api_port,
        access_log=False,
    )


if __name__ == "__main__":
    main()
