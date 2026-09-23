-- SPEC §4 schema, verbatim, with one amendment (A2): 'awaiting_post_url' added
-- to the chat_state.pending_action values. Run once in the Supabase SQL editor.

create extension if not exists vector;

-- ── past_posts: the style corpus (created FIRST — posts references it)
-- Seeded once with your 15-20 best posts + real engagement (source='human').
-- AI drafts are admitted ONLY if you edited them or they later beat
-- median human engagement (see §5.3). Never raw AI output.
create table past_posts (
  id         uuid primary key default gen_random_uuid(),
  text       text not null,
  posted_at  date,
  engagement int not null default 0,
  source     text not null check (source in ('human','ai')),
  embedding  vector(2048)                             -- nvidia/nemotron-3-embed-1b (passage)
);
alter table past_posts enable row level security;

-- ── posts: one row per generation run
-- The dispatcher claims a row by flipping status to 'posting' BEFORE
-- calling LinkedIn, so two ticks can never post it twice.
-- claimed_at lets the dispatcher alert (not auto-retry) on stuck rows.
-- alerted_at keeps the stuck-row alert to once per row.
-- edited_a / edited_b are per-draft: regen resets both; save_edit sets one.
create table posts (
  id              uuid primary key default gen_random_uuid(),
  chat_id         bigint not null,
  brief           text not null,
  topic           text,
  topic_embedding vector(2048),                        -- brief, embedded as a query
  template        text,                                -- sub-template used
  research        jsonb,                               -- Tavily facts + source URLs
  draft_a         text,                                -- story / founder-POV (fixed role)
  draft_b         text,                                -- contrarian / insight-list (fixed role)
  edited_a        boolean not null default false,
  edited_b        boolean not null default false,
  image_a_url     text,                                -- Supabase Storage public URL
  image_b_url     text,
  image_urn       text,                                -- urn:li:image:... after LinkedIn upload
  chosen          char(1) check (chosen in ('a','b')),
  scheduled_at    timestamptz,
  status          text not null default 'draft'
                  check (status in ('draft','awaiting_choice','queued','posting','posted','failed')),
  claimed_at      timestamptz,
  alerted_at      timestamptz,                         -- stuck-row alert sent once
  retry_count     int not null default 0,
  error           text,
  post_url        text,                                -- built from x-restli-id response header
  past_post_id    uuid references past_posts(id) on delete set null,
  created_at      timestamptz default now(),
  posted_at       timestamptz
);
create index posts_status_sched_idx on posts (status, scheduled_at);
alter table posts enable row level security;

-- ── chat_state: short-lived conversation state
-- Lets Custom-time and edit replies be understood in context instead of
-- being parsed as new briefs. Every lookup MUST include: and expires_at > now()
create table chat_state (
  chat_id        bigint primary key,
  pending_action text not null,   -- 'awaiting_custom_time' | 'awaiting_edit_a' | 'awaiting_edit_b' | 'awaiting_stats' | 'awaiting_post_url'
  post_id        uuid references posts(id) on delete cascade,
  expires_at     timestamptz not null default now() + interval '30 minutes'
);
alter table chat_state enable row level security;

-- ── voice_profile: singleton
-- Extracted once from your best posts; re-run when your voice shifts.
-- current_role lives here so prompts never misstate your title.
create table voice_profile (
  id         int primary key check (id = 1),
  profile    jsonb not null,
  updated_at timestamptz default now()
);
alter table voice_profile enable row level security;

-- ── settings: runtime-rotated values (tokens, author URN)
create table settings (
  key        text primary key,   -- linkedin_access_token | linkedin_token_issued_at | linkedin_author_urn
  value      text not null,
  updated_at timestamptz default now()
);
alter table settings enable row level security;

-- ── RPC: few-shot retrieval, weighted toward proven posts (capped)
-- Engagement weight is capped with least() so a viral outlier can't
-- permanently dominate the examples: max shift is 0.1 distance units.
create or replace function match_past_posts(q vector(2048), k int default 3)
returns setof past_posts
language sql stable
as $$
  select * from past_posts
  where embedding is not null
  order by (embedding <=> q) - least(0.1 * log(1 + engagement), 0.1)
  limit k;
$$;

-- ── RPC: topic dedup by meaning, not exact text
-- Scans ONLY posted rows from the last 30 days — rejected drafts and
-- regens don't count. Cosine similarity > 0.85 = duplicate.
create or replace function find_similar_topic(q vector(2048), threshold float default 0.85)
returns table (id uuid, topic text, similarity float)
language sql stable
as $$
  select p.id, p.topic, 1 - (p.topic_embedding <=> q) as similarity
  from posts p
  where p.topic_embedding is not null
    and p.status = 'posted'
    and p.posted_at > now() - interval '30 days'
    and 1 - (p.topic_embedding <=> q) > threshold
  order by p.topic_embedding <=> q
  limit 1;
$$;
