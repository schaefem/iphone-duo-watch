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
DUMP_HTML = os.getenv("DUMP_HTML", "0") == "1"   # Seiten als debug_<shop>.html speichern
# Optional: Cookie-Header aus dem Browser (mit ausgewähltem Markt), z. B. MEDIAMARKT_COOKIE
SHOP_COOKIES = {s: (os.getenv(f"{s.upper()}_COOKIE") or "").strip()
                for s in ("MediaMarkt", "Saturn", "Cyberport")}
RATE_LIMIT_PAUSE_MIN = int(os.getenv("RATE_LIMIT_PAUSE_MIN") or "15")
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


# Windows-Konsole/Umleitung: Umlaute und Sonderzeichen nie zum Absturz führen lassen
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}Z] {msg}", flush=True)


def shop_name(url: str) -> str:
    host = urlparse(url).netloc.lower().replace("www.", "")
    return {"mediamarkt.de": "MediaMarkt", "saturn.de": "Saturn",
            "cyberport.de": "Cyberport"}.get(host, host)


# --- HTTP --------------------------------------------------------------------

_session = cffi_requests.Session(impersonate="chrome") if cffi_requests else None


def fetch(url: str, cookie: str = "") -> tuple[int, str]:
    if _session is not None:
        h = {"Accept-Language": HEADERS["Accept-Language"]}
        if cookie:
            h["Cookie"] = cookie
        r = _session.get(url, headers=h, timeout=25)
        return r.status_code, r.text
    headers = dict(HEADERS)
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
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


def _types(node: dict) -> list:
    t = node.get("@type")
    return t if isinstance(t, list) else [t]


def _direct_offers(product: dict) -> list[dict]:
    """Nur die Angebote, die direkt am Produkt hängen (keine Zubehör-/Empfehlungs-Produkte)."""
    offers = product.get("offers")
    offers = offers if isinstance(offers, list) else [offers] if offers else []
    out = []
    for o in offers:
        if not isinstance(o, dict):
            continue
        out.append(o)
        if "AggregateOffer" in _types(o):  # AggregateOffer kann Einzelangebote enthalten
            sub = o.get("offers")
            out.extend(x for x in (sub if isinstance(sub, list) else [sub]) if isinstance(x, dict))
    return out


def analyse(html: str) -> dict:
    """Liefert Name, Preis und Verfügbarkeit des HAUPTPRODUKTS aus den JSON-LD-Daten."""
    info = {"name": None, "price": None, "availability": [], "other_offers": 0}
    products = [n for b in json_ld_blocks(html) for n in walk(b) if "Product" in _types(n)]
    # Hauptprodukt = erstes Produkt mit "iPhone Duo" im Namen, sonst das erste überhaupt
    main = next((p for p in products if "iphone duo" in str(p.get("name", "")).lower()),
                products[0] if products else None)
    if main is not None:
        info["name"] = main.get("name")
        for o in _direct_offers(main):
            av = str(o.get("availability") or "")
            if av:
                info["availability"].append(av.rsplit("/", 1)[-1])
            if o.get("price") and not info["price"]:
                info["price"] = o.get("price")
        info["other_offers"] = sum(1 for p in products if p is not main)
    if not info["name"]:
        m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
        info["name"] = unescape(m.group(1).strip()) if m else None
    return info


# --- Filialstatus ------------------------------------------------------------

def mms_store_status(html: str) -> dict:
    """MediaMarkt/Saturn: Status steckt in data-test="mms-cofr-pickup_<STATUS>" (bzw. delivery_)."""
    pick = re.findall(r'data-test="mms-cofr-pickup_([A-Za-z0-9_]+)"', html)
    deliv = re.findall(r'data-test="mms-cofr-delivery_([A-Za-z0-9_]+)"', html)
    return {"pickup": pick[0] if pick else None, "delivery": deliv[0] if deliv else None}


# --- MediaMarkt / Saturn: Filialbestand per GraphQL ----------------------------

MMS_HASH = os.getenv("MMS_PICKUP_HASH") or "81938af9c97c7bf5419b70d29819aba4cdee672fba4b020a5178dadd3b0cd979"
MMS_CLIENT_VERSION = os.getenv("MMS_CLIENT_VERSION") or "8.517.0"
MMS_STORES = {
    "MediaMarkt": [s.strip() for s in (os.getenv("MEDIAMARKT_STORES") or "460").split(",") if s.strip()],
    "Saturn": [s.strip() for s in (os.getenv("SATURN_STORES") or "12").split(",") if s.strip()],
}
# Alle iPhone-Duo-Varianten bei MediaMarkt/Saturn (gleiche IDs in beiden Shops)
MMS_PRODUCTS = {
    "3071630": "256 GB Sternenweiß", "3071631": "256 GB Nachthimmel",
    "3071632": "512 GB Sternenweiß", "3071633": "512 GB Nachthimmel",
    "3071634": "1 TB Sternenweiß",   "3071635": "1 TB Nachthimmel",
    "3071636": "2 TB Sternenweiß",   "3071637": "2 TB Nachthimmel",
}
_ids_env = [x.strip() for x in (os.getenv("MMS_PRODUCT_IDS") or "").split(",") if x.strip()]
if _ids_env:
    MMS_PRODUCTS = {i: MMS_PRODUCTS.get(i, f"Produkt {i}") for i in _ids_env}
MMS_SHOPS = [x.strip() for x in (os.getenv("MMS_SHOPS") or "MediaMarkt,Saturn").split(",") if x.strip()]
MMS_URL_SLUG = {
    "3071630": "256gb-star-white", "3071631": "256gb-night-sky", "3071632": "512gb-star-white",
    "3071633": "512gb-night-sky", "3071634": "1tb-star-white", "3071635": "1tb-night-sky",
    "3071636": "2tb-star-white", "3071637": "2tb-night-sky",
}


def mms_product_url(shop: str, pid: str) -> str:
    base = MMS_SITE[shop][1]
    slug = MMS_URL_SLUG.get(pid)
    return f"{base}/de/product/_apple-iphone-duo-{slug}-{pid}.html" if slug else f"{base}/de/search.html?query={pid}"


# Cyberport: alle iPhone-Duo-Varianten (Standard, ergänzbar über RETAILER_URLS)
_CP = "https://www.cyberport.de/apple-und-zubehoer/apple-iphone/apple-iphone-duo/pdp/"
CYBERPORT_DEFAULT = [
    _CP + "A415-22M/Apple-iPhone-Duo-256GB-Sternenwei--MK2D4ZD-A.html",
    _CP + "A415-22N/Apple-iPhone-Duo-256GB-Nachthimmel-MK2E4ZD-A.html",
    _CP + "A415-22P/Apple-iPhone-Duo-512GB-Sternenwei--MK2F4ZD-A.html",
    _CP + "A415-22Q/Apple-iPhone-Duo-512GB-Nachthimmel-MK2G4ZD-A.html",
    _CP + "A415-22R/Apple-iPhone-Duo-1TB-Sternenwei--MK2H4ZD-A.html",
    _CP + "A415-22S/Apple-iPhone-Duo-1TB-Nachthimmel-MK2J4ZD-A.html",
    _CP + "A415-22T/Apple-iPhone-Duo-2TB-Sternenwei--MK2K4ZD-A.html",
    _CP + "A415-22U/Apple-iPhone-Duo-2TB-Nachthimmel-MK2L4ZD-A.html",
]
CYBERPORT_ENABLED = (os.getenv("CYBERPORT") or "1") != "0"
CYBERPORT_EVERY_MIN = float(os.getenv("CYBERPORT_EVERY_MIN") or "15")

MMS_SITE = {"MediaMarkt": ("Media", "https://www.mediamarkt.de"),
            "Saturn": ("Saturn", "https://www.saturn.de")}


def mms_product_id(url: str) -> str | None:
    m = re.search(r"-(\d{5,})\.html", url)
    return m.group(1) if m else None


def mms_pickup(shop: str, product_ids: list[str], store_id: str, referer: str) -> list[dict]:
    """Fragt den Abholstatus mehrerer Produkte für einen Markt ab (gleiche Abfrage wie die Seite)."""
    sales_line, base = MMS_SITE[shop]
    variables = {
        "productIds": list(product_ids), "storeId": store_id,
        "config": {
            "isEnabled": True, "baseDomain": base, "channel": "DESKTOP", "isLegacyDataExcluded": False,
            "features": {
                "badges": {"isFreeShippingBadgeIncluded": False},
                "crossSalesLine": {"isEnabled": True, "isOutputForced": False},
                "onlineStatus": {"isParallelRequestsEnabled": True, "isPermanentlyNaIndexEnabled": True},
                "pickup": {"isStrictPickupDisplayStatusEnabled": False},
                "price": {"strikePriceTypes": [{"strikePriceType": "lop"},
                                               {"strikePriceType": "rrp", "shouldBeStruck": True,
                                                "showDiscountBadge": True, "isLegalTextInlineAllowed": False}],
                          "isBasePriceRequiredFlagRespected": False, "isDiscountLabelEnabled": True,
                          "isDiscountPercentageShown": True, "isDisplayPriceWithStrikePriceRrpThemed": True,
                          "isLongerStrikePricePrefixAllowed": False, "isPromoPriceFiltered": True,
                          "isPromoPriceUsedAsDisplayPriceInApp": False, "isHistoryChartEnabled": False,
                          "discountPercentageMinimum": 10, "discountPercentageMinimumFractionDigits": 0},
                "delivery": {"isDeliveryStatusByEarliestDateEnabled": True, "isLocationSourcingEnabled": True,
                             "isLocationSourcingMarketplaceEnabled": True},
                "refurbishedGoods": {"isEnabled": True},
                "repairability": {"isEnabled": False},
            },
        },
    }
    extensions = {
        "persistedQuery": {"version": 1, "sha256Hash": MMS_HASH},
        "pwa": {"captureChannel": "DESKTOP", "salesLine": sales_line, "country": "DE", "language": "de",
                "globalLoyaltyProgram": True, "isOneAccountProgramActive": True,
                "shouldInactiveContractsBeHidden": True, "isUsingXccCustomerComponent": True,
                "isCheckoutPhoneCompareActive": True},
    }
    from urllib.parse import urlencode
    url = f"{base}/api/v1/graphql?" + urlencode({
        "operationName": "GetCofrPickupFeaturePickups",
        "variables": json.dumps(variables, separators=(",", ":")),
        "extensions": json.dumps(extensions, separators=(",", ":")),
    })
    import uuid
    headers = {   # 1:1 wie im Browser (Chrome DevTools, 26.10.2026)
        "Accept": "*/*",
        "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
        "Apollographql-Client-Name": "pwa-client-pqm",
        "Apollographql-Client-Version": MMS_CLIENT_VERSION,
        "Content-Type": "application/json",
        "Priority": "u=1, i",
        "Referer": referer,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "X-Cacheable": "true",
        "X-Flow-Id": str(uuid.uuid4()),
        "X-Mms-Country": "DE",
        "X-Mms-Language": "de",
        "X-Mms-Salesline": sales_line,
        "X-Operation": "GetCofrPickupFeaturePickups",
    }
    if SHOP_COOKIES.get(shop):   # optional: Browser-Cookie (z. B. MEDIAMARKT_COOKIE) mitschicken
        headers["Cookie"] = SHOP_COOKIES[shop]
    if _session is not None:
        r = _session.get(url, headers=headers, timeout=25)
        status, body = r.status_code, r.text
    else:
        req = urllib.request.Request(url, headers={**HEADERS, **headers})
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                status, body = resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            status, body = e.code, e.read().decode("utf-8", errors="replace")
    if status != 200:
        raise RuntimeError(f"Abholabfrage HTTP {status}: {body[:200]!r}")
    data = json.loads(body)
    if DEBUG:
        log(f"  [GraphQL {shop} Markt {store_id}] {body[:500]}")
    if data.get("errors"):
        raise RuntimeError(f"Abholabfrage abgelehnt: {str(data['errors'])[:200]}")
    feats = [a.get("cofrPickupFeature") for a in ((data.get("data") or {}).get("cofrProductAggregates") or [])]
    feats = [f for f in feats if f]
    if not feats:
        raise RuntimeError(f"Abholabfrage ohne Daten: {body[:200]!r}")
    return feats


def cyberport_status(html: str) -> dict:
    """Cyberport (Next.js): Produktstatus steckt in __NEXT_DATA__ -> dynamicProps.productVariable."""
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
        pv = data["props"]["pageProps"]["fullPageProps"]["dynamicProps"]["productVariable"]
    except Exception:
        return {}
    return {
        "online": bool(pv.get("InStock")),
        "status": pv.get("AvailabilityStatusType"),
        "store": bool(pv.get("StockLocal")),      # in mindestens einem Cyberport-Store
        "store_only": bool(pv.get("IsStoreOnly")),
    }


def page_title(html: str) -> str | None:
    m = re.search(r'<meta property="og:title" content="([^"]+)"', html) or \
        re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if not m:
        return None
    return re.split(r"\s+[|–-]\s+(?:MediaMarkt|Saturn|Cyberport)", unescape(m.group(1).strip()))[0]


def status_means_available(status: str | None) -> bool:
    if not status:
        return False
    s = status.upper()
    if any(x in s for x in ("NOT", "UNAVAILABLE", "NO_STORE", "NONE", "SOLD", "OUT")):
        return False
    return "AVAILABLE" in s or "IN_STOCK" in s or s in ("READY", "PICKUP_TODAY", "TODAY")


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

def run_mms(shop: str, state: dict) -> None:
    """MediaMarkt/Saturn: alle Varianten mit einer Abfrage pro Markt prüfen."""
    entry = state.setdefault("mms", {}).setdefault(shop, {"available": [], "errors": 0})
    if time.time() < entry.get("pause_until", 0):
        log(f"{shop}: Pause nach 'zu viele Anfragen' – nächster Versuch ab "
            f"{datetime.fromtimestamp(entry['pause_until']).strftime('%H:%M')} Uhr.")
        return
    pids = list(MMS_PRODUCTS)
    referer = mms_product_url(shop, pids[0])
    try:
        # Produktseite einmal laden: holt Session-Cookies wie ein Browser
        status, _ = fetch(referer, SHOP_COOKIES.get(shop, ""))
        if status == 429:
            entry["pause_until"] = time.time() + RATE_LIMIT_PAUSE_MIN * 60
            log(f"{shop}: HTTP 429 (zu viele Anfragen) – pausiere {RATE_LIMIT_PAUSE_MIN} Minuten.")
            return
        if status != 200:
            log(f"{shop}: Produktseite HTTP {status} – versuche Abfrage trotzdem.")
        time.sleep(1)

        available: dict[str, str] = {}
        for sid in MMS_STORES[shop]:
            feats = mms_pickup(shop, pids, sid, referer)
            for f in feats:
                pid = str(f.get("id", "")).split(":")[2] if str(f.get("id", "")).count(":") >= 3 else "?"
                name = MMS_PRODUCTS.get(pid, pid)
                disp, anywhere = f.get("displayStatus") or "?", f.get("pickupStatus") or "?"
                log(f"{shop}: iPhone Duo {name:<20} Markt {sid}: {disp:<14} (überall: {anywhere})")
                if status_means_available(disp):
                    available[f"{pid}@{sid}"] = f"{name} – in deinem Markt ({sid}) abholbar"
                elif anywhere != "?" and "NOT_AVAILABLE" not in anywhere.upper():
                    available[f"{pid}@*"] = f"{name} – in einem anderen Markt abholbar ({anywhere})"
            time.sleep(1)

        new = [k for k in available if k not in set(entry.get("available", []))]
        if new:
            first_pid = new[0].split("@")[0]
            push(f"🛒 {shop}: iPhone Duo abholbar!",
                 "\n".join(f"• {available[k]}" for k in new) + "\n\nSchnell auf der Seite reservieren.",
                 priority=5, tags=["shopping_cart"], click=mms_product_url(shop, first_pid))
        entry.update(available=sorted(available), errors=0,
                     last_ok=datetime.now(timezone.utc).isoformat())
    except Exception as e:
        entry["errors"] = entry.get("errors", 0) + 1
        log(f"{shop}: FEHLER {e} (Fehlläufe in Folge: {entry['errors']})")
        if entry["errors"] == ERROR_ALERT_AFTER:
            push(f"⚠️ {shop}-Abfrage klappt nicht", f"{entry['errors']} Fehlläufe in Folge: {e}",
                 priority=2, tags=["warning"])


def main() -> int:
    log(f"HTTP-Client: {'curl_cffi (Chrome-Tarnung)' if _session else 'urllib (ohne Tarnung)'}")
    state = load_state()
    items = state.setdefault("items", {})

    for shop in MMS_SHOPS:
        if shop in MMS_SITE:
            run_mms(shop, state)
            time.sleep(2)

    # Übrige Seiten (z. B. Cyberport) einzeln; MediaMarkt/Saturn-URLs sind oben schon abgedeckt
    other_urls = [u for u in URLS if not (shop_name(u) in MMS_SITE and shop_name(u) in MMS_SHOPS)]
    if CYBERPORT_ENABLED:
        known = {u.lower() for u in other_urls}
        other_urls += [u for u in CYBERPORT_DEFAULT if u.lower() not in known]
    # Cyberport seltener prüfen (Schutz vor "zu viele Anfragen")
    cp_last = state.get("cyberport_last", 0)
    cp_due = time.time() - cp_last >= CYBERPORT_EVERY_MIN * 60 - 30
    if any(shop_name(u) == "Cyberport" for u in other_urls):
        if cp_due:
            state["cyberport_last"] = time.time()
        else:
            log(f"Cyberport: nächste Prüfung in ca. "
                f"{int((cp_last + CYBERPORT_EVERY_MIN * 60 - time.time()) / 60) + 1} Min.")
            other_urls = [u for u in other_urls if shop_name(u) != "Cyberport"]
    for i, url in enumerate(other_urls):
        if i:
            time.sleep(3)   # Abstand zwischen Seitenabrufen, auch nach 429/Pause
        shop = shop_name(url)
        entry = items.setdefault(url, {"available": False, "errors": 0})
        pause_until = entry.get("pause_until", 0)
        if time.time() < pause_until:
            log(f"{shop}: Pause nach 'zu viele Anfragen' – nächster Versuch ab "
                f"{datetime.fromtimestamp(pause_until).strftime('%H:%M')} Uhr.")
            continue
        try:
            status, html = fetch(url, SHOP_COOKIES.get(shop, ""))
            if status == 429:
                entry["pause_until"] = time.time() + RATE_LIMIT_PAUSE_MIN * 60
                log(f"{shop}: HTTP 429 (zu viele Anfragen) – pausiere {RATE_LIMIT_PAUSE_MIN} Minuten.")
                continue
            if status != 200:
                raise RuntimeError(f"HTTP {status}" + (" (Bot-Schutz?)" if status in (403, 429, 503) else ""))
            if DUMP_HTML:
                fn = f"debug_{shop.lower()}.html"
                with open(fn, "w", encoding="utf-8") as f:
                    f.write(html)
                log(f"  Seite gespeichert: {fn} ({len(html)} Zeichen)")

            info = analyse(html)
            info["name"] = page_title(html) or info["name"]
            in_store = False
            online_txt = store_txt = ""

            if shop in ("MediaMarkt", "Saturn"):
                # JSON-LD ist dort unzuverlässig -> nur die Statusfelder der Seite verwenden
                mms = mms_store_status(html)
                if not mms.get("delivery") and not mms.get("pickup"):
                    log("  Hinweis: Lieferstatus nicht in der Seite gefunden – nur Abholabfrage.")
                online = status_means_available(mms.get("delivery"))
                online_txt = mms.get("delivery") or "?"
                pid = mms_product_id(url)
                parts = []
                if not pid:
                    store_txt = "Produkt-ID nicht in der URL"
                else:
                    for sid in MMS_STORES[shop]:
                        try:
                            f = mms_pickup(shop, pid, sid, url)
                        except Exception as ex:
                            parts.append(f"Markt {sid}: FEHLER {ex}")
                            continue
                        disp, anywhere = f.get("displayStatus") or "?", f.get("pickupStatus") or "?"
                        parts.append(f"Markt {sid}: {disp} (überall: {anywhere})")
                        if status_means_available(disp):
                            in_store = True
                        elif anywhere != "?" and "NOT_AVAILABLE" not in anywhere.upper():
                            in_store = True   # in einem anderen Markt abholbar
                        time.sleep(1)
                    store_txt = "; ".join(parts)
            elif shop == "Cyberport":
                cp = cyberport_status(html)
                if not cp:
                    raise RuntimeError("Produktdaten (__NEXT_DATA__) nicht gefunden – Seitenstruktur geändert?")
                online = cp["online"] or cp["status"] in ("AVAILABLE", "SOON_AVAILABLE_1")
                in_store = cp["store"]
                online_txt = cp["status"] or "?"
                store_txt = "in mind. einem Store" if cp["store"] else "kein Store"
            else:
                online = is_available(info)
                online_txt = ", ".join(info["availability"]) or "keine Angabe"

            avail = online or in_store
            log(f"{shop}: {info['name'] or '?'} | Preis {info['price'] or '?'} | "
                f"Online {online_txt}" + (f" | Filiale {store_txt}" if store_txt else ""))

            if os.getenv("SNIPPETS") == "1":   # nur noch bei Bedarf: Textstellen zu Abholung
                for s in pickup_snippets(html):
                    log(f"  [Abhol-Stelle] …{s}…")

            if avail and not entry.get("available"):
                lines = [info["name"] or "iPhone Duo"]
                if in_store:
                    lines.append(f"🏬 Filiale: {store_txt} – Abholung auf der Seite prüfen")
                if online:
                    lines.append(f"📦 Online: {online_txt}")
                if info["price"]:
                    lines.append(f"Preis: {info['price']} €")
                push(f"🛒 {shop}: iPhone Duo verfügbar!", "\n".join(lines),
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

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
