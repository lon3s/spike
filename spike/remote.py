"""Andere Rechner per SSH: Befehle ausführen, Dateien kopieren, Hintergrundaufträge.

Rechner stehen in settings["ssh_hosts"]: [{"name", "target" (user@host[:port]), "mode": "voll"|"lesen", "note"}].
- Anmeldung nur per SSH-Schlüssel (BatchMode) – nie ein Passwort-Prompt im Hintergrund.
- Eine Verbindung pro Rechner bleibt 5 min offen (ControlMaster) → Folgebefehle starten sofort.
- „lesen“: nur Befehle, die nichts ändern (Allowlist), laufen ohne Rückfrage; alles andere ist gesperrt.
- sudo: klappt ohne Passwort (NOPASSWD) direkt; sonst fragt spike einmal pro Rechner und Sitzung nach dem
  Passwort (nur RAM) und reicht es über die SSH-Verbindung (stdin) an „sudo -S“ weiter – nie über die Befehlszeile.
"""
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path

CWD_MARK = "\x1eSPIKE_RCWD:"
_PW: dict[str, str] = {}        # Rechner-Name → sudo-Passwort (nur RAM, gilt bis spike endet)

# Diese Programme ändern (ohne die Prüfungen unten) nichts – erlaubt auf „lesen“-Rechnern
READ_PROGS = {
    "cat", "ls", "ll", "head", "tail", "less", "more", "grep", "egrep", "zgrep", "rg", "wc", "sort", "uniq", "cut",
    "awk", "sed", "tr", "column", "jq", "stat", "file", "du", "df", "free", "uptime", "uname", "hostname",
    "hostnamectl", "whoami", "id", "date", "ps", "pgrep", "top", "pstree", "lsblk", "blkid", "findmnt", "mount",
    "lscpu", "lsusb", "lspci", "lsmod", "ip", "ss", "netstat", "ping", "dig", "nslookup", "host", "curl",
    "journalctl", "dmesg", "find", "which", "whereis", "type", "env", "printenv", "echo", "printf", "true",
    "test", "[", "readlink", "realpath", "basename", "dirname", "md5sum", "sha256sum", "diff", "cmp", "tree",
    "sensors", "vcgencmd", "iwconfig", "iw", "nmcli", "timedatectl", "loginctl", "last", "w", "who", "groups",
    "getent", "pwd", "cd", "nproc", "vmstat", "iostat", "zcat", "xxd", "hexdump", "strings", "smartctl",
    "docker", "podman", "systemctl", "apt", "apt-cache", "dpkg", "pacman", "git", "ha", "crontab", "sqlite3",
}
# Unterbefehle, die bei diesen Programmen erlaubt sind (alles andere ändert etwas)
READ_SUB = {
    "systemctl": {"status", "is-active", "is-enabled", "is-failed", "list-units", "list-unit-files", "list-timers",
                  "show", "cat", "--failed"},
    "docker": {"ps", "logs", "inspect", "images", "stats", "top", "port", "version", "info", "compose"},
    "podman": {"ps", "logs", "inspect", "images", "stats", "top", "port", "version", "info"},
    "apt": {"list", "show", "policy", "search"},
    "apt-cache": {"show", "policy", "search", "depends", "rdepends"},
    "dpkg": {"-l", "-L", "-s", "--list", "--listfiles", "--status", "-S"},
    "pacman": {"-Q", "-Qi", "-Ql", "-Qs", "-Ss", "-Si", "-Qu"},
    "git": {"status", "log", "diff", "show", "branch", "remote", "rev-parse", "describe", "blame"},
    "ha": {"info", "core", "supervisor", "os", "host", "addons", "network", "resolution"},
    "crontab": {"-l"},
}
_BAD = re.compile(r"(^|[^0-9&])>(?!\s*/dev/null)|>>|\btee\b|`|\$\(|\brm\b|\bmv\b|\bcp\b|\bdd\b|\bchmod\b|\bchown\b|"
                  r"\bkill\b|\breboot\b|\bshutdown\b|\bpoweroff\b|\bmkfs|\btruncate\b|\binstall\b|\bwget\b|system\(")
# Diese Wörter machen aus einem Lese-Programm (systemctl, docker, ha …) einen ändernden Befehl
_ACTION_WORDS = {"restart", "stop", "start", "update", "upgrade", "rebuild", "reload", "install", "uninstall",
                 "remove", "reboot", "shutdown", "prune", "rm", "kill", "enable", "disable", "set", "delete",
                 "exec", "run", "push", "pull", "commit", "reset", "checkout", "mask", "unmask", "edit"}


def _strip_sudo(words):
    while words and words[0] in ("sudo", "env", "nice", "ionice", "timeout", "command", "LC_ALL=C"):
        words = words[1:]
        while words and words[0].startswith("-") and words[0] not in ("-l",):    # Optionen von sudo/timeout …
            words = words[1:]
        if words and re.fullmatch(r"\d+[smh]?", words[0]):                      # timeout 10
            words = words[1:]
    return words


def is_readonly(command: str) -> bool:
    """Vorsichtige Prüfung: ändert dieser Befehl (wahrscheinlich) nichts? Im Zweifel False."""
    cmd = command.strip()
    if not cmd or _BAD.search(cmd.replace("2>&1", "").replace("2>/dev/null", "")):
        return False
    for seg in re.split(r"\|\||&&|[|;\n]", cmd):
        try:
            words = shlex.split(seg)
        except ValueError:
            return False
        words = _strip_sudo([w for w in words if not re.fullmatch(r"[A-Z_][A-Z0-9_]*=.*", w)])
        if not words:
            continue
        prog = os.path.basename(words[0])
        if prog not in READ_PROGS:
            return False
        if prog == "sed" and any(w.startswith("-i") or w == "--in-place" for w in words):
            return False
        if prog == "find" and any(w in ("-delete", "-exec", "-execdir", "-ok", "-fprint") for w in words):
            return False
        if prog == "curl" and any(w in ("-X", "--request", "-d", "--data", "-F", "--form", "-T", "-o", "-O",
                                        "--output", "--upload-file") or w.startswith("--data") for w in words):
            return False
        if prog == "journalctl" and any(w.startswith("--vacuum") or w in ("--rotate", "--flush") for w in words):
            return False
        if prog == "docker" and len(words) > 2 and words[1] == "compose" and words[2] not in ("ps", "logs", "config", "ls"):
            return False
        if prog in READ_SUB and any(w in _ACTION_WORDS for w in words[1:]):
            return False
        if prog in READ_SUB:
            sub = next((w for w in words[1:] if not (w.startswith("--") and "=" in w)), "")
            if sub not in READ_SUB[prog] and not (prog == "pacman" and re.fullmatch(r"-Q[a-z]*", sub or "")):
                return False
    return True


# ---------- Rechnerliste ----------

def hosts(settings) -> list[dict]:
    return [h for h in (settings.get("ssh_hosts") or []) if h.get("name") and h.get("target")]


def find(settings, name: str) -> dict | None:
    name = (name or "").strip().lower()
    for h in hosts(settings):
        tgt = h["target"].lower()
        if name in (h["name"].lower(), tgt, tgt.split("@")[-1].split(":")[0]):
            return h
    return None


def describe(settings) -> str:
    """Kurzliste für den System-Prompt."""
    rows = []
    for h in hosts(settings):
        mode = "nur lesen" if h.get("mode") == "lesen" else "voll"
        note = f" – {h['note']}" if h.get("note") else ""
        rows.append(f"- {h['name']} ({h['target']}, {mode}){note}")
    return "\n".join(rows)


def _split_target(target: str):
    """user@host:port → (user@host, port|None)."""
    m = re.fullmatch(r"(.+?):(\d+)", target.strip())
    return (m.group(1), m.group(2)) if m else (target.strip(), None)


def _control_dir() -> str:
    """Ordner für die SSH-Steuer-Sockets (Unix-Sockets dürfen höchstens ~104 Zeichen lang sein)."""
    run = os.environ.get("XDG_RUNTIME_DIR") or ""
    if run and len(run) + 50 < 100 and os.path.isdir(run):
        return run
    d = os.path.join(tempfile.gettempdir(), f"spike-ssh-{os.getuid()}")
    os.makedirs(d, mode=0o700, exist_ok=True)
    if len(d) + 50 >= 100:
        d = f"/tmp/spike-ssh-{os.getuid()}"
        os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def ssh_opts(target: str, persist=True) -> list[str]:
    run = _control_dir()
    dest, port = _split_target(target)
    o = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new",
         "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]
    if persist:
        o += ["-o", "ControlMaster=auto", "-o", f"ControlPath={run}/spike-ssh-%C", "-o", "ControlPersist=300"]
    else:
        o += ["-o", "ControlPath=none"]
    if port:
        o += ["-p", port]
    return o + [dest]


def remote_script(command: str, cwd: str = "", mark=True) -> str:
    """Skript, das auf dem anderen Rechner läuft: Passwortzeile von stdin lesen, sudo-Hülle, Ordner merken."""
    pre = ("IFS= read -r __spw || __spw=''\n"
           "sudo() { if [ -n \"$__spw\" ]; then printf '%s\\n' \"$__spw\" | command sudo -S -p '' \"$@\"; "
           "else command sudo -n \"$@\"; fi; }\n")
    if cwd:
        pre += f"cd {shlex.quote(cwd)} 2>/dev/null\n"
    post = f"\n__rc=$?\nprintf '{CWD_MARK}%s\\n' \"$PWD\"\nexit $__rc" if mark else ""
    return pre + command + post


def remote_argv(target: str, command: str, cwd: str = "", persist=True, mark=True) -> list[str]:
    q = shlex.quote(remote_script(command, cwd, mark))
    # bash, wenn vorhanden (Modelle schreiben bash), sonst sh
    wrapper = f"if command -v bash >/dev/null 2>&1; then exec bash -c {q}; else exec sh -c {q}; fi"
    return ["ssh", *ssh_opts(target, persist), wrapper]


_NEED_PW = re.compile(r"sudo: .*(password is required|Passwort ist notwendig|Passwort erforderlich|"
                      r"terminal is required|Terminal ist erforderlich)", re.I)


def needs_password(output: str) -> bool:
    return bool(_NEED_PW.search(output or ""))


def password(name) -> str:
    return _PW.get(name, "")


def check_password(target: str, pw: str) -> bool:
    try:
        r = subprocess.run(["ssh", *ssh_opts(target), "sudo -S -k -v -p ''"], input=pw + "\n", text=True,
                           capture_output=True, timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def ask_password(host: dict, askpass: str) -> bool:
    """Fragt das sudo-Passwort für diesen Rechner (kdialog/zenity), prüft und merkt es. True = gemerkt."""
    if not askpass:
        return False
    env = {k: v for k, v in os.environ.items() if not k.startswith("SPIKE_PW")}
    for _ in range(2):
        try:
            r = subprocess.run([askpass, f"sudo-Passwort für {host['name']} ({host['target']}):"],
                               capture_output=True, text=True, timeout=300, env=env)
        except Exception:
            return False
        pw = r.stdout.rstrip("\n")
        if r.returncode != 0 or not pw:
            return False
        if check_password(host["target"], pw):
            _PW[host["name"]] = pw
            return True
    return False


def scp_argv(target: str, src: str, dst: str, upload: bool) -> list[str]:
    opts = ssh_opts(target)
    dest = opts.pop()                     # user@host steht zuletzt
    opts = ["-P" if o == "-p" else o for o in opts]
    remote = lambda p: f"{dest}:{p}"
    return ["scp", "-r", "-p", *opts, *( [src, remote(dst)] if upload else [remote(src), dst] )]


def test(target: str) -> str:
    """Für den Einstellungs-Dialog: Verbindung + sudo prüfen."""
    probe = ("echo \"$(id -un)@$(hostname) · $(. /etc/os-release 2>/dev/null && echo \"$PRETTY_NAME\" || uname -sr)\"; "
             "if sudo -n true 2>/dev/null; then echo 'sudo: ohne Passwort'; "
             "elif command -v sudo >/dev/null; then echo 'sudo: mit Passwort (spike fragt einmal)'; "
             "else echo 'sudo: nicht installiert'; fi")
    try:
        r = subprocess.run(["ssh", *ssh_opts(target, persist=False), probe], capture_output=True, text=True,
                           timeout=15, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return "✗ keine Antwort (Zeitüberschreitung)"
    if r.returncode == 0:
        return "✓ " + " · ".join(r.stdout.split("\n")).strip(" ·")
    err = (r.stderr or "").strip().splitlines()
    msg = err[-1] if err else f"exit {r.returncode}"
    if "Permission denied" in msg:
        dest = _split_target(target)[0]
        msg += f"\nSchlüssel fehlt – einmal im Terminal: ssh-copy-id {dest}"
    return "✗ " + msg
