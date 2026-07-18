"""
Research service (Milestone 3).

A plain high-intelligence LLM call under the hood — no third-party search vendor.
We point a Claude model at OpenRouter with the `:online` suffix, which gives it
Anthropic's native web search, and steer it with a research system prompt to
return concrete findings. Kept as a SEPARATE OpenRouter call from the main
tool-calling model, so web-search latency/cost only applies when research runs.

Returns a normalized, domain-agnostic contract:
    {
        "summary": str,                          # concise factual findings
        "links":   [{"label": str, "url": str}], # source citations
        "source_count": int,
    }
"""

import logging
from datetime import datetime, timezone

from openai import AsyncOpenAI

from core.config import settings

logger = logging.getLogger(__name__)

_RESEARCH_SYSTEM_PROMPT_BASE = (
    "You are a research assistant with live web access. Answer the query factually "
    "and concisely using current information. Focus on concrete, actionable facts: "
    "key dates and deadlines, required steps, costs, and official links. Reply in "
    "3-6 sentences of PLAIN PROSE — no markdown (no **bold**, no numbered/bulleted "
    "lists, no inline [text](url) links). This text is shown directly in a UI card; "
    "source links are extracted and shown separately, so don't repeat them inline. "
    "When a concrete date is known, state it explicitly (e.g. 'registration closes "
    "on 15 July 2026'). Do not speculate — if something is uncertain or you "
    "couldn't verify it, say so plainly."
)


def _system_prompt() -> str:
    # Computed per-call (not at import time) so it never goes stale on a
    # long-running process, and so the model anchors recurring-event queries
    # (e.g. "the December JLPT") to the real current date instead of whatever
    # date its training data made salient.
    today = datetime.now(timezone.utc).strftime("%A, %d %B %Y")
    return (
        f"Today's date is {today}. Resolve any relative or recurring-event date "
        "reference against THIS date, not against your training data — your "
        "training data is stale and will bias you toward a past occurrence of "
        "the event.\n\n" + _RESEARCH_SYSTEM_PROMPT_BASE
    )


def _extract_links(resp, msg) -> list[dict]:
    """Pull source URLs from OpenRouter-standardized annotations, falling back to
    Perplexity-style top-level `citations`."""
    links: list[dict] = []
    seen: set[str] = set()

    def _add(url: str | None, label: str | None) -> None:
        if url and url not in seen:
            seen.add(url)
            links.append({"label": label or url, "url": url})

    # OpenRouter standardizes citations as message.annotations[].url_citation
    for a in (getattr(msg, "annotations", None) or []):
        if isinstance(a, dict):
            uc = a.get("url_citation") or {}
            _add(uc.get("url"), uc.get("title"))
        else:
            uc = getattr(a, "url_citation", None)
            if uc is not None:
                _add(getattr(uc, "url", None), getattr(uc, "title", None))

    # Fallback: some providers return a top-level `citations` list.
    if not links:
        try:
            dump = resp.model_dump()
        except Exception:  # noqa: BLE001
            dump = {}
        for c in (dump.get("citations") or []):
            if isinstance(c, str):
                _add(c, None)
            elif isinstance(c, dict):
                _add(c.get("url"), c.get("title"))

    return links


class ResearchService:
    """Web-search-backed research via an OpenRouter native-search model."""

    def __init__(self) -> None:
        self.client = AsyncOpenAI(
            api_key=settings.OPENROUTER_API_KEY,
            base_url=settings.OPENROUTER_BASE_URL,
        )
        self.model = settings.OPENROUTER_RESEARCH_MODEL
        logger.info("Research service initialised  model=%s", self.model)

    async def research(self, query: str) -> dict:
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": _system_prompt()},
                {"role": "user", "content": query},
            ],
            temperature=0.2,
        )
        msg = resp.choices[0].message
        summary = (msg.content or "").strip()
        links = _extract_links(resp, msg)
        logger.info("research '%.60s' → %d sources", query, len(links))
        return {"summary": summary, "links": links, "source_count": len(links)}
