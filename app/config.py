"""
Configuration — loads all settings from environment variables.
Uses pydantic-settings for validation and defaults.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # AiSensy API key (required for sending replies)
    AISENSY_API_KEY: str = ""

    # Optional: AiSensy webhook signing secret for HMAC verification.
    # If empty/unset, webhook signature verification is skipped.
    AISENSY_WEBHOOK_SECRET: str = ""

    # Groq API key (required for LLM classification fallback)
    GROQ_API_KEY: str = ""

    # Fuzzy match similarity threshold (0-100).
    # Higher = stricter matching, fewer false positives.
    # Lower = more tolerant of typos, but more risk of wrong matches.
    FUZZY_THRESHOLD: int = 80

    # Maximum message length to process (characters).
    # Messages longer than this are rejected early to prevent abuse.
    MAX_MESSAGE_LENGTH: int = 500

    # Message dedup TTL in seconds.
    # Duplicate messages within this window are silently ignored.
    DEDUP_TTL_SECONDS: int = 300

    # Maximum entries in the dedup cache before LRU eviction.
    DEDUP_MAX_SIZE: int = 10000

    # --- Dashboard / Supabase (all optional — analytics + catalog upload
    # features no-op gracefully when unset, same as GROQ_API_KEY does) ---

    # Supabase project URL, e.g. https://xxxxxxxx.supabase.co
    SUPABASE_URL: str = ""

    # Supabase anon/public key (used only to verify dashboard login tokens).
    SUPABASE_ANON_KEY: str = ""

    # Supabase service_role key (server-side only — full DB/storage access).
    SUPABASE_SERVICE_ROLE_KEY: str = ""

    # Email address allowed to log into /dashboard and /api/admin/*.
    OWNER_EMAIL: str = ""

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": True,
    }


# Singleton instance
settings = Settings()
