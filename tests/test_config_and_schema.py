import json
import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.log import JsonFormatter, register_secret

ROOT = Path(__file__).resolve().parents[1]

BASE = dict(
    telegram_bot_token="123:telegram-secret-token",
    my_chat_id=42,
    supabase_url="https://x.supabase.co",
    supabase_service_key="service-role-secret",
    nvidia_api_key="nvapi-secret",
    groq_api_key="gsk-secret",
    tavily_api_key="tvly-secret",
    linkedin_api_version="202601",
)


def make(**over):
    return Settings(_env_file=None, **{**BASE, **over})


def test_defaults():
    s = make()
    assert s.nvidia_chat_model == "deepseek-ai/deepseek-v4.1-flash"
    assert s.nvidia_embed_model == "nvidia/nemotron-3-embed-1b"
    assert s.draft_temperature == 0.85
    assert not hasattr(s, "image_quality") and not hasattr(s, "anthropic_api_key") and not hasattr(s, "openai_api_key")
    assert s.keywords == []


def test_keywords_split():
    assert make(niche_keywords=" AI agents, startups ,, edtech ").keywords == ["AI agents", "startups", "edtech"]


def test_secrets_not_in_repr():
    text = repr(make())
    for secret in ("telegram-secret-token", "service-role-secret", "nvapi-secret", "tvly-secret"):
        assert secret not in text


def test_api_version_validated():
    with pytest.raises(ValidationError):
        make(linkedin_api_version="2026-01")


def test_missing_required_env_fails():
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_log_redaction():
    register_secret("super-secret-linkedin-token")
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "calling with super-secret-linkedin-token", None, None)
    rec.post_id = "p1"
    out = json.loads(JsonFormatter().format(rec))
    assert out["event"] == "calling with ***"
    assert out["post_id"] == "p1"


def test_schema_is_spec_sql_plus_awaiting_post_url():
    spec = (ROOT / "SPEC.md").read_text()
    spec_sql = spec.split("```sql\n", 1)[1].split("```", 1)[0]
    schema = (ROOT / "scripts" / "schema.sql").read_text()
    amended = spec_sql.replace("'awaiting_stats'\n", "'awaiting_stats' | 'awaiting_post_url'\n")
    assert amended != spec_sql
    assert amended.strip() in schema
    # table order: past_posts before posts (FK)
    assert schema.index("create table past_posts") < schema.index("create table posts")


def test_env_example_covers_every_setting():
    example = (ROOT / ".env.example").read_text()
    for name in Settings.model_fields:
        assert name.upper() in example, name


def test_schema_uses_2048_dim_vectors_everywhere():
    schema = (ROOT / "scripts" / "schema.sql").read_text()
    assert "vector(1536)" not in schema
    assert schema.count("vector(2048)") == 4  # past_posts.embedding, posts.topic_embedding, both RPC args


def test_embedding_migration_sql():
    sql = (ROOT / "scripts" / "migrate_embeddings_2048.sql").read_text()
    assert sql.index("drop function if exists match_past_posts(vector, int)") < sql.index("create or replace function match_past_posts(q vector(2048)")
    assert "drop function if exists find_similar_topic(vector, float)" in sql
    assert "alter table past_posts alter column embedding       type vector(2048) using null" in sql
    assert "alter table posts      alter column topic_embedding type vector(2048) using null" in sql
    assert sql.strip().startswith("--") and "begin;" in sql and sql.rstrip().endswith("commit;")
    # the recreated RPC bodies must be identical to schema.sql's
    schema = (ROOT / "scripts" / "schema.sql").read_text()
    for fn in ("match_past_posts", "find_similar_topic"):
        body = schema[schema.index(f"create or replace function {fn}"):].split("$$;", 1)[0]
        assert body in sql
