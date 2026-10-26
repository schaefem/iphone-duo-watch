#!/usr/bin/env python3
"""
Dashboard für den iPhone-Duo-Wächter
------------------------------------
Kleine Webseite auf dem Pi: Status aller Varianten je Händler, letzte Läufe,
Log, Buttons für "Jetzt prüfen" und "Test-Push". Nur Python-Standardbibliothek.

Start:  .venv/bin/python dashboard.py      ->  http://<pi>:8080
Optional in config.env: DASHBOARD_PORT=8080, DASHBOARD_PASSWORD=geheim
"""

import base64
import fcntl
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

DIR = os.path.dirname(os.path.abspath(__file__))
TZ = ZoneInfo("Europe/Berlin")


def load_config() -> dict:
    cfg = {}
    try:
        with open(os.path.join(DIR, "config.env"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return cfg


def cfg(key: str, default: str = "") -> str:
    return os.getenv(key) or load_config().get(key) or default


PORT = int(cfg("DASHBOARD_PORT", "8080"))
PASSWORD = cfg("DASHBOARD_PASSWORD", "")

VARIANTS = [(s, c) for s in ("256 GB", "512 GB", "1 TB", "2 TB") for c in ("Sternenweiß", "Nachthimmel")]


# --- Daten einlesen ----------------------------------------------------------

def read_json(name: str) -> dict:
    try:
        with open(os.path.join(DIR, "state", name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def variant_key(text: str):
    t = (text or "").lower().replace("ß", "ss")
    m = re.search(r"(\d+)\s*(gb|tb)", t)
    if not m:
        return None
    size = f"{m.group(1)} {m.group(2).upper()}"
    if "nacht" in t or "night" in t:
        color = "Nachthimmel"
    elif "weiss" in t or "white" in t or "stern" in t:
        color = "Sternenweiß"
    else:
        return None
    return (size, color)


def fmt_time(iso_or_ts) -> str:
    if not iso_or_ts:
        return "–"
    try:
        if isinstance(iso_or_ts, (int, float)):
            dt = datetime.fromtimestamp(iso_or_ts, timezone.utc)
        else:
            dt = datetime.fromisoformat(str(iso_or_ts))
        return dt.astimezone(TZ).strftime("%d.%m. %H:%M:%S")
    except Exception:
        return str(iso_or_ts)


def is_running() -> bool:
    try:
        with open(os.path.join(DIR, ".lock"), "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
            return False
    except BlockingIOError:
        return True
    except Exception:
        return False


def avail_mms(status: str) -> bool:
    s = (status or "").upper()
    return "AVAILABLE" in s and not any(x in s for x in ("NOT", "UNAVAILABLE"))


def apple_sub(f: str) -> str:
    if f.strip() == "*":
        return "alle Stores"
    parts = [x for x in f.split(",") if x.strip()]
    return parts[0] if len(parts) == 1 else f"{len(parts)} Stores"


def build_status() -> dict:
    apple = read_json("state.json")
    ret = read_json("retailers.json")
    columns = []

    # Apple
    cells = {}
    for r in apple.get("last_status") or []:
        k = variant_key(r.get("variant"))
        if not k:
            continue
        c = cells.setdefault(k, {"state": "no", "detail": []})
        if r.get("status") == "available":
            c["state"] = "yes"
            c["detail"].append(r.get("store"))
    columns.append({
        "name": "Apple", "sub": apple_sub(cfg("STORE_FILTER", "Dresden")),
        "updated": fmt_time(apple.get("last_ok")),
        "error": apple.get("last_error"), "errors": apple.get("errors", 0),
        "cells": cells,
    })

    # MediaMarkt / Saturn
    for shop, key in (("MediaMarkt", "MEDIAMARKT_STORES"), ("Saturn", "SATURN_STORES")):
        ls = (ret.get("last_status") or {}).get(shop) or {}
        cells = {}
        for r in ls.get("rows") or []:
            k = variant_key(r.get("variant"))
            if not k:
                continue
            c = cells.setdefault(k, {"state": "no", "detail": []})
            any_ = (r.get("anywhere") or "").upper()
            if avail_mms(r.get("status")):
                c["state"] = "yes"
                c["detail"].append(f"{r.get('store')}: {r.get('status')}")
            elif any_ and "NOT_AVAILABLE" not in any_ and c["state"] != "yes":
                c["state"] = "elsewhere"
                c["detail"].append(f"anderer Markt: {r.get('anywhere')}")
        m = (ret.get("mms") or {}).get(shop) or {}
        columns.append({
            "name": shop, "sub": "Markt " + cfg(key, "460" if shop == "MediaMarkt" else "12"),
            "updated": fmt_time(ls.get("at")), "error": ls.get("error"), "errors": m.get("errors", 0),
            "pause": fmt_time(m.get("pause_until")) if m.get("pause_until", 0) > datetime.now().timestamp() else None,
            "cells": cells,
        })

    # Cyberport (und andere Seiten)
    cells, last, errs = {}, None, []
    for url, p in (ret.get("page_status") or {}).items():
        if p.get("shop") != "Cyberport":
            continue
        k = variant_key(p.get("variant"))
        last = max(filter(None, [last, p.get("at")]), default=None)
        if p.get("error"):
            errs.append(p["error"])
        if not k:
            continue
        if p.get("error") and not p.get("at"):
            cells[k] = {"state": "unknown", "detail": [p["error"]]}
        elif p.get("available"):
            cells[k] = {"state": "yes", "detail": [f"Online: {p.get('online')}", f"Filiale: {p.get('store')}"]}
        else:
            cells[k] = {"state": "no", "detail": [f"Online: {p.get('online')}", f"Filiale: {p.get('store')}"]}
    columns.append({
        "name": "Cyberport", "sub": f"alle {cfg('CYBERPORT_EVERY_MIN', '15')} Min.",
        "updated": fmt_time(last), "error": errs[-1] if errs else None, "errors": len(errs),
        "cells": cells,
    })

    rows = []
    for v in VARIANTS:
        rows.append({"variant": f"{v[0]} {v[1]}",
                     "cells": [col["cells"].get(v, {"state": "unknown", "detail": []}) for col in columns]})
    for col in columns:
        del col["cells"]

    ping_h = float(cfg("PING_HOURS", "3") or 0)
    last_ping = apple.get("last_ping")
    any_hit = any(c["state"] in ("yes", "elsewhere") for r in rows for c in r["cells"])
    sched = read_schedule()
    return {
        "schedule": sched, "next_run": next_run(sched), "run": run_info(sched),
        "now": datetime.now(TZ).strftime("%d.%m.%Y %H:%M:%S"),
        "running": is_running(),
        "any_hit": any_hit,
        "columns": columns,
        "rows": rows,
        "last_ping": fmt_time(last_ping),
        "next_ping": fmt_time(last_ping + ping_h * 3600) if last_ping and ping_h > 0 else "–",
    }


def read_log(lines: int = 300) -> str:
    try:
        with open(os.path.join(DIR, "logs", "watch.log"), encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-lines:])
    except FileNotFoundError:
        return "(noch kein Log vorhanden)"


def start_run(test_push: bool = False) -> tuple[int, str]:
    if is_running():
        return 409, "Es läuft gerade schon eine Prüfung."
    env = dict(os.environ, FORCE="1", TRIGGER="manual")
    if test_push:
        env["TEST_PUSH"] = "1"
    try:
        subprocess.Popen(["bash", os.path.join(DIR, "run.sh")], cwd=DIR, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:
        return 500, f"Start fehlgeschlagen: {e}"
    return 202, "Prüfung gestartet" + (" (mit Test-Push)" if test_push else "") + " – Ergebnis in ca. 1 Minute."


# --- Zeitplan (Cronjob) ------------------------------------------------------

RUN_SH = os.path.join(DIR, "run.sh")
PAUSE_MARK = "#DUOWATCH-PAUSE# "
CRON_RE = re.compile(r"^(?:#DUOWATCH-PAUSE# )?\*/(\d+)\s+(\d+)-(\d+)\s+\*\s+\*\s+\*\s+(.*run\.sh.*)$")


def _crontab_lines() -> list[str]:
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
        return r.stdout.splitlines() if r.returncode == 0 else []
    except Exception:
        return []


def read_schedule() -> dict:
    sched = {"installed": False, "enabled": False, "every": 5, "from": 6, "to": 22, "raw": None}
    for line in _crontab_lines():
        if RUN_SH not in line:
            continue
        sched["raw"] = line
        m = CRON_RE.match(line.strip())
        sched["installed"] = True
        sched["enabled"] = not line.startswith(PAUSE_MARK) and not line.lstrip().startswith("#")
        if m:
            sched.update(every=int(m.group(1)), **{"from": int(m.group(2))}, to=int(m.group(3)) + 1)
        break
    try:
        r = subprocess.run(["systemctl", "is-active", "cron"], capture_output=True, text=True, timeout=5)
        sched["cron_service"] = r.stdout.strip() or "unbekannt"
    except Exception:
        sched["cron_service"] = "unbekannt"
    return sched


def write_schedule(enabled: bool, every: int, h_from: int, h_to: int) -> None:
    if every not in (1, 2, 3, 5, 10, 15, 20, 30) or not (0 <= h_from < h_to <= 24):
        raise ValueError("Ungültiger Zeitplan")
    line = f"*/{every} {h_from}-{h_to - 1} * * * {RUN_SH}"
    if not enabled:
        line = PAUSE_MARK + line
    lines = [l for l in _crontab_lines() if RUN_SH not in l] + [line]
    r = subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True,
                       capture_output=True, timeout=10)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "crontab fehlgeschlagen")


def next_run(sched: dict) -> str:
    if not sched.get("enabled"):
        return "–"
    now = datetime.now(TZ).replace(second=0, microsecond=0)
    from datetime import timedelta
    t = now + timedelta(minutes=1)
    for _ in range(60 * 24 * 2):
        if sched["from"] <= t.hour < sched["to"] and t.minute % sched["every"] == 0:
            return t.strftime("%d.%m. %H:%M")
        t += timedelta(minutes=1)
    return "–"


def run_info(sched: dict) -> dict:
    last = read_json("run.json")
    cron = read_json("run_cron.json")
    info = {"last": None, "cron_last": None, "overdue": False}
    if last.get("start"):
        dur = (last.get("end") or last["start"]) - last["start"]
        info["last"] = {"at": fmt_time(last["start"]), "trigger": "manuell" if last.get("trigger") == "manual" else "automatisch",
                        "duration": f"{dur} s" if last.get("end") else "läuft …",
                        "ok": last.get("rc_apple", 0) == 0 and last.get("rc_retailers", 0) == 0}
    if cron.get("start"):
        info["cron_last"] = fmt_time(cron["start"])
    # überfällig: Zeitplan aktiv, im Zeitfenster, letzter automatischer Lauf älter als 2 Intervalle + 2 Min.
    now = datetime.now(TZ)
    if sched.get("enabled") and sched["from"] <= now.hour < sched["to"]:
        ref = cron.get("start") or 0
        in_window_since = now.replace(hour=sched["from"], minute=0, second=0).timestamp()
        limit = (2 * sched["every"] + 2) * 60
        if now.timestamp() - max(ref, in_window_since) > limit:
            info["overdue"] = True
    return info


# --- Einstellungen (config.env) ----------------------------------------------

SETTINGS = {   # Schlüssel -> (Prüfmuster, Beschreibung)
    "NTFY_TOPIC": (r"[A-Za-z0-9_\-]{1,64}", "ntfy-Thema"),
    "STORE_FILTER": (r"\*|[^\"$`\\\n]{1,500}", "Apple Stores"),
    "LOCATION": (r"[0-9,]{0,200}", "PLZ für die Apple-Suche"),
    "PING_HOURS": (r"\d{1,2}(\.\d)?", "Lebenszeichen (Std.)"),
    "MEDIAMARKT_STORES": (r"[0-9,]{0,100}", "MediaMarkt-Märkte"),
    "SATURN_STORES": (r"[0-9,]{0,100}", "Saturn-Märkte"),
    "MMS_SHOPS": (r"[A-Za-z,]{0,40}", "MediaMarkt/Saturn aktiv"),
    "CYBERPORT": (r"[01]", "Cyberport aktiv"),
    "CYBERPORT_EVERY_MIN": (r"\d{1,3}", "Cyberport-Takt"),
    "DEBUG": (r"[01]", "Debug"),
}


def current_settings() -> dict:
    c = load_config()
    return {
        "NTFY_TOPIC": c.get("NTFY_TOPIC", ""),
        "STORE_FILTER": c.get("STORE_FILTER", "Dresden"),
        "LOCATION": c.get("LOCATION", ""),
        "PING_HOURS": c.get("PING_HOURS", "3"),
        "MEDIAMARKT_STORES": c.get("MEDIAMARKT_STORES", "460"),
        "SATURN_STORES": c.get("SATURN_STORES", "12"),
        "MMS_SHOPS": c.get("MMS_SHOPS", "MediaMarkt,Saturn"),
        "CYBERPORT": c.get("CYBERPORT", "1"),
        "CYBERPORT_EVERY_MIN": c.get("CYBERPORT_EVERY_MIN", "15"),
        "DEBUG": c.get("DEBUG", "0"),
    }


def write_settings(new: dict) -> None:
    for k, v in new.items():
        if k not in SETTINGS:
            raise ValueError(f"Unbekannte Einstellung: {k}")
        if not re.fullmatch(SETTINGS[k][0], str(v)):
            raise ValueError(f"Ungültiger Wert für {SETTINGS[k][1]}: {v!r}")
    path = os.path.join(DIR, "config.env")
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    done = set()
    for i, line in enumerate(lines):
        m = re.match(r"^\s*([A-Z_]+)=", line)
        if m and m.group(1) in new:
            k = m.group(1)
            lines[i] = f'{k}="{new[k]}"'
            done.add(k)
    for k, v in new.items():
        if k not in done:
            lines.append(f'{k}="{v}"')
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# --- Apple Stores ermitteln -----------------------------------------------------

DE_ANCHORS = ["01067", "04109", "10117", "20095", "30159", "40212", "50667",
              "60311", "66111", "70173", "80331", "86150", "90402"]


def discover_apple_stores() -> list[dict]:
    """Fragt Apple von mehreren PLZ aus ab und sammelt alle Stores (Name, Stadt, PLZ)."""
    import urllib.parse
    import urllib.request
    part = (read_json("state.json").get("parts") or ["MK2E4ZD/A"])[0]
    stores = {}
    for plz in DE_ANCHORS:
        url = "https://www.apple.com/de/shop/retail/pickup-message?" + urllib.parse.urlencode(
            {"pl": "true", "location": plz, "parts.0": part})
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "Accept": "application/json", "Accept-Language": "de-DE,de;q=0.9",
            "Referer": "https://www.apple.com/de/shop/buy-iphone/iphone-duo"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception:
            continue
        for st in (data.get("body") or {}).get("stores") or []:
            num = st.get("storeNumber")
            if not num or num in stores:
                continue
            addr = st.get("address") or {}
            stores[num] = {"number": num, "name": st.get("storeName") or num, "city": st.get("city") or "",
                           "postal": addr.get("postalCode") or ""}
    result = sorted(stores.values(), key=lambda x: (x["city"], x["name"]))
    if result:
        os.makedirs(os.path.join(DIR, "state"), exist_ok=True)
        with open(os.path.join(DIR, "state", "apple_stores.json"), "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)
    return result


def known_apple_stores() -> list[dict]:
    try:
        with open(os.path.join(DIR, "state", "apple_stores.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_apple_selection(numbers: list[str], all_stores: bool) -> None:
    if all_stores:
        write_settings({"STORE_FILTER": "*", "LOCATION": ""})
        return
    known = {s["number"]: s for s in known_apple_stores()}
    chosen = [known[n] for n in numbers if n in known]
    if not chosen:
        raise ValueError("Bitte mindestens einen Store auswählen (oder „Alle Stores“).")
    names = ",".join(s["name"] for s in chosen)
    plz = ",".join(dict.fromkeys(s["postal"] for s in chosen if s["postal"]))
    write_settings({"STORE_FILTER": names, "LOCATION": plz})


# --- Webseite ----------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="de"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Duo-Wächter</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2330;--muted:#6b7280;--line:#e5e7eb;
--yes:#16a34a;--yes-bg:#dcfce7;--else:#b45309;--else-bg:#fef3c7;--no:#6b7280;--no-bg:#f1f5f9;
--unk-bg:#fafafa;--err:#b91c1c;--err-bg:#fee2e2;--accent:#2563eb}
@media (prefers-color-scheme:dark){:root{--bg:#0f1218;--card:#171b23;--text:#e6e8ee;--muted:#9aa3b2;--line:#262c38;
--yes:#4ade80;--yes-bg:#12301d;--else:#fbbf24;--else-bg:#36290b;--no:#9aa3b2;--no-bg:#1e2430;--unk-bg:#161a22;
--err:#f87171;--err-bg:#3a1515;--accent:#60a5fa}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1000px;margin:0 auto;padding:16px}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;margin-bottom:16px}
h1{font-size:20px;margin:0}.muted{color:var(--muted);font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:16px}
.btns{display:flex;gap:8px;flex-wrap:wrap}
button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--text);
border-radius:8px;padding:8px 14px;cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button:disabled{opacity:.5;cursor:default}
.banner{padding:10px 14px;border-radius:10px;margin-bottom:16px;font-weight:600}
.banner.hit{background:var(--yes-bg);color:var(--yes)}.banner.run{background:var(--else-bg);color:var(--else)}
.tablewrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;min-width:480px}
th,td{padding:8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}
th.c{text-align:center}
th small{display:block;color:var(--muted);font-weight:400}
td.c{text-align:center}
.pill{display:inline-block;min-width:72px;padding:3px 8px;border-radius:999px;font-size:13px;font-weight:600}
.yes{background:var(--yes-bg);color:var(--yes)}.elsewhere{background:var(--else-bg);color:var(--else)}
.no{background:var(--err-bg);color:var(--err)}.unknown{background:var(--unk-bg);color:var(--muted)}
.meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}
.meta div{font-size:13px}.meta b{display:block;font-size:14px}
.err{color:var(--err);font-size:12px;word-break:break-word}
pre{margin:0;max-height:420px;overflow:auto;font:12px/1.4 ui-monospace,Consolas,monospace;white-space:pre-wrap;word-break:break-word}
#msg{font-size:13px;color:var(--muted);min-height:18px}
.s{display:none}
.status{display:flex;flex-wrap:wrap;gap:8px 18px;align-items:center}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;vertical-align:middle}
.dot.ok{background:var(--yes)}.dot.warn{background:var(--else)}.dot.bad{background:var(--err)}.dot.off{background:var(--no)}
.status div{font-size:14px}.status .muted{font-size:12px}
details.card>summary{cursor:pointer;font-weight:600;list-style:none}
details.card>summary::-webkit-details-marker{display:none}
details.card>summary:after{content:" ▸";color:var(--muted)}details.card[open]>summary:after{content:" ▾"}
details.sec{border:1px solid var(--line);border-radius:10px;margin:12px 0 0;background:var(--card)}
details.sec>summary{cursor:pointer;list-style:none;padding:10px 12px;font-weight:600;display:flex;justify-content:space-between}
details.sec>summary::-webkit-details-marker{display:none}
details.sec>summary:after{content:"▸";color:var(--muted)}details.sec[open]>summary:after{content:"▾"}
details.sec[open]>summary{border-bottom:1px solid var(--line)}
.secbody{padding:4px 12px 12px}
#toasts{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);display:flex;flex-direction:column;gap:8px;
z-index:50;width:min(420px,calc(100% - 32px));pointer-events:none}
.toast{pointer-events:auto;display:flex;gap:10px;align-items:flex-start;padding:12px 14px;border-radius:10px;
font-size:14px;box-shadow:0 6px 24px rgba(0,0,0,.18);border:1px solid var(--line);background:var(--card);color:var(--text);
animation:tin .2s ease-out}
.toast.ok{border-left:5px solid var(--yes)}.toast.err{border-left:5px solid var(--err)}.toast.info{border-left:5px solid var(--accent)}
.toast .x{margin-left:auto;cursor:pointer;color:var(--muted);border:0;background:none;padding:0 2px;font-size:16px;line-height:1}
.toast.out{opacity:0;transform:translateY(8px);transition:all .25s}
@keyframes tin{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
label{display:block;margin:8px 0 4px;font-size:13px;color:var(--muted)}
input[type=text],input[type=number],select{font:inherit;width:100%;max-width:320px;padding:7px 9px;border:1px solid var(--line);
border-radius:8px;background:var(--bg);color:var(--text)}
.row{display:flex;flex-wrap:wrap;gap:10px 16px;align-items:flex-end}.row>div{flex:0 1 auto}
.chk{display:flex;align-items:center;gap:8px;margin:6px 0;color:var(--text);font-size:14px}
.stores{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:2px 12px;margin-top:6px}
.hint{font-size:12px;color:var(--muted);margin-top:4px}
.save{margin-top:12px}
@media (max-width:560px){main{padding:12px}.l{display:none}.s{display:inline}
table{min-width:0}th,td{padding:7px 3px;font-size:13px}th small{font-size:11px}
.pill{min-width:0;padding:3px 7px;font-size:12px}.card{padding:10px}}
</style></head><body><main>
<header><div><h1>📱 iPhone-Duo-Wächter</h1><div class="muted" id="now"></div></div>
<div class="btns"><button class="primary" id="run">Jetzt prüfen</button>
<button id="test">Test-Push</button><button id="refresh">Aktualisieren</button></div></header>
<div id="toasts" aria-live="polite"></div>
<div id="banner"></div>
<div class="card"><div class="status" id="sched"></div></div>
<div class="card"><div class="tablewrap"><table id="tbl"></table></div>
<div class="muted" style="margin-top:8px">Grün = abholbar · Gelb = in einem anderen Markt abholbar · Rot = nicht verfügbar · Hell = keine Daten</div></div>
<div class="card"><div class="meta" id="meta"></div></div>
<details class="card" id="setbox"><summary>⚙️ Einstellungen</summary>
<details class="sec" data-k="zeitplan"><summary>🕒 Zeitplan</summary><div class="secbody">
<label class="chk"><input type="checkbox" id="s_enabled"> Überwachung aktiv (automatische Prüfungen)</label>
<div class="row">
<div><label>Takt</label><select id="s_every"><option>1</option><option>2</option><option>3</option><option selected>5</option><option>10</option><option>15</option><option>20</option><option>30</option></select></div>
<div><label>von (Uhr)</label><input type="number" id="s_from" min="0" max="23" style="width:90px"></div>
<div><label>bis (Uhr)</label><input type="number" id="s_to" min="1" max="24" style="width:90px"></div>
</div><div class="hint">Takt in Minuten. „bis 22“ heißt: letzter Lauf um 21:55 Uhr.</div>
<button class="save primary" id="save_sched">Zeitplan speichern</button></div></details>

<details class="sec" data-k="apple"><summary>🍏 Apple Stores</summary><div class="secbody">
<label class="chk"><input type="checkbox" id="a_all"> Alle Apple Stores in Deutschland</label>
<div id="a_list" class="stores"></div>
<div class="hint" id="a_hint"></div>
<div class="btns save"><button id="a_load">Store-Liste von Apple laden</button><button id="a_save" class="primary">Auswahl speichern</button></div>
</div></details>

<details class="sec" data-k="haendler"><summary>🛒 Händler</summary><div class="secbody">
<label class="chk"><input type="checkbox" id="m_mm"> MediaMarkt prüfen</label>
<label class="chk"><input type="checkbox" id="m_sat"> Saturn prüfen</label>
<div class="row">
<div><label>MediaMarkt-Markt-IDs</label><input type="text" id="MEDIAMARKT_STORES" placeholder="460"></div>
<div><label>Saturn-Markt-IDs</label><input type="text" id="SATURN_STORES" placeholder="12"></div>
</div><div class="hint">Mehrere mit Komma. Die ID steht im Browser-Cookie <code>MC_OUTLET_ID</code>, nachdem du auf der Shopseite einen Markt gewählt hast.</div>
<label class="chk" style="margin-top:12px"><input type="checkbox" id="CYBERPORT"> Cyberport prüfen</label>
<div><label>Cyberport-Takt (Minuten)</label><input type="number" id="CYBERPORT_EVERY_MIN" min="5" max="240" style="width:110px"></div>
<button class="save primary save_set">Händler speichern</button></div></details>

<details class="sec" data-k="push"><summary>🔔 Benachrichtigung</summary><div class="secbody">
<div class="row">
<div><label>ntfy-Thema</label><input type="text" id="NTFY_TOPIC"></div>
<div><label>Lebenszeichen alle … Stunden (0 = aus)</label><input type="number" id="PING_HOURS" min="0" max="24" step="0.5" style="width:110px"></div>
</div>
<label class="chk" style="margin-top:12px"><input type="checkbox" id="DEBUG"> Ausführliches Log (Debug)</label>
<button class="save primary save_set">Benachrichtigung speichern</button></div></details>
</details>

<div class="card"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
<b>Log</b><span class="muted">letzte 300 Zeilen</span></div><pre id="log"></pre></div>
</main>
<script>
const $=id=>document.getElementById(id);
const label={yes:"abholbar",elsewhere:"anderswo",no:"nein",unknown:"–"};
function toast(text,kind="ok",ms=4500){
  const t=document.createElement("div");t.className="toast "+kind;
  t.innerHTML=`<span>${kind==="ok"?"✅":kind==="err"?"⚠️":"⏳"}</span><span>${esc(text)}</span><button class="x" aria-label="Schließen">×</button>`;
  const close=()=>{t.classList.add("out");setTimeout(()=>t.remove(),260)};
  t.querySelector(".x").onclick=close;$("toasts").appendChild(t);
  if(ms) setTimeout(close,ms);
  return close;
}
function esc(s){return String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
async function load(){
  try{
    const s=await (await fetch("api/status",{cache:"no-store"})).json();
    $("now").textContent="Stand "+s.now+(s.running?" · Prüfung läuft …":"");
    const sc=s.schedule,ri=s.run;
    let st,cls;
    if(!sc.installed){st="Kein Cronjob eingerichtet";cls="bad"}
    else if(!sc.enabled){st="Überwachung pausiert";cls="off"}
    else if(sc.cron_service!=="active"&&sc.cron_service!=="unbekannt"){st="Cron-Dienst läuft nicht ("+sc.cron_service+")";cls="bad"}
    else if(ri.overdue){st="Aktiv – aber letzter automatischer Lauf überfällig";cls="warn"}
    else {st="Überwachung aktiv";cls="ok"}
    const lr=ri.last?`${esc(ri.last.at)} · ${esc(ri.last.trigger)} · ${esc(ri.last.duration)}${ri.last.ok?"":" · <span class='err'>mit Fehler</span>"}`:"–";
    $("sched").innerHTML=`<div><span class="dot ${cls}"></span><b>${st}</b></div>`+
      (sc.installed?`<div class="muted">alle ${sc.every} Min. · ${sc.from}–${sc.to} Uhr</div>`:"")+
      `<div class="muted">Letzter Lauf: ${lr}</div><div class="muted">Letzter automatischer: ${esc(ri.cron_last||"–")}</div>`+
      `<div class="muted">Nächster: ${esc(s.next_run)}</div>`;
    $("run").disabled=$("test").disabled=s.running;
    $("banner").innerHTML=s.any_hit?'<div class="banner hit">🎉 Mindestens eine Variante ist abholbar – schnell reservieren!</div>'
      :(s.running?'<div class="banner run">⏳ Prüfung läuft gerade …</div>':"");
    const short={MediaMarkt:"MM",Cyberport:"CP",Saturn:"Saturn",Apple:"Apple"};
    let h="<tr><th>Variante</th>"+s.columns.map(c=>`<th class="c"><span class="l">${esc(c.name)}</span><span class="s">${esc(short[c.name]||c.name)}</span><small>${esc(c.sub)}</small></th>`).join("")+"</tr>";
    for(const r of s.rows){const sv=r.variant.replace("Sternenweiß","Weiß").replace("Nachthimmel","Nacht");
      h+=`<tr><td><span class="l">${esc(r.variant)}</span><span class="s">${esc(sv)}</span></td>`+r.cells.map(c=>
      `<td class="c"><span class="pill ${c.state}" title="${esc(c.detail.join("\n"))}">${label[c.state]}</span></td>`).join("")+"</tr>"}
    $("tbl").innerHTML=h;
    $("meta").innerHTML=s.columns.map(c=>`<div><b>${esc(c.name)}</b>Zuletzt: ${esc(c.updated)}`+
      (c.pause?`<br>Pause bis ${esc(c.pause)}`:"")+(c.error?`<div class="err">Fehler: ${esc(c.error)}</div>`:"")+`</div>`).join("")+
      `<div><b>Lebenszeichen</b>Zuletzt: ${esc(s.last_ping)}<br>Nächstes: ${esc(s.next_ping)}</div>`;
    const lg=await (await fetch("api/log",{cache:"no-store"})).text();
    const p=$("log"),atEnd=p.scrollTop+p.clientHeight>=p.scrollHeight-20;
    p.textContent=lg; if(atEnd) p.scrollTop=p.scrollHeight;
  }catch(e){if(!window._lerr){window._lerr=1;toast("Dashboard nicht erreichbar: "+e,"err",8000);setTimeout(()=>window._lerr=0,30000)}}
}
async function post(path){
  const r=await fetch(path,{method:"POST"});toast(await r.text(),r.ok?"ok":"err");
  setTimeout(load,1500);
}
let stores=[];
function renderStores(sel){
  const all=$("a_all").checked;
  if(!stores.length){$("a_list").innerHTML="";$("a_hint").textContent="Noch keine Store-Liste – einmal „Store-Liste von Apple laden“ drücken (dauert ca. 15 s).";return}
  $("a_hint").textContent=stores.length+" Stores bekannt."+(all?" Bei „Alle Stores“ wird ganz Deutschland abgefragt.":"");
  $("a_list").innerHTML=stores.map(x=>`<label class="chk"><input type="checkbox" class="a_store" value="${esc(x.number)}" ${sel.has(x.name)?"checked":""} ${all?"disabled":""}> ${esc(x.city)} – ${esc(x.name)}</label>`).join("");
}
async function loadSettings(){
  const d=await (await fetch("api/settings",{cache:"no-store"})).json();
  const v=d.settings,sc=d.schedule;
  $("s_enabled").checked=sc.installed&&sc.enabled;$("s_every").value=String(sc.every);$("s_from").value=sc.from;$("s_to").value=sc.to;
  for(const k of ["MEDIAMARKT_STORES","SATURN_STORES","CYBERPORT_EVERY_MIN","NTFY_TOPIC","PING_HOURS"]) $(k).value=v[k];
  $("CYBERPORT").checked=v.CYBERPORT!=="0";$("DEBUG").checked=v.DEBUG==="1";
  const shops=v.MMS_SHOPS.split(",");$("m_mm").checked=shops.includes("MediaMarkt");$("m_sat").checked=shops.includes("Saturn");
  stores=d.apple_stores||[];
  $("a_all").checked=v.STORE_FILTER.trim()==="*";
  const sel=new Set(v.STORE_FILTER.split(",").map(x=>x.trim()));
  // alte Einstellung "Dresden" (Stadt) -> passende Stores vorauswählen
  for(const x of stores){if(sel.has(x.city))sel.add(x.name)}
  renderStores(sel);
}
async function send(path,obj,reload=true){
  const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(obj)});
  toast(await r.text(),r.ok?"ok":"err",r.ok?4500:9000);
  if(r.ok&&reload){loadSettings();load()}
}
$("a_all").onchange=()=>renderStores(new Set([...document.querySelectorAll(".a_store:checked")].map(c=>stores.find(x=>x.number===c.value)?.name)));
$("a_load").onclick=async()=>{
  $("a_load").disabled=true;const busy=toast("Frage Apple nach allen Stores … (ca. 15 Sekunden)","info",0);
  const r=await fetch("api/apple-discover",{method:"POST"});
  busy();
  if(r.ok){stores=await r.json();toast(stores.length+" Apple Stores gefunden – jetzt auswählen und speichern.","ok");
    renderStores(new Set([...document.querySelectorAll(".a_store:checked")].map(c=>c.parentElement.textContent.split(" – ")[1])))}
  else toast(await r.text(),"err",9000);
  $("a_load").disabled=false;
};
$("a_save").onclick=()=>send("api/apple-selection",{all:$("a_all").checked,stores:[...document.querySelectorAll(".a_store:checked")].map(c=>c.value)});
$("save_sched").onclick=()=>send("api/schedule",{enabled:$("s_enabled").checked,every:+$("s_every").value,from:+$("s_from").value,to:+$("s_to").value});
const saveSet=()=>{
  const shops=[$("m_mm").checked?"MediaMarkt":"",$("m_sat").checked?"Saturn":""].filter(Boolean).join(",");
  send("api/settings",{MEDIAMARKT_STORES:$("MEDIAMARKT_STORES").value.replace(/\s/g,""),SATURN_STORES:$("SATURN_STORES").value.replace(/\s/g,""),
    MMS_SHOPS:shops,CYBERPORT:$("CYBERPORT").checked?"1":"0",CYBERPORT_EVERY_MIN:$("CYBERPORT_EVERY_MIN").value,
    NTFY_TOPIC:$("NTFY_TOPIC").value.trim(),PING_HOURS:$("PING_HOURS").value,DEBUG:$("DEBUG").checked?"1":"0"});
};
document.querySelectorAll(".save_set").forEach(b=>b.onclick=saveSet);
// Auf-/Zuklappzustand merken (pro Browser)
function remember(el,key){
  try{if(localStorage.getItem(key)==="1")el.open=true}catch(e){}
  el.addEventListener("toggle",()=>{try{localStorage.setItem(key,el.open?"1":"0")}catch(e){}});
}
remember($("setbox"),"dw_set");
document.querySelectorAll("details.sec").forEach(d=>remember(d,"dw_sec_"+d.dataset.k));
$("setbox").addEventListener("toggle",()=>{if($("setbox").open)loadSettings()});
if($("setbox").open)loadSettings();
$("run").onclick=()=>post("api/run");$("test").onclick=()=>post("api/test-push");$("refresh").onclick=load;
load();setInterval(load,15000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keine Zugriffs-Logs im Terminal
        pass

    def _auth_ok(self) -> bool:
        if not PASSWORD:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                _, pw = base64.b64decode(h[6:]).decode("utf-8").split(":", 1)
                if pw == PASSWORD:
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Duo-Waechter"')
        self.end_headers()
        return False

    def _send(self, code: int, body: str, ctype: str = "text/plain; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._auth_ok():
            return
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif path == "/api/status":
            self._send(200, json.dumps(build_status(), ensure_ascii=False), "application/json; charset=utf-8")
        elif path == "/api/log":
            self._send(200, read_log())
        elif path == "/api/settings":
            self._send(200, json.dumps({"settings": current_settings(), "schedule": read_schedule(),
                                        "apple_stores": known_apple_stores()}, ensure_ascii=False),
                       "application/json; charset=utf-8")
        else:
            self._send(404, "Nicht gefunden")

    def do_POST(self):
        if not self._auth_ok():
            return
        path = self.path.split("?")[0]
        if path == "/api/run":
            self._send(*start_run())
        elif path == "/api/test-push":
            self._send(*start_run(test_push=True))
        elif path in ("/api/settings", "/api/schedule", "/api/apple-selection", "/api/apple-discover"):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                if path == "/api/settings":
                    write_settings({k: str(v).strip() for k, v in body.items()})
                    self._send(200, "Einstellungen gespeichert – gelten ab dem nächsten Lauf.")
                elif path == "/api/schedule":
                    write_schedule(bool(body.get("enabled")), int(body.get("every", 5)),
                                   int(body.get("from", 6)), int(body.get("to", 22)))
                    self._send(200, "Zeitplan gespeichert.")
                elif path == "/api/apple-selection":
                    save_apple_selection([str(x) for x in body.get("stores", [])], bool(body.get("all")))
                    self._send(200, "Apple-Stores gespeichert – gelten ab dem nächsten Lauf.")
                else:
                    stores = discover_apple_stores()
                    self._send(200 if stores else 502, json.dumps(stores, ensure_ascii=False) if stores
                               else "Apple hat keine Stores geliefert – später nochmal versuchen.",
                               "application/json; charset=utf-8" if stores else "text/plain; charset=utf-8")
            except (ValueError, RuntimeError) as e:
                self._send(400, str(e))
        else:
            self._send(404, "Nicht gefunden")


if __name__ == "__main__":
    print(f"Dashboard läuft auf Port {PORT}" + (" (mit Passwort)" if PASSWORD else ""), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
