"""Entry point: ``python -m claude_proxy``."""

from __future__ import annotations

import asyncio

from .app import main

if __name__ == "__main__":
    asyncio.run(main())
