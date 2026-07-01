# Application configuration — loads environment variables with sensible defaults.

import os
from pathlib import Path
from dotenv import load_dotenv

# core/config.py → core/ → project root
_project_root = Path(__file__).resolve().parent.parent
load_dotenv(_project_root / ".env")

 
class Settings:
    """Typed settings pulled from environment variables."""

    SARVAM_API_KEY: str = os.getenv("SARVAM_API_KEY", "")

    SARVAM_TTS_VOICE: str = os.getenv("SARVAM_TTS_VOICE", "anushka")
    SARVAM_TTS_LANGUAGE: str = os.getenv("SARVAM_TTS_LANGUAGE", "en-IN")
    SARVAM_TTS_MODEL: str = os.getenv("SARVAM_TTS_MODEL", "bulbul:v2")
    SARVAM_TTS_SAMPLE_RATE: int = int(os.getenv("SARVAM_TTS_SAMPLE_RATE", "22050"))

    SARVAM_STT_LANGUAGE: str = os.getenv("SARVAM_STT_LANGUAGE", "unknown")
    SARVAM_STT_MODEL: str = os.getenv("SARVAM_STT_MODEL", "saaras:v3")
    SARVAM_STT_MODE: str = os.getenv("SARVAM_STT_MODE", "codemix")

    # ── Deepgram STT (server-side end-of-turn detection) ──────
    DEEPGRAM_API_KEY: str = os.getenv("DEEPGRAM_API_KEY", "")
    DEEPGRAM_MODEL: str = os.getenv("DEEPGRAM_MODEL", "nova-3")
    DEEPGRAM_LANGUAGE: str = os.getenv("DEEPGRAM_LANGUAGE", "multi")
    DEEPGRAM_ENDPOINTING_MS: int = int(os.getenv("DEEPGRAM_ENDPOINTING_MS", "500"))
    DEEPGRAM_UTTERANCE_END_MS: int = int(os.getenv("DEEPGRAM_UTTERANCE_END_MS", "1500"))

    GOOGLE_API_KEY: str = os.getenv("GOOGLE_API_KEY", "")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "gemini-2.5-flash")  # legacy, unused
    LLM_SYSTEM_PROMPT: str = os.getenv(
        "LLM_SYSTEM_PROMPT",
        "You are a helpful, friendly AI assistant. Keep responses concise "
        "and conversational — aim for 1-3 sentences unless the user asks for detail.",
    )

    # ── Groq SLM (fast conversational path) ──────────────────────
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
    GROQ_BASE_URL: str = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
    SLM_MODEL: str = os.getenv("SLM_MODEL", "llama-3.1-8b-instant")

    # ── OpenRouter LLM (tool-calling / research path) ────────────
    OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
    OPENROUTER_BASE_URL: str = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    # Pick a cheap, capable tool-calling model from OpenRouter's current catalog.
    OPENROUTER_LLM_MODEL: str = os.getenv("OPENROUTER_LLM_MODEL", "openai/gpt-4o-mini")
    # Research model — a high-intelligence LLM that web-searches itself via the
    # `:online` suffix (reuses the key above; no third-party search vendor).
    # A Claude model uses Anthropic's native search.
    OPENROUTER_RESEARCH_MODEL: str = os.getenv(
        "OPENROUTER_RESEARCH_MODEL", "anthropic/claude-sonnet-4.6:online"
    )

    # ── Auth (Auth0 — Google sign-in via Auth0's social connection) ──
    # Frontend (Auth0 React SDK, PKCE) logs the user in; this backend only
    # verifies the resulting ID token against Auth0's public JWKS (RS256).
    # CLIENT_SECRET isn't used by the verification flow (PKCE needs no
    # secret) but is kept here for any future server-side Auth0 API calls.
    AUTH0_DOMAIN: str = os.getenv("AUTH0_DOMAIN", "")
    AUTH0_CLIENT_ID: str = os.getenv("AUTH0_CLIENT_ID", "")
    AUTH0_CLIENT_SECRET: str = os.getenv("AUTH0_CLIENT_SECRET", "")

    # ── Database (Postgres, async) ───────────────────────────────
    # Format: postgresql+asyncpg://USER:PASSWORD@HOST:PORT/DBNAME
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@localhost:5432/taskade",
    )

    # ── Reminder scheduler (Milestone 4) ─────────────────────────
    # How often the read-only due-task detection sweep runs.
    REMINDER_SWEEP_SECONDS: int = int(os.getenv("REMINDER_SWEEP_SECONDS", "60"))

    # ── Profile & memory (Milestone 5) ───────────────────────────
    # How many remembered facts to inject into the LLM context per turn.
    MEMORY_RECALL_LIMIT: int = int(os.getenv("MEMORY_RECALL_LIMIT", "10"))
    # Default local hour for the daily research refresh when a profile sets none.
    DAILY_CHECKIN_HOUR: int = int(os.getenv("DAILY_CHECKIN_HOUR", "6"))

    # ── Timezone ─────────────────────────────────────────────────
    # Default IANA timezone used to interpret user-spoken clock times ("8pm") and
    # to format notification copy. Stored datetimes stay tz-aware UTC in the DB; a
    # per-user `profile.timezone` overrides this default when set.
    DEFAULT_TIMEZONE: str = os.getenv("DEFAULT_TIMEZONE", "Asia/Kolkata")

    # ── Push notifications (FCM) + reminder delivery ─────────────
    # Firebase service-account credentials. Two ways to provide them (either works;
    # JSON takes precedence). When NEITHER is set, the delivery sweep is a safe
    # no-op — nothing else in the app is affected.
    #   • FCM_CREDENTIALS_JSON — the raw JSON content as a single env var. Best for
    #     production/Render: the secret lives in the host's env, never in git or on
    #     disk.
    #   • FCM_CREDENTIALS_FILE — path to the JSON file. Convenient for local dev
    #     (gitignored) or with a platform "secret file" mount.
    FCM_CREDENTIALS_JSON: str = os.getenv("FCM_CREDENTIALS_JSON", "")
    FCM_CREDENTIALS_FILE: str = os.getenv("FCM_CREDENTIALS_FILE", "")
    # Optional explicit project id (firebase-admin reads it from the creds file
    # otherwise; kept for visibility/overrides).
    FCM_PROJECT_ID: str = os.getenv("FCM_PROJECT_ID", "")

    # How often the delivery sweep claims + sends due reminders.
    REMINDER_DELIVERY_SECONDS: int = int(os.getenv("REMINDER_DELIVERY_SECONDS", "30"))
    # Max send attempts before a reminder is marked `failed` (logged, not retried).
    REMINDER_MAX_ATTEMPTS: int = int(os.getenv("REMINDER_MAX_ATTEMPTS", "5"))
    # A reminder stuck in `claimed` longer than this (a crashed send) is reclaimed.
    REMINDER_CLAIM_TIMEOUT_SECONDS: int = int(os.getenv("REMINDER_CLAIM_TIMEOUT_SECONDS", "120"))
    # How many reminders one sweep tick claims at most.
    REMINDER_BATCH_LIMIT: int = int(os.getenv("REMINDER_BATCH_LIMIT", "100"))
    # Default reminder offsets (minutes before due) when the user doesn't specify:
    # one 10 minutes before and one at the event time.
    REMINDER_DEFAULT_OFFSETS: str = os.getenv("REMINDER_DEFAULT_OFFSETS", "0,10")

    # ── Server ───────────────────────────────────────────────────
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))
    CORS_ORIGINS: str = os.getenv("CORS_ORIGINS", "http://localhost:5173")


settings = Settings()
