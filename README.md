# iPhone Duo Wächter

Prüft alle 5 Minuten (ca. 6–22 Uhr), ob im Apple Store Dresden ein iPhone Duo
zur Abholung verfügbar ist – alle Speichergrößen, beide Farben. Bei einem Treffer
kommt sofort eine Push-Nachricht über **ntfy** aufs Handy.

## Einrichtung (ca. 10 Minuten)

1. **ntfy-App** installieren (iOS/Android, kostenlos).
   Ein Thema abonnieren mit einem schwer erratbaren Namen, z. B.
   `duo-dresden-7f3k9q2x`. Themen auf ntfy.sh sind öffentlich – wer den Namen kennt,
   kann mitlesen, daher zufällig wählen.

2. **Neues GitHub-Repository** anlegen, **öffentlich** (Public).
   Nur so sind die Actions-Minuten unbegrenzt kostenlos. Bei einem privaten Repo
   sind 2.000 Minuten/Monat frei; alle 5 Minuten verbraucht das ca. 5.800 Minuten.
   Im Code steht nichts Geheimes, das ntfy-Thema liegt als Secret.

3. Dateien hochladen: `check_availability.py` und `.github/workflows/iphone-duo-watch.yml`
   (der Ordner `.github` muss genau so heißen).

4. **Secret anlegen:** Settings → Secrets and variables → Actions → *New repository secret*
   - Name: `NTFY_TOPIC`
   - Wert: dein Themenname aus Schritt 1

5. **Erster Testlauf:** Reiter *Actions* → „iPhone Duo Wächter“ → *Run workflow*
   (beide Häkchen an lassen).
   - Es sollte ein Test-Push auf dem Handy ankommen.
   - Im Log siehst du die Rohantwort von Apple, die gefundenen Teilenummern und
     den Status pro Variante im Store Dresden.

Ab dann läuft der Wächter automatisch.

## Optionale Variablen

Unter Settings → Secrets and variables → Actions → *Variables*:

| Variable       | Standard  | Zweck |
|----------------|-----------|-------|
| `STORE_FILTER` | `Dresden` | Kommagetrennte Stadt-/Storenamen, z. B. `Dresden,Berlin` – oder `*` für **alle Apple Stores in Deutschland** |
| `LOCATION`     | `01067`   | PLZ für die Store-Suche. Bei `STORE_FILTER=*` wird automatisch quer durch Deutschland gesucht. Eigene Liste möglich: `01067,10117,80331` |
| `PING_HOURS`   | `3`       | Lebenszeichen alle X Stunden, wenn nichts gefunden wurde (`0` = aus) |
| `PARTS`        | (leer)    | Feste Teilenummern, falls die automatische Erkennung nicht klappt, z. B. `MABC1ZD/A,MABC2ZD/A` |

## Wenn es nicht klappt

- **Push „Wächter hat Probleme“** oder Fehler im Log: Workflow manuell mit Debug starten
  und die Log-Ausgabe an Claude geben – dann wird das Skript angepasst.
- **0 Teilenummern gefunden:** Teilenummern im Apple Store (Konfigurator) bzw. aus
  einer Händlerseite ablesen und als Variable `PARTS` eintragen.
- **HTTP 403/503 von Apple:** Apple blockt evtl. die GitHub-Server. Dann wäre die
  Alternative, das Skript lokal auf einem Rechner/Raspberry Pi laufen zu lassen.

## Hinweise

- GitHub startet geplante Läufe teils mit einigen Minuten Verzögerung.
- Bei öffentlichen Repos pausiert GitHub geplante Workflows nach 60 Tagen ohne
  Repo-Aktivität – ein kleiner Commit weckt sie wieder.
- Benachrichtigt wird nur bei **neuer** Verfügbarkeit, nicht bei jedem Lauf.
- Apple-Stores bekommen Ware oft frühmorgens. Bei einem Treffer direkt über die
  Apple Store App „Abholen“ wählen, das geht schneller als hinfahren.
