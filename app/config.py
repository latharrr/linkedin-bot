"""All runtime configuration, read from the environment / .env.

Secrets are SecretStr so they never render in reprs or logs.
LinkedIn tokens are NOT here: they live in the `settings` table (SPEC §2).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Tried in order when the primary writer fails (error, rate limit, empty or cut-off reply).
# Order from live probes on 2026-09-23; entries whose provider has no API key are skipped.
DEFAULT_WRITER_FALLBACKS = ",".join(
    [
        # GROQ_API_KEY_2 = the paid (Developer, pay-per-token) account: the validated best
        # writer as soon as the free account's quota runs out. ~$0.01 per draft run.
        "groq2:openai/gpt-oss-120b",
        # Free per-model quotas on the free account (validated 2026-09-23 through the full
        # pipeline incl. the 7d polish: gpt-oss-20b 5/6; qwen 3/6 before polish).
        "groq:openai/gpt-oss-20b",
        "groq:qwen/qwen3.8-27b",
        "openrouter:dots-studio/dots-3-note-preview:free",
        "openrouter:nvidia/nemotron-3-super-120b-a12b:free",
        "openrouter:nex-agi/nex-n2.5-pro:free",
        # ModelScope: unprobed until the account is bound to Alibaba Cloud (401 until then; skipped fast)
        "modelscope:deepseek-ai/DeepSeek-V4-Pro",
        "modelscope:Qwen/Qwen3.5-397B-A17B",
        "modelscope:MiniMax/MiniMax-M3",
        "modelscope:zai-org/GLM-5.2",
        # ZenMux free models: 403 "api_key_source: payg" for the current key (2026-09-23) — skipped fast
        "zenmux:dots-studio/dots3-note-prev",
        "zenmux:z-ai/glm-4.7-flash-free",
        "zenmux:sapiens-ai/agnes-2.5-flash",
        "zenmux:atria-asi/atria-dawn-preview",
        "openrouter:qwen/qwen3.8-27b:free",
        "openrouter:google/gemma-4-31b-it:free",
        "openrouter:z-ai/glm-5.2:free",
        "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free",
        "openrouter:openrouter/free",
        "nvidia:nvidia/nemotron-3-super-120b-a12b",
    ]
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    telegram_bot_token: SecretStr
    my_chat_id: int

    # Supabase
    supabase_url: str
    supabase_service_key: SecretStr
    supabase_bucket: str = "post-images"

    # Who does what. Writer: outline, drafts, de-AI pass. Research: facts for the drafts.
    writer_provider: Literal["groq", "openrouter", "modelscope", "zenmux", "nvidia"] = "groq"
    # Comma-separated provider:model list tried after the primary writer ("" = no fallback).
    writer_fallbacks: str = DEFAULT_WRITER_FALLBACKS
    research_provider: Literal["browse", "tavily"] = "browse"
    # After de-AI, one extra call removes numbers the research doesn't support.
    number_repair: bool = True

    # Groq (writer + browsing research)
    groq_api_key: SecretStr | None = None
    groq_api_key_2: SecretStr | None = None  # paid (pay-per-token) account: failover writer + browser
    groq2_daily_usd: float = 0.25  # hard daily spend cap for GROQ_API_KEY_2 (~$0.01 per draft run)
    groq_chat_model: str = "openai/gpt-oss-120b"
    groq_reasoning_effort: Literal["low", "medium", "high"] = "low"

    # OpenRouter (free `:free` models: writer fallback chain, or the primary writer)
    openrouter_api_key: SecretStr | None = None
    openrouter_chat_model: str = "dots-studio/dots-3-note-preview:free"

    # ModelScope (free API inference; account must be bound to Alibaba Cloud)
    modelscope_api_key: SecretStr | None = None
    modelscope_base_url: str = "https://api-inference.modelscope.ai/v1"
    modelscope_chat_model: str = "deepseek-ai/DeepSeek-V4-Pro"

    # ZenMux (free models; the key must be allowed to use them)
    zenmux_api_key: SecretStr | None = None
    zenmux_chat_model: str = "dots-studio/dots3-note-prev"

    # NVIDIA NIM (embeddings, FLUX images; optional writer)
    nvidia_api_key: SecretStr
    nvidia_chat_model: str = "deepseek-ai/deepseek-v4.1-flash"
    nvidia_embed_model: str = "nvidia/nemotron-3-embed-1b"  # 2048 dims = schema vector(2048)
    nvidia_max_tokens: int = 4096
    nvidia_disable_thinking: bool = True  # see app/nvidia.py THINKING_OFF
    draft_temperature: float = 0.85
    # Fold 𝐛𝐨𝐥𝐝-Unicode to plain text in drafts. Off: the author's master profile allows it on LinkedIn.
    unbold_drafts: bool = False

    # Tavily: required for research_provider=tavily, otherwise an optional fallback
    tavily_api_key: SecretStr | None = None

    # Bright Data (optional): scrapes your own past LinkedIn posts for the voice corpus
    brightdata_api_key: SecretStr | None = None
    brightdata_daily_records: int = 10  # hard cap per IST day; 5,000 free records ≈ years at ~1–2/run
    brightdata_research: bool = True  # ChatGPT Search facts in every draft run (1 record each)
    jev_api_key: SecretStr | None = None
    newsdata_api_key: SecretStr | None = None  # NewsData.io: news topics (2 PM fallback, "you pick")

    # LinkedIn
    linkedin_api_version: str = Field(pattern=r"^\d{6}$")
    linkedin_client_id: str = ""
    linkedin_client_secret: SecretStr = SecretStr("")
    linkedin_redirect_uri: str = "http://localhost:8765/callback"

    # Content
    niche_keywords: str = ""

    # Timeouts (seconds) — every external call gets one
    http_timeout: float = 30.0
    llm_timeout: float = 180.0
    image_timeout: float = 240.0
    research_timeout: float = 60.0

    log_level: str = "INFO"

    @field_validator("niche_keywords")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @model_validator(mode="after")
    def _providers_have_keys(self) -> Settings:
        needs_groq = self.writer_provider == "groq" or self.research_provider == "browse"
        if needs_groq and not self.groq_api_key:
            raise ValueError("GROQ_API_KEY is required for WRITER_PROVIDER=groq or RESEARCH_PROVIDER=browse")
        if self.writer_provider == "openrouter" and not self.openrouter_api_key:
            raise ValueError("OPENROUTER_API_KEY is required for WRITER_PROVIDER=openrouter")
        if self.writer_provider == "modelscope" and not self.modelscope_api_key:
            raise ValueError("MODELSCOPE_API_KEY is required for WRITER_PROVIDER=modelscope")
        if self.writer_provider == "zenmux" and not self.zenmux_api_key:
            raise ValueError("ZENMUX_API_KEY is required for WRITER_PROVIDER=zenmux")
        from app.openrouter import parse_chain

        for provider, _model in parse_chain(self.writer_fallbacks):
            if provider not in ("groq", "groq2", "openrouter", "modelscope", "zenmux", "nvidia"):
                raise ValueError(f"unknown provider {provider!r} in WRITER_FALLBACKS")
        if self.research_provider == "tavily" and not self.tavily_api_key:
            raise ValueError("TAVILY_API_KEY is required for RESEARCH_PROVIDER=tavily")
        return self

    @property
    def keywords(self) -> list[str]:
        return [k.strip() for k in self.niche_keywords.split(",") if k.strip()]

    def secret_values(self) -> list[str]:
        """Every secret string, for the log redaction filter."""
        secrets = [
            self.telegram_bot_token,
            self.supabase_service_key,
            self.nvidia_api_key,
            self.groq_api_key,
            self.groq_api_key_2,
            self.openrouter_api_key,
            self.modelscope_api_key,
            self.zenmux_api_key,
            self.brightdata_api_key,
            self.jev_api_key,
            self.newsdata_api_key,
            self.tavily_api_key,
            self.linkedin_client_secret,
        ]
        return [s.get_secret_value() for s in secrets if s and s.get_secret_value()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
