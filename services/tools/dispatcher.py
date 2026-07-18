"""
Tool dispatch — maps a tool name + args to its implementation, with structured
error handling so a failed tool call yields a graceful result the LLM can speak
('I couldn't find that task') rather than crashing the voice pipeline.
"""

import logging

from .task_tools import create_task, query_tasks, update_task
from .research_tools import research
from .profile_tools import update_profile

logger = logging.getLogger(__name__)

TOOL_REGISTRY = {
    "create_task": create_task,
    "query_tasks": query_tasks,
    "update_task": update_task,
    "research": research,
    "update_profile": update_profile,
}


async def execute_tool(name: str, args: dict, session_context: dict) -> dict:
    """Execute a registered tool. Always returns a JSON-able dict."""
    fn = TOOL_REGISTRY.get(name)
    if fn is None:
        logger.warning("Unknown tool requested: %s", name)
        return {"ok": False, "error": "unknown_tool", "summary": f"Unknown tool '{name}'."}
    try:
        return await fn(args or {}, session_context)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Tool '%s' raised", name)
        return {"ok": False, "error": str(exc), "summary": "Something went wrong running that."}
