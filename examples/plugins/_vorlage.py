# VORLAGE – Dateien mit "_" am Anfang werden ignoriert.
# Kopieren, z. B. nach "mein_tool.py", anpassen und in spike unter
# ⚙ Einstellungen → Allgemein → "Plugins neu laden" klicken. Danach im Profil anhaken.

from spike.plugins import tool


@tool(
    "Kurze Beschreibung für das Modell: WAS macht das Werkzeug und WANN soll es benutzt werden.",
    params={"text": "Beschreibung des Parameters", "anzahl": "optional, Standard 1"},
    label="Mein Werkzeug",      # Name in den Einstellungen
    confirm=False,              # True = vor jedem Aufruf nachfragen
)
def mein_tool(text: str, anzahl: int = 1, ctx=None) -> str:
    # ctx.http_get(url, headers={...}) → Inhalt einer URL als Text (HTML/JSON)
    # ctx.html_to_text(html)  → HTML zu lesbarem Text
    # ctx.run("befehl")       → Shell-Befehl, gibt Ausgabe zurück
    # ctx.confirm("Frage?")   → fragt den Nutzer (True/False)
    # Rückgabe: Text (oder dict/list → wird zu JSON). Lange Ausgaben werden automatisch gekürzt.
    return (text + " ") * anzahl
