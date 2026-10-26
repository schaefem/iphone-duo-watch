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

{
  echo "===== $(date '+%d.%m.%Y %H:%M:%S') ====="
  "$PY" check_availability.py
  "$PY" check_retailers.py
} >> "$LOG" 2>&1

# Log klein halten (letzte 5000 Zeilen)
tail -n 5000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
