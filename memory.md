# Project memory: LinkedIn daily-post automation

A running record of what was built, why, and what's left. Read this first when you resume work.
- **Source of truth for requirements:** `SPEC.md`, as overridden by amendments A1–A5 (below).
- **User-facing setup and the full decision list:** `README.md`.

_Last updated: 2026-09-23 (project finalized)_

## ★ Final state (read this first)

- **Shipped and working:**
  - a conversational Telegram bot;
  - one researched post per run (Groq browse + Bright Data + Tavily + NewsData);
  - a profile card and story bank as true material;
  - layered truth checks: outline audit, claim audit, invented-detail detector, ⚠ flags;
  - human-text cleanup (no dashes, emoji or AI tells), calibrated on his 20 real posts;
  - a designed cover (FLUX photo + bold headline);
  - buttons, /status, /queue, /drafts, /interview, /comment, /ideas, /dashboard;
  - a paid-Groq failover with a daily cap.
- **First real post:** https://www.linkedin.com/feed/update/urn:li:share:7508569242183757824 (published on his explicit "post it").
- **Not scheduled:** `dispatcher.py`. It only runs when told, and `/status` shows this. He hasn't decided on "go live" (cron/launchd) or hosting.
- **Everything runs on his Mac:** `bot.py` and `scripts/usage_dashboard.py` (port 8787).
- **Known limit:** the writer (gpt-oss-120b) invents specifics about his work. The checks catch most of it, but posts can come out thin. The best fixes are more story-bank answers (/interview), and optionally a stronger writer model for the final editor (needs an Anthropic key).
- **Pending from him:**
  - rotate every API key pasted in chat;
  - set a spend limit in the Groq console;
  - re-auth LinkedIn before about 2026-11-22;
  - optionally fill the story bank.
- **Tests:** 907 passing; lint clean (`ruff.toml`).
- **Direction:** commercialize. Phase 1 is testing on himself, phase 2 adds features, phase 3 opens it to paying customers via Telegram/WhatsApp. See README "Vision and roadmap", which includes the single-user → multi-customer change list and the legal and licence checks.

---

## 1. Status

| Area | State |
|---|---|
| Code | All five build phases are complete, plus two provider migrations on 2026-09-23. First, NVIDIA NIM for embeddings and images. Second, **Groq for writing and browsing research**, with a free-model **writer fallback chain** (OpenRouter, ModelScope, ZenMux, NVIDIA) and a number-repair step. |
| Tests | 638 passed, 11 skipped (the skips are the valid button/status combinations in the stale-callback matrix). No network is used. |
| Lint | Clean under `ruff check --select E,F,I,B,UP,SIM --ignore E501,SIM108,B008,SIM300`. |
| Live APIs | **Verified live:**<br>- NVIDIA: FLUX, and embeddings (2048-dim).<br>- Groq: chat and browsing (7 grounded facts in 41 s).<br>- Telegram: `getMe` for @linkedinwala_bot.<br>- Supabase: schema applied via MCP.<br>- OpenRouter: free models probed.<br>**Not yet:** LinkedIn, a Telegram end-to-end run, and a full pipeline run against Supabase (needs the service_role key). |
| **Writer validation** (`scripts/validate_writer.py`, 3 briefs × 2 drafts) | **Groq `openai/gpt-oss-120b` with number repair: 6/6 PASS.** Without repair it scored 1/6. The dominant failure for every model was invented story numbers. Earlier results: NVIDIA deepseek-v4.1-flash 0/6, NVIDIA nemotron-3-super 1/6. The OpenRouter fallback `dots-3-note-preview:free` scored **1/6**: number repair worked, but the "X isn't A. It's B" cliché survived in 5/6. Fallback drafts are usable but weaker, and the ⚠ flags and human edit gate still apply. Reports are in `out/validation-*.md`. |
| **Writer decision (made)** | The user said "use whatever you like" and asked for a model with internet browsing. So: Groq gpt-oss-120b writes and browses. The user later asked to "use all free ones" (OpenRouter), and supplied ModelScope and ZenMux keys, so every usable free model sits in the fallback chain. Claude was not re-added. |
| **Supabase** | Schema applied on 2026-09-23 via the Supabase MCP to the existing project **`proofmart`** (ref `<supabase-project-ref>`, `ap-south-1`), at the user's request. It added 5 new tables (RLS on), the 2 RPCs, pgvector 0.8.2 (in `public`) and the public `post-images` bucket. Migration name: `linkedin_bot_schema`. None of proofmart's own tables were touched (no name collisions). `.env` has `SUPABASE_URL`; the **service_role key must still be copied** from Dashboard → Settings → API keys. Advisors: the RLS-no-policy INFO is intended; `function_search_path_mutable` and `extension_in_public` WARNs are open and optional. |
| Deployed | No. |
| Blocking input from the user | (1) **LinkedIn app** (Client ID + Secret; the redirect must be `http://localhost:8765/callback`), then run `scripts/linkedin_auth.py` on the user's Mac. (2) ~~seed posts~~ **DONE 2026-09-23.** `scripts/scrape_linkedin_posts.py https://www.linkedin.com/in/deepanshulathar/` scraped via Bright Data (dataset `gd_lyy3tktm25m4avu764`; the user has a $2 promo credit; cost ≈ $0.01). It found **9 authored posts** (Feb–Aug 2026, all with engagement > 0, so the promotion gate is open), and all 9 are seeded in `past_posts` as source='human'.
  - Confirmed field mapping: `post_text`, `date_posted`, `num_likes`, `num_comments`, `post_type`.
  - The posts use 𝐛𝐨𝐥𝐝 Unicode styling. The Embedder applies NFKC only for embeddings; the stored text keeps the styling.
  - Retrieval was verified against a real brief.
  - **More posts (the user wants 30–40):** Bright Data can only reach 9 authored posts (Feb–Aug 2026), even with start_date 2015. LinkedIn only shows recent activity to logged-out viewers. The profile says 37 posts, and the 23 Feb post mentions "Day 30 of my 30-day writing challenge", so ~30 older posts exist.
    - The route is the user's **LinkedIn data export** (Settings → Data privacy → Get a copy of your data → Posts), then `scripts/import_linkedin_export.py <zip|Shares.csv> [--enrich]`, then `seed_past_posts.py`.
    - `--enrich` fetches likes/comments by ShareLink via Bright Data collect-by-URL.
    - Activity-feed scrapes (`--include-reposts`) contain OTHER people's posts. The scraper keeps only the profile owner's (`user_id` == profile slug), and the importer never adds scraped posts that aren't in the export.
(3) ~~current_role~~ set from the user's own headline/About/master profile (2026-09-23). Done on 2026-09-23: `SUPABASE_SERVICE_KEY` (verified: REST RPCs work with 2048-dim vectors); `MY_CHAT_ID=<MY_CHAT_ID>` (@contactfornoreason, from /start via getUpdates); `LINKEDIN_API_VERSION=202601`; `TAVILY_API_KEY` (dev key, verified: 8 results in 3 s, news topic works; now the browsing fallback and the 2 PM topic source). (4) Optional: link the ModelScope account to Alibaba Cloud; fix the ZenMux key (403 payg); add a Tavily key as a research backstop. |

## 2. What the system does

1. The user sends a topic brief to a Telegram bot.
2. The pipeline researches it (Tavily), outlines it (NVIDIA NIM chat model), then writes two drafts: **A = story / founder-POV** and **B = contrarian / insight-list**. Each draft gets a de-AI pass, a mechanical number check, and one image (FLUX.1-dev on NVIDIA → 1080×1350 PNG).
3. The bot sends the preview with buttons.
4. After **two human gates** (pick a draft, then pick a time), the row is `queued`.
5. `dispatcher.py` (cron, every 5 min) posts due rows to the LinkedIn Posts API.

The style corpus (`past_posts`) only learns from:
- human-seeded posts;
- drafts the user edited;
- AI drafts that later beat the median human engagement.

## 3. Amendments (they override SPEC)

- **A1.** Before `POST /rest/posts`, backslash-escape `\ | { } @ [ ] ( ) < > # * _ ~` in the post text. A `#word` becomes `{hashtag|\#|word}` so it stays clickable. Implemented in `app/linkedin.py::escape_little_text` and applied inside `build_post_payload`, so no code path can skip it.
- **A2.** The "may be live" alert has two buttons: `Live ✓` (`live:{id}`) and `Not live` (`notlive:{id}`).
  - **Live ✓** sets `chat_state(awaiting_post_url)`. When the user pastes the URL, the row becomes `posted` (with `posted_at = now`) and corpus admission runs.
  - **Not live** sets `queued`, `retry_count = 0`, `claimed_at = null`, `alerted_at = null`.
- **A3.** `save_edit` applies only while `status = 'awaiting_choice'`. Otherwise the bot replies "Draft already locked" and clears the chat state.
- **A4.** `parse_mode` is never set. Everything is sent as plain text.
- **A5.** Two SPEC §1 cross-references are wrong. The number check is §5.1 step 8, and the promotion gate is §5.3.

## 4. Stack and versions (installed in `.venv`, Python 3.11)

python-telegram-bot 21.11.1 · supabase 2.31.0 (postgrest/storage3 2.31) · tavily-python 0.8.4 · httpx 0.28.1 · Pillow 12.3 · pydantic-settings 2.15 · pytest 9.1 + pytest-asyncio 1.4 (`asyncio_mode = auto`) · ruff (dev only). **No `openai` or `anthropic` packages** (a test enforces this).

**Model providers** (all over plain httpx; shared retry and parsing in `app/apiclient.py`):

- **Groq** (`GROQ_API_KEY`): primary writer plus browsing research, with `openai/gpt-oss-120b` for both.
  - Free-tier limits: 8,000 tokens/min, so calls are serialised and 429s waited out, and 200,000 tokens/day.
  - A browsing call uses about 30–50k tokens, which allows ~3–4 runs a day.
  - A 429 whose retry-after exceeds 30 s is a quota; it fails fast so the fallback chain takes over.
- **Writer fallback chain** (`WRITER_FALLBACKS`, default in `app/config.py`), in this order:
  - OpenRouter `dots-3-note-preview` → `nemotron-3-super` → `nex-n2.5-pro` (these three probed clean with reasoning disabled);
  - ModelScope `DeepSeek-V4-Pro`, `Qwen3.5-397B`, `MiniMax-M3`, `GLM-5.2` (401 until the account is linked to Alibaba Cloud);
  - ZenMux `dots3-note-prev`, `glm-4.7-flash-free`, `agnes-2.5-flash`, `atria-dawn-preview` (403 "api_key_source: payg" for the current key);
  - the 429-prone OpenRouter models: `qwen3.8`, `gemma-4`, `glm-5.2`, `nemotron-3-ultra`, `openrouter/free`;
  - NVIDIA `nemotron-3-super` last.

  A link is skipped on any error, 429, 401/403, empty reply or cut-off reply. Providers without a key are left out. OpenRouter's free tier without credits allows ~50 requests/day.
- **NVIDIA NIM** (`NVIDIA_API_KEY`): embeddings and FLUX images, plus the optional writer.
- **Tavily**: optional. It's the research fallback when fewer than 3 facts are grounded, and the 2 PM news-topic source when set.

| Use | Model / endpoint | Notes |
|---|---|---|
| Chat (outline, drafts, de-AI, voice) | `NVIDIA_CHAT_MODEL`, default `deepseek-ai/deepseek-v4.1-flash` via `integrate.api.nvidia.com/v1/chat/completions` | Drafts are sent with temperature 0.85; the other calls use the model's default. `max_tokens` = `NVIDIA_MAX_TOKENS` (4096). `<think>` blocks are stripped. `finish_reason=length` raises. |
| Embeddings | `NVIDIA_EMBED_MODEL`, default `nvidia/nemotron-3-embed-1b` via `/v1/embeddings` | **2048-dim**, so the schema is `vector(2048)`. `input_type` is `query` for briefs and `passage` for posts. `truncate` is END. Batches of 16. |
| Images | FLUX.1-dev via `ai.api.nvidia.com/v1/genai/black-forest-labs/flux.1-dev` | The body is exactly `{"prompt": …}`. The response's `artifacts[0].base64` is a JPEG (square). It's cropped to 4:5, giving a 1080×1350 PNG. |

**Reasoning must stay off.** Set `NVIDIA_DISABLE_THINKING=true` (the default). It sends `chat_template_kwargs={"thinking": false, "enable_thinking": false}`. With reasoning on:
- deepseek de-AI calls took more than 400 s;
- nemotron burned all 4096 tokens;
- the `/no_think` prompt trick leaked untagged reasoning into drafts.

With it off: about 35 s per call on deepseek, about 2 s on nemotron.

The client retries 429, 500, 502, 503 and 504, plus connection errors, up to 3 attempts, honouring `Retry-After` (capped at 30 s). Read timeouts and 4xx errors are not retried.

**Live probe with the user's key (2026-09-23):**
- **Works:** chat `deepseek-ai/deepseek-v4.1-flash` and `nvidia/nemotron-3-super-120b-a12b`; embeddings `nvidia/nemotron-3-embed-1b` and `nvidia/llama-nemotron-embed-vl-1b-v2` (both 2048-dim); FLUX (returns JPEG).
- **404 for this account** (even though listed in the catalog): `nv-embedqa-mistral-7b-v2` (4096-dim), `llama-3.2-nv-embedqa-1b-v1`, `embed-qa-4`, `arctic-embed-l`, `mistral-large`, `mistral-large-2-instruct`.
- **Timed out after 120 s:** `mistral-nemotron`, `kimi-k3`, `glm-5.3`.
- **Not in the catalog at all:** `deepseek-v4-flash`, `mistral-large-3`, `nv-embed-v2`. That's why the originally planned 4096-dim schema became 2048.

**Rollback:** a snapshot of the pre-migration (Claude + OpenAI) code is in the session scratchpad at `…/scratchpad/pre-nvidia-snapshot.tgz`. It's temporary; copy it somewhere permanent if it's needed.

## 5. File map

```
app/config.py        Settings (pydantic-settings, .env). Secrets are SecretStr. LINKEDIN_API_VERSION must match ^\d{6}$.
                     NVIDIA_API_KEY, NVIDIA_CHAT_MODEL, NVIDIA_EMBED_MODEL, NVIDIA_MAX_TOKENS, DRAFT_TEMPERATURE.
app/apiclient.py     ApiClient base: _post (Bearer auth, timeouts, retry on 429/5xx/connect errors, fail fast on quota 429s), _parse_chat, ChatResult, strip_think.
app/nvidia.py        NvidiaClient(ApiClient): chat(), embed(input_type), flux(); THINKING_OFF chat_template_kwargs.
app/groq.py          GroqClient(ApiClient): chat() and browse() (browser_search); page_evidence(executed_tools) gives the raw page text.
                     Serialised by a semaphore; max_attempts 6.
app/openrouter.py    CompatChatClient, plus OpenRouterClient / ModelScopeClient / ZenMuxClient (reasoning off, max_attempts 1).
                     FallbackChat(ChainLink…) with last_served; parse_chain('provider:model,…').
app/log.py           JSON-line logging to stdout. register_secret() and redaction run on every line.
                     httpx and telegram loggers are raised to WARNING (Telegram URLs contain the bot token).
app/db.py            Database class: every query and RPC, plus Storage upload. EMBED_DIM = 2048 (matches schema).
                     Re-embed helpers: past_posts_missing_embedding / posts_missing_topic_embedding / set_*_embedding. Pydantic models Post, PastPost, ChatState, SimilarTopic.
                     Transitions are conditional UPDATEs (update_post(..., status=, require_chosen=), claim_post, mark_alerted).
app/timeutil.py      IST helpers: next_9am/6pm (strictly after now), custom_time (a past time rolls to tomorrow), ist_day_start, parse_ts.
app/verify.py        normalize() → extract_numbers() → unverified_numbers(draft, corpus).
app/research.py      BrowsingResearcher (Groq browse, then ground_facts: a fact is kept only if its numbers are on the cited page; <3 facts → Tavily top-up;
                     news_topic via Tavily or browsing). Researcher (Tavily: "brief statistics YEAR", advanced depth, top 8). ResearchSource protocol.
                     Also format_research and research_corpus (used as the verification source; includes the brief).
app/writer.py        Loads the prompts in app/prompts/*.txt. render() fills only known {keys}. Outline, draft, deai, extract_voice.
                     Takes any ChatClient (protocol), so swapping the writing provider back is a small change.
app/images.py        ImageMaker → NvidiaClient.flux (JPEG) → crop_to_4x5_png (1080x1350 PNG). sniff_image_type (magic bytes).
app/style_check.py   check_draft(): hard = clichés / "it's not X, it's Y" / >3 hashtags / emoji bullets; soft = length, CTA, question hook, 'landscape'.
app/services.py      Embedder (embed_query / embed_passage(s) / embed_queries, 2048-dim guard); build_nvidia / build_groq / build_openrouter /
                     build_modelscope / build_zenmux; writer_links (primary + fallbacks, skipping missing keys); build_writer; build_researcher.
app/pipeline.py      generate (steps 1–9, no writes), run_brief (+ upload, insert, preview), regenerate (steps 6–11 on the same row).
                     preview(). CLI: python -m app.pipeline "brief" (reads the DB, writes nothing, saves images to ./out/).
app/corpus.py        should_admit_on_post, admit_on_post, promotion_gate_open (≥5), beats_median (strictly greater), record_stats.
app/linkedin.py      escape_little_text, post_url_from_restli_id, looks_like_post_url, build_post_payload.
                     LinkedInClient (userinfo_sub, init_upload, put_image, create_post).
                     Exceptions: ImageUploadError, PostRejected, PostMaybeLive, AuthLookupError.
app/telegram_ui.py   build/parse_callback, guard_allows, the draft/time/stuck keyboards, the Messenger protocol,
                     TelegramMessenger (PTB), telegram_request (timeouts), send_preview, split_text.
app/prompts/         voice_extraction, outline, draft_system, draft_user, deai, image (verbatim from SPEC §8), plus research, news_topic, number_repair (new).
bot.py               BotHandler (single entry point on_update), cron modes nudge/fallback/token_check, run_polling, main.
dispatcher.py        Dispatcher dataclass (run, resolve_author, check_stuck, publish, dry_publish, succeed, fail_retryable,
                     maybe_live), LogMessenger (dry run), amain, main.
scripts/schema.sql   SPEC §4 verbatim. The only change is 'awaiting_post_url' added to the chat_state comment (no CHECK constraint).
scripts/storage.sql  Creates the public 'post-images' bucket.
scripts/linkedin_auth.py    Local OAuth on localhost:8765 with a CSRF state check. Writes 3 settings keys. Never prints the token.
scripts/seed_past_posts.py  JSONL {text, posted_at, likes, comments} → embed → past_posts source='human'.
                            Idempotent (skips exact duplicate text).
scripts/extract_voice.py    §8.1 over the human posts. Asks for current_role (Enter keeps the existing one) → upsert voice_profile.
scripts/validate_writer.py  Writer quality gate. Needs only the writer's key; --provider groq|openrouter|modelscope|zenmux|nvidia, --no-repair. Runs 3 fixed briefs → outline → A+B → de-AI
                            and style_check before and after the de-AI pass, plus the number check. Writes out/validation-*.md.
                            Exit code 1 on any hard failure. --model to compare models, --voice for a real voice_profile.
scripts/migrate_embeddings_2048.sql  One-time, for databases on the old 1536 schema: drop the RPCs,
                                     alter both columns to vector(2048) USING NULL, recreate the RPCs.
scripts/reembed_corpus.py   After the migration: fills NULL vectors (posts as passages, briefs as queries).
                            Idempotent, 16 texts per request, 2 s pause. --dry-run counts what's missing.
deploy/crontab.txt   CRON_TZ=Asia/Kolkata. The dispatcher line uses flock -n. Paths assume /opt/linkedin-bot.
deploy/bot.service   systemd unit, user linkedinbot, hardened (ProtectSystem=strict etc.).
deploy/logrotate.conf
seed_posts.example.jsonl, .env.example, requirements.txt, requirements-dev.txt, pytest.ini, .gitignore
```

## 5b. Usage dashboard (added 2026-09-23)

- **Ledger:** `bot_api_calls`, created by `scripts/usage.sql` and applied to proofmart as the migration `linkedin_bot_usage_ledger`. It's named so it doesn't collide with proofmart's own `api_usage_events` table.
  - `ApiClient._post` records **every HTTP attempt**, including retries, 429s and exceptions. Each row has the provider, endpoint, model, status, tokens and `x-ratelimit-*` header values.
  - Tavily's `Researcher._search` records credits (advanced = 2, basic = 1).
  - `UsageRecorder` writes rows in the background and never raises. Short-lived processes call `drain()` before exiting.
  - The RPC `bot_resource_usage()` is SECURITY DEFINER and callable only by service_role. It returns DB bytes, storage bytes and object count, and row counts.
- **Collector:** `app/usage.collect()` builds 9 cards server-side. The live sources are OpenRouter `/api/v1/key` (`free_model_daily_requests`), Tavily `/usage`, Supabase (the RPC above), Telegram `getMe`, and LinkedIn token age from `settings`. Everything else comes from the ledger or Groq's headers.
  - Free-tier constants live at the top of `app/usage.py`: Groq 200k tokens/day, NVIDIA ~1000 credits, Supabase 500 MB DB and 1 GB storage, LinkedIn 60 days.
- **Server:** `scripts/usage_dashboard.py` uses only the standard library and binds to `127.0.0.1:8787`. It caches for 30 s. Routes: `/` for the page, `/api/usage` (`?refresh=1` forces a refresh). `--once` prints the JSON.
- Tests (`tests/test_usage.py`) assert that no secret appears in the JSON and that every live check can fail without breaking the page.

## 5c. About-me background facts + style rules (2026-09-23)

- **The primary source is the user's site, deepanshulathar.com**: /about, /experience, /work/*, /resume, /archive. The `.dev` link on LinkedIn is wrong and returned HTTP 436.
- Secondary sources: the Bright Data LinkedIn profile (`out/linkedin_profile_raw.json`) and the user's 9 posts.
- `out/about_me.json` / `.md` hold 15 sourced facts plus 3 **style rules the user states himself**:
  - no bold-Unicode (he ports posts to real emphasis for screen readers and search);
  - say when a number is self-reported, internal or unshipped;
  - honest status words over hype.
- `out/voice_extra.json` holds 3 long-form samples (/about and the two case studies, ~1.4k words). They're used only by `extract_voice.py` for analysis, and are **not** added to the `past_posts` corpus.
- `extract_voice.py` merges `background`, `style_rules` and the extra samples into `voice_profile`. `writer.clean_post()` → `unbold()` mechanically folds the Mathematical Alphanumeric block (U+1D400–1D7FF) to plain text in every model output.
- Key facts:
  - Full-stack & AI engineer; third-year CSE at LPU.
  - PicaPool Founder's Office since Jul '25: attribution system, 15+ internal tools in 8 weeks over a 79-table Supabase schema.
  - UniLyf Growth & Marketing Intern (Jan–May '26).
  - Building ProofMart (document forensics, ~70% built). **The bot's Supabase tables live in the ProofMart project; consider moving them to a separate project later.**
  - Gapl (Groq → OpenAI → Gemini failover), College-CLI.
- The /writing articles are still "ported text pending", so there's no extra post text there.
- current_role is still to be **confirmed by the user**. Proposed from his own site headline, not guessed.

## 5d. Voice profile built (2026-09-23)

- **Corpus: 20 human posts in `past_posts`.** 9 came from the Bright Data scrape; 11 more (challenge Days 14–23 plus the automation-script post) came from the user's pasted signed-in feed. The feed was parsed from `scratchpad/feed_paste.txt` into `out/linkedin_feed_posts.json`, which includes impressions (not stored in the DB). Day-N dates were derived as 23 Feb 2026 − (30−N) days, which matched the scraped dates for Days 24–30.
- **`voice_profile` is saved.** The model extraction was served by the fallback chain (the prompt exceeds Groq's 8k tokens/min). The profile contains:
  - `current_role` = "Full-stack & AI engineer in PicaPool's Founder's Office (Building PicaPool); third-year B.Tech CSE at LPU; building ProofMart". This is the user's own wording from his LinkedIn headline, About section and master profile. Re-run `extract_voice.py` to change it.
  - `author_voice_notes` (master profile §10.2), `background` (22 facts), `style_rules`, `claim_rules`.
  - `hashtag_use` manually aligned to at most 3 (the bot's rule; his old posts used 5–8).
- **Sources used:**
  - `/Users/lathar/Documents/deepanshu-lathar-master-profile-v2.md` (highest precedence);
  - `Deepanshu_Lathar_CV_Final (1).pdf` (only non-conflicting facts; the conflicts are listed in `out/about_me.md`);
  - deepanshulathar.com;
  - the LinkedIn About section, which was added to `out/voice_extra.json`.
- **Private data** (phone, email, registration number, CGPA, school percentages) is deliberately excluded everywhere.
- **Settings:** `UNBOLD_DRAFTS` defaults to false. Bold-Unicode is allowed on LinkedIn per master profile §10.3; I first over-applied the website rule and reversed it.
- **Background facts count as number sources:** they're stored in `posts.research.background`, so "15+ tools in 8 weeks" isn't flagged as invented. Claim rules deliberately contain no forbidden figures.
- **Current state:** the sample-voice test bot was stopped. Its 2 test drafts were set to `failed` ("test run — never post"). The **real `bot.py` is running locally** (log: `scratchpad/bot.log`) with the real voice. The dispatcher and cron are not running, and LinkedIn isn't connected, so nothing can post.

## 5e. LinkedIn connected + first real run hardening (2026-09-23)

- LinkedIn OAuth works. The app has the products "Share on LinkedIn" and "Sign In with LinkedIn using OpenID Connect". The token, issue date and author URN `urn:li:person:<member-id>` are in `settings`, valid ~59 days from 2026-09-23 (re-auth by ~2026-11-20). `scripts/linkedin_auth.py` prints `auth_hint()` for setup errors.
- The first real run crashed on FLUX `CONTENT_FILTERED`. Images now go hook → neutral prompt → local text card (`app/images.py`, `text_card_png`/`card_text`). The card uses a system TTF, because Pillow's default font has no em dash.
- Groq per-minute 429s came from bold-Unicode few-shot examples (~7× tokens). They are now folded to plain text in the prompt. Per-minute 429s wait ≤65 s; daily-quota 429s fail fast to the fallback chain (`app/apiclient.py`).
- `ruff.toml` holds the lint rules (E,F,I,B,UP,SIM; ignore E501,SIM108,B008,SIM300,E402), so plain `.venv/bin/ruff check .` is the lint command.
- Tests: 676 passed, 11 skipped.

## 5f. Chat mode, links, Bright Data, Jev, NewsData, single image (2026-09-23)

- **Chat:** `app/chat.py` (regex request parsing, `ChatMemory`, you-pick/decline), `Writer.converse` + `prompts/chat_system.txt` (returns JSON `{reply, draft_topic}`), and bot routing in `BotHandler.on_text`/`chat_flow`/`save_brief`. New chat state `awaiting_brief`, also set by `--nudge`. HELP text rewritten.
- **Links:** `app/linkread.py` `LinkReader` (public URLs only, article extraction, 4k chars into the brief). `pipeline.generate` puts the page first in research as source [1] and adds `LINK_NOTE`; the stored brief is the user's original text.
- **Bright Data:** `app/brightdata.py` (sync `/scrape` → 202 snapshot polling, daily cap from the ledger, datasets chatgpt_search `gd_m7aof0k82r803d5bjm`, x_posts `gd_lwxkxvnf1cynvib9co`, linkedin_posts), `answer_facts` (maps `[n]` markers to `links_attached` positions), `research.FreshSearch` running in parallel with browsing. A live run took ~65 s and used 1 record; 1 of 6 facts was grounded (strict).
- **Jev:** `app/jev.py`, POST `https://www.jevai.org/api/v1/decisions`, body `{model:"typesafe-ai/jev", state, questions:{wants_drafts:{type:"noul", instructions}}}` → `data.answers.wants_drafts.noul`. The "choice" type needs an option format the docs don't show (400 "provide 2–255 choice options"); unresolved and unused. Free tier returns 429 after a few calls.
- **NewsData:** `app/news.py` (`NewsData`, `NewsFirst` wrapper around the researcher). `NICHE_KEYWORDS` was empty before today, so the news fallback never worked until now.
- **Images:** `Writer.image_scene` + `prompts/image_scene.txt`; `prompts/image.txt` is now a style wrapper around `{scene}`. FLUX body now includes mode/width/height/steps/cfg (1088×1344, 50 steps, ~16 s).
- **Copy guard:** `verify.copied_ratio` (8-word shingles, MAX_COPIED 0.2), `pipeline.own_words_draft` (one rewrite), `copied_for` → ⚠ in `draft_message`.
- **Dashboard:** Bright Data (lifetime/5,000 + 24 h/cap), Jev and NewsData cards.
- **Groq:** the daily 200k TPD was exhausted by today's testing (~12:00 IST), so the writer ran on OpenRouter dots-3 for the link run. A normal run is ~25–35k tokens, so about 6 runs/day fit on Groq alone.
- **Live posts in Telegram:** `fd8899d6` (bot brief) and `11fd4455` (the Jev link). **11fd4455's Draft B is a verbatim copy of the article**, made before the guard existed; don't pick it, tap Regenerate.
- Tests: 744 passed, 11 skipped. The bot was restarted with all of this.

## 5g. Copywriting, length, buttons, commands, /dashboard (2026-09-23)

- **Research-based rules:** `app/prompts/copy_playbook.txt` (six hook frameworks from samber/cc-skills' linkedin-ghostwriting; PAS/BAB structures; ≤2 stats; 1–2 line paragraphs; one easy closing question). It goes into draft_system, deai (hook kept word for word) and tune. The outline adds metric / counter_intuitive / mechanism / cta_question.
- **Length:** his posts have a median of 1,078 chars (best 924–1,318). Target 900–1,400; tighten above HARD_MAX 1,600; hook ≤140 (the mobile "see more" cut). Constants live in `app/style_check.py` (`CHAR_MIN/CHAR_MAX/HARD_MAX/HOOK_MAX/DENSE_PARAGRAPH`), along with `airy()` (deterministic sentence-boundary split; hook group ≤140) and `post_hook()`. `pipeline.fit_shape` = step 7c. A hook edit must keep ≥80% of the text after the hook (`KEEP_REST`).
- **Buttons:** `telegram_ui` has new actions `tune:<a|b>-<short|hook|bold>`, `img`, `drop`, `show`, `unq`, `asap`, plus keyboards `draft_tools` (under each draft), `draft_keyboard` (footer), `queued_keyboard` and `waiting_keyboard`. `send_draft()` attaches the tools to the last chunk. `/ideas` buttons use the separate `idea:<n>` namespace (in-memory `BotHandler.ideas`).
- **Bot:** `on_command` handles QUICK_COMMANDS before chat state; `MENU`/`MENU_COMMANDS` is the persistent ReplyKeyboard (`Messenger.send_menu`); `BOT_COMMANDS` go to setMyCommands in post_init. `_tune` and `_new_image` run under the `regenerating` lock. `list_posts` uses `Database.posts_with_status`.
- **/dashboard:** `usage.cards_text()` formats `collect()` for Telegram (status icons; used / limit · left; bytes shown as MB; split under 3,800 chars). NewsData now has its own card (credits in the last 24 h out of 200).
- Tests: 789 passed. The bot was restarted and `getMyCommands` confirms the 8 commands.

## 5h. Truth, formatting, polish, paid Groq failover (2026-09-23)

- **Live runs showed** invented personal scenes, markdown (`**x**`, `1️⃣`), 5–7 hashtags, bracketed citations, stacked stats and "it's not X, it's Y". All came from free fallback writers while Groq's free daily quota was exhausted.
- **Fixes:**
  - TRUTH rule in the playbook, outline and de-AI prompts.
  - `writer.linkedin_format` inside `clean_post`: markdown bold → Unicode bold, italics → plain, keycaps → "1.", emoji lists → "- ", "(Source, 2025)" → ", per Source", hashtags capped at 3. It never touches human edits.
  - Step 7d `pipeline.polish` (lint → one `Writer.fix`; `style_check.fixable_issues`, `stat_count`, MAX_STATS 2). `fit_shape` retries "short" once.
- **Validation now runs 7b/7d/7c:** `groq:openai/gpt-oss-20b` 5/6; `qwen/qwen3.8-27b` 3/6 (before 7d). Qwen needs `reasoning_effort: "none"` (`groq.reasoning_effort_for`).
- **GROQ_API_KEY_2 is the user's PAID account** (Developer plan, pay per token). The chain is groq 120b (free) → groq2 120b (paid) → groq gpt-oss-20b → groq qwen → OpenRouter → ….
  - `app/spend.py` `SpendCap` wraps groq2 (chat + browse). Today's spend comes from the ledger (tokens × $0.15/$0.60 per M). At `GROQ2_DAILY_USD` (0.25) it raises ApiError, so the chain moves on.
  - `research.FailoverBrowser` covers groq → groq2.
  - The dashboard card "Groq (paid account)" shows USD.
  - Prompts put the per-call parts last (draft role, tune instruction) so Groq's prompt cache (half-price input) hits across the A/B calls.
- Tests: 811 passed.

## 5i. Truth layers + resilience (2026-09-23, end of day)

- **Story mode** (`is_own_story`: first person → 2 research facts, `research.mode="story"`); `with_own_hook`/`brief_hook`.
- **`grounded_outline`**: outline audit → one retry with `OUTLINE_NOTE` → flagged hooks dropped.
- **`polish`**: 2 rounds; claim audit, fix accepted when the flagged sentences are gone (`_present`).
- **`fixable_issues(keep_numbers=True)`**: brief numbers must appear; `verify._same_value_other_form` treats 0.92 and 92% as equal.
- **Final `audit_flags`**: stored in `research.claim_flags`, shown as "⚠ check" lines (`claim_flags_for`; none for human edits; `_tune` re-audits).
- **`pipeline.optional`**: retries enrichment reads once, then continues.
- **Live result on the copy-check brief:** his hook, his numbers, no false framing, 3 leftover lines flagged ⚠. $0.011 per run on the paid key; $0.09 for all testing today.
- **Many test previews sit in his Telegram** (posts fd8899d6, 11fd4455, b956b297, f7b16920, 05a958d1, d0be19a6, a5a48cde, 7291fb81, ec8dad0d, 4e2f717b). He can clear them with /drafts → 🗑.
- Tests: 829 passed.

## 5j. ONE post per run + all research sources (2026-09-23, late)

- The user asked for "a single post, well researched, using all the APIs, not two".
  - `write_drafts` returns `{"a": post, "b": ""}`, via `final_post` (`Writer.final`, `prompts/final.txt`).
  - The UI shows "YOUR POST" with `FOOTER`; `draft_tools(single=True)` and `draft_keyboard(two=False)`.
  - Picking an empty draft is refused (bot + dispatcher).
- Research: `BrowsingResearcher` runs browse, fresh (Bright Data) and `_search_facts` (Tavily) in parallel, merged by `_interleave`. `NewsFirst.research` adds `NewsData.context` (`key_terms`, a relevance filter). `tidy_snippet` and `MIN_SCORE` clean Tavily.
- `writing_voice` (no background for general topics), `ROLE_A_TAKE`, `sourced_statistic`/`could_be_about_him` flag filters, `rewrite_without` (COHERENT_SHARE 0.7).
- `LinkReader._fetch` streaming + `TOTAL_SECONDS`; `upload_with_retry`.
- Tests: 844 passed.

## 5k. Final version: profile, human text, designed cover (2026-09-23, night)

- `out/profile_card.md` is stored in `voice_profile.profile` and shown to the writer as WHO HE IS. `writing_voice` drops the raw `background`, which stays in research for the audits (`audit_facts` adds the CLAIM RULEs).
- `dehumanize_tells` (dashes, U+2011/U+202F, all emoji) and the extended `_BANNED` list; sarcasm level 1–2 in the playbook; no dashes in any prompt file.
- `app/cover.py` (`photo_cover`, `type_cover`, `clean_headline`); `Writer.cover_headline`; `Writer.image_scene` returns [scene, simple]; `images.safe_scene`.
- `style_check.invented_details` + `pipeline.invented_lines` feed polish issues and the flags. `EDIT_TEMPERATURE` 0.2.
- **Live result on "dashboards count downloads":** truthful and on his work, but thin (686 chars) because the writer (gpt-oss-120b) invents heavily and the checks strip it (15 issues in round 1). Recommended next: a stronger writer model for the final editor, and/or a facts-first "allowed claims" writing step.
- Tests: 867 passed.

## 5l. Signal from sergebulaev/linkedin-skills + posting status (2026-09-23, late night)

- **First real LinkedIn post published:** https://www.linkedin.com/feed/update/urn:li:share:7508569242183757824 (post 640f07a9, via `dispatcher.py` run once on his "post it"). The dispatcher is NOT scheduled; `/status` shows the publisher state from the `dispatcher_last_run` heartbeat.
- He discarded all 17 unposted test drafts. The voice corpus (20 human posts) is untouched.
- **Added:**
  - `/status` board (and at the top of `/dashboard`) plus the web "Posts & publishing" card;
  - in-place button updates: spent buttons cleared, dead buttons explain themselves, more time options, every tap logged;
  - `/interview`, `/bank` (`app/storybank.py`), `/comment` (`prompts/comment.txt`);
  - the repo's AI-tell rules (calibrated), 0-2 hashtags, link stripping, crowding warning, time hint, after-post tips.
- Tests: 907 passed.

## 6. Key flows (as implemented)

### Bot text routing (`BotHandler.on_text`)

The order is: `/cancel` → chat state (`awaiting_edit_a/b`, `awaiting_custom_time`, `awaiting_stats`, `awaiting_post_url`) → `/stats` → `/start` or `/help` → any other `/…` gets "Unknown command" → a brief.

- A brief must be 10–800 characters.
- An accepted brief gets the reply "On it…", and `run_brief` runs as a background task (`self.spawn`).
- Every chat-state read filters on `expires_at > now`. The TTL is 30 minutes.

### Callbacks (`on_callback`)

1. Parse the callback data (strict; malformed data → "Expired").
2. Load the post and check it belongs to this chat.
3. Run the status guard.
4. Check the regen lock: pick, time, edit and regen are blocked while a regen is running.
5. Route the callback. `answerCallbackQuery` is always called exactly once (in a `finally`).

Guards:

| Buttons | Allowed only when |
|---|---|
| pick, regen, edit | `status = awaiting_choice` |
| time | `status = awaiting_choice` and `chosen` is set |
| live, notlive | `status = posting` |

callback_data is at most 48 bytes (`time:custom:` + a UUID), under Telegram's 64-byte limit.

### Actions

- **pick** sets `chosen` and shows the time keyboard.
- **time now / 9am / 6pm** calls `queue()`: a conditional update to `queued`, then "Queued ✓ Draft X → <IST time>". "Now" means now + 2 minutes.
- **time custom** sets the `awaiting_custom_time` state. The reply is parsed as `HH:MM`, with `H:MM` or `.` also accepted.
- **edit** sets the `awaiting_edit_x` state. `save_edit` rejects:
  - a locked post ("Draft already locked");
  - a regen in flight;
  - text under 20 characters;
  - text over 3000 characters once escaped.

  On success it sets `draft_x`, `edited_x = true` and `chosen = null`, then resends the preview.
- **regen** is added to the `regenerating` set, then `regenerate()` reuses `research.outline`, resets `edited_a`, `edited_b` and `chosen`, and uploads fresh image names. If the row was locked in the meantime, the new drafts are discarded.
- **live** and **notlive** implement A2.

### `/stats`

- Lists the last 5 `posted` rows. `chat_state.post_id` stores the **newest listed row** as an anchor.
- The reply must have one `likes comments` line per post (`-` skips a post). It is mapped to the posts where `posted_at <= anchor.posted_at`, so a newer post can't shift the lines.
- Each post then goes through `record_stats`, which returns `updated`, `promoted`, `gate_closed` or `below_median`.

### Cron modes

- **`--nudge`** sends "What's today's topic?".
- **`--fallback`** is skipped if any post was created since IST midnight. Otherwise:
  - NICHE_KEYWORDS rotate by IST date ordinal;
  - Tavily news for the past week, top result becomes the brief;
  - `run_brief` produces drafts only.
- **`--token-check`** warns when the token is missing or more than 50 days old, and includes the re-auth steps.

### Pipeline (`generate`)

1. Load the voice profile first, so the run fails before any paid call.
2. Embed the brief.
3. Run in parallel: `find_similar_topic`, research, and `match_past_posts(k=3)`.
4. Outline.
5. Two drafts in parallel, then two de-AI passes in parallel.
6. Two images in parallel.

`run_brief` then:
1. generates a UUID in Python;
2. uploads the images to `{post_id}/{a|b}-{rand}.png`;
3. inserts the row with `status = awaiting_choice`, `topic` = the outline insight, `template` = the sub-template, `research = {query, results, outline}` and `topic_embedding`;
4. sends the preview: photo A, photo B, draft A (+⚠), draft B (+⚠), then "Pick a draft:" with the keyboard.

The unverified-number warnings are recomputed from the stored research each time a preview is sent; they are not stored.

### Dispatcher (`Dispatcher.run`)

1. **Resolve the author.** Read `settings.linkedin_author_urn`. Call `userinfo` only if it's missing, and never in a dry run.
2. **Check stuck rows.** Rows in `posting` with `claimed_at` more than 15 minutes ago and no alert yet: `mark_alerted`, then send the alert with the A2 buttons.
3. **Process due rows** (at most 5, ordered by `scheduled_at`):
   1. Claim the row (`queued` → `posting`, conditional).
   2. If there is no token or author URN, go to `fail_retryable`.
   3. Otherwise `publish`: download the image, check its magic bytes, `init_upload`, PUT the image, `create_post`, build the URL from `x-restli-id`.

Outcomes:

| Outcome | Result |
|---|---|
| `ImageUploadError` or `PostRejected` | `fail_retryable`: `attempts = retry_count + 1`; `queued` if under 2, otherwise `failed`; `claimed_at` cleared; alert sent. |
| `PostMaybeLive`, or an unparseable id | `maybe_live`: stays `posting`, `alerted_at = now`, alert with buttons, never retried. |
| Success | `posted`, then corpus admission (errors are logged, never undo the post), then "Posted ✓ url". |
| Unexpected exception | Left in `posting`; the stuck check alerts later. |

How `create_post` errors are classified:
- **Retryable (definitely not sent):** 4xx, `ConnectError`, `ConnectTimeout`, `PoolTimeout`.
- **Maybe live:** read/write timeout, other transport errors, 5xx, a 2xx without `x-restli-id`.

**Dry run** never claims, updates or alerts, and never calls `userinfo`. It downloads the image, builds the payload with `urn:li:image:DRY_RUN`, and logs it.

### Payload

```
author, commentary (escaped), visibility PUBLIC,
distribution {feedDistribution MAIN_FEED, targetEntities [], thirdPartyDistributionChannels []},
content.media {id, title = first line (≤100 chars)}, lifecycleState PUBLISHED, isReshareDisabledByAuthor false.
```

Headers on every `/rest/` call: `LinkedIn-Version`, `X-Restli-Protocol-Version: 2.0.0`, `Authorization: Bearer`.

## 7. Number verification rules (`app/verify.py`)

- Commas are stripped only between digits.
- `$`, `₹`, `Rs`, `INR` and `US$` become a currency marker, so `$5` still counts as a stat.
- Word units map to symbols: percent / per cent / pct → `%`; billion(s)/bn → `B`; million(s)/mn → `M`; thousand(s) → `K`; crore(s) → `Cr`; lakh(s)/lac(s) → `L`.
- `B`, `M`, `K`, `Cr` and `L` are folded into the numeric value, so `₹5 crore` equals `50 million`. `%` and `x` are separate kinds.
- A bare integer from 1 to 10 (no unit, no currency, no decimal) is ignored as a list count.
- Ordinals (`21st`) and alphanumerics (`B2B`, `Web3`) are ignored.
- The source corpus is the research titles and contents **plus the brief**.
- Matching is exact: `47%` vs `47.3%` is flagged.
- Warnings are shown per draft.

## 8. Testing approach (`tests/`)

- **Blocking network access.** `conftest.no_network` (autouse) patches `socket.connect` and `socket.create_connection` to raise.
- **NVIDIA fake (`tests/fakes.py`).** `FakeNvidia` is an `httpx.MockTransport` handler that serves NIM's real chat, embeddings and FLUX JSON shapes. The real `NvidiaClient`, `Writer`, `ImageMaker` and `Embedder` all run in tests. It records `chat_calls`, `embed_calls` and `image_calls`, and has knobs: `finish_reason`, `wrap_think`, `fail_images`, `image_finish`, `embed_dim`. `FakeTavily` fakes the Tavily SDK and `FakeMessenger` records sent messages.
- **`tests/test_nvidia.py`** covers retries and Retry-After, 4xx vs 5xx, think-stripping, embedding request shape and batching, the dimension guard, the re-embed script, the validation-script logic, the style checker, and that no `openai` or `anthropic` import exists.
- **`FakeDB`.** An in-memory copy of `Database` with the same conditional-update semantics. `due_posts` yields (`sleep(0)`) so the double-claim race is real.
- **Real clients over mocks.**
  - `test_db.py` runs the **real** `Database` through supabase-py via `AsyncClientOptions(httpx_client=MockTransport)` and asserts the exact PostgREST and Storage requests.
  - `test_linkedin_client.py` runs the real `LinkedInClient` via `httpx.MockTransport`.
- **End to end.** `test_e2e.py` runs the bot and dispatcher over one DB. It proves nothing posts before both gates, that fallback drafts never post, and that Not live reposts once.
- **Fixtures.** `svc` builds `Services` with fakes. `BotHandler(svc, now=lambda: NOW)` pins the clock. `drain(h)` awaits background tasks.

Run the tests with:

```bash
.venv/bin/python -m pytest -o addopts="" -q
```

## 9. Go-live checklist (the order matters)

1. On the server:
   ```bash
   python3.11 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   cp .env.example .env && chmod 600 .env
   ```
   Then fill in `.env`.
2. In the Supabase SQL editor, run `scripts/schema.sql`, then `scripts/storage.sql`. Put `SUPABASE_URL` and the service_role key in `.env`.
3. Set up the LinkedIn app:
   - Add the products "Share on LinkedIn" and "Sign In with LinkedIn using OpenID Connect".
   - Set the redirect to `http://localhost:8765/callback`.
   - On a laptop, run `python scripts/linkedin_auth.py`.
4. Seed and build the voice:
   ```bash
   .venv/bin/python scripts/seed_past_posts.py seed_posts.jsonl
   .venv/bin/python scripts/extract_voice.py
   ```
   `extract_voice.py` asks for `current_role`.
5. Optional smoke test: `.venv/bin/python -m app.pipeline "brief"` (paid calls, no DB writes).
6. Start the bot:
   ```bash
   sudo cp deploy/bot.service /etc/systemd/system/linkedin-bot.service
   sudo systemctl daemon-reload && sudo systemctl enable --now linkedin-bot
   ```
   On Debian/Ubuntu, also run `sudo timedatectl set-timezone Asia/Kolkata`, because their cron ignores `CRON_TZ`.
7. Dry run:
   - In Telegram: send a brief → Post A → Now.
   - Wait 2 minutes, so the row is due. The dry run only sees due rows.
   - Run `.venv/bin/python dispatcher.py --dry-run` and check the logged payload.
8. Go live with `crontab deploy/crontab.txt`. The queued post goes out on the next 5-minute tick.

## 10. Gotchas and things to remember

- The first contact with each real API happens at go-live. These are the most likely places to need adjustment:
  - LinkedIn payload acceptance for `LINKEDIN_API_VERSION` (currently 202601; versions sunset after about a year);
  - PostgREST passing JSON arrays into `vector(2048)` (the same pattern the Supabase docs use);
  - Telegram fetching the photo from the public Storage URL.
- The LinkedIn token lasts about 60 days, with no refresh token. `--token-check` warns weekly after day 50. Re-run `scripts/linkedin_auth.py`.
- NVIDIA free tier: ~40 req/min per model and ~1000 credits. A run is about 8 requests (a regen about 6). Fine for daily use, not for bulk backfills.
- NVIDIA model availability is **per account** and differs from the public catalog (see §4). Before switching models, probe with a one-line call, or run `scripts/validate_writer.py --model …`.
- 2048 dims is above pgvector's 2000-dim HNSW/IVFFlat limit, so there's no vector index and the RPCs scan sequentially (fine for a small corpus).
- The user pasted their NVIDIA key into the chat on 2026-09-23. It's stored only in the gitignored `.env` (chmod 600). Advise rotating it.
- "Not live" re-queues with the original, already-past `scheduled_at`, so the dispatcher posts it within 5 minutes.
- The Tavily search depth is `advanced` (about 2 credits). Switch to `basic` in `app/research.py` to cut cost.
- Money figures: the SPEC estimates about ₹11–15 per run at medium image quality. `IMAGE_QUALITY=low` brings that to about ₹4–6.

## 11. Possible next steps (not requested yet)

- Run the go-live checklist and fix whatever the first real API calls reveal.
- Optionally add a CHECK constraint on `chat_state.pending_action`. It was kept out to match SPEC §4 exactly.
- Optionally hold Telegram "Posted/failed" alerts in a retry queue if Telegram itself is down. Right now a send failure is only logged.
- If voice quality drifts: re-seed, run `extract_voice.py` again, and review `past_posts` rows with `source='ai'`.
