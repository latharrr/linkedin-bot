#!/bin/sh
while true; do
  python dispatcher.py >> logs/dispatcher.log 2>&1
  HOUR=$(date +%H); MIN=$(date +%M)
  if [ "$HOUR" = "08" ] && [ "$MIN" -lt 5 ]; then python bot.py --nudge >> logs/nudge.log 2>&1; fi
  if [ "$HOUR" = "14" ] && [ "$MIN" -lt 5 ]; then python bot.py --fallback >> logs/fallback.log 2>&1; fi
  sleep 300
done
