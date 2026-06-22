"""
Profile tool — lets the model persist stable personal details (location,
timezone) so future turns are personalized without the user repeating them.

Thin adapter: opens a session, calls `profile_service`, and refreshes the
in-memory session context so the detail is usable for the rest of THIS
conversation, not just future ones.
"""

import logging

from db.session import async_session
import services.memory.profile_service as profile_service

logger = logging.getLogger(__name__)


async def update_profile(args: dict, session_context: dict) -> dict:
    user_id = session_context["user_id"]
    location = (args.get("location") or "").strip() or None
    timezone = (args.get("timezone") or "").strip() or None

    if not location and not timezone:
        return {"ok": False, "summary": "Nothing to update."}

    async with async_session() as session:
        profile = await profile_service.set_profile_details(
            session, user_id, location=location, timezone=timezone
        )
        # Make it effective immediately for the rest of this session.
        session_context["profile"] = profile.to_context()
        await session.commit()

    saved = ", ".join(p for p in (location, timezone) if p)
    return {"ok": True, "summary": f"Saved ({saved})."}
