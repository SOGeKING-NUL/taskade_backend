"""
Research tool adapter — bridges the LLM tool call to the research service.

The LLM is expected to chain `research` → `create_task` in one turn (reusing the
existing multi-round tool loop): research first, then create the task passing the
findings as `research_summary` and any official URLs as `source_links`.
"""

import logging

from services.research.research_service import ResearchService

logger = logging.getLogger(__name__)

# Stateless, shared — instantiated once.
_service = ResearchService()


async def research(args: dict, session_context: dict) -> dict:
    query = (args.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "missing_query", "summary": "I need something to research."}

    try:
        result = await _service.research(query)
    except Exception as exc:  # noqa: BLE001
        logger.exception("research tool failed")
        return {"ok": False, "error": str(exc), "summary": "I couldn't complete that research."}

    n = result["source_count"]
    return {
        "ok": True,
        # Short line for the UI activity chip:
        "summary": f"Researched the topic — {n} source(s) found.",
        # Full payload the LLM reads back to decide dates/details and populate context:
        "findings": result["summary"] or "No clear findings.",
        "links": result["links"],
        "source_count": n,
    }
