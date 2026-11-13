# LinkedIn posting bot

A Telegram-controlled draft and publishing app. Python services use Supabase for the post queue and settings, with separate bot and scheduler containers. This repository contains source and safe configuration examples, not live credentials or private account data.

## Current status

The owner's deployment is paused. Both containers have automatic restart disabled. Cloning this repository does not restore credentials, database rows, uploaded images or a LinkedIn login, and must not resume posting by itself. Review queued posts and obtain the account owner's approval before enabling the scheduler.

## Clone and set up

```sh
git clone https://github.com/latharrr/linkedin-bot.git
cd linkedin-bot
cp .env.example .env
chmod 600 .env
```

Use Docker Engine with Compose, or Python with the pinned requirements. Fill required values privately: Telegram token and allowed chat ID, Supabase URL/service key, model provider keys, and LinkedIn OAuth app credentials. See `.env.example` for names. Never commit `.env`, exported account data, tokens, logs, generated images, or database backups.

For a new database, review `scripts/schema.sql`, `scripts/storage.sql` and `scripts/usage.sql` before applying them in your own Supabase project. The storage script creates a public post-image bucket. Do not run migrations against an existing database without a backup. LinkedIn OAuth setup is in `scripts/linkedin_auth.py`; configure its localhost callback in your LinkedIn app. OAuth and provider commands may call external services or incur charges.

## Development and tests

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt
ruff check .
python -m pytest -q -p no:cacheprovider
```

Tests mock providers and block network access. A passing unit suite is not proof of live Telegram, LinkedIn or provider delivery. Local secrets are still needed for real use. No test result is claimed for this documentation-only update.

## Start deliberately

Only after reviewing credentials, account permissions, costs and the post queue:

```sh
docker compose build
docker compose up -d bot
docker compose ps
docker compose logs --tail 50 bot
```

The bot supports drafting and review in Telegram. Review text and image before approving or scheduling. Starting the scheduler can publish already queued posts:

```sh
# Read the queue without publishing first.
docker compose run --rm scheduler python dispatcher.py --dry-run
# Enable publication only when the owner wants it.
docker compose up -d scheduler
```

Direct enqueue scripts bypass the Telegram review screen. Do not use them without an approved draft, recipient/account and publication time. Do not treat this README as permission to post or spend.

## Stop and keep data

```sh
docker compose stop bot scheduler
docker update --restart=no linkedin-bot-bot-1 linkedin-bot-scheduler-1
```

The second command uses the current deployment's container names; check `docker compose ps -a` if yours differ. `docker compose up` can recreate containers and restore the restart policy from Compose, so it is an intentional resume action. Do not use `down -v` or delete the Supabase project to pause the app.

## Back up and restore

Keep source in Git. Back up the Supabase database and storage separately, and store credentials in a private secret store. Local `logs/` is not the database. Before restoring, keep both services stopped, restore and validate the database/storage, add private credentials, run tests, then start the bot alone. Inspect queued items before starting the scheduler.

## Main files

- `bot.py`: Telegram conversation and draft review.
- `dispatcher.py`: claims due posts and publishes them.
- `app/`: generation, grounding, usage accounting and publisher clients.
- `scripts/`: database schema, OAuth, corpus import and maintenance tools.
- `docker-compose.yml`, `Dockerfile`, `deploy/`: deployment examples.
- `tests/`: offline fixtures.

Provider failures or an uncertain publish result need investigation before retrying, to avoid duplicate posts. Use the app's approval flow rather than assuming chat text authorizes changes.
