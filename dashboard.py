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
        "name": "Apple", "sub": cfg("STORE_FILTER", "Dresden"),
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
    return {
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
    env = dict(os.environ, FORCE="1")
    if test_push:
        env["TEST_PUSH"] = "1"
    try:
        subprocess.Popen(["bash", os.path.join(DIR, "run.sh")], cwd=DIR, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:
        return 500, f"Start fehlgeschlagen: {e}"
    return 202, "Prüfung gestartet" + (" (mit Test-Push)" if test_push else "") + " – Ergebnis in ca. 1 Minute."


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
.no{background:var(--no-bg);color:var(--no)}.unknown{background:var(--unk-bg);color:var(--muted)}
.meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}
.meta div{font-size:13px}.meta b{display:block;font-size:14px}
.err{color:var(--err);font-size:12px;word-break:break-word}
pre{margin:0;max-height:420px;overflow:auto;font:12px/1.4 ui-monospace,Consolas,monospace;white-space:pre-wrap;word-break:break-word}
#msg{font-size:13px;color:var(--muted);min-height:18px}
.s{display:none}
@media (max-width:560px){main{padding:12px}.l{display:none}.s{display:inline}
table{min-width:0}th,td{padding:7px 3px;font-size:13px}th small{font-size:11px}
.pill{min-width:0;padding:3px 7px;font-size:12px}.card{padding:10px}}
</style></head><body><main>
<header><div><h1>📱 iPhone-Duo-Wächter</h1><div class="muted" id="now"></div></div>
<div class="btns"><button class="primary" id="run">Jetzt prüfen</button>
<button id="test">Test-Push</button><button id="refresh">Aktualisieren</button></div></header>
<div id="msg"></div>
<div id="banner"></div>
<div class="card"><div class="tablewrap"><table id="tbl"></table></div>
<div class="muted" style="margin-top:8px">Grün = abholbar · Gelb = in einem anderen Markt abholbar · Grau = nicht verfügbar · Hell = keine Daten</div></div>
<div class="card"><div class="meta" id="meta"></div></div>
<div class="card"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
<b>Log</b><span class="muted">letzte 300 Zeilen</span></div><pre id="log"></pre></div>
</main>
<script>
const $=id=>document.getElementById(id);
const label={yes:"abholbar",elsewhere:"anderswo",no:"nein",unknown:"–"};
function esc(s){return String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
async function load(){
  try{
    const s=await (await fetch("api/status",{cache:"no-store"})).json();
    $("now").textContent="Stand "+s.now+(s.running?" · Prüfung läuft …":"");
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
  }catch(e){$("msg").textContent="Fehler beim Laden: "+e}
}
async function post(path){
  $("msg").textContent="…";
  const r=await fetch(path,{method:"POST"});$("msg").textContent=await r.text();
  setTimeout(load,1500);
}
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
        else:
            self._send(404, "Nicht gefunden")


if __name__ == "__main__":
    print(f"Dashboard läuft auf Port {PORT}" + (" (mit Passwort)" if PASSWORD else ""), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
