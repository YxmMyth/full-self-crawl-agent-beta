"""One-shot launcher: codepen.io recon + harvest (three.js pens).

Requirement is held as a UTF-8 string literal here to avoid Windows shell
arg-encoding issues with Chinese text. Run:

    .venv/Scripts/python.exe scripts/run_codepen.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.main import run_auto  # noqa: E402
from src.utils.logging import setup  # noqa: E402

REQUIREMENT = (
    "采集 codepen.io 上 three.js 相关的 pen 50 个，每个含："
    "标题 / 作者 / pen 链接 + HTML / CSS / JS 源码 + 标签 (tags)。"
)

if __name__ == "__main__":
    setup(level="INFO")
    asyncio.run(run_auto("codepen.io", REQUIREMENT, no_gate=True))
