"""OpenAI-compatible LLM client — unified interface for all LLM calls."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI, APIStatusError, APITimeoutError, APIConnectionError

from src.config import Config
from src.utils.logging import get_logger

logger = get_logger(__name__)


# ── Response types ───────────────────────────────────────


@dataclass
class ToolCall:
    """A single tool call from the LLM."""
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    """Structured response from chat_with_tools."""
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"  # stop / tool_calls / length
    reasoning: str | None = None  # deepseek reasoning_content if present
    usage: TokenUsage | None = None

    def to_assistant_message(self) -> dict[str, Any]:
        """Build an OpenAI-format assistant message from this response,
        suitable for appending to the messages array of the next API call.

        Echoes back reasoning_content. DeepSeek thinking-mode models (e.g.
        deepseek-v4-pro) REQUIRE the prior turn's reasoning_content to be
        present AND non-empty on every assistant message in the history —
        if the key is missing OR the value is "", the API returns HTTP 400:
          "The `reasoning_content` in the thinking mode must be passed
           back to the API."
        So we always emit the key, with a placeholder when the model didn't
        return one. Non-thinking models ignore the field harmlessly.

        Empirically validated against the gateway:
          missing key       → 400
          value == ""       → 400
          value with spaces → OK (server doesn't introspect content)

        Returns a dict with role + populated fields. Caller is responsible
        for ensuring the message has at least content or tool_calls (some
        providers reject pure-empty assistant messages).
        """
        msg: dict[str, Any] = {"role": "assistant"}
        if self.content:
            msg["content"] = self.content
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in self.tool_calls
            ]
        # Always emit reasoning_content — required-non-empty by thinking-mode APIs.
        msg["reasoning_content"] = self.reasoning if self.reasoning else "(no thinking captured)"
        return msg


@dataclass
class TokenUsage:
    """Token counts for a single LLM call."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


# ── Cumulative usage tracker ─────────────────────────────


class UsageTracker:
    """Accumulates token usage across multiple LLM calls."""

    def __init__(self) -> None:
        self.total_prompt: int = 0
        self.total_completion: int = 0
        self.total_tokens: int = 0
        self.call_count: int = 0

    def record(self, usage: TokenUsage | None) -> None:
        if usage is None:
            return
        self.total_prompt += usage.prompt_tokens
        self.total_completion += usage.completion_tokens
        self.total_tokens += usage.total_tokens
        self.call_count += 1

    def summary(self) -> dict[str, int]:
        return {
            "calls": self.call_count,
            "prompt_tokens": self.total_prompt,
            "completion_tokens": self.total_completion,
            "total_tokens": self.total_tokens,
        }


# ── LLM Client ──────────────────────────────────────────


class LLMClient:
    """Async LLM client wrapping OpenAI-compatible API.

    Three call modes:
    - chat_with_tools: Agent tool-use loops (returns LLMResponse with tool_calls)
    - generate: Single text generation (maintain_model, summarize)
    - describe_image: Vision model call (browse visual mode)
    """

    def __init__(self) -> None:
        Config.require("LLM_API_KEY", "LLM_BASE_URL")
        self._client = AsyncOpenAI(
            api_key=Config.LLM_API_KEY,
            base_url=Config.LLM_BASE_URL,
            timeout=120.0,
            max_retries=2,  # SDK-level retries for transient network errors
        )
        self.usage = UsageTracker()

    # ── chat_with_tools ──────────────────────────────────

    async def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str | None = None,
    ) -> LLMResponse | None:
        """Call LLM with tool definitions. Returns LLMResponse or None on empty.

        Args:
            messages: OpenAI-format message array.
            tools: OpenAI-format tool definitions.
            model: Override model name (defaults to Config.LLM_MODEL).

        Returns:
            LLMResponse with content, tool_calls, finish_reason.
            None if LLM returns empty after retries.
        """
        model = model or Config.LLM_MODEL

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools

        raw = await self._call_with_retry(kwargs)
        if raw is None:
            return None

        return self._parse_response(raw)

    # ── generate ─────────────────────────────────────────

    async def generate(
        self,
        prompt: str,
        system: str | None = None,
        model: str | None = None,
    ) -> str | None:
        """Single text generation. Returns text or None on empty.

        Used for maintain_model, summarize_session, and other LLM functions.
        """
        model = model or Config.LLM_MODEL
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        raw = await self._call_with_retry({"model": model, "messages": messages})
        if raw is None:
            return None

        choice = raw.choices[0]
        return choice.message.content or None

    # ── describe_image ───────────────────────────────────

    async def describe_image(
        self,
        image_base64: str,
        prompt: str = "Describe what you see on this webpage. Focus on layout, interactive elements, and data content.",
    ) -> str | None:
        """Vision model call for browse visual mode.

        Uses VISION_LLM_MODEL (kimi-k2.5) to describe a screenshot.
        """
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_base64}",
                        },
                    },
                ],
            }
        ]

        raw = await self._call_with_retry({
            "model": Config.VISION_LLM_MODEL,
            "messages": messages,
        })
        if raw is None:
            return None

        return raw.choices[0].message.content or None

    # ── Retry logic ──────────────────────────────────────

    async def _call_with_retry(
        self, kwargs: dict[str, Any], max_retries: int = 3
    ) -> Any | None:
        """Call the API with content_filter retry logic.

        - content_filter errors: retry up to max_retries times, sleep 2s each
        - Network timeout / connection: handled by SDK (max_retries=2)
        - Empty response (no choices): return None
        """
        last_error: Exception | None = None

        for attempt in range(max_retries):
            try:
                t0 = time.monotonic()
                raw = await self._client.chat.completions.create(**kwargs)
                elapsed = int((time.monotonic() - t0) * 1000)

                # Track usage
                usage = self._extract_usage(raw)
                self.usage.record(usage)

                logger.debug(
                    "LLM call",
                    extra={
                        "tool": kwargs.get("model"),
                        "duration_ms": elapsed,
                        "tokens": usage.total_tokens if usage else 0,
                    },
                )

                # Empty response check
                if not raw.choices:
                    logger.warning("LLM returned no choices")
                    return None

                return raw

            except APIStatusError as e:
                last_error = e
                # Only retry on actual content_filter / moderation errors. A
                # bare 400 is usually a schema problem (bad messages, missing
                # required field, etc.) — retrying with the same payload just
                # wastes 3 attempts before raising the real error. The earlier
                # "any 400 → retry" rule masked a reasoning_content schema bug
                # for hours before we noticed.
                err_str = str(e).lower()
                is_content_filter = (
                    "content_filter" in err_str
                    or "content filter" in err_str
                    or "moderation" in err_str
                )
                if is_content_filter:
                    logger.warning(
                        f"Content filter hit (attempt {attempt + 1}/{max_retries}), retrying in 2s",
                        extra={"error": str(e)},
                    )
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2)
                        continue
                # Other API errors — don't retry, surface immediately
                logger.error(f"LLM API error: {e}", extra={"error": str(e)})
                raise

            except (APITimeoutError, APIConnectionError) as e:
                # SDK handles its own retries, if we get here all retries failed
                logger.error(f"LLM connection failed: {e}", extra={"error": str(e)})
                raise

        # All content_filter retries exhausted
        logger.error(f"All {max_retries} retries exhausted: {last_error}")
        return None

    # ── Response parsing ─────────────────────────────────

    def _parse_response(self, raw: Any) -> LLMResponse:
        """Parse raw OpenAI response into LLMResponse."""
        choice = raw.choices[0]
        msg = choice.message

        # Parse tool calls
        tool_calls: list[ToolCall] = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, TypeError):
                    args = {"_raw": tc.function.arguments}
                    logger.warning(
                        f"Failed to parse tool_call args for {tc.function.name}",
                        extra={"tool": tc.function.name, "error": "json_decode"},
                    )
                tool_calls.append(ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=args,
                ))

        # Extract reasoning_content if present (deepseek-chat may include it)
        reasoning = getattr(msg, "reasoning_content", None)

        return LLMResponse(
            content=msg.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            reasoning=reasoning,
            usage=self._extract_usage(raw),
        )

    @staticmethod
    def _extract_usage(raw: Any) -> TokenUsage | None:
        """Extract token usage from raw response."""
        if raw.usage is None:
            return None
        return TokenUsage(
            prompt_tokens=raw.usage.prompt_tokens or 0,
            completion_tokens=raw.usage.completion_tokens or 0,
            total_tokens=raw.usage.total_tokens or 0,
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.close()
