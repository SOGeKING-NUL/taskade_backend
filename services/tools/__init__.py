"""Tool registry: schemas (LLM-facing declarations) + dispatcher (execution)."""

from .schemas import ESCALATE_TOOL, get_tool_declarations
from .dispatcher import TOOL_REGISTRY, execute_tool

__all__ = ["ESCALATE_TOOL", "get_tool_declarations", "TOOL_REGISTRY", "execute_tool"]
