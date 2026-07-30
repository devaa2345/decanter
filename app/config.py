"""
Configuration — loads all settings from environment variables.
Uses pydantic-settings for validation and defaults.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Chat Mitra API bearer token (Settings -> API Keys in their dashboard).
    # Required for sending replies.
    CHATMITRA_API_TOKEN: str = ""

    # Chat Mitra webhook signing secret (shown once when the webhook is
    # created — see CHATMITRA_SETUP.md). Required for HMAC-SHA256 signature
    # verification of inbound webhooks. If empty/unset, verification is
    # skipped (local dev only — must be set before go-live).
    CHATMITRA_WEBHOOK_SECRET: str = ""

    # Groq API key. Optional: without it the deterministic name index still
    # matches perfumes on its own (see app.matcher) — what's lost is
    # context-aware intent judgment and the natural reply phrasing.
    GROQ_API_KEY: str = ""

    # Groq model used for intent judgment / disambiguation / phrasing.
    # Configurable so the model can be upgraded without a code change.
    GROQ_MODEL: str = "llama-3.1-8b-instant"

    # Hard ceiling on a Groq call. A WhatsApp reply that arrives late is
    # worse than one phrased by the deterministic fallback, and the matching
    # itself no longer depends on Groq at all.
    GROQ_TIMEOUT_SECONDS: float = 6.0

    # How many recent turns of a conversation are kept per sender, and for
    # how long, to resolve follow-ups like "and the 5ml?" — see
    # app.conversation.
    CONVERSATION_TURNS: int = 8
    CONVERSATION_TTL_SECONDS: int = 7200

    # Minimum weighted-evidence score for the name index to call something a
    # match (see app.name_index — roughly "one token unique to a single
    # catalog entry, spelled right"). This is the recall/precision dial that
    # FUZZY_THRESHOLD used to be: lower tolerates more mangled input at the
    # cost of more wrong guesses. Raise it if customers start seeing price
    # cards for perfumes they didn't ask about.
    MATCH_MIN_SCORE: float = 4.0

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
        # BaseSettings forbids unrecognized keys by default, so any stray
        # local note in .env (e.g. a reminder of a dashboard login password —
        # never read by this app; Supabase Auth checks it client-side) would
        # otherwise crash the whole app on startup. Ignore instead of forbid.
        "extra": "ignore",
    }


# Singleton instance
settings = Settings()
