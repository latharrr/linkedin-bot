#!/bin/sh
while true; do
  python dispatcher.py >> logs/dispatcher.log 2>&1
  HOUR=$(date +%H); MIN=$(date +%M)
  # The daily 08:00 topic nudge is retired (Oct 26): daily drafting is owned by the
  # Instinct pipeline under his Oct 25 "draft, review and post without asking each
  # day" direction - a standing "what's today's topic?" ask is what he opted out of.
  if [ "$HOUR" = "14" ] && [ "$MIN" -lt 5 ]; then python bot.py --fallback >> logs/fallback.log 2>&1; fi
  sleep 300
done
