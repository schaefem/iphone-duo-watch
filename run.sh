#!/usr/bin/env bash
# Startet Apple- und Händler-Wächter nacheinander (wird per Cron alle 5 Minuten aufgerufen).
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1

# Nie zwei Läufe gleichzeitig
exec 9>"$DIR/.lock"
flock -n 9 || exit 0

# Einstellungen laden
set -a
[ -f "$DIR/config.env" ] && . "$DIR/config.env"
set +a

mkdir -p "$DIR/logs" "$DIR/state"
LOG="$DIR/logs/watch.log"
PY="$DIR/.venv/bin/python"
TRIGGER="${TRIGGER:-cron}"
START=$(date +%s)

# Laufprotokoll für das Dashboard
echo "{\"start\": $START, \"trigger\": \"$TRIGGER\", \"running\": true}" > "$DIR/state/run.json"

{
  echo "===== $(date '+%d.%m.%Y %H:%M:%S') ($TRIGGER) ====="
  "$PY" check_availability.py; RC1=$?
  "$PY" check_retailers.py; RC2=$?
} >> "$LOG" 2>&1

END=$(date +%s)
echo "{\"start\": $START, \"end\": $END, \"trigger\": \"$TRIGGER\", \"running\": false, \"rc_apple\": ${RC1:-0}, \"rc_retailers\": ${RC2:-0}}" > "$DIR/state/run.json"
[ "$TRIGGER" = "cron" ] && cp "$DIR/state/run.json" "$DIR/state/run_cron.json"

# Log klein halten (letzte 5000 Zeilen)
tail -n 5000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
