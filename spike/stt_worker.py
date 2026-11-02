"""Spracherkennung als Hintergrund-Prozess (läuft mit einem Python, in dem faster-whisper installiert ist).

Protokoll über stdin/stdout, eine JSON-Zeile pro Auftrag:
    -> {"file": "/pfad/aufnahme.wav", "language": "de"}
    <- {"text": "…", "device": "cuda"|"cpu", "seconds": 1.2}   oder   {"error": "…"}
Das Modell wird beim ersten Auftrag geladen und bleibt im Speicher.
Aufruf: python stt_worker.py <modellpfad-oder-name> <device: auto|cuda|cpu>
"""
import json
import sys
import time

MODEL, DEVICE = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "auto")
_model = None
_device = None


def load():
    global _model, _device
    from faster_whisper import WhisperModel
    tries = [("cuda", "float16"), ("cpu", "int8")] if DEVICE == "auto" else \
        [("cuda", "float16")] if DEVICE == "cuda" else [("cpu", "int8")]
    last = None
    for dev, ct in tries:
        try:
            _model = WhisperModel(MODEL, device=dev, compute_type=ct)
            _device = dev
            return
        except Exception as e:          # z. B. GPU voll → CPU
            last = e
    raise RuntimeError(f"Modell konnte nicht geladen werden: {last}")


def reply(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    try:
        job = json.loads(line)
        t = time.time()
        if _model is None:
            load()
        segs, _ = _model.transcribe(job["file"], language=job.get("language") or None, vad_filter=True,
                                    beam_size=5)
        text = " ".join(s.text.strip() for s in segs).strip()
        reply({"text": text, "device": _device, "seconds": round(time.time() - t, 2)})
    except Exception as e:
        reply({"error": f"{type(e).__name__}: {e}"})
