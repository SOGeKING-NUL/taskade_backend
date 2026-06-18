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

    # ── Database (Postgres, async) ───────────────────────────────
    # Format: postgresql+asyncpg://USER:PASSWORD@HOST:PORT/DBNAME
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@localhost:5432/taskade",
    )

    # ── Reminder scheduler (Milestone 4) ─────────────────────────
    # How often the read-only due-task detection sweep runs.
    REMINDER_SWEEP_SECONDS: int = int(os.getenv("REMINDER_SWEEP_SECONDS", "60"))

    # ── Server ───────────────────────────────────────────────────
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))
    CORS_ORIGINS: str = os.getenv("CORS_ORIGINS", "http://localhost:5173")


settings = Settings()
