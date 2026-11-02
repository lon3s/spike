"""Kontext sparsam halten: alte Werkzeug-Ausgaben kürzen, älteste Runden verwerfen."""
import json

ALLOWED_KEYS = {"role", "content", "tool_calls", "tool_call_id", "name"}


def estimate_tokens(messages) -> int:
    chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
    return int(chars / 3.3) + 1


def build_messages(system, history, n_ctx, max_tokens, keep_full_tools=2, old_tool_chars=300):
    """Gibt (messages_für_api, anzahl_verworfener_nachrichten) zurück."""
    msgs = [{k: v for k, v in m.items() if k in ALLOWED_KEYS} for m in history]

    # Ältere Tool-Ergebnisse auf eine kurze Zusammenfassung stutzen
    tool_idx = [i for i, m in enumerate(msgs) if m["role"] == "tool"]
    old = tool_idx[:-keep_full_tools] if keep_full_tools > 0 else tool_idx
    for i in old:
        c = msgs[i].get("content") or ""
        if len(c) > old_tool_chars:
            msgs[i]["content"] = c[:old_tool_chars] + f"\n[... gekürzt, ursprünglich {len(c)} Zeichen]"

    sysm = [{"role": "system", "content": system}] if system else []
    dropped = 0
    if n_ctx:
        reserve = max_tokens if max_tokens else max(512, n_ctx // 4)
        budget = n_ctx - reserve
        # Ganze Runden ab der ältesten Nutzer-Nachricht verwerfen, die letzte Runde bleibt immer
        while estimate_tokens(sysm + msgs) > budget:
            user_positions = [i for i, m in enumerate(msgs) if m["role"] == "user"]
            if len(user_positions) < 2:
                break
            cut = user_positions[1]
            msgs = msgs[cut:]
            dropped += cut
    return sysm + msgs, dropped
