# Bot-Connector: den WhatsApp-Bot über Claude ändern

Ein Remote-MCP-Server (Connector für claude.ai und Claude Code). Wer verbunden ist, kann mit Claude
den kompletten Bot bearbeiten: Code lesen und ändern, neue Endpoints bauen, Variablen setzen, deployen,
Logs lesen und Endpoints testen.

```
Claude ──HTTPS + OAuth──> Connector ──┬──> GitHub-API   (Code: jede Änderung = Commit)
                                      ├──> Coolify-API  (Deploy, Status, Logs, Variablen)
                                      └──> Bot-URL      (Endpoints testen)
```

- **Verlauf und Rücknahme:** Jede Änderung ist ein Commit auf `main` mit dem Namen der Person
  („Über Bot-Connector · Name“). Mit `aenderung_zuruecknehmen` lässt sich jede Änderung rückgängig machen.
- **Erst prüfen, dann live:** Änderungen landen sofort im Repo, live gehen sie erst mit `deployen`.
  Python-, YAML- und JSON-Dateien werden vor dem Speichern und vor dem Deploy auf Syntaxfehler geprüft.
- **Eigene Coolify-Ressource:** Der Connector läuft getrennt vom Bot. Auch wenn ein Deploy den Bot
  kaputtmacht, bleibt der Connector erreichbar, und man kann damit reparieren.
- **Anmeldung:** Beim Verbinden öffnet sich eine Login-Seite (Name + Connector-Passwort).

| Datei | Zweck |
|---|---|
| `server.py` | MCP-Server mit allen Tools |
| `oauth.py` | Anmeldung (OAuth 2.1, Login-Seite, Tokens in SQLite) |
| `backends.py` | Clients für GitHub, Coolify und den Bot |
| `docker-compose.yml` | Für Coolify (eigene Ressource) |
| `.env.example` | Alle Variablen, kommentiert |

---

## Teil A: Einrichtung (Paul, einmalig, ca. 20 Minuten)

### A1. GitHub-Token erstellen

1. GitHub → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**
2. **Token name:** `Bot-Connector` · **Expiration:** 1 Jahr. Trag dir das Ablaufdatum in den Kalender ein.
3. **Repository access:** *Only select repositories* → `paulalb72/whatsapp-termin-webhook`
4. **Permissions → Repository permissions → Contents:** `Read and write`
   (`Metadata: Read-only` setzt GitHub automatisch, sonst nichts anhaken.)
5. **Generate token** → Token (`github_pat_…`) kopieren.

### A2. Coolify-API freischalten und Token erstellen

1. Coolify → **Settings → Advanced** → **API Access** aktivieren und speichern.
   Ist dort „Allowed IPs“ gesetzt, die IP deines Servers ergänzen oder das Feld leer lassen.
2. Coolify → **Keys & Tokens → API Tokens** → neuer Token:
   - Name: `Bot-Connector`
   - Rechte: `read`, `read:sensitive`, `write`, `deploy` (**nicht** `root`)
   - Token kopieren (wird nur einmal angezeigt, Form `1|abc…`).
3. **UUID der Bot-Ressource:** Bot-Ressource in Coolify öffnen. Die Adresse im Browser endet auf
   `…/application/<UUID>`. Diesen letzten Teil kopieren.

> Coolify-Tokens gelten für das ganze Team. Der Connector nutzt den Token nur für die Bot-Ressource:
> Die UUID steht fest in seiner Konfiguration und lässt sich über die Tools nicht ändern.

### A3. Domain

A-Record, z. B. `connector.paulalberts.de` → IP des Servers. Entfällt, wenn ein Wildcard-Record auf den
Server zeigt.

### A4. Code pushen

Der Ordner `connector/` muss auf GitHub liegen (`git push`), weil Coolify direkt aus dem Repo baut.

### A5. Connector in Coolify anlegen

1. Im Projekt des Bots: **+ New → Public Repository** → Repo-URL dieses Repos, Branch `main`.
   - Build Pack: **Docker Compose**
   - Base Directory: **`/connector`**
   - Docker Compose Location: **`/docker-compose.yml`**
2. **Environment Variables:**

   | Variable | Wert |
   |---|---|
   | `PUBLIC_URL` | `https://connector.paulalberts.de` (ohne `/mcp`, ohne `/` am Ende) |
   | `CONNECTOR_PASSWORD` | neues Passwort, z. B. `openssl rand -hex 16` |
   | `GITHUB_TOKEN` | Token aus A1 |
   | `BOT_COOLIFY_URL` | Adresse, unter der du Coolify im Browser öffnest, z. B. `https://coolify.paulalberts.de` |
   | `BOT_COOLIFY_TOKEN` | Token aus A2 |
   | `BOT_COOLIFY_UUID` | UUID aus A2 |
   | `BOT_URL` | `https://whasup.paulalberts.de` |
   | `BOT_TOKEN` | derselbe Wert wie `WEBHOOK_TOKEN` beim Bot |

   `GITHUB_REPO`, `GITHUB_BRANCH` und `TIMEZONE` haben passende Defaults.
3. **Domains** (Configuration → Services): beim Service `connector` `https://connector.paulalberts.de:8000`
   eintragen. `:8000` ist der interne Port und wird nicht öffentlich geöffnet.
4. **Deploy** und prüfen:
   ```bash
   curl https://connector.paulalberts.de/health   # -> {"status":"ok"}
   ```
   In den Logs der Ressource steht `Connector bereit: https://connector.paulalberts.de/mcp`.
   Bei einem Konfigurationsfehler beendet sich der Container und nennt im Log die fehlende Variable.

### A6. Zugang weitergeben

Dem Kollegen geben:
- **Connector-URL:** `https://connector.paulalberts.de/mcp`
- **Passwort:** `CONNECTOR_PASSWORD`, am besten über einen anderen Kanal als die URL

---

## Teil B: Connector verbinden (Kollege, 5 Minuten)

### In claude.ai (Browser oder Desktop-App)

1. **Einstellungen → Connectors → Benutzerdefinierten Connector hinzufügen** (engl. *Add custom connector*).
   Im Team- oder Enterprise-Plan darf das nur ein Owner. Dann fügt Paul ihn unter
   *Organisationseinstellungen → Connectors* hinzu, und du klickst danach nur noch auf **Verbinden**.
2. **Name:** `WhatsApp-Bot` · **URL:** `https://connector.paulalberts.de/mcp`.
   Die erweiterten Einstellungen (OAuth Client ID/Secret) bleiben leer.
3. **Hinzufügen → Verbinden.** Es öffnet sich die Login-Seite des Connectors:
   deinen **Namen** und das **Connector-Passwort** eingeben → **Zugriff erlauben**.
   Danach springt der Browser zurück zu Claude, und der Connector ist verbunden.
   Dein Name steht später bei jeder Änderung im Verlauf.
4. Im Chat über **+ → Connectors** (bzw. das Tools-Menü) den Connector *WhatsApp-Bot* einschalten.
   Claude fragt vor jedem Tool um Erlaubnis. Für lesende Tools kannst du „Immer erlauben“ wählen.

Auf dem Handy (Claude-App) ist der Connector danach ebenfalls verfügbar.

### Alternativ in Claude Code

```bash
claude mcp add --transport http whatsapp-bot https://connector.paulalberts.de/mcp
```

Dann in Claude Code `/mcp` → `whatsapp-bot` → **Authenticate**. Im Browser öffnet sich dieselbe
Login-Seite.

---

## Teil C: Arbeiten mit dem Connector

Einfach in normaler Sprache sagen, was passieren soll. Claude liest zuerst den Code, ändert ihn, sagt,
was live geht, deployt und testet. Beispiele:

- „Gib mir einen Überblick über den Bot.“
- „Die Tagesbilanz soll um 19 Uhr kommen.“
- „Bau einen Endpoint `GET /stats`, der die Termine dieser Woche pro Person als JSON liefert, geschützt
  mit dem Webhook-Token. Danach deployen und testen.“
- „Ändere den Text der Termin-Nachricht: statt ‚Stark, weiter so!‘ soll ‚Mega! 🚀‘ dastehen.“
- „Warum kam gestern keine Tagesbilanz? Schau in die Logs.“
- „Nimm die letzte Änderung zurück und deploye.“

**Ablauf jeder Änderung:** ändern (Commit, noch nicht live) → `deployen` (1–3 Minuten) → `deploy_status`
→ testen mit `bot_aufrufen` und `logs`. Bei einem Fehler im Build läuft der alte Stand weiter.

### Tools

| Bereich | Tool | Was es tut |
|---|---|---|
| Überblick | `uebersicht` | Bot-Status, letzter Deploy, nicht deployte Änderungen, Dateien |
| Code lesen | `dateien_liste`, `datei_lesen`, `suchen` | Dateien auflisten, lesen, durchsuchen |
| Code ändern | `datei_ersetzen` | Textabschnitt gezielt ersetzen (Standard für Änderungen) |
| | `datei_schreiben` | Datei neu anlegen oder komplett ersetzen |
| | `datei_verschieben`, `datei_loeschen` | Umbenennen, löschen |
| Verlauf | `aenderungen_liste`, `aenderung_anzeigen` | Letzte Commits, Diff eines Commits |
| | `aenderung_zuruecknehmen` | Commit rückgängig machen (als neuer Commit) |
| Betrieb | `deployen`, `deploy_status` | Live schalten, Build-Status und Build-Log |
| | `logs` | Container-Logs von `webhook` oder `waha` |
| | `variablen_liste`, `variable_setzen`, `variable_loeschen` | Coolify-Variablen (geheime Werte maskiert) |
| Test | `bot_aufrufen` | HTTP-Anfrage an den Bot, auf Wunsch mit Webhook-Token |

### Gut zu wissen

- **Neue Einstellung oder neues Geheimnis** (z. B. ein API-Key): Nie in eine Datei schreiben, das Repo ist
  öffentlich. Stattdessen `variable_setzen`, die Variable in `docker-compose.yml` unter
  `services.webhook.environment` eintragen und im Code mit `os.environ` lesen. Claude kennt diesen Ablauf.
- **Echte Nachrichten:** `POST /webhook/termin` und `/report/daily|weekly` ohne `?preview=1` schreiben
  wirklich in die WhatsApp-Gruppe und zählen in der Statistik.
- **Paul lokal:** Vor eigenen Änderungen erst `git pull`, weil der Connector direkt auf GitHub committet.

---

## Teil D: Sicherheit und Verwaltung

**Schutzmechanismen im Connector**
- Login mit Passwort. Nach 5 Fehlversuchen pro IP ist der Login 15 Minuten gesperrt. Gespeichert werden
  nur Hashes der Tokens.
- `connector/` und `.github/` sind über die Tools nur lesbar. Der Connector kann sich also nicht selbst
  verändern oder abschalten.
- Die Variablen `WEBHOOK_TOKEN`, `WAHA_API_KEY`, `WAHA_DASHBOARD_PASSWORD` und `WHATSAPP_HOOK_HMAC_KEY`
  kann man über den Connector weder ändern noch lesen. Geheime Werte werden überall maskiert.
- Inhalte mit Tokens oder Schlüsseln (GitHub, API-Keys, private Keys, die echten Tokens) werden nicht gespeichert.
- Syntaxprüfung vor jedem Commit und vor jedem Deploy.

**Was das nicht verhindert:** Wer verbunden ist, kann beliebigen Code in den Bot deployen. Dieser Code kann
z. B. Variablen auslesen oder Nachrichten an andere Chats schicken. Gib den Zugang darum nur Personen,
denen du den Bot auch direkt anvertrauen würdest. Jede Änderung steht mit Namen im GitHub-Verlauf.

**Zugang entziehen:** In Coolify bei der Connector-Ressource `CONNECTOR_PASSWORD` ändern → **Redeploy**.
Ab dann sind alle Anmeldungen ungültig. Wer weiter arbeiten soll, verbindet sich mit dem neuen Passwort.

**Tokens erneuern:** Läuft der GitHub-Token ab, melden die Tools „Token ungültig oder abgelaufen“. Dann
in A1 einen neuen erstellen, in Coolify bei `GITHUB_TOKEN` eintragen → **Redeploy**. Beim Coolify-Token
gilt das Gleiche. Ändert sich `WEBHOOK_TOKEN` des Bots, auch `BOT_TOKEN` hier anpassen.

**Connector selbst ändern:** nur direkt im Repo (`connector/`), danach Redeploy der Connector-Ressource.
Ein Deploy des Bots berührt den Connector nicht.

---

## Teil E: Fehlerbehebung

| Problem | Lösung |
|---|---|
| Claude kann sich nicht verbinden | `https://…/health` im Browser prüfen. `PUBLIC_URL` muss exakt der Domain entsprechen (https, ohne `/mcp`). Domain in Coolify mit `:8000`. In Claude die URL **mit** `/mcp` eintragen. |
| Login-Seite: „Link abgelaufen“ | Der Link gilt 15 Minuten und nur einmal. In Claude erneut auf **Verbinden** klicken. |
| Login: „Zu viele Fehlversuche“ | 15 Minuten warten oder die Connector-Ressource neu starten. |
| Nach einem Passwortwechsel kommt „nicht autorisiert“ | In Claude beim Connector **Trennen → Verbinden** und mit dem neuen Passwort anmelden. |
| `Coolify: Zugriff verweigert` | API Access in Coolify aktiv? Token-Rechte `read`, `read:sensitive`, `write`, `deploy`? Allowed IPs? |
| `Coolify nicht erreichbar` | `BOT_COOLIFY_URL` muss vom Server aus erreichbar sein. Die normale Coolify-Domain funktioniert in der Regel. |
| `logs` zeigt den falschen Container | Ältere Coolify-Versionen kennen den Parameter `service_name` nicht. Coolify aktualisieren oder die Logs in der Coolify-Oberfläche ansehen. |
| Deploy `failed` | `deploy_status` zeigt das Ende des Build-Logs. Der vorherige Stand läuft weiter. Fehler beheben oder `aenderung_zuruecknehmen` und erneut `deployen`. |
| Bot startet nach dem Deploy nicht | `logs` (Dienst `webhook`) zeigt den Fehler, z. B. `Konfigurationsfehler: …`. Korrigieren oder zurücknehmen und erneut deployen. |
