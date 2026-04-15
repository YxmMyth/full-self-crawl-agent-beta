"""ToolRegistry — register tools, generate OpenAI-format schemas, dispatch execution.

Each tool is registered with:
  - name: unique identifier (used in LLM tool_calls)
  - description: for LLM tool selection (the "new employee handbook", not API doc)
  - parameters: JSON Schema dict (OpenAI function parameters format)
  - handler: async callable(ctx, **kwargs) -> str | dict
"""

from __future__ import annotations

from typing import Any, Callable, Awaitable

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

    async def execute(self, name: str, ctx: Any, **kwargs: Any) -> Any:
        """Dispatch a tool call by name.

        Args:
            name: Tool name from LLM tool_call.
            ctx: ToolContext (browser context) or other context object.
            **kwargs: Tool arguments from LLM.

        Returns:
            Tool result (str or dict).

        Raises:
            KeyError: Unknown tool name.
        """
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"Unknown tool: '{name}'. Available: {', '.join(self._tools.keys())}")

        try:
            result = await tool.handler(ctx, **kwargs)
            return result
        except TypeError as e:
            # Bad arguments from LLM — return error instead of crashing
            logger.warning(f"Tool '{name}' argument error: {e}", extra={"tool": name})
            return f"Error: invalid arguments for {name}: {e}"
        except Exception as e:
            logger.error(f"Tool '{name}' execution error: {e}", extra={"tool": name, "error": str(e)})
            raise
