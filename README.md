# LinkedIn posting bot

A Telegram-controlled LinkedIn posting system for one person, run as an agentic chat. You ask for ideas or a post. The bot researches it and writes one draft plus one image in your voice. You tune or approve it and pick a time, and the scheduler posts it. A post reaches the queue in one of two ways: approved in Telegram with your explicit ✅ or button tap, or reviewed and approved outside Telegram (with Instinct on WhatsApp, say) and enqueued directly via `scripts/enqueue.py`. **Inside Telegram, nothing is published, scheduled, moved or deleted without your explicit ✅ or button tap.**

```
you ─► Telegram ─► bot.py (long polling, chat agent)          dispatcher.py (scheduler, every 5 min)
                    │ ideas (pillars + sourced news)            │ claim queued + due rows
                    │ brief → research → outline → draft        │ → app/publishers/<platform>
                    │ → grounding gate → image → preview        │ → "Posted ✓ <url>" in Telegram
                    │ ✅ / buttons → queue                       │ → platform_post_id + platform_data on the row
                    └──────────── Supabase: posts queue, chat_state, voice_profile, settings, Storage, usage ledger
```

**The loop:**
1. "What should I post about?" gets ideas, each with a pillar tag, a hook and a ✍️ Draft button.
2. Pick one; the bot researches it and sends one draft with one image.
3. Tune it (✂️ 🎣 🔥 ✏️ 🖼) or tap **✅ Post this** and pick a time.
4. The scheduler posts it and you get **Posted ✓**.

Read-only questions ("what's queued", "any drafts", "dashboard", "ideas", "status") are answered at any time.

## Architecture

| Part | What it does |
|---|---|
| `bot.py` | The Telegram bot: chat agent (read-only tools plus proposed actions), inline buttons, confirmations, the generation runs. Also the `--nudge`, `--fallback` and `--token-check` modes |
| `dispatcher.py` | The scheduler. Runs every 5 minutes (`deploy/docker-scheduler.sh`), claims queued rows that are due, and hands each to its platform's publisher. Classifies failures as retryable or "may already be live" |
| `app/publishers/` | The publisher layer. `base.py` defines the `Publisher` interface (`Draft` in, `PublishResult` out) and the error classes `NotPublished` (safe to retry) and `MaybeLive` (a human checks). `linkedin.py` is the reference implementation: image upload, Posts API, little-text escaping. The row gets `platform_post_id` and `platform_data.<platform>` |
| Supabase | The `posts` table is the queue (`awaiting_choice` → `queued` → `posting` → `posted`/`failed`, with conditional updates so two actors can't both win). Also `chat_state`, `voice_profile`, `settings` (tokens, standing rules, last pillar), Storage for images, and `bot_api_calls` for usage |
| `app/pipeline.py` | Generation: research → outline (audited) → two candidates → one final post → de-AI → number repair → polish → shape → P.S. ending → **grounding gate** → image |
| `app/grounding.py` | The gate's deterministic checks: profile lifts, 14-day repeats, blanks, unbacked numbers about you, `[verify]` tags |
| `app/constraints.py` | Rules you state in chat ("keep my personal story out", "no politics"), persisted until you lift them |
| `app/ideas.py` · `app/pillars.py` · `pillars.toml` | Pillar-rotated ideas with sourced news · the weighted rotation · niche and pillar config |
| `app/style_check.py` · `app/writer.py` · `app/prompts/` | Voice rules and cleanup · model calls · every prompt |
| `app/research.py` · `app/news.py` · `app/brightdata.py` | Research with page grounding (Groq browsing, Bright Data, Tavily) · NewsData headlines |
| `app/groq.py` · `app/openrouter.py` · `app/nvidia.py` | Writer and browsing (Groq), the free fallback chain, embeddings and FLUX images (NVIDIA) |

Deployment: two containers from one image (`docker-compose.yml`), `bot` (the Telegram bot) and `scheduler` (the dispatcher loop plus the 14:00 fallback; the 08:00 daily topic nudge was retired on Oct 26 - daily drafting is handled by the Instinct pipeline, which queues reviewed posts directly via `scripts/enqueue.py`, so the bot no longer asks "what's today's topic?" every morning). Secrets reach them through `env_file: .env`, never the image. Logs are in `./logs`.

## Behaviour rules

- **In Telegram, nothing changes without your ✅.** Chat actions (schedule, reschedule, unschedule, discard) and inline buttons alike. The model only *proposes*; the bot asks, and the bot executes. The one path around the buttons is `scripts/enqueue.py`, which queues a post you already reviewed and approved outside Telegram; it never posts directly and never moves or deletes anything.
- **🗑 Discard, ↩️ Unschedule and ⚡ Post now always ask ✅/✖ first**, and so does ⚡ Now in the time picker. The question names the post and its time. ✖ leaves the post and its buttons exactly as they were. (Why: a single stray 🗑 tap on a "Queued ✓" message once killed a scheduled post.)
- **"Post it now" typed in chat is refused.** Posting immediately is button-only.
- **Bulk scope follows your words, not the model's.** "Discard all" means waiting drafts only. Scheduled posts are included only when you say "scheduled", "queued" or "everything".
- **Clarify, don't guess.** If a topic matches several posts, you get a pick list of just those. If it matches none, the bot says so and offers 📝 what's waiting or ✍️ a new draft; it never invents a reference. An unclear destructive scope gets scope buttons, and a vague or missing time gets hour buttons (9 AM first). The bot doesn't over-ask: one obvious candidate goes straight to ✅/✖, and read-only questions never need a clarification.
- **Typed yes/no answers the pending ✅/✖ directly**, without another model turn ("yes", "yep", "do it", "no", "keep it"). A "yes" with nothing pending gets "Yes to what?". A plain "ok" is chat.
- **Filler never answers a question the bot asked.** When the bot is waiting for a detail only you have (your referral code, your real experience), a low-signal reply like "hey" or "ok" gets the question asked again — it is never used as the answer. A single word that could be the value (a code, a name) still counts.
- **Inputs only you know are asked for, never invented.** "…and add my referral code" without the code gets "What's your referral code?".
- Past times roll forward to the next valid slot. "Tune", "new image" and "show" need no confirmation.

## Grounding

- **Every claim about your history, employers, metrics or timelines must trace to the brief, your background profile, your story bank or your own answer.** Anything that doesn't gets one grounded rewrite. If it still doesn't trace, **the draft is held back**: nothing is saved or sent, and the bot asks you for the real detail ("✋ I held this draft back instead of guessing…"). That question stays in the chat's memory, so the bot remembers it asked. You can answer, tap ✍️ Write it without a personal story, or tap ✖ Drop it. A ⚠ warning is never shipped instead.
- **Third-party numbers nobody can verify are tagged `[verify]`** in the text, never stated as fact. A draft with a `[verify]` tag can't be scheduled until you resolve it.
- **14-day no-repeat.** Each draft logs the claims it made about you. A story, metric or achievement used in a post from the last 14 days (posted or scheduled) is blocked; the writer is told to reframe instead.
- **The background profile is context, never copy.** A line that copies 6+ words in a row from your profile card (the credentials paragraph, say) is blocked. The personal stake must be one specific anecdote tied to this post's idea.
- Tunes (✂️ 🎣 🔥) that add an ungrounded claim are thrown away and the original kept.

## Niche, pillars and voice

**Niche:** "a student founder's-office operator who builds AI and growth systems in public and translates between engineering and business." Every idea and draft is filtered through it. An off-niche brief is refused, with a ✍️ Draft anyway button.

**Pillars** live in `pillars.toml`. Edit the weights there and restart; no code changes needed. Every idea is tagged, the rotation is weighted, and the same pillar never comes up twice in a row.

| Pillar | Weight |
|---|---|
| Build in public (what shipped this week) | 30% |
| Attribution & growth systems | 25% |
| CS → startup metaphors | 20% |
| Personal stakes | 15% |
| Honest AI war stories | 10% |

**Voice rules (every draft):**
- The hook lives in lines 1–2 and is a curiosity gap, never a thesis statement.
- A personal stake comes before the lesson: real and topical, or omitted.
- One idea per post, and real numbers only.
- The post ends on a genuine `P.S.` question.
- **Banned:** bold-Unicode (always stripped), emoji spam, hashtag dumps (the formatter keeps at most 2; the validator rejects more than 4), generic advice with no story, borrowed credibility.

## Ideas

- Every news-based idea shows its source: 📰 title (source, date) URL. The model must cite a fetched headline by number; an idea citing anything else is dropped. If the fetch fails or finds nothing new, the bot says so and never makes up headlines.
- Ideas already shown in the conversation are never pitched again, reworded or not. "More ideas", "something more interesting", "what's new" and "what happened today in tech" each trigger a genuinely new fetch on other keywords, skipping articles already used. Each fetch uses 2 NewsData credits and no model calls.
- **Standing rules** such as "keep my personal story out for now", "no politics" or "avoid crypto posts" persist across messages and restarts until you lift them ("personal stories are fine again", "politics is fine now", "clear my constraints"). They filter chat replies, ideas and drafts.

## Telegram reference

| Say or tap | What happens |
|---|---|
| "what should I post about?" · 💡 Ideas · `/ideas` | 3 pillar-tagged ideas with hooks, sources for news, and ✍️ Draft buttons |
| "more ideas" · "what's new" · "what happened today in tech" | A new fetch; never the same ideas twice |
| "make a post about X" · `/new X` · a pasted link | Research → one post + one image, with buttons |
| ✂️ Shorter · 🎣 New hook · 🔥 Bolder · ✏️ Edit · 🔄 New version · 🖼 New image | Tune the waiting draft (no confirmation needed) |
| ✅ Post this → a time | 🌅 9 AM (best) · 🕕 6 PM · 🌙 9 PM · ⏱ 30 min · ⚡ Now (asks first) · ✍️ Custom |
| "schedule the eval-costs one for 9am" · "move it to Friday 9am" | ✅/✖ question naming the post and the time |
| "discard it" · "unschedule the KPI draft" · "discard all drafts" | ✅/✖ (or a pick list, or scope buttons) first |
| 🗑 · ↩️ · ⚡ Post now under a post | ✅/✖ first |
| "yes" / "no" | Answers the pending ✅/✖ |
| "what's queued" · "any drafts" · 📋 `/status` · 📅 `/queue` · 📝 `/drafts` · 📊 `/dashboard` | Live state: posted (with links), scheduled, waiting, publisher health, API usage |
| "keep my personal story out" · "no politics" · "… is fine again" | Standing rules, until lifted |
| 🎙 `/interview` · `/bank` | Answer questions; your answers become true material |
| 💬 `/comment <link or text>` | Two comment drafts to copy (the bot never posts comments) |
| `/stats` | Log likes and comments for recent posts |
| `/cancel` | Cancel a pending edit, time, stats or URL reply, or a draft being generated |

The scheduler also sends "What's today's topic?" at 08:00 IST, and at 14:00 IST drafts from a pillar idea if you sent nothing that day (drafts only, never posts). "⚠ May already be live" means you check LinkedIn, then tap **Live ✓** or **Not live**.

## Setup

### Environment (`.env`)

Copy `.env.example` to `.env` (`chmod 600`). `.env` is gitignored and never baked into the image.

**Required:**

| Variable | What |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From BotFather |
| `MY_CHAT_ID` | Your chat id; every other chat is ignored |
| `SUPABASE_URL` · `SUPABASE_SERVICE_KEY` | The project URL and the service_role key |
| `NVIDIA_API_KEY` | Embeddings and FLUX images |
| `GROQ_API_KEY` | The default writer and browsing research |
| `LINKEDIN_API_VERSION` | `YYYYMM` |
| `LINKEDIN_CLIENT_ID` · `LINKEDIN_CLIENT_SECRET` | Only for `scripts/linkedin_auth.py`; the token itself lives in the `settings` table |

**Recommended:**
- `NEWSDATA_API_KEY`: sourced news ideas.
- `NICHE_KEYWORDS`: the news search terms.
- `GROQ_API_KEY_2` and `GROQ2_DAILY_USD`: a paid Groq failover with a daily cap.
- `OPENROUTER_API_KEY`, `MODELSCOPE_API_KEY`, `ZENMUX_API_KEY`: the free writer fallbacks.
- `TAVILY_API_KEY`, `BRIGHTDATA_API_KEY`: research backups.

The rest have defaults; see `.env.example`.

### Enqueue an approved draft directly

When a post was reviewed and approved outside Telegram (in WhatsApp with Instinct, say), put it straight into the publish queue:

```
python scripts/enqueue.py post.txt image.png "2026-10-25 09:00" "topic label"
```

It uploads the image, inserts one `queued` row (chosen A, time read as IST) and prints the row id and the SHA-256 of the stored text so you can verify the enqueue. The dispatcher's normal exactly-once claim publishes it at the slot. Nothing here posts directly.

### Tests and lint

There is no host virtualenv. Tests and lint run in the test image with the repo mounted:

```bash
docker run --rm -v "$PWD":/app -w /app linkedin-bot-test sh -c "ruff check . && python -m pytest -q -p no:cacheprovider"
```

Tests make no network calls; a fixture blocks sockets. The model providers, Supabase, LinkedIn and Telegram are faked at the HTTP or client level, so the real clients and pipeline code run. Tests and lint must be green before every commit.

### Deploy and redeploy

```bash
docker compose up -d --build                 # rebuild the image, restart bot + scheduler
docker compose ps                            # both "Up"
docker compose logs --since 1m bot           # expect {"event": "bot_started"}
tail -f logs/dispatcher.log                  # one "dispatch_done" every 5 minutes
docker compose exec scheduler python dispatcher.py --dry-run   # what would post, without posting
```

After a redeploy, verify the change live on Telegram before calling it done.

### One-time setup

Script commands below mount the repo (`-v "$PWD":/app`), because `seed_posts.jsonl` and `out/` are kept out of the image.

#### 1. Supabase

1. Create a project. In **SQL editor**, run `scripts/schema.sql` (SPEC §4 verbatim, plus `awaiting_post_url`; embeddings are `vector(2048)`), then `scripts/storage.sql` (creates the public `post-images` bucket), then `scripts/usage.sql` (the API call ledger behind the usage dashboard).
2. Put the project URL and the **service_role** key in `.env` (`SUPABASE_URL`, `SUPABASE_SERVICE_KEY`). RLS stays on; the service_role key bypasses it.

**Upgrading a database created with the old 1536-dim (OpenAI) schema?** Do this once instead of step 1.1. Vectors from different models can't be compared, so every stored embedding is rebuilt; post rows, text and engagement are untouched.

1. In the SQL editor, run `scripts/migrate_embeddings_2048.sql`. It drops and recreates the two RPCs, converts both vector columns to `vector(2048)`, and sets every stored vector to NULL, all in one transaction.
2. Run:
   ```bash
   docker compose run --rm -v "$PWD":/app bot python scripts/reembed_corpus.py --dry-run    # counts what's missing
   docker compose run --rm -v "$PWD":/app bot python scripts/reembed_corpus.py              # re-embeds with NVIDIA_EMBED_MODEL
   ```
   It fills only NULL vectors, so it's safe to re-run after an interruption. It sends 16 texts per request and pauses 2 s between requests, which keeps it under the free-tier rate limit.

Until the re-embed finishes, few-shot retrieval and topic dedup find nothing, but drafting still works.

#### 2. LinkedIn OAuth (on your laptop; it opens a browser)

1. At developer.linkedin.com, create an app. Add the products **Share on LinkedIn** (`w_member_social`) and **Sign In with LinkedIn using OpenID Connect** (`openid profile`). Under *Auth*, add the redirect URL `http://localhost:8765/callback`.
2. Put `LINKEDIN_CLIENT_ID` and `LINKEDIN_CLIENT_SECRET` in a `.env` on your laptop (the same `.env` works), then:

```bash
python scripts/linkedin_auth.py
```

This writes `linkedin_access_token`, `linkedin_token_issued_at` and `linkedin_author_urn` to the `settings` table. Re-run it every ~60 days; `--token-check` warns from day 50.

#### 3. Seed your voice corpus

Put your 15–20 best posts in `seed_posts.jsonl`, one per line, with **real** likes and comments (format in `seed_posts.example.jsonl`):

```bash
docker compose run --rm -v "$PWD":/app bot python scripts/seed_past_posts.py seed_posts.jsonl
```

It's safe to re-run; duplicate texts are skipped.

**Or scrape them automatically** (Bright Data, about $1.50 per 1,000 records; the account must be active):

```bash
docker compose run --rm -v "$PWD":/app bot python scripts/scrape_linkedin_posts.py https://www.linkedin.com/in/<you>/ --limit 100 --top 20
```

It pulls your own posts (no reposts) with their like and comment counts, and writes the top 20 by engagement to `seed_posts.jsonl`. The raw records are saved to `out/linkedin_posts_raw.json`.

**Older posts:** LinkedIn only shows recent activity publicly, so scrapers miss older posts. To get all of them, use your own data export: LinkedIn → Settings → Data privacy → **Get a copy of your data** → Posts. Then:

```bash
docker compose run --rm -v "$PWD":/app bot python scripts/import_linkedin_export.py Basic_LinkedInDataExport_*.zip --enrich   # copy the zip into the repo first
docker compose run --rm -v "$PWD":/app bot python scripts/seed_past_posts.py seed_posts.jsonl
```

`--enrich` fetches likes and comments for each exported post through Bright Data. Already-seeded posts are recognised and not duplicated. The `/stats` promotion gate opens once 5 human posts have engagement > 0.

#### 4. Voice profile

```bash
docker compose run --rm -v "$PWD":/app bot python scripts/extract_voice.py
```

The NVIDIA chat model analyzes the seeded posts. The script then **asks you for your current role** and never guesses it (SPEC §2 item 10). Generation refuses to run until `current_role` is set.

Optional smoke test (makes real paid calls, writes nothing to the DB, saves images to `./out/`):

```bash
docker compose run --rm -v "$PWD":/app bot python -m app.pipeline "why most student founders quit in year one"
```

## Roadmap

LinkedIn only for now. The publisher layer is built so another platform can be added as a new `app/publishers/<platform>.py`. **The multi-platform and SaaS work (Telegram/Instagram/Facebook/Threads publishers, multi-user accounts, billing) is parked, not cancelled.** When it resumes, this is what has to change in the single-user code:

| Today (just me) | Needed for customers |
|---|---|
| One Telegram chat whitelisted (`MY_CHAT_ID`) | A `customers` table; each chat or phone number maps to a customer; onboarding by invite or payment |
| One `voice_profile` row, one profile card, one story bank | Per-customer voice, profile card, story bank and claim rules |
| My name hard-coded in `app/cover.py` and 5 prompts (`draft_system`, `final`, `tune`, `comment`, `claim_audit`) | Name, pronouns and profile injected per customer |
| One LinkedIn token and author URN in `settings` | Per-customer tokens, **encrypted at rest**, with a refresh and re-auth flow per customer |
| Global caps (paid-Groq $/day, Bright Data records/day) and one usage ledger | Per-customer quotas tied to their plan; a ledger tagged by customer for cost and margin |
| `posts` queries mostly assume one owner | Every query scoped by customer; row-level security in Supabase |
| One dispatcher for my posts | One dispatcher for all customers, with fairness and per-account rate limits |
| An admin view I don't need | An admin board for me: customers, plans, usage, costs, failures |

**Business and legal checks before charging anyone:**

- **LinkedIn API terms:** confirm that posting on customers' behalf through my app is allowed for a paid service, and what app review, product approvals and rate limits that needs. Use only the official API; no scraping of customers' accounts.
- **Model and data licences:** confirm commercial-use terms for every provider (FLUX.1-dev through NVIDIA, Groq, Bright Data, Tavily, NewsData, OpenRouter's free models). Some free tiers or model licences may not allow commercial use.
- **WhatsApp:** the WhatsApp Business Platform needs Meta business verification and approved message templates, and it charges per conversation.
- **Payments:** subscriptions through a gateway that supports recurring billing in India (e.g. Razorpay subscriptions or UPI Autopay), with invoices and GST if applicable.
- **Privacy:** customers' posts, stories and LinkedIn tokens are personal data. Write a privacy policy and terms, support data export and deletion, and follow India's DPDP Act 2023. Keep the rule that **nothing posts without the customer's own approval**.
- **Pricing:** measured in phase 1. Model cost is about $0.01-0.02 per post today, plus images, research credits, hosting and support time.

## Usage dashboard

```bash
python scripts/usage_dashboard.py        # → http://127.0.0.1:8787 (needs Python + the .env; binds to localhost only)
```

One card per service, showing what's used and what's left: Groq, OpenRouter, Tavily, NVIDIA, ModelScope, ZenMux, Supabase, LinkedIn and Telegram. Each number is labelled with where it came from:

| Label | Meaning |
|---|---|
| **live** | Asked from the provider right now: OpenRouter's free-requests-per-day counter, Tavily plan credits, Supabase DB and storage size, Telegram status |
| **headers** | Taken from Groq's rate-limit headers on the bot's latest call (requests per day, tokens per minute) |
| **ledger** | Counted from `bot_api_calls`. Every HTTP attempt the bot makes, including retries and 429s, is recorded with its tokens and status. Covers Groq's 200k tokens/day and each provider's calls and errors over 24 h |
| **estimate** | Used where the provider exposes nothing (NVIDIA credits: about 1 per request) |

Keys are used only inside the local server; the page and `/api/usage` carry numbers, never keys. The server binds to `127.0.0.1`, so only your machine can open it. The page refreshes every 60 s. The ledger only counts calls made after it was added (2026-09-23). `--once` prints the JSON and exits.

## Decisions

Places where SPEC was silent or ambiguous, or where following it literally was unsafe. In each case the safer option was chosen.

**Model and generation**

1. **Draft temperature.** Drafts are sent with `DRAFT_TEMPERATURE` (0.85, per SPEC §5.1). The outline, de-AI pass and voice extraction send no temperature, so they use the model's default, as before.
2. **Outline stored for regenerate.** Regen re-runs steps 6–11 only, which need the outline, and the schema has no outline column. So it's stored at `posts.research.outline`. No schema change was needed. Regen reuses the stored research and outline and re-fetches few-shot examples through the stored `topic_embedding`.
3. **`topic` column** holds the outline's one-line insight (falls back to the brief). It's shown as "recently covered" in later outlines.
4. **Sub-template vs fixed roles.** The outline picks one sub-template. It refines whichever role it belongs to (a `story_*` template shapes Draft A; `contrarian_*` or `insight_list` shapes Draft B). The other draft keeps its base role. An unknown template falls back to `story_cold_open`.
5. **Fallback topic.** `NICHE_KEYWORDS` rotate by IST day. The top Tavily news result from the past week becomes the brief (with its source). No extra chat call is made to pick it. The fallback also skips when 3 or more drafts are already waiting unchosen, so /drafts never piles up.
6. **Research depth.** Tavily uses `search_depth="advanced"` (2 credits, about ₹1.3) for better stats. Change it in `app/research.py` to save cost.

**Number verification (§5.1 step 8)**

7. Magnitude units are folded into the value, so `$2B` = `2 billion` = `2,000,000,000`, and `₹5 crore` = `50 million`. Also normalized: plurals (`crores`, `lakhs`), `lac`, `per cent`, `pct`, `bn`, `mn`, `Rs`/`INR`/`US$`. Commas are stripped only between digits (this handles `1,20,000`).
8. The **brief counts as a source.** Numbers you typed aren't invented.
9. A small number with a currency sign (`$5`) or a unit (`5%`, `3x`) is a stat and is checked. Only a bare integer from 1 to 10 counts as a list count. `3x` multipliers are checked; ordinals (`21st`) and alphanumerics (`B2B`, `Web3`) are ignored. Matching is exact, so a rounded `47%` vs a researched `47.3%` is flagged.
10. Warnings appear on **each** draft's message, not only after Draft B.

**Escaping (A1)**

11. A hashtag is `#` that is not preceded by a letter, digit, underscore or `#`, followed by letters/digits with at least one letter. So `#1` and `C#` stay escaped text. Underscore ends a tag because it's reserved. `#` inside URLs is never turned into a hashtag. Escaping happens inside `build_post_payload`, so no code path can skip it.

**Bot safety**

12. `/cancel` is checked before chat state. Otherwise SPEC's order is kept (whitelist → callbacks → state → commands → brief). `/start`, `/help` and unknown `/commands` never trigger a paid run.
13. **Brief length limits.** Briefs must be 10–800 characters. Short text like "ok" won't start a paid run. Long text is most likely an edited draft sent after its 30-minute edit window expired, so the bot says so instead of generating.
14. **Edit limits.** Edits must be at least 20 characters and at most 3000 once escaped (LinkedIn's limit). Edits are rejected while a regen is running.
15. **Double taps and stale buttons.** Every transition is a conditional `UPDATE … WHERE status = …`, so double taps can't both win. Each callback also checks the post belongs to your chat. Pick, time, edit and regen are blocked while a regen is in flight.
16. **Regen and edits reset `chosen`.** Without this, a time button tapped earlier could queue text you haven't picked. Regen also resets `edited_a` and `edited_b` (per SPEC).
17. **A3.** `save_edit` applies only while the post is `awaiting_choice`. Otherwise the bot replies "Draft already locked" and clears state. Regenerated drafts that finish after the row was locked are discarded.
18. **`/stats` mapping.** `chat_state.post_id` stores the newest listed post. The reply is mapped to the posts at or before it, so a post published in between can't shift the lines.
19. **Promotion median.** The median is computed over human posts with engagement > 0, the same rows the ≥5 gate counts. Seeded rows without numbers would otherwise drag the median down and let average AI posts in.

**Dispatcher**

20. **What counts as "definitely not sent"** on `POST /rest/posts`: 4xx, `ConnectError`, `ConnectTimeout`, `PoolTimeout`. These are retried. **Maybe live:** read/write timeout, broken connection, 5xx, or a 2xx with no or unparseable `x-restli-id`. These are never retried, and you get the Live ✓ / Not live alert.
21. **Image failures.** An image download failure, or a file that isn't PNG/JPEG (magic bytes), is treated like an upload failure and is retryable.
22. **Retry count.** The count increments before comparing: first failure → `queued` (attempt 1/2), second → `failed`.
23. **Unexpected exceptions** mid-publish leave the row in `posting`. The stuck-row check alerts after 15 minutes. The row is never guessed back to `queued`.
24. **Missing LinkedIn token or author URN.** Claimed rows fail through the normal retry path. You get at most 2 alerts per post, instead of posts piling up silently.
25. **Both "may be live" alerts** (the 15-minute stuck check and a timeout/5xx) carry the A2 buttons. **Not live** re-queues with the original (past) `scheduled_at`, so it posts on the next tick.
26. **`--dry-run`** never claims, updates or alerts, and never calls `/v2/userinfo` (it uses a placeholder URN if none is stored). It still downloads each image from Supabase to validate it.
27. **Payload fields beyond SPEC §7.** The payload includes `distribution` (required by the Posts API, not listed in SPEC) and `content.media.title` (the draft's first line, ≤100 characters).
28. **Corpus admission after posting** runs after the row is marked `posted`. If it fails (e.g. the embeddings API is down), it's logged and never undoes the post.

**NVIDIA migration**

32. **Model IDs were checked against your actual account, not just the catalog.** On 2026-09-23, three of the requested IDs didn't exist in `integrate.api.nvidia.com/v1/models`: `deepseek-v4-flash`, `mistral-large-3` and `nv-embed-v2`. Several models that *are* listed return 404 for this key: `nv-embedqa-mistral-7b-v2`, `llama-3.2-nv-embedqa-1b-v1`, `embed-qa-4`, `arctic-embed-l`, and every `mistral-large` variant. Others timed out after 120 s: `mistral-nemotron`, `kimi-k3`, `glm-5.3`. What works for this key:
    - **Chat:** `deepseek-ai/deepseek-v4.1-flash` (default) and `nvidia/nemotron-3-super-120b-a12b` (alternative).
    - **Embeddings:** `nvidia/nemotron-3-embed-1b` (text) and `nvidia/llama-nemotron-embed-vl-1b-v2` (vision-language). Both are **2048-dim**. No 4096-dim model is reachable, so the schema is **`vector(2048)`** rather than the planned 4096.
33. **Query vs passage.** The embedding model is asymmetric. Briefs are embedded as `query` (so `posts.topic_embedding` holds query vectors and topic dedup compares like with like), and posts as `passage` (`past_posts.embedding`). Inputs use `truncate: "END"` so long posts don't error.
34. **Dimension guard.** Every vector's length is checked against `EMBED_DIM = 2048`. A different model fails loudly with a message, instead of the database rejecting the insert mid-pipeline.
35. **No vector index.** 2048 dimensions is above pgvector's 2000-dimension limit for HNSW/IVFFlat indexes, so the RPCs scan sequentially. That's fine for a corpus of tens to hundreds of rows.
36. **Retries.** The NVIDIA client retries 429, 500, 502, 503, 504 and connection failures up to 3 attempts, honouring `Retry-After` (capped at 30 s). Read timeouts are not retried, so a slow generation isn't doubled. 4xx errors fail immediately with the status and body.
37. **Reasoning is switched off (`NVIDIA_DISABLE_THINKING=true`).** Both working chat models reason by default. Measured live on 2026-09-23:
    - `deepseek-v4.1-flash` didn't answer a single de-AI call within **400 s**;
    - `nemotron-3-super` spent all 4096 tokens reasoning;
    - with a prompt-level `/no_think`, nemotron put its reasoning **straight into the draft text, untagged**.

    The client therefore sends `chat_template_kwargs: {"thinking": false, "enable_thinking": false}`. Each model reads its own key, and both accept the pair. With it, a de-AI call takes about 35 s on deepseek and about 2 s on nemotron. `<think>` blocks are still stripped as a backstop, and a `finish_reason` of `length` raises instead of shipping a truncated draft.
38. **FLUX request.** The request body is exactly `{"prompt": …}`, the verified call. FLUX returns a square JPEG (typically 1024×1024), so the 4:5 center-crop trims the sides and upscales to 1080×1350. The image prompt text is unchanged. A `finishReason` other than `SUCCESS` (e.g. content-filtered) fails the run.
39. **Writer validation (2026-09-23).** Same 3 briefs and 6 drafts in every run:
    - NVIDIA `deepseek-v4.1-flash`: 0/6. NVIDIA `nemotron-3-super`: 1/6.
    - Groq `gpt-oss-120b`: 1/6 on the de-AI pass alone, but **6/6 with number repair** (decision 43). Every invented number was removed, and no clichés appeared in any final draft.
    - The dominant failure for every model was invented story numbers (credit loads, plan prices) that the de-AI pass should have deleted.
    - OpenRouter `dots-3-note-preview:free`, the first fallback: **1/6**. Number repair worked, but the "X isn't A. It's B" cliché survived the de-AI pass in 5 of 6 drafts. Fallback drafts are usable but weaker than Groq's.
40. **Cost of retries.** A retried call can spend an extra NVIDIA credit. At one post a day, that's negligible.

**Providers (Groq, browsing, free fallbacks)**

41. **Writer: Groq `openai/gpt-oss-120b`,** the only model validated 6/6 (decision 39). Reasoning is kept short and out of the output with `reasoning_effort=low` and `include_reasoning=false`.
42. **Research: Groq browsing with page grounding.** The model uses `browser_search` to find facts with URLs. A fact is kept only if **every number in it appears in the raw text of the page it cites**. Groq returns the full text of each opened page in `executed_tools`, which is what the check runs against. So the research that drafts must stick to is still real source text, not the model's summary.
    - First live run: 7 grounded facts in 41 s, from the GUESSS 2025 report and the German Student Entrepreneurship Monitor 2025.
    - Fewer than 3 grounded facts triggers the Tavily fallback, if a Tavily key is set.
43. **Number repair (step 7b).** After the de-AI pass, if the number check finds numbers not in the research, one call asks the model to remove exactly those numbers and change nothing else.
    - The result is kept only if it has fewer unverified numbers and keeps at least 60% of the text. Otherwise the original stays, with ⚠ flags.
    - This takes 0–2 extra calls per run. `NUMBER_REPAIR=false` turns it off.
44. **Groq free-tier limits shape the client.**
    - **8,000 tokens/minute:** calls are serialised and 429s are waited out, honouring `retry-after`.
    - **200,000 tokens/day on gpt-oss-120b:** browsing is costly, since every opened page counts as input (about 30–50k tokens per research call). That allows about 3–4 full runs a day, which is fine for one post.
    - A 429 whose retry-after exceeds 30 s is a quota, not a burst. It fails immediately so the fallback chain can take over, instead of waiting minutes.
45. **Writer fallback chain (`WRITER_FALLBACKS`).** After the primary writer, the chain tries every usable free model, in the order live probes ranked them on 2026-09-23:
    - OpenRouter: `dots-3-note-preview`, then `nemotron-3-super`, then `nex-n2.5-pro` (these three returned clean drafts);
    - ModelScope, then ZenMux;
    - the 429-prone OpenRouter models (`qwen3.8`, `gemma-4`, `glm-5.2`), `nemotron-3-ultra` and the `openrouter/free` router;
    - NVIDIA nemotron last.

    A link is skipped on any error, 429, 401/403, empty reply or cut-off reply. Providers without a key are left out. Free models run with reasoning **disabled**: at low effort, most OpenRouter free models spent the whole budget thinking and returned 0 words. `inkling` is excluded (it's limited to agentic harnesses). The chain logs which model served each call.
46. **Accounts that still need attention.**
    - **ModelScope:** the key is valid, but the account must be linked to an Alibaba Cloud account (modelscope.ai → Settings → Account). Until then every call is a fast 401 that the chain skips.
    - **ZenMux:** the key is pay-as-you-go and gets 403 on the free models. Check the key type or balance in the dashboard.
    - **OpenRouter:** the free tier without credits allows ~50 requests/day and 20/minute across all `:free` models.
47. **Tavily is optional, but recommended as a backstop.** Without it, a day when Groq's quota is exhausted means research comes back empty. Drafting still runs, but with no facts to cite, and the fallback news topic is unavailable.

48. **An image failure never discards finished drafts.** FLUX can refuse a hook (`finishReason=CONTENT_FILTERED`) or be down. `ImageMaker.generate` tries the hook, then a neutral prompt, then draws a local 1080×1350 text card with Pillow. The card uses a system TrueType font (Helvetica / Arial Unicode / DejaVu). Punctuation the font lacks becomes ASCII, and emoji are dropped, so there are never empty glyph boxes.
49. **Few-shot examples are sent as plain text.** Bold-Unicode letters cost about 7× the tokens of ASCII. Folding them in the prompt (not in the drafts) cut the draft system prompt from ~4.7k to ~2.9k tokens, which keeps a run under Groq's 8k tokens-per-minute cap. Drafts may still use bold (`UNBOLD_DRAFTS=false`, per master profile §10.3).
50. **Per-minute and per-day 429s are handled differently.** A 429 whose body mentions a per-minute limit (TPM/RPM) is waited out for up to 65 s. A long retry-after on any other 429 (the daily quota) fails fast, so the fallback chain takes over at once instead of sleeping for minutes.

51. **The bot is a conversation; post requests are explicit.** Plain messages ("hey", questions, ideas) get a short chat reply from the writer chain. Drafts start only from one of these:
    - an explicit request: "make a post about X", "draft: X", or `/draft X`, matched by regex with no model call;
    - a pasted link;
    - the chat model setting `draft_topic`, meaning he asked or said yes to a proposed idea;
    - a reply to "What should the post be about?", sent after "make a post" with no topic or by the 8 AM nudge. That reply goes straight to drafts. "you pick" drafts from the news; "not today" does nothing.

    Chat history is kept in memory only (the last 12 messages), so a restart forgets the conversation, never any post state. Chat never publishes: "post A now" gets pointed to the buttons.
52. **Jev (TypeSafe AI) is a second opinion, not a gate.** When the chat model decides on its own to start drafts, Jev is asked "does this message ask for drafts now?" (a calibrated yes/no). Below 0.5, the bot offers instead of starting. Explicit requests skip Jev. Jev's free tier rate-limits after a few calls, so a 429, timeout or error falls back to the chat model's decision. The worst case is one unwanted draft run, which still can't publish.
53. **Links in a brief are read.** The page is fetched directly, with a 15 s timeout and a 2 MB cap, http(s) only, and never a private or loopback address, including after redirects. The `<article>`/`<main>` text becomes research source [1], the brief names the article for search and embeddings, and the article's numbers count as verified. X and LinkedIn post URLs go through Bright Data (1 record each), because a plain fetch can't read them.
54. **Bright Data is spent sparingly, with a hard cap.**
    - It runs one ChatGPT Search record per draft run (web search on), in parallel with Groq browsing.
    - Each fact is kept only if every number in it appears on a page it cites. The cited pages are fetched for free, and a page that blocks the fetch takes its facts with it.
    - `BRIGHTDATA_DAILY_RECORDS` (default 10 per IST day) is checked against the usage ledger before any request. The 5,000 free records last years at 1–2 per run.
55. **One image per run, shared by both drafts, at higher quality.** A model call turns draft A plus the outline insight into a concrete, filter-safe scene: setting, subject, light, no text or stock clichés. FLUX then renders it natively at 1088×1344 with 50 steps; before this it was a square image cropped and upscaled. It's uploaded once, both `image_*_url` columns point at it, and the preview sends one photo. The fallbacks are unchanged: scene → neutral scene → text card.
56. **Copy guard.** A weaker fallback model once reproduced a linked article's opening word for word as "Draft B". Every draft is now checked against the research sources for shared 8-word runs. Above 20%, it's rewritten once with an explicit "your own words" instruction. If it still copies, the preview flags it with ⚠. On real drafts, the copied one measured 0.92 and the original ones 0.00–0.01.
57. **NewsData.io picks news topics** (the 2 PM fallback and "you pick"). It searches for the niche keyword as an exact phrase in headlines first, then in article bodies, limited to the technology and business categories. Press-release wires and market-size reports are skipped, and the lowest `source_priority` wins. The key goes in the `X-ACCESS-KEY` header, never the URL. The free plan has a ~12 h delay and 200 credits/day, and Tavily and browsing remain the fallbacks. `NICHE_KEYWORDS` defaults to: AI agents, LLM, startup, developer tools, Indian startup.

58. **The copywriting playbook** (`app/prompts/copy_playbook.txt`) is sent with every draft, de-AI pass and tune. It's built from 2026 research (sources below) plus his own best posts.
    - **Hook:** at most 140 characters, the mobile "see more" cut. It uses one of six frameworks: dissonance, A to C without B, contrarian, insider reveal, proof first (his verified facts only), or dated moment. The de-AI pass keeps it word for word.
    - **Next:** a re-hook line, then one idea per post.
    - **Structure:** story is before → tension → bridge in 2–3 steps → lesson. Contrarian is belief → problem/agitate → what works → proof. List is a promise, 3–5 one-line bullets, then a takeaway.
    - **Proof:** at most 2 statistics, with the source named in the sentence, and the real cost shown.
    - **Style:** paragraphs of 1–2 lines, AND→BUT→THEREFORE flow, and one closing question that's easy to answer.
    - **Outline:** it now settles the metric, the counter-intuitive point, the 2–3 step mechanism and the closing question before any draft is written.

    Sources: [AuthoredUp length study](https://authoredup.com/blog/linkedin-character-limit), [see-more cutoff](https://postformatter.com/blog/linkedin-see-more-cutoff/), [LinkedIn copywriting 2026](https://linkedinpreview.com/blog/linkedin-copywriting-tips-2026), [hook frameworks (samber/cc-skills)](https://awesomeskill.ai/skill/samber-cc-skills-linkedin-ghostwriting), [AIDA/PAS/BAB](https://monolit.sh/blog/copywriting-formulas-social-media-aida-pas-bab-explained).
59. **Length is 900–1,400 characters.** The research's best-performing band is 1,200–2,000, but his own 11 human posts have a median of 1,078 characters, with his best 924–1,318, and he asked for shorter. So the target is the lower edge of that band, which is also his proven range. It's enforced in code by step 7c, `fit_shape`:
    - A draft over 1,600 characters gets one "shorter" edit.
    - Paragraphs over 220 characters are split at sentence ends, and the hook at 140 (`style_check.airy`). Words never change, and lists are left alone.
    - If the hook still runs past 140, one "new hook" edit follows. That edit must keep at least 80% of the rest of the post.
    - A model edit that adds an unverifiable number is discarded.
    - The preview header shows `words · chars · hook N/140`.
60. **Image: 4:5 portrait.** 2026 guides agree 1080×1350 (4:5) takes the most mobile-feed space and gets the most engagement. FLUX renders 1088×1344 natively, which is center-cropped and resized to 1080×1350 PNG. There's one scene image per run (decision 55).
61. **Buttons.**
    - **Under each draft:** ✂️ Shorter · 🎣 New hook · 🔥 Bolder · ✅ Post · ✏️ Edit.
    - **Under the preview:** ✅ Post A/B · 🔄 New drafts · 🖼 New image · 🗑 Discard.
    - **After scheduling:** ⚡ Post now · ↩️ Unschedule · 🗑 Discard.

    Tunes and a new image run in the background under the same per-post lock as Regenerate. They apply only while the post is `awaiting_choice`, and they reset `chosen`, so a tuned draft must be picked again. A tune that adds an unverifiable number is refused. Unschedule and Post now are conditional on `queued`, so they can't race the dispatcher's claim. Discard sets `failed` with the error "discarded by user". Both gates still apply to everything.
62. **Commands and the quick menu.**
    - **Commands:** `/new <topic or link>`, `/ideas` (3 news headlines, each with a ✍️ Draft button), `/drafts`, `/queue`, `/dashboard` (every API's used vs left, from the same data as the web dashboard), `/stats`, `/help`, `/cancel`. They're registered with Telegram via `setMyCommands` at startup.
    - **Quick menu:** a persistent keyboard under the message box (✍️ New post · 💡 Ideas · 📝 Drafts · 📅 Queue · 📊 Dashboard · ❓ Help). Taps are handled as commands, even while a reply such as an edit is pending, and they never change that pending reply.

63. **Truth over vividness.** The playbook's specificity rule made a fallback model invent scenes: "the digest email got zero opens", "I handed the prototype to ops that afternoon". The playbook, outline and de-AI prompts now require every personal detail to come from his background facts, the voice profile or the brief. Research statistics may never be presented as his results. The hook examples are labelled as shapes, not his story.
64. **Formatting is enforced in code** (`writer.linkedin_format`, applied by `clean_post` to every model output, never to human edits). LinkedIn renders no markdown, so:
    - `**bold**` becomes Unicode sans-serif bold (or plain, with `UNBOLD_DRAFTS`), and `*italics*` becomes plain;
    - keycap-emoji numbering (`1️⃣`) becomes "1.";
    - markdown headings and two-space line breaks are dropped;
    - "(Smartsheet, 2025)" becomes ", per Smartsheet";
    - the closing hashtag line is cut to 3 tags. The models ignored the 0–3 rule in live runs, with 7 and 5 tags.
65. **Step 7d, lint then fix** (`pipeline.polish`). If `style_check.fixable_issues` finds a banned pattern (the exact line is quoted) or more than 2 statistics, one `Writer.fix` call repairs just those issues. The fix is kept only if the issue count drops, no unverifiable number was added, and at least half the post remains. In live runs, "it's not X, it's Y" survived the de-AI pass with qwen, dots-3 and gpt-oss-20b. `scripts/validate_writer.py` now runs 7b, 7d and 7c too, so its score is what gets delivered.
66. **Writer failover, Groq first.**
    - Groq sets free limits per model, so its other models have their own quota. The chain is: `groq:gpt-oss-120b` → `groq2:gpt-oss-120b` (optional `GROQ_API_KEY_2`) → Groq's other models → OpenRouter free models → the rest.
    - `reasoning_effort` depends on the model: gpt-oss takes low/medium/high, Qwen takes "none". With "low", Qwen returned an empty reply.
    - Browsing research fails over across Groq accounts too (`research.FailoverBrowser`).
    - The dashboard shows account 2 as its own card.
    - `GROQ_API_KEY_2` is a **paid** Developer-plan account (pay per token: gpt-oss-120b $0.15 in / $0.60 out per 1M, cached input half price). It runs only after the free account's quota is gone. It's capped per IST day by `GROQ2_DAILY_USD` (default $0.25; `app/spend.py`, estimated from the ledger), and at the cap the chain moves on. A draft run costs about $0.01. It never runs the weaker models. Prompts put the per-call part last so Groq's prompt cache hits across the A and B calls. Also set a spend limit in the Groq console as a hard backstop.

67. **Truth checks, in layers.** Eight live runs on one first-person brief showed the writer blending nearby research into his story: "LinkedIn penalized my post", invented tool internals, invented outcomes. No single prompt rule stopped it. So there are layers:
    - **Story mode:** a first-person brief gets at most 2 outside facts, as context.
    - **Own hook:** his first sentence leads the hook list.
    - **Outline audit** (`grounded_outline`): regenerate once with the problems named, then drop hooks that are still flagged.
    - **Polish claim audit** (`Writer.audit_claims`, `prompts/claim_audit.txt`), in up to 2 rounds. A fix counts only if the flagged sentences are gone (`_present`, 5-word runs); re-auditing is noisy.
    - **His numbers:** in story mode, the brief's numbers must appear exactly (`fixable_issues(keep_numbers=True)`). The number check treats 0.92 and 92% as the same value.
    - **Visible leftovers:** a final audit stores `research.claim_flags`, and the preview shows each as "⚠ check: “…”". Drafts he edited himself carry no flags. ✂️/🎣/🔥 re-audits that draft.

    The audit ignores opinions, lessons and attributed findings; flagging those stripped posts to ~600 characters. Cost is about 18–21 paid calls, roughly $0.011 per run.
68. **Enrichment reads can't sink a run.** The similar-topic and style-example lookups (`pipeline.optional`) retry once, then continue without them. This was seen live as a Supabase HTTP/2 stream reset.

69. **One post per run (user request, 2026-09-23).**
    - **Candidates:** two are written internally. The story (or, for a general topic, his informed take) and the contrarian version run in parallel.
    - **Merge:** `Writer.final` (`prompts/final.txt`) merges them into ONE post: the strongest hook, the best proof, one idea.
    - **Then:** de-AI → number repair → polish (claim audit) → mobile shape → final audit.
    - **Storage:** the post goes in `draft_a` and `draft_b` stays "", so there's no schema change and older two-draft rows keep their A/B controls.
    - **Telegram:** one "YOUR POST" message with ✂️ 🎣 🔥 / ✅ Post this / ✏️ Edit, then 🔄 New version / 🖼 New image / 🗑 Discard.
    - **Gates are unchanged:** ✅ Post this is gate 1, the time is gate 2.
    - **Empty drafts:** the bot refuses to pick one, and the dispatcher refuses to publish one.
70. **Research uses every source in parallel.** Groq browsing (failing over to the paid account), Bright Data ChatGPT Search and Tavily all run every time. Results are interleaved so each source's best facts survive the 8-fact cap. NewsData adds up to 2 recent articles whose headline or summary mentions at least 2 of the brief's key terms. Tavily page text is cleaned (`tidy_snippet`), and results scoring under 0.4 are dropped (the top 3 are always kept).
71. **General topics don't get his project list.** For a non-first-person brief, `writing_voice` removes `background` from what the writer sees. Live runs used PicaPool facts to invent incidents. Audits and number checks still use the facts. Candidate A becomes an informed take (`ROLE_A_TAKE`).
72. **The flag filter drops auditor noise.** A flag is dropped when it's a third-person statistic whose numbers are all in the research (`sourced_statistic`). A flag is kept only when it's first person, names one of his organizations or projects, or carries a number the research doesn't back (`could_be_about_him`).
73. **Gutted posts are rewritten whole.** If the audit's deletions leave less than 70% of the post, `rewrite_without` has the final editor rewrite it, with the unsupported claims listed to avoid.
74. **Network hardening from live failures:**
    - `LinkReader` streams: headers are checked first, the body stops at 2 MB, and the whole fetch has a 20 s deadline. A slow PDF once held a run for 11 minutes.
    - Image uploads retry 3 times under fresh names. A ReadTimeout once lost a finished post.

75. **The profile card** (`out/profile_card.md` → `voice_profile.profile`; `extract_voice.py` loads it). It covers who he is, what he does now, what he did before, his through-line, themes and signature move, written from his verified facts under his claim rules. The draft and final prompts get it as "WHO HE IS", so posts connect to his real work. The raw background list is kept for the audits and number checks.
76. **Human-sounding text, enforced in code** (`writer.dehumanize_tells`):
    - no em or en dashes; number ranges get a plain hyphen;
    - no non-breaking hyphens or thin spaces;
    - no emoji; emoji bullet lists become "- ";
    - his own habits stay: → arrows and "P.S.";
    - the prompt files contain no dashes, because models copy their instructions' style;
    - stock phrases ("Here's the kicker", "The result?", "Lesson:", "seamless", "moreover"…) are lint issues that the fix step rewrites;
    - tone: sarcasm at level 1–2 out of 10, one wry aside at most.
77. **The cover image** (`app/cover.py`): a documentary 35 mm-style FLUX photo with grain and muted colour, a dark fade, a bold Avenir Next Heavy headline of 3–7 words written by the model (`prompts/cover_headline.txt`), and his name, at 1080×1350. With no photo, it's a paper-toned type cover. The scene step writes two scenes (specific, then a simpler objects-only daylight version). `safe_scene` moves scenes out of homes, because NVIDIA's filter refused "a desk in a shared apartment" but passed the same desk in an office.
78. **Invented details about his work are caught deterministically** (`style_check.invented_details`). A line about him (first person, his organizations, or a bullet under such a line) is flagged if it adds implementation terms, acronyms or durations missing from his facts and brief, or 4 or more new content words. Any other line is flagged at 7 or more new content words; in live runs, inventions scored 8–9 and good lines 5–6. Flags feed the fix step and the ⚠ lines. The auditor also gets his claim rules ("CLAIM RULE: …").
79. **Editing runs cool.** Candidates are sampled at `DRAFT_TEMPERATURE`. The final editor, de-AI, repair, fix, tune, audit and headline calls all run at `EDIT_TEMPERATURE` 0.2; the model default (1.0 for gpt-oss) produced vivid invented specifics.

80. **Taken from [sergebulaev/linkedin-skills](https://github.com/sergebulaev/linkedin-skills) (signal only):**
    - **Story bank and `/interview`** (`app/storybank.py`, settings key `story_bank`, never in git). It asks one question at a time and presses once on a vague number. "Rather not" is never asked again, and answers are kept verbatim. The writer gets them as HIS STORY BANK, and the audits and number check treat them as verified facts.
    - **AI-tell rules** in `style_check`:
      - reveal bridges, stop X/start Y, sincerity openers, "when it comes to", filler openers, dead closers;
      - staccato stacks, one-word paragraphs, no-X-no-Y-just-Z, pseudo-Socratic Q&A;
      - vocabulary density per paragraph (3 or more is flagged);
      - curly quotes straightened;
      - a caution against over-cleaning.
    - **Calibrated to his voice:** the fragment limit is 4, not the repo's 2 (his posts: median 2, 17 of 20 at 4 or fewer). "It's not X, it's Y" stays banned for bot text even though he uses it. Zero em dashes, per his rule, where the repo allows one per 100 words.
    - **Reach rules:** 0–2 hashtags; links stripped from the body (the source goes in the first comment); a warning when two posts fall within 24 hours; a best-time hint in the picker; an after-post "reply within 60–90 minutes, no big edits for 3 hours" note.
    - **`/comment <link or text>`:** two options from the repo's 7 templates and comment rules (200–350 characters, one new idea, no generic praise), grounded in his profile and bank. The fetched post is treated as data. The bot never posts comments.
    - **Skipped as noise:** the Apify, Publora and Pixfaro integrations, AI-detector testing, employee advocacy, engager analytics, thread monitoring, video and carousel rules, and unverifiable reach percentages.

**Ops**

29. `schema.sql` is SPEC §4 byte-for-byte, except the `chat_state` comment now lists `awaiting_post_url`. There's no CHECK constraint, matching SPEC. The Storage bucket is created in a separate `scripts/storage.sql`.
30. The dispatcher cron line uses `flock -n` so slow runs don't pile up. Claim-before-post already prevents double posting.
31. **Logs** are JSON lines on stdout. Every configured secret, plus the LinkedIn token once it's loaded, is redacted from every line. `httpx` and `telegram` loggers are raised to WARNING because Telegram URLs embed the bot token.
