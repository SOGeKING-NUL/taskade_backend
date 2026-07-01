"""
FCM push sender (Firebase Admin SDK, HTTP v1 under the hood).

Deliberately thin and defensive:

  • If no service-account credentials are configured, `is_configured()` is False
    and nothing here is ever called by the sweep — the rest of the app is
    completely unaffected (reminders simply stay `pending` until creds exist).
  • `firebase-admin` is imported lazily so a missing dependency / unconfigured
    deploy can't break import of the app.
  • The blocking SDK calls run in a worker thread (`asyncio.to_thread`) so the
    async event loop is never stalled.
  • `send_reminder` classifies the per-token outcome into three buckets the
    ledger understands: delivered, prune (token is dead), or transient (retry).

The notification channel id ("task_reminders") must match the channel the
Android client creates.
"""

import asyncio
import json
import logging
import os
import threading
from dataclasses import dataclass, field

from core.config import settings

logger = logging.getLogger(__name__)

ANDROID_CHANNEL_ID = "task_reminders"

_init_lock = threading.Lock()
_initialized = False
_fcm_app = None  # firebase_admin.App


@dataclass
class PushResult:
    """Outcome of one `send_reminder` call across a user's tokens."""
    success_count: int = 0
    prune_tokens: list[str] = field(default_factory=list)  # dead → remove
    transient: bool = False                                 # at least one retryable failure
    error: str = ""

    @property
    def delivered(self) -> bool:
        return self.success_count > 0


def is_configured() -> bool:
    """True when credentials are available via either the JSON env var or a
    readable service-account file."""
    if settings.FCM_CREDENTIALS_JSON.strip():
        return True
    path = settings.FCM_CREDENTIALS_FILE
    return bool(path) and os.path.isfile(path)


def _load_credentials():
    """Build a Firebase Certificate from the JSON env var (preferred) or the file
    path. `credentials.Certificate` accepts either a dict or a path."""
    from firebase_admin import credentials

    raw = settings.FCM_CREDENTIALS_JSON.strip()
    if raw:
        return credentials.Certificate(json.loads(raw))
    return credentials.Certificate(settings.FCM_CREDENTIALS_FILE)


def _ensure_app():
    """Initialise the Firebase app once (thread-safe). Raises if misconfigured —
    callers guard with `is_configured()` first."""
    global _initialized, _fcm_app
    if _initialized:
        return _fcm_app
    with _init_lock:
        if _initialized:
            return _fcm_app
        import firebase_admin

        cred = _load_credentials()
        # Named app so we never collide with any other firebase_admin usage.
        _fcm_app = firebase_admin.initialize_app(cred, name="taskade-fcm")
        _initialized = True
        logger.info("Firebase Admin initialised for FCM push")
        return _fcm_app


def _blocking_send(tokens: list[str], title: str, body: str, data: dict) -> PushResult:
    from firebase_admin import messaging, exceptions as fb_exc

    _ensure_app()

    # DATA-ONLY messages (no `notification` block) so the Android client's
    # onMessageReceived ALWAYS runs — foreground AND background — letting it build
    # the notification itself and acknowledge delivery. The display title/body
    # travel inside the data payload. FCM requires all data values to be strings.
    str_data = {k: ("" if v is None else str(v)) for k, v in (data or {}).items()}
    str_data["title"] = title
    str_data["body"] = body

    messages = [
        messaging.Message(
            token=t,
            data=str_data,
            # High priority so it's delivered promptly even in Doze.
            android=messaging.AndroidConfig(priority="high"),
        )
        for t in tokens
    ]

    result = PushResult()
    try:
        batch = messaging.send_each(messages, app=_fcm_app)
    except Exception as exc:  # noqa: BLE001 — whole batch failed to dispatch (network/auth)
        result.transient = True
        result.error = f"batch_send_failed: {exc}"
        logger.warning("FCM batch send failed: %s", exc)
        return result

    for token, resp in zip(tokens, batch.responses):
        if resp.success:
            result.success_count += 1
            continue
        exc = resp.exception
        # Dead/invalid registration → prune so we stop sending to it.
        if isinstance(exc, (messaging.UnregisteredError, messaging.SenderIdMismatchError)):
            result.prune_tokens.append(token)
        elif isinstance(exc, fb_exc.InvalidArgumentError):
            # Malformed token (not a transient server problem) → prune.
            result.prune_tokens.append(token)
        else:
            # Quota / unavailable / internal / network → retry later.
            result.transient = True
            result.error = str(exc)
    return result


async def send_reminder(
    tokens: list[str], *, title: str, body: str, data: dict | None = None
) -> PushResult:
    """Send one reminder to all of a user's tokens. Never raises — returns a
    `PushResult` the ledger uses to decide sent / prune / retry."""
    if not tokens:
        return PushResult(transient=False, error="no_tokens")
    if not is_configured():
        return PushResult(transient=True, error="fcm_not_configured")
    try:
        return await asyncio.to_thread(_blocking_send, list(tokens), title, body, data or {})
    except Exception as exc:  # noqa: BLE001 — defensive; treat as transient
        logger.warning("FCM send_reminder error: %s", exc)
        return PushResult(transient=True, error=str(exc))
