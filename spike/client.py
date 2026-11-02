"""Minimaler OpenAI-kompatibler Client (llama.cpp llama-server, ohne externe Abhängigkeiten)."""
import json
import urllib.error
import urllib.request


def api_base(url: str) -> str:
    url = url.strip().rstrip("/")
    return url if url.endswith("/v1") else url + "/v1"


def server_root(url: str) -> str:
    url = url.strip().rstrip("/")
    return url[:-3] if url.endswith("/v1") else url


def _open(url, api_key="", data=None, timeout=600):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method="POST" if body else "GET")
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"HTTP {e.code}: {detail}") from None


def list_models(ep: dict) -> list[str]:
    with _open(api_base(ep["url"]) + "/models", ep.get("api_key", ""), timeout=5) as r:
        return [m["id"] for m in json.load(r).get("data", [])]


def get_n_ctx(ep: dict) -> int | None:
    """llama.cpp liefert die Kontextgröße unter /props."""
    try:
        with _open(server_root(ep["url"]) + "/props", ep.get("api_key", ""), timeout=5) as r:
            d = json.load(r)
        return d.get("default_generation_settings", {}).get("n_ctx") or d.get("n_ctx")
    except Exception:
        return None


def stream_chat(ep, model, messages, tools, params, should_stop):
    payload = {
        "model": model or "default",
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        **params,
    }
    if tools:
        payload["tools"] = tools
    resp = _open(api_base(ep["url"]) + "/chat/completions", ep.get("api_key", ""), payload)
    try:
        for raw in resp:
            if should_stop():
                return
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                return
            try:
                yield json.loads(data)
            except json.JSONDecodeError:
                continue
    finally:
        resp.close()


def complete(ep, model, messages, params, should_stop=lambda: False) -> str:
    """Antwort ohne Werkzeuge als Text (für Zusammenfassungen). Denken wird – wo möglich – abgeschaltet."""
    def run(extra):
        out = []
        for chunk in stream_chat(ep, model, messages, None, {**params, **extra}, should_stop):
            for ch in chunk.get("choices") or []:
                out.append((ch.get("delta") or {}).get("content") or "")
        return "".join(out).strip()
    try:
        # llama.cpp: Denken für diese Anfrage aus (Qwen u. a.); andere Server lehnen das evtl. ab → ohne
        return run({"chat_template_kwargs": {"enable_thinking": False}})
    except RuntimeError as e:
        if "HTTP 4" not in str(e):
            raise
        return run({})
