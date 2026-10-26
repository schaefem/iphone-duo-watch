#!/usr/bin/env bash
# Einrichtung des iPhone-Duo-Wächters auf einem Raspberry Pi (Raspberry Pi OS 64-bit)
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "== iPhone-Duo-Wächter: Einrichtung in $DIR =="

if [ "$(uname -m)" != "aarch64" ]; then
  echo "WARNUNG: Kein 64-bit-System ($(uname -m)). Für die Browser-Tarnung (curl_cffi)"
  echo "         wird Raspberry Pi OS 64-bit empfohlen. Weiter in 5 Sekunden ..."
  sleep 5
fi

# Windows-Zeilenenden entfernen, falls die Dateien unter Windows bearbeitet wurden
sed -i 's/\r$//' run.sh config.env.example 2>/dev/null || true
[ -f config.env ] && sed -i 's/\r$//' config.env

echo "-- Pakete installieren"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv

echo "-- Python-Umgebung anlegen"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q curl_cffi

echo "-- Zeitzone auf Europe/Berlin setzen"
sudo timedatectl set-timezone Europe/Berlin || true

if [ ! -f config.env ]; then
  cp config.env.example config.env
  read -rp "Dein ntfy-Thema (wie in der ntfy-App abonniert): " TOPIC
  sed -i "s|^NTFY_TOPIC=.*|NTFY_TOPIC=$TOPIC|" config.env
fi
chmod 600 config.env
chmod +x run.sh

echo "-- Cronjob: alle 5 Minuten von 6 bis 22 Uhr"
( crontab -l 2>/dev/null | grep -v "$DIR/run.sh" ; echo "*/5 6-21 * * * $DIR/run.sh" ) | crontab -

echo "-- Testlauf (mit Test-Push) ..."
TEST_PUSH=1 ./run.sh || true
echo
tail -n 40 logs/watch.log
echo
echo "-- Dashboard einrichten"
bash "$DIR/install_dashboard.sh" || echo "Dashboard-Einrichtung fehlgeschlagen (Wächter läuft trotzdem)."
echo "== Fertig. Log ansehen: tail -f $DIR/logs/watch.log =="
