"""Runtime settings via env / .env. Read once at import."""
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="TRACER_",
        extra="ignore",
    )

    # === JWT (shared with tracer-frontend) =================================
    # The Next.js frontend mints device JWTs with this same HS256 secret.
    # Value must match what's in tracer-frontend/.env.local.
    device_jwt_secret: str = Field(default="dev-only-change-me")
    device_jwt_issuer: str = "tracer-web"
    device_jwt_audience: str = "tracer-backend"

    # === Supabase (jti freshness check + tier reads) =======================
    # Public URL from Project Settings → API. NOT the anon key.
    supabase_url: str = Field(default="")
    # SERVICE ROLE key (server-only). Bypasses RLS to read profiles.jti.
    supabase_service_role_key: str = Field(default="")

    # === Gemini upstream ===================================================
    # Server-side key. Users don't see it. Every LLM call uses this one.
    gemini_api_key: str = Field(default="")

    # === CORS ==============================================================
    # Only the plugin origin(s) need to hit this backend. localhost during dev.
    cors_origins: list[str] = Field(default_factory=lambda: [
        "http://localhost:3000", "http://localhost:5173",
    ])

    # === Tier quotas — two-window model (Claude-style) ====================
    # Each tier has BOTH a 5-hour session cap AND a weekly cap.
    # A request has to fit under both to succeed.
    #
    #   session — bounds a single sitting so one user can't drain the
    #             shared free-tier Gemini pool in an afternoon.
    #   weekly  — bounds total spend, tied to what a paid plan can
    #             recover in gross margin.
    #
    # 4M Pro/week gives us room to sell $20/mo with real margin even
    # once we swap the backend to paid Anthropic/OpenAI keys.
    tier_session_hours: int = 5
    tier_session_quotas: dict[str, int] = Field(default_factory=lambda: {
        "hobby":   30_000,   # ~30 short chats before you have to wait
        "basic":  300_000,
        "pro":  1_300_000,   # ~1/3 of weekly, so a single sitting can't burn it
    })
    tier_weekly_quotas: dict[str, int] = Field(default_factory=lambda: {
        "hobby":  100_000,
        "basic": 1_000_000,
        "pro":   4_000_000,
    })


@lru_cache
def get_settings() -> Settings:
    return Settings()
