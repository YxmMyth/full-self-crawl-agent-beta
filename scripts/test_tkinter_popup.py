"""Quick visual test for TkinterPopupGateway.

Pops the assist dialog and prints which button you clicked.
Run this in your own terminal to see what the popup looks like.

Run: python scripts/test_tkinter_popup.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


async def main() -> None:
    from src.utils.logging import setup
    setup(level="INFO")

    from src.runtime.human_assist import TkinterPopupGateway

    gw = TkinterPopupGateway()
    print("\n弹一个测试对话框,你看一下:")
    print("  - 是否永远置顶(切到别的应用还能看到)")
    print("  - 字体 / 排版是否清楚")
    print("  - 完成 / 跳过 按钮位置 OK 吗")
    print("  - 关掉窗口 (X) 应该当作'跳过'处理")
    print()
    print("点击任意按钮(或关闭窗口)后,这里会显示结果。")
    print()

    response = await gw.request(
        reason=(
            "网站 github.com 需要登录\n\n"
            "请切到浏览器窗口完成登录,完成后点下方的"
            "'完成 ✓'。\n\n"
            "如果不想 / 搞不定,点'跳过',agent 会跳过这个 session。"
        ),
        page=None,  # no page in this test
    )

    print(f"\nresult: status={response.status}")
    if response.status == "completed":
        print("✓ 完成路径 OK")
    elif response.status == "cancelled":
        print("✓ 跳过路径 OK")
    else:
        print(f"? unexpected: {response.status}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
