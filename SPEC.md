# LinkedIn Daily Post Automation — Build Spec

**Goal:** one human-sounding, well-researched LinkedIn post per day. You send a topic brief in Telegram; the system researches it, writes 2 drafts in your voice (built from your past posts), generates 1 image per draft, and after your tap + time pick, posts it to LinkedIn. Nothing posts without your approval.

**Stack:** Python service (any runtime works) · Telegram Bot API · Supabase (queue + style corpus) · Groq `openai/gpt-oss-120b` — writing (outline, drafts, de-AI pass) and web research via its built-in `browser_search` · NVIDIA NIM — `nemotron-3-embed-1b` embeddings (2048-dim) and FLUX.1-dev images · Tavily (optional research fallback) · LinkedIn Posts API. No n8n.

**Two processes:**
- `bot.py` — handles Telegram updates (long polling or webhook). One update handler, routes everything.
- `dispatcher.py` — runs every 5 min via cron. Picks up queued posts, publishes to LinkedIn.

---

## 1. Decisions locked

- LinkedIn only. 2 drafts, 1 image each.
- Draft A = story / founder-POV, Draft B = contrarian / insight-list. Always. Rotation happens across sub-templates (`template` column: `story_cold_open`, `story_failure_first`, `story_dialogue`, `contrarian_myth_bust`, `contrarian_teardown`, `insight_list`), not across A/B roles.
- Human approval is mandatory at two gates: draft pick and time pick. No auto-posting, ever.
- Every stat in a draft must come from the research step. No invented numbers — enforced by a code check (§5.4), not just the prompt.
- The style corpus never learns from raw AI output: only human-seeded posts, your edited drafts, or AI drafts that beat median human engagement get in (§6.4).
- All times IST (`Asia/Kolkata`).

## 2. Prerequisites

| # | Item | Notes |
|---|------|-------|
| 1 | A place to run it | Any VPS / always-on machine. Python 3.11+, cron with `CRON_TZ=Asia/Kolkata` |
| 2 | Supabase project | Run the SQL in §4 in the SQL editor. The service uses the **service_role** key (bypasses RLS) |
| 3 | Telegram bot | BotFather → token. Whitelist your `chat_id` (get it via `@userinfobot`) |
| 4 | NVIDIA API key (build.nvidia.com) | Embeddings via `integrate.api.nvidia.com/v1/embeddings` and images via FLUX.1-dev (`ai.api.nvidia.com`); optionally the writer (`WRITER_PROVIDER=nvidia`). Free tier: ~40 req/min per model, ~1000 credits — fine for one post/day, not for bulk backfills |
| 5 | Writer quality check | Run `scripts/validate_writer.py` before go-live: 3 test briefs through the real outline → drafts → de-AI → number-repair pipeline, checked against the prompt rules. Switch `WRITER_PROVIDER` / model if it can't meet them |
| 6 | Groq API key (console.groq.com) | Writer (`openai/gpt-oss-120b`) and browsing research (`browser_search`). Free tier: 8,000 tokens/min on gpt-oss-120b, so calls are serialised and 429s waited out — a run takes ~1–3 min |
| 7 | Tavily API key (optional) | Required only for `RESEARCH_PROVIDER=tavily`; otherwise a fallback when browsing yields < 3 verified facts, and the 2 PM news-topic source |
| 8 | LinkedIn Developer app | "Share on LinkedIn" product, scopes `w_member_social` + `openid profile`. **Standard apps don't get refresh tokens** — plan on a manual re-auth every ~60 days (§3.4) |
| 9 | 15–20 best past LinkedIn posts **with real engagement numbers** (likes + comments each) | Single biggest lever for voice quality. Seeded as `source='human'`. The promotion gate (§5.3) needs at least 5 of these with `engagement > 0` before it activates |
| 10 | **Current role one-liner** | ⚠ Still unanswered. Stored in `voice_profile`; every draft prompt depends on it |

### Environment variables

| Variable | Purpose |
|----------|---------|
| `LINKEDIN_API_VERSION` | `LinkedIn-Version` header value, e.g. `202601`. Versions sunset after ~1 year — bump when LinkedIn announces |
| `NICHE_KEYWORDS` | e.g. `AI agents, startups, edtech, student founders` — source for the 2 PM fallback topic |
| `MY_CHAT_ID` | Your Telegram chat id — whitelist check, first thing in the update handler |
| `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` | Database access |
| `TELEGRAM_BOT_TOKEN` | Bot API token |
| `GROQ_API_KEY`, `NVIDIA_API_KEY`, `TAVILY_API_KEY` | Writer + browsing research · embeddings + images · optional research fallback |
| `WRITER_PROVIDER`, `RESEARCH_PROVIDER`, `NUMBER_REPAIR` | `groq`/`nvidia` · `browse`/`tavily` · step 7b on/off |
| `GROQ_CHAT_MODEL`, `NVIDIA_CHAT_MODEL`, `NVIDIA_EMBED_MODEL` | Writing model per provider, and the embedding model (must output 2048-dim vectors to match §4) |

LinkedIn tokens live in the `settings` table (`linkedin_access_token`, `linkedin_token_issued_at`, `linkedin_author_urn`), not in env vars — env vars can't hold issue dates or be rotated at runtime.

---

## 3. Cron schedule

```
CRON_TZ=Asia/Kolkata
*/5 * * * *  dispatcher.py          # every 5 min: publish due posts
0 8 * * *    bot.py --nudge         # 8:00 AM IST: "What's today's topic?"
0 14 * * *   bot.py --fallback      # 2:00 PM IST: draft from news if no brief today (IST date)
0 9 * * 1    bot.py --token-check   # weekly: warn if LinkedIn token older than 50 days
```

### 3.4 LinkedIn re-auth (manual, ~every 60 days)

Standard "Share on LinkedIn" apps don't receive refresh tokens (that's partner-only), so `--token-check` only warns — it can't refresh. When it fires: open the LinkedIn OAuth authorize URL (`https://www.linkedin.com/oauth/v2/authorization?response_type=code&client_id={id}&redirect_uri={uri}&scope=w_member_social%20openid%20profile`), complete the flow, exchange the code for a token, and write three keys to `settings`: `linkedin_access_token`, `linkedin_token_issued_at = now()`, and **`linkedin_author_urn`** (call `/v2/userinfo` once here and store `urn:li:person:{sub}` — the dispatcher reads it from settings instead of calling the API). Takes ~2 minutes.

---

## 4. Database schema

Run once in Supabase SQL editor. RLS is on (anon key gets nothing); the service uses `service_role`. **Table order matters:** `past_posts` is created before `posts` because `posts.past_post_id` references it.

```sql
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
  pending_action text not null,   -- 'awaiting_custom_time' | 'awaiting_edit_a' | 'awaiting_edit_b' | 'awaiting_stats'
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
```

---

## 5. Bot process — update handling

Single update handler. **Order matters: whitelist → state checks → commands → plain brief last.**

```
on_update(update):
    chat_id = update.message.chat.id or update.callback_query.message.chat.id
    if chat_id != MY_CHAT_ID: return                      # whitelist first

    if update.callback_query: return button_router(update)

    state = get_chat_state(chat_id)  # WHERE expires_at > now()
    text  = update.message.text

    if state == 'awaiting_edit_a':    return save_edit(chat_id, 'a', text)
    if state == 'awaiting_edit_b':    return save_edit(chat_id, 'b', text)
    if state == 'awaiting_custom_time': return save_custom_time(chat_id, text)
    if state == 'awaiting_stats':     return save_stats(chat_id, text)
    if text == '/stats': return stats_flow(chat_id)
    return generation_pipeline(chat_id, text)             # plain brief, last
```

### 5.1 Generation pipeline

1. Embed brief → `find_similar_topic` RPC (posted, last 30 days only). If a row returns, pass its topic to the outline step: "find a fresh angle, this was covered."
2. Research: the browsing model (`browser_search`) finds 5–8 facts with numbers and source URLs. **Each fact is kept only if every number in it appears in the raw text of the page it cites** (pages the model actually opened, from `executed_tools`) — so the research the drafts must stick to is real source text, not the model's summary. If fewer than 3 facts survive and `TAVILY_API_KEY` is set, top up with Tavily (`brief + " statistics " + current_year`, top 5–8 with URLs). → `research` JSON.
3. Embed brief → `match_past_posts` RPC → 3 most similar past posts, engagement-weighted and capped (few-shot).
4. Load `voice_profile`.
5. Chat model → **Outline** (§8.2) → insight, 3 hooks, sub-template pick.
6. Chat model → **Draft A** (story/founder-POV) and **Draft B** (contrarian/insight-list) (§8.3), temp 0.85. Inputs: brief, research, outline, voice profile, 3 few-shot posts.
7. Chat model → **De-AI pass** (§8.4) on each draft.
7b. **Number repair** (`NUMBER_REPAIR=true`): if step 8's check finds numbers not in the research, one call asks the model to remove exactly those numbers and change nothing else. The result is kept only if it has fewer unverified numbers and keeps ≥ 60% of the text; anything still unverified is shown as ⚠.
8. **Number verification (code, not prompt):** normalize both draft and research text first, then extract numbers from the draft and check each appears in the research. Misses are appended to the Telegram preview as "⚠ unverified numbers: …". Normalization:
   - Strip `$`, `₹`, and commas.
   - Map `percent`→`%`, `billion`→`B`, `million`→`M`, `thousand`→`K`, `crore`→`Cr`, `lakh`→`L` (case-insensitive, word boundaries).
   - Ignore bare integers 1–10 with no unit ("3 lessons", "5 reasons") — those are list counts, not stats.
   - Anything left unmatched is flagged. The LLM will still slip; this catches it mechanically.
9. Images: FLUX.1-dev on NVIDIA (`POST https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.1-dev`, body `{"prompt": …}`, returns a base64 JPEG in `artifacts[0]`). Center-crop to 4:5 and resize to 1080×1350 with Pillow, save as **PNG** (LinkedIn rejects WebP — verify content-type before upload). Upload to Supabase Storage → public URLs.
10. Insert `posts` row (`status='awaiting_choice'`, `topic_embedding`, `template`).
11. Telegram send sequence (separate API calls, in order): photo A → photo B → message with Draft A → message with Draft B (+ ⚠ line if any) → "Pick a draft:" with inline keyboard `Post A` (`pick:a:{id}`) · `Post B` (`pick:b:{id}`) · `Regenerate` (`regen:{id}`) · `Edit A` (`edit:a:{id}`) · `Edit B` (`edit:b:{id}`). (Buttons can't go on media groups; captions cap at 1024 chars — this ordering respects both. Every callback carries the post id; all fit the 64-byte limit.)

### 5.2 Button router (`callback_query.data`)

Parse `data.split(':')` → e.g. `['pick','a','<uuid>']`. **Status guard first, on every callback:**

| Callback | Guard | Action |
|----------|-------|--------|
| `pick:a:{id}` / `pick:b:{id}` | `status='awaiting_choice'` | Set `chosen`, then send time picker: `Now` (`time:now:{id}`) · `9 AM` (`time:9am:{id}`) · `6 PM` (`time:6pm:{id}`) · `Custom` (`time:custom:{id}`) |
| `time:now:{id}` | `status='awaiting_choice' AND chosen IS NOT NULL` | `scheduled_at = now + 2 min`, status `queued` |
| `time:9am:{id}` / `time:6pm:{id}` | same as above | Next occurrence IST, status `queued` |
| `time:custom:{id}` | same as above | Write `chat_state(awaiting_custom_time)`, reply "Send time as HH:MM (IST)" |
| `regen:{id}` | `status='awaiting_choice'` | Re-run pipeline steps 6–11 but **UPDATE the existing row** (no new row); reset `edited_a = edited_b = false` |
| `edit:a:{id}` / `edit:b:{id}` | `status='awaiting_choice'` | Write `chat_state(awaiting_edit_a / awaiting_edit_b)`, reply "Send your edited version of draft A/B" |

Guard failure → `answerCallbackQuery("Expired")` and do nothing. Always call `answerCallbackQuery` so the button stops spinning.

`save_edit(chat_id, which, text)`: replaces `draft_a`/`draft_b`, sets `edited_a`/`edited_b = true` for that draft only, clears `chat_state`, resends the preview.

`save_custom_time(chat_id, text)`: parse `HH:MM`; build today IST at that time; **if it's in the past, roll to tomorrow**; set `scheduled_at`, status `queued`, confirm.

### 5.3 Stats flow (`/stats`)

Bot lists the last 5 **posted** rows from `posts` (date + `post_url`), asks for `likes comments` per post one per line, writes `chat_state(awaiting_stats)`. On reply, for each row:
- If `past_post_id` exists → `UPDATE past_posts SET engagement = likes + comments`.
- If not (AI draft never admitted) → **promotion gate:** first check `SELECT count(*) >= 5 FROM past_posts WHERE source='human' AND engagement > 0`. If the gate isn't met, skip promotion entirely (the median is meaningless on thin data). If met and `likes + comments` beats the median engagement of `source='human'` rows, insert into `past_posts` (`source='ai'`, with embedding) and set `past_post_id`.

### 5.4 Cron modes

- `--nudge`: send "What's today's topic?"
- `--fallback`: if no `posts` row with `created_at >= date_trunc('day', now() at time zone 'Asia/Kolkata') at time zone 'Asia/Kolkata'` (IST date, not UTC), a news search over `NICHE_KEYWORDS` (Tavily if configured, otherwise the browsing model), pick 1 topic, run the generation pipeline. Drafts only — never auto-posts.
- `--token-check`: read `settings.linkedin_token_issued_at`; if older than 50 days, Telegram warning with the manual re-auth steps (§3.4).

---

## 6. Dispatcher process (every 5 min)

1. **Author URN:** read `settings.linkedin_author_urn`. Call `/v2/userinfo` only if the key is missing (then store it).
2. **Stuck-row check:** `SELECT * FROM posts WHERE status='posting' AND claimed_at < now() - interval '15 min' AND alerted_at IS NULL`. For each → set `alerted_at = now()`, Telegram alert: "May already be live — check LinkedIn before touching it: {brief}". Never auto-retry these; alert fires once per row.
3. **Claim due rows:** `SELECT … WHERE status='queued' AND scheduled_at <= now() ORDER BY scheduled_at LIMIT 5`. For each: `UPDATE posts SET status='posting', claimed_at=now() WHERE id=… AND status='queued'`. Proceed only if a row was actually updated.
4. **Post to LinkedIn:**
   1. Download the image binary (HTTP GET the Supabase Storage URL — LinkedIn won't pull from your URL). Verify PNG/JPEG.
   2. `POST https://api.linkedin.com/rest/images?action=initializeUpload` with `owner` = author URN → upload URL + image URN.
   3. `PUT` the binary to the upload URL (`Content-Type: image/png` or `image/jpeg`).
   4. `POST https://api.linkedin.com/rest/posts` — single-image post: `content.media: {id: "urn:li:image:…"}`. **Headers on every `/rest/` call:** `LinkedIn-Version: {LINKEDIN_API_VERSION}`, `X-Restli-Protocol-Version: 2.0.0`.
5. **On success:** read the `x-restli-id` response header → `post_url = https://www.linkedin.com/feed/update/{urn}` → status `posted`, `posted_at=now()`, store `image_urn`, `post_url`.
   **Corpus admission (gated):** embed the posted draft → insert into `past_posts` (`source='ai'`) **only if `(chosen='a' AND edited_a) OR (chosen='b' AND edited_b)`** → set `past_post_id`. Unedited AI drafts stay out until they earn their way in via the `/stats` promotion check.
   Telegram: "Posted ✓ {post_url}".
6. **On failure — split by cause:**
   - Image upload failure (init or PUT), or **4xx** on `POST /rest/posts`: the post definitely didn't go out → `retry_count+1`, `error` = message, status back to `queued` if `retry_count < 2` else `failed`. Telegram alert with the error.
   - **Timeout or 5xx** on `POST /rest/posts`: the post may already be live → leave the row in `posting`, set `alerted_at = now()`, Telegram alert "may be live — check LinkedIn before touching it". **No retry.**

### 6.4 Corpus admission policy (anti self-poisoning)

The corpus must never learn from raw AI output, or the voice drifts generic:
- `source='human'` — your seeded posts. Always eligible.
- `source='ai'` — admitted only via: (a) you edited the chosen draft (`edited_a`/`edited_b`, auto-admitted on post), or (b) it beat median human engagement (admitted via `/stats` promotion, gated on ≥5 engaged human rows).
- `match_past_posts` weights by engagement (capped), so proven posts dominate the few-shot examples.

---

## 7. LinkedIn API reference

| Call | Method | Notes |
|------|--------|-------|
| Author URN | `GET /v2/userinfo` | `sub` → `urn:li:person:{sub}` — stored in `settings` at re-auth; dispatcher only calls if missing |
| Init upload | `POST /rest/images?action=initializeUpload` | Body: `{"initializeUploadRequest": {"owner": "{author_urn}"}}` |
| Binary upload | `PUT {uploadUrl}` | Raw bytes, `Content-Type: image/png` or `image/jpeg` |
| Create post | `POST /rest/posts` | `author`, `commentary` (draft text), `content: {media: {id, title}}`, `lifecycleState: PUBLISHED`, `visibility: PUBLIC` |
| Post URL | — | Built from `x-restli-id` response header |

---

## 8. Prompts (copy-paste ready; `{braces}` = injected at runtime)

> **Superseded (2026-09-23):** the live prompts are in `app/prompts/`. They add the copywriting playbook (`copy_playbook.txt`: hook ≤140 chars from six frameworks, one idea, 900–1,400 characters, ≤2 stats), a per-draft `tune.txt` (✂️ shorter / 🎣 new hook / 🔥 bolder), and a scene-based single image (`image_scene.txt` → `image.txt`, 1088×1344 → 1080×1350). See README Decisions 55 and 58–62.

### 8.1 Voice extraction (one-time; feed it your 15–20 posts)

```
Analyze these LinkedIn posts and return ONLY a JSON object:
{
  "current_role": "<ask the user, do not guess>",
  "avg_length_words": <int>,
  "hook_archetypes": ["<3-5 patterns, e.g. 'opens with a specific number'>"],
  "line_style": "<how line breaks are used>",
  "sentence_rhythm": "<short/long mix>",
  "emoji_use": "<frequency and placement>",
  "hashtag_use": "<count and style>",
  "cta_style": "<how posts end>",
  "vocab_notes": "<characteristic words, avoided words>",
  "tone": "<2-3 adjectives>"
}
POSTS:
{posts}
```

### 8.2 Outline

```
Brief: {brief}
Research (facts with sources — use ONLY these for any number you cite): {research}
Recently covered (find a fresh angle if related): {recent_topics}
Similar past posts (match cadence, not content): {few_shot_posts}
Voice: {voice_profile}

Return ONLY JSON:
{"insight": "<one-sentence core insight>",
 "hooks": ["<hook 1: specific — a number, name, or failure>",
           "<hook 2>", "<hook 3>"],
 "audience": "<who this is for>",
 "sub_template": "<one of: story_cold_open, story_failure_first, story_dialogue, contrarian_myth_bust, contrarian_teardown, insight_list>",
 "angle_note": "<what makes this different from the recent topics>"}
```

### 8.3 Draft (run twice; `{role}` = story/founder-POV or contrarian/insight-list)

System:
```
You write LinkedIn posts as Deepanshu Lathar: {current_role}.
Direct, zero hype, every claim traceable to the research provided.
This draft's role: {role}.
NEVER use: "in today's fast-paced world", "delve", "game-changer", "unlock",
"supercharge", "elevate", "embark", "crucial", "tapestry", "landscape" as metaphor,
"it's not X, it's Y" constructions, "here's the thing", "let that sink in",
emojis as bullet points, more than 3 hashtags, or opening with a question you
immediately answer.
Short lines. One idea per line. Hooks are specific (a number, a name, a failure),
never generic. End with one CTA question, not a summary.
Match this voice profile: {voice_profile}
Learn from these past posts — copy their cadence, not their content:
{post_1}
{post_2}
{post_3}
```
User:
```
Brief: {brief}
Outline: {outline_json}
Research (cite ONLY these facts; include source names where natural): {research}
Write the post now. 150–280 words.
```

### 8.4 De-AI pass

```
You are an editor. Rewrite the draft below so it is indistinguishable from a
human-written LinkedIn post. Rules:
1. Remove every AI cliché: "delve", "game-changer", "unlock", "supercharge",
   "in today's fast-paced world", "it's not X, it's Y", "here's the thing",
   "let that sink in", emoji bullets, hashtag stacks.
2. If a sentence could appear in any founder's post, rewrite it to be specific
   to this story.
3. Every number or factual claim MUST appear in the research. Delete any that don't.
4. Vary sentence length. Prefer concrete nouns and verbs over abstractions.
5. Keep the hook, the line-break cadence from the voice profile, and the closing
   CTA question. 150–280 words.
Voice profile: {voice_profile}
Research: {research}
DRAFT:
{draft}
Return ONLY the rewritten post.
```

### 8.5 Image prompt (per draft, FLUX.1-dev on NVIDIA → center-crop → 1080×1350 PNG)

```
Minimal editorial illustration, visual metaphor for: "{hook}".
Mood: dark, technical, developer aesthetic. Muted colors, generous negative space.
Portrait, subject centered with margin top and bottom (will be cropped to 4:5).
Text in image: none, unless a single short headline of ≤5 words — then render it
perfectly, no typos.
```

---

## 9. Guardrails

- Two human gates (draft pick, time pick). LinkedIn flags spammy automation; the account's safety is worth the two taps.
- `chat_id` whitelist is the first check — anyone who finds the bot can't trigger paid runs.
- Every callback carries the post id and is status-guarded — stale buttons answer "Expired" instead of acting on the wrong row.
- Dispatcher never double-posts (claim-before-post). Timeout/5xx failures are never retried blindly — they alert "may be live" instead.
- Stat traceability is enforced in code with normalization (§5.1 step 8), not just in the prompt.
- `LINKEDIN_API_VERSION` is an env var — bump it when LinkedIn sunsets the version (~yearly).
- `current_role` is data, not prompt text — update `voice_profile` when your title changes.
- If the machine goes down, the queue is just rows in Supabase: nothing is lost, the dispatcher resumes on restart.
- Manual LinkedIn re-auth every ~60 days (§3.4) — `--token-check` warns at day 50.

## 10. Cost per run

Everything model-shaped runs on free tiers: Groq (writer + browsing research: ~1 browse + 5 chat calls, plus ≤ 2 number-repair calls per run) and NVIDIA API credits (1 embedding + 2 FLUX images per run; images are ~free on NVIDIA credits, previously ~₹8–10/run with gpt-image-1). Tavily (~₹1/search) is only spent when browsing yields < 3 verified facts, or for the 2 PM news topic.
**~₹0–1/run in cash** while the free tiers last. Groq's free tier allows 8,000 tokens/min on gpt-oss-120b (a browse call can use far more of the daily budget than a writing call); NVIDIA's ~1000 credits cover on the order of 300+ runs at ~3 requests/run. Check both dashboards and plan for paid tiers once usage grows.
Rate limits are irrelevant at one post/day but matter for bulk work: the one-time re-embedding (`scripts/reembed_corpus.py`) batches 16 texts per request and paces itself.
