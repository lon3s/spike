from spike.plugins import tool


@tool("Zeigt Systemzustand dieses PCs: CPU-Last, RAM, Speicherplatz und – falls vorhanden – GPU/VRAM.",
      label="System-Info")
def system_info(ctx) -> str:
    teile = [
        "== Last ==\n" + ctx.run("uptime"),
        "== RAM ==\n" + ctx.run("free -h"),
        "== Platten ==\n" + ctx.run("df -h -x tmpfs -x devtmpfs -x efivarfs | head -15"),
    ]
    gpu = ctx.run("nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu "
                  "--format=csv 2>/dev/null || rocm-smi --showuse --showmemuse 2>/dev/null")
    if gpu and "keine Ausgabe" not in gpu:
        teile.append("== GPU ==\n" + gpu)
    return "\n\n".join(teile)
