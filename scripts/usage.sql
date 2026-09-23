-- Usage ledger + resource probe for the local usage dashboard (scripts/usage_dashboard.py).
-- Not part of SPEC §4; run once after schema.sql. Safe to re-run.

-- One row per external API call the bot makes (written by app/usage.py's recorder).
create table if not exists bot_api_calls (
  id                    bigint generated always as identity primary key,
  at                    timestamptz not null default now(),
  provider              text not null,          -- groq | openrouter | modelscope | zenmux | nvidia | tavily
  endpoint              text not null,          -- completions | embeddings | flux.1-dev | search
  model                 text,
  ok                    boolean not null,
  status                int,                    -- HTTP status; null on timeout / connection error
  prompt_tokens         int,
  completion_tokens     int,
  total_tokens          int,
  credits               int,                    -- Tavily credits (basic 1, advanced 2)
  ratelimit_remaining_requests int,             -- from x-ratelimit-* headers when the provider sends them
  ratelimit_limit_requests     int,
  ratelimit_remaining_tokens   int,
  ratelimit_limit_tokens       int,
  error                 text
);
create index if not exists bot_api_calls_at_idx on bot_api_calls (at desc);
create index if not exists bot_api_calls_provider_at_idx on bot_api_calls (provider, at desc);
alter table bot_api_calls enable row level security;

-- Database + Storage footprint for the dashboard. SECURITY DEFINER so it can read
-- storage.objects; callable only by the service role.
create or replace function bot_resource_usage()
returns json
language sql stable security definer
set search_path = public, pg_catalog
as $$
  select json_build_object(
    'db_bytes',        pg_database_size(current_database()),
    'storage_bytes',   coalesce((select sum((metadata->>'size')::bigint) from storage.objects where bucket_id = 'post-images'), 0),
    'storage_objects', (select count(*) from storage.objects where bucket_id = 'post-images'),
    'rows', json_build_object(
      'posts',          (select count(*) from posts),
      'past_posts',     (select count(*) from past_posts),
      'bot_api_calls',  (select count(*) from bot_api_calls)
    )
  );
$$;
revoke all on function bot_resource_usage() from public, anon, authenticated;
grant execute on function bot_resource_usage() to service_role;
