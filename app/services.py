"""Dependency container: one place that builds every external client with a timeout.
Tests construct `Services` directly with fakes."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

import httpx
from tavily import AsyncTavilyClient

from app.brightdata import BrightData
from app.config import Settings
from app.db import EMBED_DIM, Database
from app.groq import GroqClient
from app.images import ImageMaker
from app.jev import JevGate
from app.linkread import LinkReader
from app.news import NewsData, NewsFirst
from app.nvidia import THINKING_OFF, InputType, NvidiaClient
from app.openrouter import (
    ChainLink,
    FallbackChat,
    ModelScopeClient,
    OpenRouterClient,
    ZenMuxClient,
    parse_chain,
)
from app.research import (
    BrowsingResearcher,
    FailoverBrowser,
    FreshSearch,
    Researcher,
    ResearchSource,
)
from app.spend import SpendCap
from app.telegram_ui import Messenger
from app.usage import UsageRecorder
from app.writer import Writer


class EmbeddingError(RuntimeError):
    pass


class Embedder:
    """Asymmetric retrieval embeddings: a brief is a *query*; a post is a *passage*.
    posts.topic_embedding holds query vectors (brief-vs-brief dedup); past_posts
    holds passage vectors (brief-vs-post few-shot retrieval)."""

    BATCH = 16  # texts per request — keeps bulk jobs well under the ~40 req/min limit

    def __init__(self, client: NvidiaClient, model: str) -> None:
        self._client = client
        self._model = model

    async def _embed(self, texts: list[str], input_type: InputType) -> list[list[float]]:
        # NFKC folds "𝐛𝐨𝐥𝐝 𝐔𝐧𝐢𝐜𝐨𝐝𝐞" post styling (and full-width chars) to plain letters, so the
        # embedding sees words instead of rare symbols. Stored text keeps the original styling.
        texts = [unicodedata.normalize("NFKC", t)[:30000] for t in texts]
        vectors: list[list[float]] = []
        for i in range(0, len(texts), self.BATCH):
            vectors += await self._client.embed(self._model, texts[i : i + self.BATCH], input_type)
        for v in vectors:
            if len(v) != EMBED_DIM:
                raise EmbeddingError(
                    f"{self._model} returned {len(v)}-dim vectors; the schema expects vector({EMBED_DIM}). "
                    "Use a 2048-dim model or change the schema (see README: embedding migration)."
                )
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([text], "query"))[0]

    async def embed_passage(self, text: str) -> list[float]:
        return (await self._embed([text], "passage"))[0]

    async def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return await self._embed(texts, "passage") if texts else []

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return await self._embed(texts, "query") if texts else []


def build_nvidia(settings: Settings, http: httpx.AsyncClient) -> NvidiaClient:
    return NvidiaClient(
        settings.nvidia_api_key.get_secret_value(),
        http,
        chat_timeout=settings.llm_timeout,
        embed_timeout=settings.http_timeout,
        image_timeout=settings.image_timeout,
        chat_template_kwargs=THINKING_OFF if settings.nvidia_disable_thinking else None,
    )


def build_groq(settings: Settings, http: httpx.AsyncClient, second: bool = False) -> GroqClient:
    key = settings.groq_api_key_2 if second else settings.groq_api_key
    assert key is not None
    client = GroqClient(
        key.get_secret_value(),
        http,
        chat_timeout=settings.llm_timeout,
        browse_timeout=settings.research_timeout,
        reasoning_effort=settings.groq_reasoning_effort,
    )
    if second:
        client.provider_name = "groq2"  # its own ledger rows and dashboard card
    return client


def build_openrouter(settings: Settings, http: httpx.AsyncClient) -> OpenRouterClient:
    assert settings.openrouter_api_key is not None
    return OpenRouterClient(settings.openrouter_api_key.get_secret_value(), http, chat_timeout=settings.llm_timeout)


def build_modelscope(settings: Settings, http: httpx.AsyncClient) -> ModelScopeClient:
    assert settings.modelscope_api_key is not None
    return ModelScopeClient(
        settings.modelscope_api_key.get_secret_value(), http, chat_timeout=settings.llm_timeout, base_url=settings.modelscope_base_url
    )


def build_zenmux(settings: Settings, http: httpx.AsyncClient) -> ZenMuxClient:
    assert settings.zenmux_api_key is not None
    return ZenMuxClient(settings.zenmux_api_key.get_secret_value(), http, chat_timeout=settings.llm_timeout)


def primary_model(settings: Settings) -> str:
    return {
        "groq": settings.groq_chat_model,
        "openrouter": settings.openrouter_chat_model,
        "modelscope": settings.modelscope_chat_model,
        "zenmux": settings.zenmux_chat_model,
        "nvidia": settings.nvidia_chat_model,
    }[settings.writer_provider]


def writer_links(settings: Settings, clients: dict[str, object]) -> list[ChainLink]:
    """Primary writer first, then WRITER_FALLBACKS; providers without a key are skipped."""
    wanted = [(settings.writer_provider, primary_model(settings)), *parse_chain(settings.writer_fallbacks)]
    links: list[ChainLink] = []
    for provider, model in wanted:
        client = clients.get(provider)
        if client is not None and all((link.provider, link.model) != (provider, model) for link in links):
            links.append(ChainLink(provider, client, model))  # type: ignore[arg-type]
    return links


def build_writer(settings: Settings, clients: dict[str, object]) -> Writer:
    links = writer_links(settings, clients)
    chat = links[0].client if len(links) == 1 else FallbackChat(links)
    label = links[0].model if len(links) == 1 else f"chain({links[0].provider}:{links[0].model}+{len(links) - 1})"
    return Writer(chat, label, settings.draft_temperature, settings.nvidia_max_tokens, settings.unbold_drafts)


def build_researcher(settings: Settings, groq: GroqClient | None, fresh: FreshSearch | None = None) -> ResearchSource:
    tavily = (
        Researcher(AsyncTavilyClient(api_key=settings.tavily_api_key.get_secret_value()), settings.research_timeout)
        if settings.tavily_api_key
        else None
    )
    if settings.research_provider == "browse":
        assert groq is not None
        return BrowsingResearcher(groq, settings.groq_chat_model, fallback=tavily, fresh=fresh)
    assert tavily is not None
    return tavily


@dataclass
class Services:
    settings: Settings
    db: Database
    writer: Writer
    researcher: ResearchSource
    images: ImageMaker
    embedder: Embedder
    messenger: Messenger | None = None
    recorder: UsageRecorder | None = None
    links: LinkReader | None = None  # reads URLs pasted into a brief
    jev: JevGate | None = None  # second opinion before chat-started drafts
    news: NewsData | None = None  # /ideas
    http: httpx.AsyncClient | None = None  # /dashboard live checks


async def build_services(settings: Settings, messenger: Messenger | None = None) -> Services:
    db = await Database.connect(
        settings.supabase_url,
        settings.supabase_service_key.get_secret_value(),
        settings.supabase_bucket,
        settings.http_timeout,
    )
    http = httpx.AsyncClient()
    nvidia = build_nvidia(settings, http)
    groq = build_groq(settings, http) if settings.groq_api_key else None
    clients: dict[str, object] = {"nvidia": nvidia}
    if groq is not None:
        clients["groq"] = groq
    groq2 = build_groq(settings, http, second=True) if settings.groq_api_key_2 else None
    if groq2 is not None:
        clients["groq2"] = groq2
    if settings.openrouter_api_key:
        clients["openrouter"] = build_openrouter(settings, http)
    if settings.modelscope_api_key:
        clients["modelscope"] = build_modelscope(settings, http)
    if settings.zenmux_api_key:
        clients["zenmux"] = build_zenmux(settings, http)
    recorder = UsageRecorder(db)
    for client in clients.values():
        client.recorder = recorder  # type: ignore[attr-defined]
    if groq2 is not None:  # the paid account: capped per IST day, then the chain moves on
        clients["groq2"] = groq2 = SpendCap(groq2, db, settings.groq2_daily_usd)  # type: ignore[assignment]
    brightdata = None
    if settings.brightdata_api_key:
        brightdata = BrightData(http, settings.brightdata_api_key.get_secret_value(), db, settings.brightdata_daily_records)
        brightdata.recorder = recorder
    links = LinkReader(http, brightdata=brightdata)
    fresh = FreshSearch(brightdata, links) if brightdata and settings.brightdata_research else None
    browser = FailoverBrowser([groq, groq2]) if groq is not None and groq2 is not None else groq
    researcher = build_researcher(settings, browser, fresh)  # type: ignore[arg-type]
    jev = JevGate(http, settings.jev_api_key.get_secret_value()) if settings.jev_api_key else None
    if jev is not None:
        jev.recorder = recorder
    fallback = getattr(researcher, "_fallback", None)
    for r in (researcher, fallback):
        if r is not None and hasattr(r, "recorder"):
            r.recorder = recorder
    news = None
    if settings.newsdata_api_key:
        news = NewsData(http, settings.newsdata_api_key.get_secret_value())
        news.recorder = recorder
        researcher = NewsFirst(researcher, news)
    return Services(
        settings=settings,
        db=db,
        writer=build_writer(settings, clients),
        researcher=researcher,
        recorder=recorder,
        links=links,
        jev=jev,
        news=news,
        http=http,
        images=ImageMaker(nvidia),
        embedder=Embedder(nvidia, settings.nvidia_embed_model),
        messenger=messenger,
    )
