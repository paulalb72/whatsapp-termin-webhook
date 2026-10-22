"""Webhook vom Akquise-Tool -> Nachricht in eine WhatsApp-Gruppe (via WAHA)."""

import asyncio
import logging
import os
import secrets
import sys
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("webhook")
logging.getLogger("httpx").setLevel(logging.WARNING)  # keine Request-URLs im Log


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or "").strip() or default


WEBHOOK_TOKEN = env("WEBHOOK_TOKEN")
WAHA_URL = env("WAHA_URL", "http://waha:3000").rstrip("/")
WAHA_API_KEY = env("WAHA_API_KEY")
WAHA_SESSION = env("WAHA_SESSION", "default")
WAHA_GROUP_ID = env("WAHA_GROUP_ID")
FIELD_WER = env("FIELD_WER", "wer")
FIELD_UNTERNEHMEN = env("FIELD_UNTERNEHMEN", "unternehmen")
DEFAULT_TEMPLATE = (
    "🎉 *NEUER TERMIN!* 🎉\n"
    "\n"
    "👤 *{wer}* hat gerade einen Termin mit\n"
    "🏢 *{unternehmen}* gelegt.\n"
    "\n"
    "🔥 Stark, weiter so! 💪"
)
# "\n" in der Umgebungsvariable wird zum Zeilenumbruch (WhatsApp: *fett*, _kursiv_)
MESSAGE_TEMPLATE = env("MESSAGE_TEMPLATE", DEFAULT_TEMPLATE).replace("\\n", "\n")
MAX_FIELD_LENGTH = int(env("MAX_FIELD_LENGTH", "100"))
DEDUP_SECONDS = int(env("DEDUP_SECONDS", "60"))
RETRY_DELAYS = (1, 3)  # Wartezeit (s) vor Retry 1 und 2 -> max. 3 Versuche


def check_config() -> None:
    errors = []
    if len(WEBHOOK_TOKEN) < 16:
        errors.append("WEBHOOK_TOKEN fehlt oder ist kürzer als 16 Zeichen")
    if not WAHA_API_KEY:
        errors.append("WAHA_API_KEY fehlt")
    try:
        MESSAGE_TEMPLATE.format(wer="", unternehmen="")
    except (KeyError, IndexError, ValueError) as e:
        errors.append(f"MESSAGE_TEMPLATE ungültig (erlaubt sind nur {{wer}} und {{unternehmen}}): {e!r}")
    for error in errors:
        log.critical("Konfigurationsfehler: %s", error)
    if errors:
        sys.exit(1)
    if not WAHA_GROUP_ID:
        log.warning("WAHA_GROUP_ID ist leer – Webhooks werden mit 503 abgelehnt, bis sie gesetzt ist.")


check_config()

http: httpx.AsyncClient


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http
    http = httpx.AsyncClient(timeout=10)
    log.info("Dienst gestartet (Felder: %r / %r)", FIELD_WER, FIELD_UNTERNEHMEN)
    yield
    await http.aclose()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def error(status: int, message: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message, **extra})


def token_ok(request: Request) -> bool:
    candidates = []
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        candidates.append(auth[7:].strip())
    if "token" in request.query_params:
        candidates.append(request.query_params["token"])
    expected = WEBHOOK_TOKEN.encode()
    return any(secrets.compare_digest(c.encode(), expected) for c in candidates)


def clean(value) -> str:
    """Whitespace normalisieren (auch Zeilenumbrüche) und Länge begrenzen."""
    if value is None or isinstance(value, (dict, list)):
        return ""
    return " ".join(str(value).split())[:MAX_FIELD_LENGTH].rstrip()


# Duplikat-Schutz: (wer, unternehmen) -> Zeitpunkt des Versands
_recent: dict[tuple[str, str], float] = {}


def claim(key: tuple[str, str]) -> bool:
    """True, wenn der Key in den letzten DEDUP_SECONDS noch nicht gesendet wurde."""
    now = time.monotonic()
    for k, t in list(_recent.items()):
        if now - t >= DEDUP_SECONDS:
            del _recent[k]
    if key in _recent:
        return False
    _recent[key] = now
    return True


async def send_whatsapp(text: str) -> bool:
    payload = {"session": WAHA_SESSION, "chatId": WAHA_GROUP_ID, "text": text}
    attempts = len(RETRY_DELAYS) + 1
    for attempt in range(1, attempts + 1):
        try:
            r = await http.post(f"{WAHA_URL}/api/sendText", json=payload, headers={"X-Api-Key": WAHA_API_KEY})
            if r.is_success:
                return True
            log.warning("WAHA-Fehler (Versuch %d/%d): HTTP %d %s", attempt, attempts, r.status_code, r.text[:300])
        except Exception as e:
            log.warning("WAHA nicht erreichbar (Versuch %d/%d): %s: %s", attempt, attempts, type(e).__name__, e)
        if attempt < attempts:
            await asyncio.sleep(RETRY_DELAYS[attempt - 1])
    return False


@app.post("/webhook/termin")
async def termin(request: Request):
    ip = request.client.host if request.client else "?"
    if not token_ok(request):
        log.warning("401 – ungültiger oder fehlender Token (von %s)", ip)
        return error(401, "Ungültiger oder fehlender Token.")

    try:
        data = await request.json()
    except Exception:
        log.warning("400 – Body ist kein gültiges JSON (von %s)", ip)
        return error(400, "Body ist kein gültiges JSON.")
    if not isinstance(data, dict):
        log.warning("400 – JSON-Body ist kein Objekt (von %s)", ip)
        return error(400, "JSON-Body muss ein Objekt sein.")

    wer = clean(data.get(FIELD_WER))
    unternehmen = clean(data.get(FIELD_UNTERNEHMEN))
    missing = [name for name, value in ((FIELD_WER, wer), (FIELD_UNTERNEHMEN, unternehmen)) if not value]
    if missing:
        log.warning("400 – Feld(er) fehlen oder leer: %s; empfangene Felder: %s", missing, list(data))
        return error(400, f"Feld(er) fehlen oder sind leer: {', '.join(missing)}", received_fields=list(data))

    if not WAHA_GROUP_ID:
        log.error("503 – WAHA_GROUP_ID nicht konfiguriert")
        return error(503, "WhatsApp-Gruppe ist nicht konfiguriert.")

    key = (wer.casefold(), unternehmen.casefold())
    if not claim(key):
        log.info("Duplikat ignoriert: %s / %s", wer, unternehmen)
        return {"status": "duplicate"}

    if await send_whatsapp(MESSAGE_TEMPLATE.format(wer=wer, unternehmen=unternehmen)):
        log.info("Gesendet: %s / %s", wer, unternehmen)
        return {"status": "sent"}

    _recent.pop(key, None)  # fehlgeschlagen -> erneuter Webhook darf es wieder versuchen
    log.error("502 – Senden endgültig fehlgeschlagen: %s / %s", wer, unternehmen)
    return error(502, "WhatsApp-Nachricht konnte nicht gesendet werden.")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("Unerwarteter Fehler")
    return error(500, "Interner Fehler.")
