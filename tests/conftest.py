from __future__ import annotations

import socket

import pytest

from app.config import Settings
from app.images import ImageMaker
from app.research import Researcher
from app.services import Embedder, Services
from app.writer import Writer
from tests.fakes import FakeDB, FakeMessenger, FakeNvidia, FakeTavily

CHAT_ID = 42


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any real socket connection in a test is a bug."""

    def guard(*args, **kwargs):
        raise RuntimeError("network access attempted in tests")

    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket, "create_connection", guard)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        telegram_bot_token="123:telegram-token",
        my_chat_id=CHAT_ID,
        supabase_url="https://x.supabase.co",
        supabase_service_key="service-key",
        nvidia_api_key="nvapi-test",
        groq_api_key="gsk-test",
        tavily_api_key="tvly",
        linkedin_api_version="202601",
        niche_keywords="AI agents, startups, edtech",
    )


@pytest.fixture
def fakes():
    return {
        "nvidia": FakeNvidia(),
        "tavily": FakeTavily(),
        "db": FakeDB(),
        "messenger": FakeMessenger(),
    }


@pytest.fixture
def svc(settings, fakes) -> Services:
    nvidia = fakes["nvidia"].client()
    return Services(
        settings=settings,
        db=fakes["db"],  # type: ignore[arg-type]
        writer=Writer(nvidia, settings.nvidia_chat_model, settings.draft_temperature, settings.nvidia_max_tokens),
        researcher=Researcher(fakes["tavily"], settings.research_timeout),
        images=ImageMaker(nvidia),
        embedder=Embedder(nvidia, settings.nvidia_embed_model),
        messenger=fakes["messenger"],
    )
