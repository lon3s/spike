from spike.plugins import tool


@tool("Aktuelles Wetter und kurze Vorhersage für einen Ort.",
      params={"ort": "Stadt oder Ort, z. B. 'Berlin'"}, label="Wetter")
def wetter(ort: str, ctx) -> str:
    return ctx.http_get(f"https://wttr.in/{ort}?format=%l:+%c+%t+(gefühlt+%f),+Wind+%w,+Regen+%p&lang=de",
                         headers={"User-Agent": "curl/8"})
