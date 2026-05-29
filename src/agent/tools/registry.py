"""ToolRegistry — register tools, generate OpenAI-format schemas, dispatch execution.

Each tool is registered with:
  - name: unique identifier (used in LLM tool_calls)
  - description: for LLM tool selection (the "new employee handbook", not API doc)
  - parameters: JSON Schema dict (OpenAI function parameters format)
  - handler: async callable(ctx, **kwargs) -> str | dict

Args validation: borrowed from Claude Code's design (single chokepoint at
dispatch, strict-object semantics — unknown keys rejected, type mismatches
flagged). The validation error is returned to the LLM as a tool_result so
the model can self-correct. Avoids the kwargs-passthrough trap where
quirky model output silently triggers tool side effects.

Codex (Rust) was researched as an alternative reference but found NOT to
schema-validate at dispatch — it falls into the same trap we're escaping.
"""

from __future__ import annotations

from typing import Any, Callable, Awaitable

from jsonschema import Draft7Validator
from jsonschema.exceptions import ValidationError

from src.utils.logging import get_logger

logger = get_logger(__name__)

# Handler signature: async (ctx: ToolContext, **kwargs) -> str | dict
ToolHandler = Callable[..., Awaitable[Any]]


class ToolDef:
    """A registered tool definition."""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: ToolHandler,
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters
        self.handler = handler
        # Pre-compile validator once per tool — Draft7Validator is the
        # standard for OpenAI function parameter schemas.
        self.validator = Draft7Validator(parameters)

    def to_openai_schema(self) -> dict[str, Any]:
        """Generate OpenAI tools format for this tool."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _format_validation_errors(errors: list[ValidationError]) -> str:
    """Build a clear, model-facing error message from JSON Schema failures.

    Returned text becomes the tool_result content the LLM sees, so it must
    explicitly name unknown keys and type mismatches — otherwise the model
    just retries the same broken call.
    """
    lines: list[str] = []
    for err in errors:
        path = ".".join(str(p) for p in err.absolute_path) or "(root)"
        validator = err.validator  # 'required', 'additionalProperties', 'type', ...
        if validator == "additionalProperties":
            # err.message: "Additional properties are not allowed ('/proxy' was unexpected)"
            lines.append(f"unexpected argument: {err.message}")
        elif validator == "required":
            lines.append(f"missing required argument: {err.message}")
        elif validator == "type":
            expected = err.schema.get("type", "?")
            got = type(err.instance).__name__
            lines.append(
                f"argument '{path}' has wrong type: expected {expected}, "
                f"got {got} ({err.instance!r})"
            )
        elif validator == "enum":
            allowed = err.schema.get("enum", [])
            lines.append(
                f"argument '{path}' value {err.instance!r} not in allowed "
                f"values {allowed}"
            )
        else:
            lines.append(f"{path}: {err.message}")
    return "; ".join(lines)


class ToolRegistry:
    """Central registry for all agent tools.

    Usage:
        registry = ToolRegistry()
        registry.register("think", "Reason without side effects", {...}, think_handler)
        schemas = registry.openai_schemas()          # for LLM call
        result = await registry.execute("think", ctx, thought="...")  # dispatch
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolDef] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: ToolHandler,
    ) -> None:
        """Register a tool. Raises if name already registered."""
        if name in self._tools:
            raise ValueError(f"Tool '{name}' already registered")
        self._tools[name] = ToolDef(name, description, parameters, handler)
        logger.debug(f"Registered tool: {name}")

    def get(self, name: str) -> ToolDef | None:
        """Get a tool definition by name."""
        return self._tools.get(name)

    def names(self) -> list[str]:
        """List all registered tool names."""
        return list(self._tools.keys())

    def openai_schemas(self) -> list[dict[str, Any]]:
        """Generate OpenAI-format tool schemas for all registered tools."""
        return [tool.to_openai_schema() for tool in self._tools.values()]

    async def execute(self, tool_name: str, ctx: Any, **kwargs: Any) -> Any:
        """Dispatch a tool call by name.

        Args:
            tool_name: Tool name from LLM tool_call.
            ctx: ToolContext (browser context) or other context object.
            **kwargs: Tool arguments from LLM.

        Returns:
            Tool result (str or dict). On schema validation failure, returns
            a string error so the LLM sees the message in the tool_result and
            can self-correct on the next turn.

        Raises:
            KeyError: Unknown tool name.
        """
        # Defensive: deepseek-v4-pro sometimes adds a spurious `name` key inside
        # tool arguments, which would collide with the positional `tool_name`
        # via **kwargs and raise "got multiple values for argument 'name'".
        kwargs.pop("name", None)

        tool = self._tools.get(tool_name)
        if tool is None:
            raise KeyError(f"Unknown tool: '{tool_name}'. Available: {', '.join(self._tools.keys())}")

        # P0: schema-validate kwargs against the tool's JSON Schema BEFORE
        # invoking the handler. Unknown keys / type mismatches / missing
        # required fields are rejected here, error returned to LLM as
        # tool_result. This blocks the cascade pattern where quirky LLM
        # output (e.g. `{"/proxy": true}` instead of valid args) silently
        # triggers tool side effects with default values.
        errors = sorted(tool.validator.iter_errors(kwargs), key=lambda e: e.path)
        if errors:
            msg = _format_validation_errors(errors)
            logger.warning(
                f"Tool '{tool_name}' args validation failed: {msg}",
                extra={"tool": tool_name, "tool_args": kwargs},
            )
            return (
                f"Error: invalid arguments for {tool_name}: {msg}. "
                f"Fix the arguments and call the tool again."
            )

        try:
            result = await tool.handler(ctx, **kwargs)
            return result
        except TypeError as e:
            # Defense-in-depth: schema should catch most type issues, but
            # handlers can still reject (e.g. wrong arg name from kwargs vs
            # explicit signature). Don't crash — feed back to LLM.
            logger.warning(f"Tool '{tool_name}' argument error: {e}", extra={"tool": tool_name})
            return f"Error: invalid arguments for {tool_name}: {e}"
        except Exception as e:
            logger.error(f"Tool '{tool_name}' execution error: {e}", extra={"tool": tool_name, "error": str(e)})
            raise
