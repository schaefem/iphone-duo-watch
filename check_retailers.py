#!/usr/bin/env python3
"""
Händler-Wächter (MediaMarkt, Saturn, Cyberport, ...)
----------------------------------------------------
Lädt die in RETAILER_URLS hinterlegten Produktseiten und wertet die
eingebetteten Produktdaten (schema.org / JSON-LD) aus. Springt ein Produkt
auf "lieferbar" bzw. "abholbereit", kommt eine Push-Nachricht über ntfy.

Im DEBUG-Modus werden zusätzlich alle Textstellen zu Abholung / Filiale
protokolliert – daraus wird im nächsten Schritt die Filialabfrage gebaut.

Nutzt curl_cffi (Browser-Fingerabdruck von Chrome), falls installiert,
sonst die Python-Standardbibliothek.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html import unescape
from urllib.parse import urlparse

try:
    from curl_cffi import requests as cffi_requests  # type: ignore
except ImportError:  # Fallback ohne Browser-Tarnung
    cffi_requests = None

# --- Konfiguration -----------------------------------------------------------

RAW_URLS = os.getenv("RETAILER_URLS") or ""
URLS = [u.strip() for u in re.split(r"[\s,]+", RAW_URLS) if u.strip().startswith("http")]

NTFY_SERVER = (os.getenv("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = (os.getenv("NTFY_TOPIC") or "").strip()
STATE_FILE = os.getenv("RETAILER_STATE_FILE", "state/retailers.json")
DEBUG = os.getenv("DEBUG", "0") == "1"
ERROR_ALERT_AFTER = 6   # Händlerseiten sind wackliger als Apple -> erst nach 30 Min. warnen

PICKUP_WORDS = re.compile(
    r"(abhol\w*|im markt|in der filiale|filialbestand|marktverfügbarkeit|reservier\w*|"
    r"click\s*&\s*collect|pickup|storeavailability|instore\w*)", re.I)

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.6",
}


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}Z] {msg}", flush=True)


def shop_name(url: str) -> str:
    host = urlparse(url).netloc.lower().replace("www.", "")
    return {"mediamarkt.de": "MediaMarkt", "saturn.de": "Saturn",
            "cyberport.de": "Cyberport"}.get(host, host)


# --- HTTP --------------------------------------------------------------------

_session = cffi_requests.Session(impersonate="chrome") if cffi_requests else None


def fetch(url: str) -> tuple[int, str]:
    if _session is not None:
        r = _session.get(url, headers={"Accept-Language": HEADERS["Accept-Language"]}, timeout=25)
        return r.status_code, r.text
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


# --- Auswertung --------------------------------------------------------------

def json_ld_blocks(html: str) -> list:
    out = []
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(unescape(m.group(1).strip()))
        except Exception:
            continue
        out.extend(data if isinstance(data, list) else [data])
    return out


def walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def analyse(html: str) -> dict:
    """Liefert Produktname, Preis und Verfügbarkeit aus den JSON-LD-Daten."""
    info = {"name": None, "price": None, "availability": [], "pickup": []}
    for block in json_ld_blocks(html):
        for node in walk(block):
            t = node.get("@type")
            types = t if isinstance(t, list) else [t]
            if "Product" in types and not info["name"]:
                info["name"] = node.get("name")
            if "Offer" in types or "AggregateOffer" in types:
                av = str(node.get("availability") or "")
                if av:
                    info["availability"].append(av.rsplit("/", 1)[-1])
                if node.get("price") and not info["price"]:
                    info["price"] = node.get("price")
                # Manche Shops hinterlegen Abholung als eigene Lieferart
                for dm in walk(node.get("availableDeliveryMethod") or node.get("deliveryLeadTime") or []):
                    info["pickup"].append(str(dm))
    if not info["name"]:
        m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
        info["name"] = unescape(m.group(1).strip()) if m else None
    return info


def pickup_snippets(html: str, limit: int = 15) -> list[str]:
    """Textstellen rund um Abhol-/Filial-Begriffe (nur für Debug)."""
    text = re.sub(r"\s+", " ", html)
    out, covered_until = [], -1
    for m in PICKUP_WORDS.finditer(text):
        if m.start() < covered_until:  # liegt schon im letzten Ausschnitt
            continue
        start, end = max(0, m.start() - 120), m.end() + 160
        covered_until = end
        out.append(text[start:end])
        if len(out) >= limit:
            break
    return out


def is_available(info: dict) -> bool:
    good = {"InStock", "InStoreOnly", "LimitedAvailability", "OnlineOnly"}
    return any(a in good for a in info["availability"])


# --- State & Push ------------------------------------------------------------

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
    payload = {"topic": NTFY_TOPIC, "title": title, "message": message,
               "priority": priority, "tags": tags or []}
    if click:
        payload["click"] = click
    req = urllib.request.Request(NTFY_SERVER + "/", data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            log(f"Push gesendet ({resp.status}): {title}")
    except Exception as e:
        log(f"Push fehlgeschlagen: {e}")


# --- Hauptablauf -------------------------------------------------------------

def main() -> int:
    if not URLS:
        log("Keine Händler-URLs hinterlegt (Variable RETAILER_URLS) – nichts zu tun.")
        return 0
    log(f"HTTP-Client: {'curl_cffi (Chrome-Tarnung)' if _session else 'urllib (ohne Tarnung)'}")

    state = load_state()
    items = state.setdefault("items", {})

    for url in URLS:
        shop = shop_name(url)
        entry = items.setdefault(url, {"available": False, "errors": 0})
        try:
            status, html = fetch(url)
            if status != 200:
                raise RuntimeError(f"HTTP {status}" + (" (Bot-Schutz?)" if status in (403, 429, 503) else ""))
            info = analyse(html)
            avail = is_available(info)
            log(f"{shop}: {info['name'] or '?'} | Preis {info['price'] or '?'} | "
                f"Verfügbarkeit {', '.join(info['availability']) or 'keine Angabe'}")
            if not info["availability"]:
                log(f"  Hinweis: keine strukturierten Verfügbarkeitsdaten gefunden "
                    f"(Seite {len(html)} Zeichen – evtl. Bot-Schutz oder andere Seitenstruktur).")

            if DEBUG:
                for s in pickup_snippets(html):
                    log(f"  [Abhol-Stelle] …{s}…")
                if info["pickup"]:
                    log(f"  [JSON-LD Lieferarten] {info['pickup'][:10]}")

            if avail and not entry.get("available"):
                push(f"🛒 {shop}: iPhone Duo verfügbar!",
                     f"{info['name'] or 'iPhone Duo'}\nStatus: {', '.join(info['availability'])}"
                     + (f"\nPreis: {info['price']} €" if info["price"] else "")
                     + "\n\nAuf der Seite die Filialabholung prüfen.",
                     priority=5, tags=["shopping_cart"], click=url)
            entry.update(available=avail, errors=0, name=info["name"],
                         last_ok=datetime.now(timezone.utc).isoformat())
        except Exception as e:
            entry["errors"] = entry.get("errors", 0) + 1
            log(f"{shop}: FEHLER {e} (Fehlläufe in Folge: {entry['errors']})")
            if entry["errors"] == ERROR_ALERT_AFTER:
                push(f"⚠️ {shop}-Abfrage klappt nicht",
                     f"{entry['errors']} Fehlläufe in Folge: {e}\n{url}",
                     priority=2, tags=["warning"])
        time.sleep(2)

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
