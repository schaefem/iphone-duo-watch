#!/usr/bin/env python3
"""
iPhone Duo Abholungs-Wächter
----------------------------
Fragt Apples "Abholung im Store"-Schnittstelle ab und schickt bei neuer
Verfügbarkeit eine Push-Nachricht über ntfy. Alle 3 Stunden kommt zusätzlich
ein kurzes Lebenszeichen, wenn nichts gefunden wurde.

Konfiguration über Umgebungsvariablen (siehe README.md).
Keine externen Abhängigkeiten, nur Python-Standardbibliothek.
"""

import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# --- Konfiguration -----------------------------------------------------------

COUNTRY = os.getenv("APPLE_COUNTRY", "de").strip() or "de"

# STORE_FILTER: "*" = alle Stores, sonst kommagetrennte Städte/Storenamen
_store_filter_raw = (os.getenv("STORE_FILTER") or "Dresden").strip()
ALL_STORES = _store_filter_raw == "*"
STORE_FILTER = [] if ALL_STORES else [s.strip().lower() for s in _store_filter_raw.split(",") if s.strip()]

# PLZ, von denen aus gesucht wird. Apple liefert pro Abfrage nur Stores im Umkreis.
# Für ganz Deutschland verteilen wir Abfragepunkte übers Land; doppelte Stores werden entfernt.
DE_ANCHORS = ["01067", "04109", "10117", "20095", "30159", "40212", "50667",
              "60311", "66111", "70173", "80331", "86150", "90402"]
_loc_raw = (os.getenv("LOCATION") or "").strip()
if _loc_raw and "," in _loc_raw:
    LOCATIONS = [p.strip() for p in _loc_raw.split(",") if p.strip()]
elif ALL_STORES and COUNTRY == "de" and _loc_raw in ("", "01067"):
    LOCATIONS = DE_ANCHORS
else:
    LOCATIONS = [_loc_raw or "01067"]

PARTS_ENV = [p.strip() for p in (os.getenv("PARTS") or "").split(",") if p.strip()]
TITLE_MUST_CONTAIN = (os.getenv("TITLE_MUST_CONTAIN") or "iPhone Duo").lower()
TITLE_EXCLUDE = [s.strip().lower() for s in (os.getenv("TITLE_EXCLUDE") or "case,folio,hülle,cover").split(",") if s.strip()]

NTFY_SERVER = (os.getenv("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = (os.getenv("NTFY_TOPIC") or "").strip()

PING_HOURS = float(os.getenv("PING_HOURS") or "3")   # 0 = kein Lebenszeichen

STATE_FILE = os.getenv("STATE_FILE", "state/state.json")
DEBUG = os.getenv("DEBUG", "0") == "1"
TEST_PUSH = os.getenv("TEST_PUSH", "0") == "1"

PRODUCT_URL = f"https://www.apple.com/{COUNTRY}/shop/buy-iphone/iphone-duo"
FULFILLMENT_URL = f"https://www.apple.com/{COUNTRY}/shop/fulfillment-messages"
PICKUP_URL = f"https://www.apple.com/{COUNTRY}/shop/retail/pickup-message"

PARTS_REFRESH_SECONDS = 24 * 3600   # Teilenummern einmal täglich neu ermitteln
ERROR_ALERT_AFTER = 3               # nach so vielen Fehlläufen in Folge warnen
REQUEST_PAUSE = 1.0                 # Sekunden zwischen Anfragen
TZ = ZoneInfo("Europe/Berlin")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.6",
}


# --- Hilfsfunktionen ---------------------------------------------------------

def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}Z] {msg}", flush=True)


COOKIES = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(COOKIES))
_page_cache: str | None = None


try:
    from curl_cffi import requests as _cffi   # Browser-Fingerabdruck von Chrome (auf dem Pi installiert)
    _SESSION = _cffi.Session(impersonate="chrome")
except Exception:
    _SESSION = None

# Antworten, die auf Sperre/Drosselung durch Apple hindeuten
BLOCK_CODES = {403, 429, 503, 541}
BLOCK_STEPS_MIN = [15, 30, 60, 120, 240]


class AppleBlocked(Exception):
    pass


def http_get(url: str, accept: str, referer: str | None = None, xhr: bool = False) -> str:
    headers = {} if _SESSION is not None else dict(HEADERS)
    headers["Accept"] = accept
    headers["Accept-Language"] = HEADERS["Accept-Language"]
    if referer:
        headers["Referer"] = referer
    if xhr:
        headers["X-Requested-With"] = "XMLHttpRequest"
        headers["Sec-Fetch-Mode"] = "cors"
        headers["Sec-Fetch-Site"] = "same-origin"
        headers["Sec-Fetch-Dest"] = "empty"
    if _SESSION is not None:
        r = _SESSION.get(url, headers=headers, timeout=20)
        if r.status_code != 200:
            log(f"HTTP {r.status_code} für {url.split('?')[0]} – Antwort: {r.text[:300]!r}")
            raise urllib.error.HTTPError(url, r.status_code, r.reason or "Fehler", None, None)
        return r.text
    req = urllib.request.Request(url, headers=headers)
    try:
        with OPENER.open(req, timeout=20) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            body = ""
        log(f"HTTP {e.code} für {url.split('?')[0]} – Antwort: {body!r}")
        raise


def product_page() -> str:
    """Lädt die Produktseite einmal pro Lauf (setzt dabei auch Session-Cookies)."""
    global _page_cache
    if _page_cache is None:
        _page_cache = http_get(PRODUCT_URL, accept="text/html,application/xhtml+xml")
        log(f"Produktseite geladen, {len(COOKIES)} Cookies erhalten.")
    return _page_cache


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def push(title: str, message: str, priority: int = 5, tags=None, click: str | None = None) -> bool:
    if not NTFY_TOPIC:
        log("WARNUNG: NTFY_TOPIC ist nicht gesetzt – keine Push-Nachricht gesendet.")
        return False
    payload = {
        "topic": NTFY_TOPIC,
        "title": title,
        "message": message,
        "priority": priority,
        "tags": tags or [],
    }
    if click:
        payload["click"] = click
    req = urllib.request.Request(
        NTFY_SERVER + "/",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            log(f"Push gesendet ({resp.status}): {title}")
            return True
    except Exception as e:  # Push-Fehler sollen den Lauf nicht abbrechen
        log(f"Push fehlgeschlagen: {e}")
        return False


# --- Teilenummern ermitteln --------------------------------------------------

PART_RE = re.compile(r"\b([A-Z0-9]{5}ZD/A)\b") if COUNTRY == "de" else \
          re.compile(r"\b([A-Z0-9]{5}[A-Z]{1,2}/A)\b")


def discover_parts() -> list[str]:
    """Liest die Teilenummern aus der Apple-Produktseite (Zubehör wird später per Titel gefiltert)."""
    html = product_page().replace("\\u002F", "/").replace("\\/", "/")
    parts = sorted(set(PART_RE.findall(html)))
    log(f"{len(parts)} Teilenummern auf der Produktseite gefunden.")
    if DEBUG:
        log("Gefundene Teilenummern: " + ", ".join(parts))
    return parts


def get_parts(state: dict) -> list[str]:
    if PARTS_ENV:
        return PARTS_ENV
    cached = state.get("parts") or []
    age = time.time() - state.get("parts_ts", 0)
    if cached and age < PARTS_REFRESH_SECONDS:
        return cached
    try:
        parts = discover_parts()
    except Exception as e:
        log(f"Teilenummern konnten nicht ermittelt werden: {e}")
        parts = []
    if parts:
        state["parts"] = parts
        state["parts_ts"] = time.time()
        return parts
    return cached  # notfalls die alten weiterverwenden


# --- Verfügbarkeit abfragen --------------------------------------------------

# (Name, URL, feste Parameter, Teile pro Anfrage). Die PLZ wird pro Anfrage ergänzt.
VARIANTS = [
    ("pickup-multi", PICKUP_URL, {"pl": "true"}, 8),
    ("pickup-message", PICKUP_URL, {"pl": "true"}, 1),
    ("fulfillment-voll", FULFILLMENT_URL,
     {"fae": "true", "little": "false", "mts.0": "regular", "mts.1": "sticky", "searchNearby": "true"}, 4),
    ("fulfillment-minimal", FULFILLMENT_URL, {"pl": "true", "mts.0": "regular"}, 1),
]
VARIANT_STATE_KEY = "variant_v2"   # neuer Schlüssel -> nach Update einmal neu prüfen


def query(parts: list[str], location: str, variant) -> dict:
    name, base, fixed, _ = variant
    params = dict(fixed)
    params["location"] = location
    for i, p in enumerate(parts):
        params[f"parts.{i}"] = p
    url = base + "?" + urllib.parse.urlencode(params)
    raw = http_get(url, accept="application/json, text/plain, */*", referer=PRODUCT_URL, xhr=True)
    if DEBUG:
        log(f"Rohantwort [{name} @ {location}] (gekürzt): " + raw[:600])
    return json.loads(raw)


def find_variant(state: dict, parts: list[str]):
    """Liefert die zuletzt funktionierende Variante oder probiert alle durch.
    Eine Variante gilt nur, wenn die Antwort ALLE angefragten Teile enthält."""
    order = list(VARIANTS)
    saved = state.get(VARIANT_STATE_KEY)
    order.sort(key=lambda v: 0 if v[0] == saved else 1)
    errors = []
    for v in order:
        batch = parts[:v[3]]
        try:
            data = query(batch, LOCATIONS[0], v)
            got = {e["part"] for e in extract(data)}
            if got and set(batch) <= got:
                if v[0] != saved:
                    log(f"Funktionierende Abfrage gefunden: {v[0]} ({v[3]} Teil(e) pro Anfrage)")
                state[VARIANT_STATE_KEY] = v[0]
                return v, data
            errors.append(f"{v[0]}: unvollständige Antwort ({len(got)}/{len(batch)} Teile)")
            log(f"Variante {v[0]}: nur {len(got)}/{len(batch)} Teile in der Antwort.")
        except urllib.error.HTTPError as e:
            errors.append(f"{v[0]}: HTTP {e.code}")
            if v[0] == saved and e.code in BLOCK_CODES:
                # Die bisher funktionierende Abfrage wird abgewiesen -> Sperre, nicht weiter probieren
                raise AppleBlocked(f"HTTP {e.code} bei der bisher funktionierenden Abfrage ({v[0]})")
        except Exception as e:
            errors.append(f"{v[0]}: {e}")
            log(f"Variante {v[0]} fehlgeschlagen: {e}")
        time.sleep(REQUEST_PAUSE)
    if errors and all(any(f"HTTP {c}" in x for c in BLOCK_CODES) for x in errors):
        raise AppleBlocked("Alle Abfragen abgewiesen – " + "; ".join(errors))
    raise RuntimeError("Keine Abfragevariante funktioniert – " + "; ".join(errors))


def extract(data: dict) -> list[dict]:
    """Zieht (Store, Teil, Titel, Status) aus der Antwort."""
    out = []
    body = data.get("body") or {}
    content = body.get("content") or {}
    stores = ((content.get("pickupMessage") or {}).get("stores")
              or body.get("stores")            # Format von /retail/pickup-message
              or [])
    for st in stores:
        store_name = st.get("storeName") or st.get("storeNumber") or "?"
        store_id = st.get("storeNumber") or store_name
        city = st.get("city") or ""
        for part, pa in (st.get("partsAvailability") or {}).items():
            regular = (pa.get("messageTypes") or {}).get("regular") or {}
            title = (regular.get("storePickupProductTitle")
                     or pa.get("storePickupProductTitle") or part)
            quote = (pa.get("pickupSearchQuote") or regular.get("storePickupQuote")
                     or pa.get("storePickupQuote") or "")
            out.append({
                "store": store_name,
                "store_id": store_id,
                "city": city,
                "part": part,
                "title": title,
                "display": (pa.get("pickupDisplay") or "").lower(),
                "quote": re.sub(r"<[^>]+>", "", quote),
            })
    return out


def is_wanted(entry: dict) -> bool:
    title = entry["title"].lower()
    if entry["title"] != entry["part"]:  # Titel bekannt -> nach Titel filtern
        if TITLE_MUST_CONTAIN not in title:
            return False
        if any(x in title for x in TITLE_EXCLUDE):
            return False
    if ALL_STORES:
        return True
    where = f"{entry['store']} {entry['city']}".lower()
    return any(f in where for f in STORE_FILTER)


# --- Hauptablauf -------------------------------------------------------------

def now_local() -> str:
    return datetime.now(TZ).strftime("%H:%M")


def main() -> int:
    state = load_state()

    if TEST_PUSH:
        push("Test: iPhone-Duo-Wächter", "Wenn du das liest, kommen die Push-Nachrichten an. 👍",
             priority=3, tags=["white_check_mark"])

    if os.getenv("FORCE") != "1" and time.time() < state.get("block_until", 0):
        until = datetime.fromtimestamp(state["block_until"], TZ).strftime("%H:%M")
        log(f"Apple: Pause wegen Sperre/Drosselung bis {until} Uhr – übersprungen.")
        save_state(state)
        return 0

    try:
        parts = get_parts(state)
        if not parts:
            raise RuntimeError("Keine Teilenummern bekannt (Produktseite nicht lesbar und PARTS nicht gesetzt).")

        variant, first = find_variant(state, parts)
        size = variant[3]

        # Alle Abfragepunkte x alle Teile-Pakete; doppelte Stores werden zusammengeführt
        merged: dict[str, dict] = {}
        for e in extract(first):
            merged[f"{e['part']}@{e['store_id']}"] = e
        jobs = [(loc, parts[i:i + size]) for loc in LOCATIONS for i in range(0, len(parts), size)]
        jobs = jobs[1:]  # erste Anfrage wurde schon in find_variant gestellt
        failed = 0
        for loc, batch in jobs:
            time.sleep(REQUEST_PAUSE)
            try:
                for e in extract(query(batch, loc, variant)):
                    merged[f"{e['part']}@{e['store_id']}"] = e
            except Exception as ex:
                failed += 1
                log(f"Abfrage für PLZ {loc} fehlgeschlagen: {ex}")
        if failed and failed == len(jobs):
            raise RuntimeError("Alle Folgeabfragen sind fehlgeschlagen.")

        entries = list(merged.values())
        if not entries:
            raise RuntimeError("Antwort enthielt keine Store-Daten – evtl. hat Apple das Format geändert.")

        wanted = [e for e in entries if is_wanted(e)]
        stores_seen = sorted({f"{e['store']} ({e['city']})" for e in wanted})
        log(f"{len(LOCATIONS)} Suchorte, {len(stores_seen)} Stores geprüft: {', '.join(stores_seen)}")
        if not wanted:
            log(f"WARNUNG: Kein Store passt zum Filter {STORE_FILTER}. Filter prüfen!")

        available = {f"{e['part']}@{e['store_id']}": e for e in wanted if e["display"] == "available"}
        if DEBUG or not ALL_STORES:
            for e in sorted(wanted, key=lambda x: (x["city"], x["store"], x["title"])):
                log(f"  {e['title']:<34} {e['store']:<26} {e['display'] or '-'}")
        else:
            log(f"  {len(wanted)} Einträge geprüft, davon {len(available)} verfügbar.")

        previously = set(state.get("available", []))
        new_keys = [k for k in available if k not in previously]

        state["checks_since_ping"] = state.get("checks_since_ping", 0) + 1

        if new_keys:
            lines = [f"• {available[k]['title']} – {available[k]['store']} ({available[k]['city']})"
                     for k in new_keys]
            if push(
                "📱 iPhone Duo abholbereit!",
                "\n".join(lines) + "\n\nSchnell in der Apple Store App reservieren.",
                priority=5,
                tags=["rotating_light"],
                click=PRODUCT_URL,
            ):
                state["last_ping"] = time.time()
                state["checks_since_ping"] = 0
        else:
            log("Keine neue Verfügbarkeit.")
            maybe_ping(state, len(stores_seen), len(available))

        state["available"] = sorted(available.keys())
        state["last_status"] = [{"variant": e["title"], "store": f"{e['store']} ({e['city']})",
                                 "status": e["display"] or "?"} for e in wanted]
        state["last_error"] = None
        state["errors"] = 0
        state["last_ok"] = datetime.now(timezone.utc).isoformat()

    except AppleBlocked as e:
        return handle_block(state, str(e))
    except urllib.error.HTTPError as e:
        return handle_error(state, f"HTTP {e.code} von Apple ({e.reason})")
    except Exception as e:
        return handle_error(state, str(e))

    if state.get("block_level"):
        log("Apple antwortet wieder normal – Sperre aufgehoben.")
        push("✅ Apple wieder erreichbar", "Die Abfragen bei Apple funktionieren wieder.", priority=2, tags=["white_check_mark"])
    state["block_level"] = 0
    state["block_until"] = 0
    save_state(state)
    return 0


def handle_block(state: dict, msg: str) -> int:
    """Apple weist ab: wachsende Pause statt weiter anzufragen (verschärft die Sperre sonst nur)."""
    level = state.get("block_level", 0)
    minutes = BLOCK_STEPS_MIN[min(level, len(BLOCK_STEPS_MIN) - 1)]
    state["block_level"] = level + 1
    state["block_until"] = time.time() + minutes * 60
    state["last_error"] = f"Apple sperrt/drosselt – Pause {minutes} Min. ({msg})"
    state["last_error_at"] = datetime.now(timezone.utc).isoformat()
    until = datetime.fromtimestamp(state["block_until"], TZ).strftime("%H:%M")
    log(f"SPERRE: {msg} – pausiere Apple {minutes} Minuten (bis {until} Uhr).")
    if level == 0:
        push("⏸️ Apple drosselt die Abfragen",
             f"Apple weist die Abfragen gerade ab. Der Wächter pausiert Apple bis {until} Uhr und "
             f"versucht es dann mit wachsendem Abstand erneut. MediaMarkt, Saturn und Cyberport laufen weiter.",
             priority=2, tags=["pause_button"])
    save_state(state)
    return 0


def maybe_ping(state: dict, store_count: int, still_available: int) -> None:
    """Lebenszeichen alle PING_HOURS Stunden, wenn nichts Neues gefunden wurde."""
    if PING_HOURS <= 0:
        return
    last = state.get("last_ping")
    if last is None:  # erster Lauf: Zähler starten, nicht sofort pingen
        state["last_ping"] = time.time()
        return
    if time.time() - last < PING_HOURS * 3600 - 120:  # 2 Min. Puffer für GitHub-Verzögerungen
        return
    n = state.get("checks_since_ping", 0)
    extra = f"\n{still_available} Variante(n) weiterhin abholbereit (bereits gemeldet)." if still_available else ""
    if push(
        "✅ Wächter läuft – nichts Neues",
        f"Stand {now_local()} Uhr: {n} Prüfungen seit dem letzten Lebenszeichen, "
        f"{store_count} Store(s) überwacht, kein neues iPhone Duo gefunden.{extra}",
        priority=2,
        tags=["eyes"],
    ):
        state["last_ping"] = time.time()
        state["checks_since_ping"] = 0


def handle_error(state: dict, msg: str) -> int:
    log(f"FEHLER: {msg}")
    state["errors"] = state.get("errors", 0) + 1
    state["last_error"] = msg
    state["last_error_at"] = datetime.now(timezone.utc).isoformat()
    n = state["errors"]
    # einmal nach ERROR_ALERT_AFTER Fehlern warnen, danach etwa einmal täglich (alle 192 Läufe)
    if n == ERROR_ALERT_AFTER or (n > ERROR_ALERT_AFTER and n % 192 == 0):
        push("⚠️ iPhone-Duo-Wächter hat Probleme",
             f"{n} Fehlläufe in Folge. Letzter Fehler: {msg}\nDetails im Dashboard bzw. im Log.",
             priority=3, tags=["warning"])
    save_state(state)
    return 1 if DEBUG else 0  # im Normalbetrieb den Workflow nicht rot färben


if __name__ == "__main__":
    sys.exit(main())
