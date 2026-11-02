"""Sprache: Aufnahme (PipeWire), Spracherkennung (faster-whisper oder OpenAI-kompatibel), Vorlesen (VoxCPM-API).

Einstellungen (settings):
    stt_python   Python mit faster-whisper (leer = automatisch, z. B. ~/pyvideotrans/.venv/bin/python)
    stt_model    Modellordner oder Name (Standard: large-v3-turbo aus ~/pyvideotrans/models)
    stt_device   auto | cuda | cpu
    stt_url      statt lokal: OpenAI-kompatibler Server (…/v1/audio/transcriptions)
    stt_language de
    tts_url      VoxCPM-API (POST text/voice/language → JSON mit url), Standard http://127.0.0.1:8000/tts
    tts_voice    senol1
    tts_start    Befehl, der die TTS-API startet, falls sie nicht läuft (leer = nicht automatisch starten)
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .config import DATA_DIR

REC_DIR = DATA_DIR / "audio"
DEFAULT_MODEL_DIRS = [
    Path.home() / "pyvideotrans/models/models--mobiuslabsgmbh--faster-whisper-large-v3-turbo",
    Path.home() / "pyvideotrans/models/models--Systran--faster-whisper-large-v3",
]
DEFAULT_PYTHONS = [Path.home() / "pyvideotrans/.venv/bin/python", Path(sys.executable)]


# ---------- Aufnahme ----------

class Recorder:
    def __init__(self):
        self.proc = None
        self.path = None
        self.started = 0.0

    def start(self):
        if not shutil.which("pw-record"):
            raise RuntimeError("pw-record (PipeWire) fehlt")
        REC_DIR.mkdir(parents=True, exist_ok=True)
        self.path = REC_DIR / f"aufnahme-{time.strftime('%Y%m%d-%H%M%S')}.wav"
        self.proc = subprocess.Popen(["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16",
                                      str(self.path)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.started = time.monotonic()

    def stop(self) -> Path | None:
        if not self.proc:
            return None
        self.proc.send_signal(signal.SIGINT)        # pw-record schreibt dann den WAV-Kopf sauber
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None
        if time.monotonic() - self.started < 0.4 or not self.path.exists() or self.path.stat().st_size < 4000:
            return None                              # zu kurz → ignorieren
        return self.path

    @property
    def active(self):
        return self.proc is not None

    def seconds(self):
        return time.monotonic() - self.started if self.proc else 0.0


# ---------- Spracherkennung ----------

_worker = None
_worker_key = None
_wlock = threading.Lock()


def _find_python(settings):
    p = (settings.get("stt_python") or "").strip()
    if p:
        return p
    for cand in DEFAULT_PYTHONS:
        if cand.exists():
            r = subprocess.run([str(cand), "-c", "import faster_whisper"], capture_output=True)
            if r.returncode == 0:
                return str(cand)
    return ""


def _find_model(settings):
    m = (settings.get("stt_model") or "").strip()
    if m:
        return os.path.expanduser(m)
    for d in DEFAULT_MODEL_DIRS:
        if (d / "model.bin").exists():
            return str(d)
    return "large-v3-turbo"


def transcribe(path, settings) -> tuple[str, str]:
    """Gibt (text, info) zurück. info z. B. 'cuda · 0.8 s'."""
    url = (settings.get("stt_url") or "").strip()
    lang = settings.get("stt_language", "de") or ""
    if url:
        return _transcribe_http(path, url, lang, settings.get("stt_api_key", "")), "Server"
    global _worker, _worker_key
    with _wlock:
        py, model, dev = _find_python(settings), _find_model(settings), settings.get("stt_device", "auto")
        if not py:
            raise RuntimeError("Kein Python mit faster-whisper gefunden (⚙ → Sprache → Python eintragen "
                               "oder stt_url für einen Server)")
        key = (py, model, dev)
        if _worker is None or _worker.poll() is not None or _worker_key != key:
            stop_worker()
            script = str(Path(__file__).with_name("stt_worker.py"))
            _worker = subprocess.Popen([py, "-I", script, model, dev], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
            _worker_key = key
        _worker.stdin.write(json.dumps({"file": str(path), "language": lang}) + "\n")
        _worker.stdin.flush()
        line = _worker.stdout.readline()
    if not line:
        raise RuntimeError("Spracherkennung abgestürzt")
    res = json.loads(line)
    if res.get("error"):
        raise RuntimeError(res["error"])
    return res.get("text", ""), f"{res.get('device')} · {res.get('seconds')} s"


def stop_worker():
    global _worker
    if _worker and _worker.poll() is None:
        try:
            _worker.stdin.close()
            _worker.wait(timeout=3)
        except Exception:
            _worker.kill()
    _worker = None


def _transcribe_http(path, url, lang, api_key=""):
    url = url.rstrip("/")
    if not url.endswith("/audio/transcriptions"):
        url += ("" if url.endswith("/v1") else "/v1") + "/audio/transcriptions"
    boundary = uuid.uuid4().hex
    data = Path(path).read_bytes()
    parts = []
    for k, v in (("model", "whisper-1"), ("language", lang), ("response_format", "json")):
        if v:
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\n'
                 f'Content-Type: audio/wav\r\n\r\n'.encode() + data + b"\r\n")
    body = b"".join(parts) + f"--{boundary}--\r\n".encode()
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r).get("text", "")


# ---------- Vorlesen ----------

def speakable(text: str) -> str:
    """Markdown in vorlesbaren Text umwandeln (Code, Links, Zeichen weg)."""
    t = re.sub(r"```.*?(```|\Z)", " (Code-Block ausgelassen) ", text, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"https?://\S+", "(Link)", t)
    t = re.sub(r"^\s*[#>*\-+]+\s*", "", t, flags=re.M)
    t = re.sub(r"[*_~|]+", "", t)
    t = re.sub(r"[\U0001F300-\U0001FAFF☀-➿]", "", t)       # Emojis
    return re.sub(r"\s+", " ", t).strip()


def gpu_free_mb() -> int:
    """Größter freier GPU-Speicher in MB (0 = keine NVIDIA-GPU oder alles voll)."""
    if not shutil.which("nvidia-smi"):
        return 0
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10)
        return max(int(x) for x in r.stdout.split() if x.strip().isdigit())
    except Exception:
        return 0


def chunks(text: str, size=220) -> list[str]:
    sents = [x for x in re.split(r"(?<=[.!?…:])\s+", text) if x]
    if len(sents) > 1:            # erster Satz allein → Vorlesen beginnt schneller
        return [sents[0]] + _merge(sents[1:], size)
    return _merge(sents, size)


def _merge(sents, size):
    out, cur = [], ""
    for s in sents:
        if len(cur) + len(s) < size:
            cur = f"{cur} {s}".strip()
        else:
            if cur:
                out.append(cur)
            cur = s
    if cur:
        out.append(cur)
    return out


class Speaker:
    """Liest Text satzweise vor: der nächste Abschnitt wird erzeugt, während der aktuelle läuft."""

    def __init__(self, settings, on_state=None):
        self.settings = settings
        self.on_state = on_state or (lambda s: None)
        self._stop = threading.Event()
        self._play = None
        self._thread = None
        self._started_server = None

    @property
    def busy(self):
        return self._thread is not None and self._thread.is_alive()

    def stop(self):
        self._stop.set()
        p = self._play
        if p and p.poll() is None:
            p.kill()

    def speak(self, text):
        self.stop()
        if self._thread:
            self._thread.join(timeout=2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(text, self._stop), daemon=True)
        self._thread.start()

    def _server_up(self, url):
        try:
            u = urllib.parse.urlparse(url)
            urllib.request.urlopen(f"{u.scheme}://{u.netloc}/docs", timeout=2).close()
            return True
        except Exception:
            return False

    def _ensure_server(self, url, stop):
        if self._server_up(url):
            return True
        cmd = (self.settings.get("tts_start") or "").strip()
        if not cmd:
            raise RuntimeError(f"Vorlese-Server {url} läuft nicht (⚙ → Sprache: Startbefehl eintragen)")
        dev = self.settings.get("tts_device", "auto")
        need = int(self.settings.get("tts_min_vram_mb", 6000))
        free = gpu_free_mb()
        use_cpu = dev == "cpu" or (dev == "auto" and free < need)
        env = dict(os.environ)
        if use_cpu:
            env["CUDA_VISIBLE_DEVICES"] = ""          # GPUs ausblenden → VoxCPM rechnet auf der CPU
            self.on_state(f"Starte Vorlese-Server auf der CPU (GPU-Speicher frei: {free} MB) – langsam, "
                          f"etwa 5× so lang wie gesprochen …")
        else:
            self.on_state("Starte Vorlese-Server auf der GPU …")
        self.cpu_mode = use_cpu
        if not (self._started_server and self._started_server.poll() is None):
            self._started_server = subprocess.Popen(["bash", "-c", cmd], stdout=subprocess.DEVNULL,
                                                    stderr=subprocess.DEVNULL, start_new_session=True, env=env)
            import atexit
            atexit.register(lambda p=self._started_server: p.poll() is None and os.killpg(p.pid, signal.SIGTERM))
        for _ in range(240):
            if stop.is_set():
                return False
            if self._server_up(url):
                return True
            if self._started_server.poll() is not None:
                raise RuntimeError("Vorlese-Server hat sich beendet (GPU-Speicher voll?)")
            time.sleep(0.5)
        raise RuntimeError("Vorlese-Server antwortet nicht")

    def _synth(self, url, text):
        data = urllib.parse.urlencode({"text": text, "voice": self.settings.get("tts_voice", "senol1"),
                                       "language": self.settings.get("tts_language", "de")}).encode()
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=300) as r:
            res = json.load(r)
        audio_url = res.get("url") or res.get("audio_url") or res.get("file")
        f = Path(tempfile.gettempdir()) / f"spike-tts-{uuid.uuid4().hex[:8]}.wav"
        with urllib.request.urlopen(audio_url, timeout=60) as r:
            f.write_bytes(r.read())
        return f

    def _run(self, text, stop):
        url = self.settings.get("tts_url") or "http://127.0.0.1:8000/tts"
        try:
            if not self._ensure_server(url, stop):
                return
            parts = chunks(speakable(text))
            if not parts:
                return
            slow = " (CPU, dauert)" if getattr(self, "cpu_mode", False) else ""
            self.on_state(f"🔊 Erzeuge Sprache …{slow}")
            nxt = self._synth(url, parts[0])
            for i in range(len(parts)):
                if stop.is_set():
                    break
                cur = nxt
                gen = None
                if i + 1 < len(parts):        # nächsten Abschnitt schon erzeugen, während dieser läuft
                    box = {}
                    gen = threading.Thread(target=lambda b=box, t=parts[i + 1]: b.update(f=self._synth(url, t)),
                                           daemon=True)
                    gen.start()
                self.on_state(f"🔊 Liest vor ({i + 1}/{len(parts)}) – /stop beendet")
                self._play = subprocess.Popen(["pw-play", str(cur)], stdout=subprocess.DEVNULL,
                                              stderr=subprocess.DEVNULL)
                self._play.wait()
                cur.unlink(missing_ok=True)
                if gen:
                    gen.join()
                    nxt = box.get("f")
                    if nxt is None:
                        break
        except Exception as e:
            self.on_state(f"⚠ Vorlesen: {e}")
            return
        self.on_state("")
