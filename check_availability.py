#!/usr/bin/env python3
"""
iPhone Duo Abholungs-Wächter
----------------------------
Fragt Apples "Abholung im Store"-Schnittstelle ab und schickt bei neuer
Verfügbarkeit eine Push-Nachricht über ntfy.

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

# --- Konfiguration -----------------------------------------------------------

COUNTRY = os.getenv("APPLE_COUNTRY", "de").strip() or "de"
LOCATION = os.getenv("LOCATION", "01067").strip() or "01067"          # PLZ Dresden
STORE_FILTER = [s.strip().lower() for s in (os.getenv("STORE_FILTER") or "Dresden").split(",") if s.strip()]
PARTS_ENV = [p.strip() for p in (os.getenv("PARTS") or "").split(",") if p.strip()]
TITLE_MUST_CONTAIN = (os.getenv("TITLE_MUST_CONTAIN") or "iPhone Duo").lower()
TITLE_EXCLUDE = [s.strip().lower() for s in (os.getenv("TITLE_EXCLUDE") or "case,folio,hülle,cover").split(",") if s.strip()]

NTFY_SERVER = (os.getenv("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = (os.getenv("NTFY_TOPIC") or "").strip()

STATE_FILE = os.getenv("STATE_FILE", "state/state.json")
DEBUG = os.getenv("DEBUG", "0") == "1"
TEST_PUSH = os.getenv("TEST_PUSH", "0") == "1"

PRODUCT_URL = f"https://www.apple.com/{COUNTRY}/shop/buy-iphone/iphone-duo"
FULFILLMENT_URL = f"https://www.apple.com/{COUNTRY}/shop/fulfillment-messages"

PARTS_REFRESH_SECONDS = 24 * 3600   # Teilenummern einmal täglich neu ermitteln
BATCH_SIZE = 4                      # Teilenummern pro Anfrage
ERROR_ALERT_AFTER = 3               # nach so vielen Fehlläufen in Folge warnen

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


def http_get(url: str, accept: str, referer: str | None = None, xhr: bool = False) -> str:
    headers = dict(HEADERS)
    headers["Accept"] = accept
    if referer:
        headers["Referer"] = referer
    if xhr:
        headers["X-Requested-With"] = "XMLHttpRequest"
        headers["Sec-Fetch-Mode"] = "cors"
        headers["Sec-Fetch-Site"] = "same-origin"
    req = urllib.request.Request(url, headers=headers)
    try:
        with OPENER.open(req, timeout=20) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")[:500]
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


def push(title: str, message: str, priority: int = 5, tags=None, click: str | None = None) -> None:
    if not NTFY_TOPIC:
        log("WARNUNG: NTFY_TOPIC ist nicht gesetzt – keine Push-Nachricht gesendet.")
        return
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
    except Exception as e:  # Push-Fehler sollen den Lauf nicht abbrechen
        log(f"Push fehlgeschlagen: {e}")


# --- Teilenummern ermitteln --------------------------------------------------

PART_RE = re.compile(r"\b([A-Z0-9]{5}" + re.escape("ZD") + r"/A)\b") if COUNTRY == "de" else \
          re.compile(r"\b([A-Z0-9]{5}[A-Z]{1,2}/A)\b")


def discover_parts() -> list[str]:
    """Liest die Teilenummern aus der Apple-Produktseite (inkl. Zubehör – das wird später per Titel gefiltert)."""
    html = product_page()
    html = html.replace("\\u002F", "/").replace("\\/", "/")
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

PICKUP_URL = f"https://www.apple.com/{COUNTRY}/shop/retail/pickup-message"

# Verschiedene Abfrageformen – Apple ändert diese gelegentlich.
# (Name, URL, feste Parameter, Teile pro Anfrage)
VARIANTS = [
    ("fulfillment-voll", FULFILLMENT_URL,
     {"fae": "true", "little": "false", "mts.0": "regular", "mts.1": "sticky",
      "searchNearby": "true", "location": LOCATION}, 4),
    ("fulfillment-einzeln", FULFILLMENT_URL,
     {"fae": "true", "little": "false", "mts.0": "regular", "mts.1": "sticky",
      "searchNearby": "true", "location": LOCATION}, 1),
    ("fulfillment-minimal", FULFILLMENT_URL,
     {"pl": "true", "mts.0": "regular", "location": LOCATION}, 1),
    ("fulfillment-plz", FULFILLMENT_URL,
     {"pl": "true", "mts.0": "regular", "mts.1": "compact", "searchNearby": "true",
      "location": LOCATION}, 1),
    ("pickup-message", PICKUP_URL,
     {"pl": "true", "location": LOCATION}, 1),
    ("pickup-message-multi", PICKUP_URL,
     {"pl": "true", "cppart": "UNLOCKED/WW", "location": LOCATION}, 4),
]


def query_batch(parts: list[str], variant) -> dict:
    name, base, fixed, _ = variant
    params = dict(fixed)
    for i, p in enumerate(parts):
        params[f"parts.{i}"] = p
    url = base + "?" + urllib.parse.urlencode(params)
    raw = http_get(url, accept="application/json, text/plain, */*", referer=PRODUCT_URL, xhr=True)
    if DEBUG:
        log(f"Rohantwort [{name}] (gekürzt):\n" + raw[:3000])
    return json.loads(raw)


def find_variant(state: dict, parts: list[str]):
    """Liefert die zuletzt funktionierende Variante oder probiert alle durch."""
    order = list(VARIANTS)
    saved = state.get("variant")
    order.sort(key=lambda v: 0 if v[0] == saved else 1)
    errors = []
    for v in order:
        try:
            data = query_batch(parts[:v[3]], v)
            if extract(data):
                if v[0] != saved:
                    log(f"Funktionierende Abfrage gefunden: {v[0]}")
                state["variant"] = v[0]
                return v, data
            errors.append(f"{v[0]}: keine Store-Daten")
            log(f"Variante {v[0]}: Antwort ohne Store-Daten.")
        except urllib.error.HTTPError as e:
            errors.append(f"{v[0]}: HTTP {e.code}")
        except Exception as e:
            errors.append(f"{v[0]}: {e}")
            log(f"Variante {v[0]} fehlgeschlagen: {e}")
        time.sleep(1.5)
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
        city = st.get("city") or ""
        for part, pa in (st.get("partsAvailability") or {}).items():
            regular = (pa.get("messageTypes") or {}).get("regular") or {}
            title = (regular.get("storePickupProductTitle")
                     or pa.get("storePickupProductTitle") or part)
            quote = (pa.get("pickupSearchQuote") or regular.get("storePickupQuote")
                     or pa.get("storePickupQuote") or "")
            out.append({
                "store": store_name,
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
    where = f"{entry['store']} {entry['city']}".lower()
    return any(f in where for f in STORE_FILTER) if STORE_FILTER else True


# --- Hauptablauf -------------------------------------------------------------

def main() -> int:
    state = load_state()

    if TEST_PUSH:
        push("Test: iPhone-Duo-Wächter", "Wenn du das liest, kommen die Push-Nachrichten an. 👍",
             priority=3, tags=["white_check_mark"])

    try:
        parts = get_parts(state)
        if not parts:
            raise RuntimeError("Keine Teilenummern bekannt (Produktseite nicht lesbar und PARTS nicht gesetzt).")

        try:
            product_page()  # Session-Cookies holen, wie ein echter Browser
        except Exception as e:
            log(f"Produktseite nicht ladbar (weiter ohne Cookies): {e}")

        variant, first = find_variant(state, parts)
        size = variant[3]
        entries = extract(first)
        for i in range(size, len(parts), size):
            time.sleep(1.5)
            entries.extend(extract(query_batch(parts[i:i + size], variant)))

        if not entries:
            raise RuntimeError("Antwort enthielt keine Store-Daten – evtl. hat Apple das Format geändert.")

        wanted = [e for e in entries if is_wanted(e)]
        stores_seen = sorted({f"{e['store']} ({e['city']})" for e in entries})
        log(f"{len(entries)} Einträge, Stores in Reichweite: {', '.join(stores_seen)}")
        if not wanted:
            log(f"WARNUNG: Kein Store passt zum Filter {STORE_FILTER}. Filter prüfen!")

        available = {f"{e['part']}@{e['store']}": e for e in wanted if e["display"] == "available"}
        for e in wanted:
            log(f"  {e['title']:<40} {e['store']:<28} {e['display'] or '-'}  {e['quote']}")

        previously = set(state.get("available", []))
        new_keys = [k for k in available if k not in previously]

        if new_keys:
            lines = [f"• {available[k]['title']} – {available[k]['store']}"
                     + (f" ({available[k]['quote']})" if available[k]['quote'] else "")
                     for k in new_keys]
            push(
                "📱 iPhone Duo abholbereit!",
                "\n".join(lines) + "\n\nSchnell in der Apple Store App reservieren.",
                priority=5,
                tags=["rotating_light"],
                click=PRODUCT_URL,
            )
        else:
            log("Keine neue Verfügbarkeit.")

        state["available"] = sorted(available.keys())
        state["errors"] = 0
        state["last_ok"] = datetime.now(timezone.utc).isoformat()

    except urllib.error.HTTPError as e:
        return handle_error(state, f"HTTP {e.code} von Apple ({e.reason})")
    except Exception as e:
        return handle_error(state, str(e))

    save_state(state)
    return 0


def handle_error(state: dict, msg: str) -> int:
    log(f"FEHLER: {msg}")
    state["errors"] = state.get("errors", 0) + 1
    n = state["errors"]
    # einmal nach ERROR_ALERT_AFTER Fehlern warnen, danach etwa einmal täglich (alle 192 Läufe)
    if n == ERROR_ALERT_AFTER or (n > ERROR_ALERT_AFTER and n % 192 == 0):
        push("⚠️ iPhone-Duo-Wächter hat Probleme",
             f"{n} Fehlläufe in Folge. Letzter Fehler: {msg}\nBitte Log in GitHub Actions ansehen.",
             priority=3, tags=["warning"])
    save_state(state)
    return 1 if DEBUG else 0  # im Normalbetrieb den Workflow nicht rot färben


if __name__ == "__main__":
    sys.exit(main())
