# Projekt: iPhone-Duo-Wächter

**Ziel:** Ein iPhone Duo (Apples erstes Foldable, Verkaufsstart 23.10.2026, online bis Ende Januar
ausverkauft) lokal zur Abholung ergattern. Mehrere Händler werden automatisch überwacht; bei
Verfügbarkeit kommt sofort eine Push-Nachricht aufs Handy (ntfy).

**Stand:** 26.10.2026 – Apple-, MediaMarkt-, Saturn- und Cyberport-Abfragen funktionieren
(getestet). Offen: Dauerbetrieb auf dem Raspberry Pi 4 einrichten.

---

## Überwachung

| Händler | Varianten | Filiale | Technik | Takt |
|---|---|---|---|---|
| Apple | alle 8 | Apple Store Altmarkt-Galerie Dresden (R430), optional alle DE-Stores | `check_availability.py` | 5 Min. |
| MediaMarkt | alle 8 | Markt **460** + „irgendwo abholbar“ | `check_retailers.py` | 5 Min. |
| Saturn | alle 8 | Markt **12** + „irgendwo abholbar“ | `check_retailers.py` | 5 Min. |
| Cyberport | alle 8 | „in mind. einem Store“ (u. a. Dresden Elbepark / Waldschlösschen) | `check_retailers.py` | 15 Min. |

Laufzeit 6–22 Uhr. Push nur bei **neuer** Verfügbarkeit; zusätzlich Lebenszeichen alle 3 Std.
und Warn-Push nach mehreren Fehlläufen in Folge.

**Varianten:** 256 GB / 512 GB / 1 TB / 2 TB × Sternenweiß (Star White) / Nachthimmel (Night Sky).
Preise DE: 2.299 / 2.549 / 3.049 / 3.799 €.

---

## Technische Erkenntnisse

### Apple
- `/de/shop/fulfillment-messages` liefert von Rechenzentren aus HTTP 541 („Page Not Found“).
- **Funktioniert:** `https://www.apple.com/de/shop/retail/pickup-message?pl=true&location=01067&parts.0=…`
  (bis zu 8 Teile pro Anfrage). Antwort: `body.stores[].partsAvailability[part].pickupDisplay`
  (`available` / `unavailable`).
- Teilenummern: MK2D4ZD/A, MK2E4ZD/A, MK2F4ZD/A, MK2G4ZD/A, MK2H4ZD/A, MK2J4ZD/A, MK2K4ZD/A, MK2L4ZD/A
  (in dieser Reihenfolge 256 GB weiß/nacht … 2 TB weiß/nacht). Werden automatisch von der Produktseite gelesen.
- Pro PLZ nur Stores im Umkreis → für „alle Stores“ 13 PLZ-Ankerpunkte quer durch DE.

### MediaMarkt / Saturn (gleiche Plattform)
- Produkt-IDs (in beiden Shops gleich): 3071630–3071637
  (256 weiß, 256 nacht, 512 weiß, 512 nacht, 1 TB weiß, 1 TB nacht, 2 TB weiß, 2 TB nacht).
- JSON-LD der Produktseite ist **unzuverlässig** (meldet `InStock` trotz „keine Lieferung“) → nicht verwenden.
- Filialbestand kommt **nicht** im HTML (zeigt immer `NO_STORE_SELECTED`), sondern per GraphQL:
  - `GET /api/v1/graphql?operationName=GetCofrPickupFeaturePickups&variables=…&extensions=…`
  - Persisted-Query-Hash: `81938af9c97c7bf5419b70d29819aba4cdee672fba4b020a5178dadd3b0cd979`
  - `variables.productIds` = Liste (alle 8 in einer Anfrage), `variables.storeId` = Markt-ID
  - `extensions.pwa.salesLine` = `Media` bzw. `Saturn`
  - Pflicht-Header: `Apollographql-Client-Name: pwa-client-pqm`, `Apollographql-Client-Version: 8.517.0`,
    `X-Operation`, `X-Flow-Id` (UUID), `X-Mms-Country/Language/Salesline`, `X-Cacheable: true`, `Sec-Fetch-*`
  - Antwort: `cofrPickupFeature.displayStatus` (eigener Markt), `pickupStatus` (z. B. `NOT_AVAILABLE_ANYWHERE`)
  - Ohne Browser-Cookie lauffähig, wenn vorher die Produktseite in derselben Session geladen wird.
- Ändert sich die Client-Version / der Hash: per `MMS_CLIENT_VERSION` / `MMS_PICKUP_HASH` anpassbar
  (Werte in Chrome-DevTools → Network → Filter `graphql` ablesen).

### Cyberport
- Next.js-Seite; Status in `__NEXT_DATA__ → props.pageProps.fullPageProps.dynamicProps.productVariable`:
  `InStock`, `AvailabilityStatusType` (z. B. `UNAVAILABLE`), `StockLocal` (= in mind. einem Store).
- Artikelnummern A415-22M … A415-22U. Empfindlich gegen viele Anfragen (HTTP 429) →
  15-Min.-Takt, 3 s Abstand, 15 Min. Pause nach 429.

### Infrastruktur
- **MediaMarkt, Saturn, Cyberport blocken Rechenzentrums-IPs** (GitHub Actions USA und Hetzner DE: HTTP 403),
  auch mit Browser-Tarnung (`curl_cffi`, impersonate=chrome). Vom privaten Anschluss: funktioniert.
- **GitHub-Actions-Zeitplan** (`*/5`) ist im neuen Repo nie automatisch angelaufen → unzuverlässig.
- Deshalb: Dauerbetrieb auf **Raspberry Pi 4** (Raspberry Pi OS Lite 64-bit) am privaten Anschluss.

---

## Dateien (Repo `schaefem/iphone-duo-watch`, öffentlich)

| Datei | Zweck |
|---|---|
| `check_availability.py` | Apple-Wächter (nur Standardbibliothek) |
| `check_retailers.py` | MediaMarkt/Saturn/Cyberport (`curl_cffi`) |
| `run.sh` | startet beide nacheinander, Lock, Log (`logs/watch.log`, max. 5000 Zeilen) |
| `install.sh` | Pi-Einrichtung: venv + curl_cffi, Zeitzone, `config.env`, Cronjob `*/5 6-21 * * *`, Testlauf |
| `config.env.example` | Einstellungen (Vorlage; echte `config.env` nur auf dem Pi, nicht im Repo) |
| `.github/workflows/iphone-duo-watch.yml` | alter GitHub-Workflow (zu deaktivieren) |

**Wichtige Einstellungen (`config.env`):** `NTFY_TOPIC`, `STORE_FILTER` (`Dresden` / `*`),
`PING_HOURS`, `MEDIAMARKT_STORES`, `SATURN_STORES`, `CYBERPORT_EVERY_MIN`, `DEBUG`.

---

## Nächste Schritte
1. Neue Dateien (install.sh, run.sh, config.env.example, .gitignore, README.md, check_retailers.py) ins Repo laden.
2. Pi: Raspberry Pi OS Lite 64-bit, SSH an → `git clone …` → `bash install.sh`.
3. Testlauf-Log prüfen, Test-Push abwarten.
4. GitHub-Workflow deaktivieren (Actions → Disable workflow).

## Ideen / offen
- Weitere Märkte ergänzen (MediaMarkt/Saturn: weitere Markt-IDs kommagetrennt).
- Bei Cyberport ggf. Store-genaue Abfrage (API hinter „Verfügbarkeit prüfen“) – bisher nur „irgendein Store“.
- Bei einem Treffer schnell reservieren: Apple Store App → „Abholen“; MediaMarkt/Saturn Seite → Markt-Abholung.
