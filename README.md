# Termin-Webhook → WhatsApp-Gruppe (Coolify)

Akquise-Tool → `POST /webhook/termin` → WAHA → WhatsApp-Gruppe:

```
🎉 *NEUER TERMIN!* 🎉

👤 *{wer}* hat gerade einen Termin mit
🏢 *{unternehmen}* gelegt.

🔥 Stark, weiter so! 💪
```

Anpassbar über `MESSAGE_TEMPLATE` (`\n` = Zeilenumbruch, `*fett*`, `_kursiv_`).

```
Internet ──HTTPS──> Coolify-Proxy ──> webhook ──(internes Docker-Netz)──> waha ──> WhatsApp
```

| Datei | Zweck |
|---|---|
| `webhook/main.py` | Webhook-Dienst (FastAPI) |
| `webhook/list_groups.py` | Listet Gruppen mit Name und ID |
| `docker-compose.yml` | webhook + waha, für Coolify |
| `.env.example` | Alle Variablen, kommentiert |

Antworten des Webhooks: `200 {"status":"sent"}`, `200 {"status":"duplicate"}`, `400` (Feld fehlt / kein JSON), `401` (Token), `502` (WAHA nach 3 Versuchen nicht erfolgreich), `503` (`WAHA_GROUP_ID` nicht gesetzt).

---

## 1. Deployment mit Coolify

1. **DNS:** A-Record, z. B. `termine.[MEINE-DOMAIN]` → IP der Hetzner-Instanz. Entfällt, wenn schon ein Wildcard-Record auf den Server zeigt.
2. Das Repo ist öffentlich, es enthält keine Geheimnisse. Alle Tokens kommen ausschließlich in die Coolify-Variablen.
3. **In Coolify:** Projekt → **+ New → Public Repository** → Repo-URL dieses Repos einfügen, Branch `main`.
   - Build Pack: **Docker Compose**
   - Docker Compose Location: `/docker-compose.yml`
4. **Environment Variables** (Tab der Ressource) ausfüllen. Werte z. B. auf dem Server mit `openssl rand -hex 32` erzeugen.
   - `WEBHOOK_TOKEN`, `WAHA_API_KEY`, `WAHA_DASHBOARD_PASSWORD`: Pflicht
   - `WAHA_GROUP_ID`: vorerst leer lassen
   - Rest: Defaults passen (Erklärungen in `.env.example`)
5. **Domains** (unter Configuration → Services):
   - `webhook`: `https://termine.[MEINE-DOMAIN]:8000`. Das `:8000` ist der interne Port und wird nicht öffentlich geöffnet.
   - `waha`: **leer lassen.** Falls Coolify dort automatisch eine Domain eingetragen hat, löschen.
6. **Deploy** klicken und prüfen:
   ```bash
   curl https://termine.[MEINE-DOMAIN]/health      # -> {"status":"ok"}
   ```

## 2. WAHA-Session starten und QR-Code scannen

WAHA hat weder Domain noch Port. Zum Scannen tunnelst du per SSH direkt auf die interne IP des WAHA-Containers:

1. Auf dem Server die Container-IP ermitteln:
   ```bash
   ssh root@<SERVER-IP>
   docker ps --format '{{.Names}}' | grep waha            # z. B. waha-abc123...
   docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' <waha-container-name>
   # -> z. B. 10.0.2.5
   ```
2. Auf dem eigenen Rechner den Tunnel öffnen (Fenster offen lassen):
   ```bash
   ssh -L 3000:10.0.2.5:3000 root@<SERVER-IP>
   ```
3. Im Browser **http://localhost:3000/dashboard** öffnen und mit `admin` / `WAHA_DASHBOARD_PASSWORD` einloggen.
   Bei der Server-Verbindung im Dashboard: URL `http://localhost:3000`, API-Key = `WAHA_API_KEY`.
4. Die Session `default` startet automatisch und steht auf `SCAN_QR_CODE`. Falls `STOPPED` oder `FAILED` angezeigt wird: Start/Restart klicken. Dann den QR-Code anzeigen lassen.
5. Auf dem Handy: **WhatsApp Business → Einstellungen (⋮) → Verknüpfte Geräte → Gerät hinzufügen** → QR-Code scannen. Die Session wechselt auf `WORKING`.
6. Tunnel schließen (Strg+C). Fertig, es bleibt nichts offen.

Die Anmeldung liegt im Volume `waha_sessions`. Nach Redeploys oder Neustarts ist kein neuer Scan nötig.
WAHA hängt als „verknüpftes Gerät“ am Handy. Das Handy mit der WhatsApp-Business-App (Twilio-Nummer) muss also eingerichtet bleiben und regelmäßig online sein (mindestens alle ca. 14 Tage), sonst meldet WhatsApp verknüpfte Geräte ab.

## 3. Gruppen-ID ermitteln und eintragen

Die Twilio-Nummer muss Mitglied der Zielgruppe sein.

1. In Coolify: Ressource → **Terminal** → Container `webhook` wählen → `python list_groups.py`
   Oder per SSH: `docker exec -it $(docker ps -qf name=webhook) python list_groups.py`
   ```
   Vertrieb Team      120363012345678901@g.us
   ```
2. Die ID in Coolify bei `WAHA_GROUP_ID` eintragen → **Redeploy**. Erst dann übernimmt der Container geänderte Variablen.

## 4. Testaufrufe per curl

In Git Bash oder auf dem Server:

```bash
TOKEN=<WEBHOOK_TOKEN>
URL=https://termine.[MEINE-DOMAIN]/webhook/termin

# a) Token als Header -> 200 {"status":"sent"}
curl -i -X POST "$URL" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"wer":"Max Mustermann","unternehmen":"Muster GmbH"}'

# b) Token als Query-Parameter -> 200 {"status":"sent"}
#    (andere Werte als in a), sonst greift der 60-s-Duplikat-Schutz)
curl -i -X POST "$URL?token=$TOKEN" -H "Content-Type: application/json" \
  -d '{"wer":"Erika Musterfrau","unternehmen":"Beispiel AG"}'

# c) Falscher Token -> 401
curl -i -X POST "$URL?token=falsch" -H "Content-Type: application/json" \
  -d '{"wer":"Test","unternehmen":"Test"}'
```

Logs: in Coolify unter **Logs** → Container `webhook`.

## 5. Eintrag im Akquise-Tool

- **Methode:** POST, Body als JSON, z. B. `{"wer": "...", "unternehmen": "..."}`
- **Wenn das Tool eigene Header erlaubt (bevorzugt):**
  URL `https://termine.[MEINE-DOMAIN]/webhook/termin` mit Header `Authorization: Bearer <WEBHOOK_TOKEN>`
- **Sonst:** URL `https://termine.[MEINE-DOMAIN]/webhook/termin?token=<WEBHOOK_TOKEN>`
- **Feldnamen:** Schickt das Tool andere Namen, stehen sie in der 400-Antwort und im Log („empfangene Felder“). Dann `FIELD_WER` / `FIELD_UNTERNEHMEN` in Coolify anpassen und Redeploy klicken.

## Hinweise

- Der Webhook-Dienst schreibt keine Tokens ins Log; das uvicorn-Access-Log ist aus. Falls im Coolify-Proxy Access-Logs aktiviert sind, kann dort ein `?token=` auftauchen. Darum lieber den Header verwenden.
- Der Duplikat-Schutz liegt im Arbeitsspeicher und wird bei Neustart/Redeploy geleert.
- WAHA nutzt WhatsApp Web, also keine offizielle API. Für wenige Nachrichten in eine eigene Gruppe ist das unkritisch, für Massenversand nicht geeignet.
- Code-Änderungen: pushen → Redeploy in Coolify (oder Auto-Deploy über die GitHub App).
