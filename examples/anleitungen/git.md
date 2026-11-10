Mein Git-Server ist ein Forgejo unter http://git.example.lan:3000, Benutzer „alex“. Die Anmeldung ist gespeichert.

Was meine Worte bedeuten:
- „sichere es“ / „push es lokal“ = committen und zum Remote „lokal“ (Forgejo) pushen.
- „auf GitHub“ = zum Remote „origin“ pushen. Nur wenn ich das ausdrücklich sage.
- Sonst nichts committen oder pushen, nur anbieten.

Neues Projekt sichern:
1. git status – ist es schon ein Repo? Sonst git init.
2. .gitignore prüfen (.venv/, __pycache__/, node_modules/, *.log).
3. Remote „lokal“ eintragen: http://git.example.lan:3000/alex/<ordnername>.git
   (Forgejo legt das Repo beim ersten Push selbst an, wenn „push to create“ eingeschaltet ist.)
4. Commit, dann Push.

Private Projekte nur auf den eigenen Server, nie auf GitHub.
