"""Home Assistant (optional): Zustände lesen, Geräte schalten, Verlauf, Kamerabild, Push.

Alles über die REST-API von Home Assistant mit einem langlebigen Zugangs-Token (HA → Profil → Sicherheit).
Einstellungen (alle in settings, Standard: aus):
  ha_enabled      – ohne diesen Schalter sieht das Modell keine HA-Werkzeuge
  ha_url          – z. B. http://homeassistant.local:8123
  ha_token        – langlebiger Zugangs-Token
  ha_mode         – "lesen" (nur ansehen) oder "schalten" (Dienste aufrufen, mit Freigabe)
  ha_block        – Geräte, die nie geschaltet werden dürfen: Liste von Mustern (lock.*, alarm_control_panel.*)
  ha_notify       – Push-Ziel, z. B. notify.mobile_app_mein_handy
  ha_watch_push   – Wachposten-Meldungen zusätzlich als Push
"""
import fnmatch
import json
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BLOCK = ["lock.*", "alarm_control_panel.*"]
# Domains, deren Zustand sich lohnt anzuzeigen, wenn nichts gesucht wird (Rest ist meist Technik-Rauschen)
USEFUL = ("light", "switch", "sensor", "binary_sensor", "climate", "cover", "media_player", "lock", "person",
          "camera", "fan", "vacuum", "alarm_control_panel", "scene", "script", "weather", "input_boolean")


def enabled(settings) -> bool:
    return bool(settings.get("ha_enabled") and settings.get("ha_url") and settings.get("ha_token"))


class HAError(Exception):
    pass


def _req(settings, method, path, data=None, timeout=15, raw=False):
    url = settings.get("ha_url", "").rstrip("/") + path
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": f"Bearer {settings.get('ha_token', '')}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read()
            ctype = r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise HAError("Zugang verweigert (401) – Token falsch oder abgelaufen.")
        if e.code == 404:
            raise HAError(f"Nicht gefunden (404): {path}")
        raise HAError(f"HTTP {e.code}: {e.read()[:200].decode('utf-8', 'replace')}")
    except urllib.error.URLError as e:
        raise HAError(f"Home Assistant nicht erreichbar ({settings.get('ha_url')}): {e.reason}")
    except TimeoutError:
        raise HAError("Home Assistant antwortet nicht (Zeitüberschreitung).")
    if raw:
        return payload, ctype
    try:
        return json.loads(payload or b"null")
    except ValueError:
        return payload.decode("utf-8", "replace")


def test(settings) -> str:
    try:
        cfg = _req(settings, "GET", "/api/config", timeout=8)
        states = _req(settings, "GET", "/api/states", timeout=15)
    except HAError as e:
        return f"✗ {e}"
    return f"✓ Home Assistant {cfg.get('version', '?')} · „{cfg.get('location_name', '')}“ · {len(states)} Entitäten"


def notify_services(settings) -> list[str]:
    try:
        svcs = _req(settings, "GET", "/api/services", timeout=10)
    except HAError:
        return []
    for d in svcs:
        if d.get("domain") == "notify":
            return sorted(f"notify.{s}" for s in d.get("services", {}) if s not in ("notify", "persistent_notification", "send_message"))
    return []


def blocked(settings, entity_id: str) -> bool:
    pats = settings.get("ha_block")
    pats = DEFAULT_BLOCK if pats is None else pats
    return any(fnmatch.fnmatch(entity_id or "", p.strip()) for p in pats if p.strip())


def _name(s):
    return s.get("attributes", {}).get("friendly_name") or s["entity_id"]


def _fmt(s):
    a = s.get("attributes", {})
    unit = a.get("unit_of_measurement") or ""
    extra = ""
    if s["entity_id"].startswith("climate."):
        extra = f" (Ist {a.get('current_temperature')}°, Soll {a.get('temperature')}°)"
    elif s["entity_id"].startswith("light.") and s.get("state") == "on" and a.get("brightness") is not None:
        extra = f" ({round(a['brightness'] / 2.55)} %)"
    return f"{s['entity_id']} · {_name(s)}: {s.get('state')}{(' ' + unit) if unit else ''}{extra}"


def area_entities(settings, area: str) -> list[str]:
    tpl = "{{ area_entities(%s) | tojson }}" % json.dumps(area, ensure_ascii=False)
    try:
        r = _req(settings, "POST", "/api/template", {"template": tpl}, timeout=10)
        return json.loads(r) if isinstance(r, str) else (r or [])
    except (HAError, ValueError):
        return []


def states(settings, search="", domain="", area="", limit=60) -> str:
    all_ = _req(settings, "GET", "/api/states", timeout=20)
    ids = set(area_entities(settings, area)) if area else None
    words = [w for w in (search or "").lower().split() if w]
    fold = lambda t: t.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    out, partial = [], []
    for s in all_:
        eid = s["entity_id"]
        if domain and not eid.startswith(domain.rstrip(".") + "."):
            continue
        if ids is not None and eid not in ids:
            continue
        if not (words or domain or area) and eid.split(".")[0] not in USEFUL:
            continue
        hay = (eid + " " + _name(s)).lower()
        hay += " " + fold(hay)                              # „buero“ findet „Büro“ und umgekehrt
        hits = sum(1 for w in words if w in hay or fold(w) in hay)
        if hits == len(words):
            out.append(_fmt(s))
        elif hits:
            partial.append((hits, _fmt(s)))
    note = ""
    if not out and partial:          # kein Gerät passt auf alle Wörter → die mit den meisten Treffern
        best = max(h for h, _ in partial)
        out = [f for h, f in partial if h == best]
        note = "(nicht alle Suchwörter passten – das hier kommt am nächsten)\n"
    if not out:
        return "Nichts gefunden." + (" (Tipp: kürzerer Suchbegriff oder domain=…)" if words else "")
    more = f"\n… und {len(out) - limit} weitere – genauer suchen (search/domain/area)." if len(out) > limit else ""
    return note + "\n".join(sorted(out)[:limit]) + more


def state(settings, entity_id) -> str:
    s = _req(settings, "GET", f"/api/states/{urllib.parse.quote(entity_id)}")
    attrs = {k: v for k, v in (s.get("attributes") or {}).items() if k not in ("entity_picture", "icon", "supported_features")}
    return (f"{_fmt(s)}\nzuletzt geändert: {s.get('last_changed', '')[:19].replace('T', ' ')} (UTC)\n"
            f"Attribute: {json.dumps(attrs, ensure_ascii=False)[:2500]}")


def call(settings, domain, service, entity_id="", data=None) -> str:
    body = dict(data or {})
    if entity_id:
        body["entity_id"] = entity_id
    r = _req(settings, "POST", f"/api/services/{domain}/{service}", body, timeout=20)
    changed = [f"{x['entity_id']}: {x.get('state')}" for x in (r or []) if isinstance(x, dict) and "entity_id" in x]
    return "OK." + (" Jetzt: " + ", ".join(changed[:10]) if changed else "")


def history(settings, entity_id, hours=24) -> str:
    start = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - float(hours) * 3600)) + "+00:00"
    q = urllib.parse.urlencode({"filter_entity_id": entity_id, "minimal_response": "", "no_attributes": ""})
    r = _req(settings, "GET", f"/api/history/period/{urllib.parse.quote(start)}?{q}", timeout=30)
    rows = (r or [[]])[0] if r else []
    if not rows:
        return f"Kein Verlauf für {entity_id} in den letzten {hours} h."
    out, prev = [], None
    for x in rows:
        st = x.get("state")
        if st == prev:
            continue
        prev = st
        t = (x.get("last_changed") or "")[:19]
        try:
            t = time.strftime("%d.%m. %H:%M", time.localtime(time.mktime(time.strptime(t, "%Y-%m-%dT%H:%M:%S")) - time.timezone))
        except ValueError:
            pass
        out.append(f"{t}  {st}")
    lim = out[-80:]
    return (f"{entity_id}, letzte {hours} h ({len(out)} Wechsel" + (", nur die letzten 80" if len(out) > 80 else "")
            + "):\n" + "\n".join(lim))


def camera(settings, entity_id) -> bytes:
    data, ctype = _req(settings, "GET", f"/api/camera_proxy/{urllib.parse.quote(entity_id)}", timeout=20, raw=True)
    if "image" not in ctype:
        raise HAError(f"Kein Bild von {entity_id} ({ctype or 'unbekannt'}).")
    return data


def push(settings, message, title="spike", target="") -> str:
    target = target or settings.get("ha_notify") or ""
    if not target.startswith("notify."):
        raise HAError("Kein Push-Ziel eingestellt (⚙ Einstellungen → Home Assistant → Push-Ziel).")
    _req(settings, "POST", f"/api/services/notify/{target.split('.', 1)[1]}",
         {"message": message[:1000], "title": title[:100]}, timeout=15)
    return f"Push an {target} gesendet."
