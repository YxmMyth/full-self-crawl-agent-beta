"""request_human_assist — escape hatch for autonomous agent stuck states.

Thin wrapper: dispatches to ctx.human_assist.request() and shapes the result
into a model-facing tool_result.

When to call (agent's decision):
  - Login pages / OAuth flows
  - CAPTCHA / Turnstile / FunCaptcha
  - 2FA / SMS / email verification codes
  - Device verification / "new device" challenges
  - Genuinely stuck states the agent can't resolve

When NOT to call (agent should figure out):
  - Pages still loading (wait first)
  - 404s / wrong URL (try alternative)
  - SPA-rendered content (scroll / interact)
  - Logged-in state verification (use browse + read_network)

The tool blocks until the human signals completion. After it returns, the
browser page state may have changed — agent should call `browse` to re-observe
before deciding the next action.
"""

from __future__ import annotations

from typing import Any

TOOL_NAME = "request_human_assist"
TOOL_DESCRIPTION = (
    "Pause execution and request human intervention via the browser window. "
    "Use this when you encounter a state you cannot resolve autonomously: "
    "login pages, CAPTCHAs, 2FA codes, email verification, device verification, "
    "or any blocking challenge requiring real-person input.\n\n"
    "The browser window will be brought to the foreground. The human will "
    "complete the requested action in that window directly. When they signal "
    "completion, you receive a status and resume.\n\n"
    "After this tool returns:\n"
    "  - The page state is likely changed — call `browse` to re-observe before "
    "the next action.\n"
    "  - Verify outcome with normal tools (browse / read_network) — this tool "
    "does NOT auto-confirm 'login successful'. You judge from the new page state.\n\n"
    "Do not attempt to fill login forms or solve CAPTCHAs yourself. Use this tool. "
    "Be specific in the reason field — describe exactly what the human should do."
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "reason": {
            "type": "string",
            "description": (
                "Free-text description of what's needed. Be specific so the human "
                "knows exactly what to do without guessing.\n"
                "Good examples:\n"
                "  - '请用 OAuth/账号密码登录这个站点,完成后回复'\n"
                "  - '页面弹出 Cloudflare Turnstile 拼图,请点击通过'\n"
                "  - '需要邮箱验证码,请去收件箱拿 6 位数填进去'\n"
                "Bad examples:\n"
                "  - 'help' (太模糊)\n"
                "  - 'login' (没说怎么登)"
            ),
        },
    },
    "required": ["reason"],
}


async def handle(ctx: Any, **kwargs: Any) -> dict:
    reason = kwargs.get("reason", "").strip()
    if not reason:
        return {
            "status": "error",
            "message": "reason is required and must be a non-empty string",
        }

    gateway = getattr(ctx, "human_assist", None)
    if gateway is None:
        return {
            "status": "error",
            "message": (
                "human_assist gateway not configured in ToolContext. "
                "This is a runtime setup bug — report to the system."
            ),
        }

    response = await gateway.request(reason=reason, page=ctx.page)

    # Model-facing result: structured + a hint pushing agent to re-observe
    return {
        "status": response.status,
        "human_message": response.message,
        "next_step_hint": (
            "Human assistance returned. The browser page state may have changed. "
            "Call `browse` to re-observe the current page before your next action. "
            "Do not assume the requested action succeeded — verify via the page content."
        ),
    }
