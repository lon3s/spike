"""Kontext sparsam halten: alte Werkzeug-Ausgaben kürzen, alten Verlauf zusammenfassen.

Ablauf pro Modell-Aufruf:
1. Ältere Werkzeug-Ausgaben werden gekürzt (die letzten N bleiben voll).
2. Ist ein Teil des Chats schon zusammengefasst (chat["summary"], chat["summary_upto"]), geht statt dieser
   Nachrichten nur die Zusammenfassung mit.
3. Wird es trotzdem zu voll, fasst agent.py den älteren Teil mit dem Modell zusammen (compact) –
   auch mitten in einer langen Werkzeug-Kette, an einer Stelle, die keine Werkzeug-Antwort abtrennt.
4. Notbremse: Passt es dann immer noch nicht, werden die ältesten Runden weggelassen.
"""
import json

ALLOWED_KEYS = {"role", "content", "tool_calls", "tool_call_id", "name"}
CONTINUE_NOTE = "(Fortsetzung – die früheren Schritte stehen zusammengefasst im System-Prompt.)"


def estimate_tokens(messages) -> int:
    chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
    return int(chars / 3.3) + 1


def budget(n_ctx, max_tokens) -> int:
    reserve = max_tokens if max_tokens else max(512, n_ctx // 4)
    return n_ctx - reserve


def _clean(history, keep_full_tools, old_tool_chars):
    msgs = [{k: v for k, v in m.items() if k in ALLOWED_KEYS} for m in history]
    tool_idx = [i for i, m in enumerate(msgs) if m["role"] == "tool"]
    old = tool_idx[:-keep_full_tools] if keep_full_tools > 0 else tool_idx
    for i in old:
        c = msgs[i].get("content") or ""
        if len(c) > old_tool_chars:
            msgs[i]["content"] = c[:old_tool_chars] + f"\n[... gekürzt, ursprünglich {len(c)} Zeichen]"
    return msgs


def system_with_summary(system, summary):
    if not summary:
        return system
    return (system + "\n\n" if system else "") + \
        "## Bisheriger Verlauf (zusammengefasst, ältere Nachrichten sind ausgeblendet)\n" + summary


def build_messages(system, history, n_ctx, max_tokens, keep_full_tools=2, old_tool_chars=300,
                   summary="", start=0):
    """Gibt (messages_für_api, anzahl_verworfener_nachrichten) zurück."""
    msgs = _clean(history[start:], keep_full_tools, old_tool_chars)
    if msgs and msgs[0]["role"] != "user":
        # Viele Chat-Vorlagen verlangen, dass nach dem System-Prompt eine Nutzer-Nachricht kommt
        msgs.insert(0, {"role": "user", "content": CONTINUE_NOTE})
    sysm = [{"role": "system", "content": system_with_summary(system, summary)}] if (system or summary) else []
    dropped = 0
    if n_ctx:
        b = budget(n_ctx, max_tokens)
        # Notbremse: ganze Runden ab der ältesten Nutzer-Nachricht verwerfen, die letzte Runde bleibt immer
        while estimate_tokens(sysm + msgs) > b:
            user_positions = [i for i, m in enumerate(msgs) if m["role"] == "user"]
            if len(user_positions) < 2:
                break
            cut = user_positions[1]
            msgs = msgs[cut:]
            dropped += cut
    return sysm + msgs, dropped


def pick_cut(history, start, keep_tokens):
    """Wählt die Stelle, ab der der Verlauf wörtlich bleibt (die letzten ~keep_tokens).

    Geschnitten wird nur vor einer Nutzer- oder Assistenten-Nachricht, nie vor einer Werkzeug-Antwort
    (die gehört zu ihrem Aufruf). Gibt None zurück, wenn es nichts Sinnvolles zu kürzen gibt.
    """
    acc, cut = 0, None
    for i in range(len(history) - 1, start, -1):
        acc += estimate_tokens([history[i]])
        if history[i]["role"] in ("user", "assistant"):
            cut = i
        if acc >= keep_tokens and cut is not None:
            break
    if cut is None or cut <= start + 1:
        return None
    return cut


def transcript(history, start, end, tool_chars=1500, total_chars=200_000):
    """Lesbarer Text der Nachrichten [start, end) für die Zusammenfassung."""
    parts = []
    for m in history[start:end]:
        role = m["role"]
        if role == "tool":
            c = m.get("content") or ""
            if len(c) > tool_chars:
                c = c[:tool_chars] + f" [… {len(c) - tool_chars} Zeichen gekürzt]"
            parts.append(f"[Werkzeug-Ergebnis {m.get('name', '')}]\n{c}")
        elif role == "assistant":
            line = m.get("content") or ""
            for tc in m.get("tool_calls") or []:
                f = tc.get("function") or {}
                args = f.get("arguments") or ""
                line += f"\n→ ruft {f.get('name')}({args[:600]}{'…' if len(args) > 600 else ''})"
            parts.append(f"[Assistent]\n{line.strip()}")
        else:
            parts.append(f"[Nutzer]\n{m.get('content') or ''}")
    text = "\n\n".join(parts)
    if len(text) > total_chars:       # Anfang und Ende behalten
        half = total_chars // 2
        text = text[:half] + "\n\n[… Mitte ausgelassen …]\n\n" + text[-half:]
    return text


SUMMARY_SYSTEM = (
    "Du fasst einen Gesprächs- und Arbeitsverlauf zwischen Nutzer und KI-Assistent zusammen, damit der "
    "Assistent ohne den Originalverlauf nahtlos weiterarbeiten kann. Schreibe auf Deutsch, knapp, in Stichpunkten. "
    "Behalte unbedingt: das Ziel und die aktuelle Aufgabe des Nutzers (möglichst wörtlich), getroffene "
    "Entscheidungen, Vorlieben des Nutzers, genaue Dateipfade, Befehle, URLs, Namen, Zahlen und Fehlermeldungen, "
    "was schon erledigt ist und was noch offen ist. Lasse weg: Höflichkeiten, Wiederholungen, lange "
    "Werkzeug-Ausgaben (nur das Ergebnis nennen). Keine Einleitung, nur die Zusammenfassung."
)


def summary_request(prev_summary, history, start, end, last_user_text, max_chars):
    body = ""
    if prev_summary:
        body += f"Bisherige Zusammenfassung (fortschreiben):\n{prev_summary}\n\n"
    body += f"Neuer Verlauf:\n{transcript(history, start, end, total_chars=max_chars)}"
    if last_user_text:
        body += f"\n\nDie aktuelle Aufgabe des Nutzers lautet: {last_user_text[:2000]}"
    return [{"role": "system", "content": SUMMARY_SYSTEM}, {"role": "user", "content": body}]
