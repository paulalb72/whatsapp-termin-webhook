"""Webhook vom Akquise-Tool -> Nachricht in eine WhatsApp-Gruppe (via WAHA)."""

import asyncio
import logging
import os
import secrets
import sqlite3
import sys
import time
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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

# Statistik: Tagesbilanz täglich um REPORT_TIME, Wochenbilanz an WEEKLY_REPORT_DAY um REPORT_TIME
DB_PATH = os.path.join(env("DATA_DIR", "/data"), "termine.db")
TIMEZONE = env("TIMEZONE", "Europe/Berlin")
REPORT_TIME = env("REPORT_TIME", "20:00")
WEEKLY_REPORT_DAY = env("WEEKLY_REPORT_DAY", "fri").lower()
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAYS_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def parse_time(value: str) -> tuple[int, int]:
    hour, minute = (int(part) for part in value.split(":"))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(value)
    return hour, minute


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
    try:
        parse_time(REPORT_TIME)
    except ValueError:
        errors.append(f"REPORT_TIME ungültig (Format HH:MM): {REPORT_TIME!r}")
    if WEEKLY_REPORT_DAY not in WEEKDAYS:
        errors.append(f"WEEKLY_REPORT_DAY ungültig (erlaubt: {', '.join(WEEKDAYS)}): {WEEKLY_REPORT_DAY!r}")
    try:
        ZoneInfo(TIMEZONE)
    except Exception:
        errors.append(f"TIMEZONE ungültig: {TIMEZONE!r}")
    for error in errors:
        log.critical("Konfigurationsfehler: %s", error)
    if errors:
        sys.exit(1)
    if not WAHA_GROUP_ID:
        log.warning("WAHA_GROUP_ID ist leer – Webhooks werden mit 503 abgelehnt, bis sie gesetzt ist.")


check_config()
TZ = ZoneInfo(TIMEZONE)
REPORT_HOUR, REPORT_MINUTE = parse_time(REPORT_TIME)
WEEKLY_WEEKDAY = WEEKDAYS.index(WEEKLY_REPORT_DAY)

http: httpx.AsyncClient


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http
    http = httpx.AsyncClient(timeout=10)
    init_db()
    scheduler = asyncio.create_task(report_loop())
    log.info(
        "Dienst gestartet (Felder: %r / %r, Tagesbilanz %s, Wochenbilanz %s %s)",
        FIELD_WER, FIELD_UNTERNEHMEN, REPORT_TIME, WEEKLY_REPORT_DAY, REPORT_TIME,
    )
    yield
    scheduler.cancel()
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


# ---------- Statistik ----------

def init_db() -> None:
    with closing(sqlite3.connect(DB_PATH)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS termine (ts REAL NOT NULL, wer TEXT NOT NULL, unternehmen TEXT NOT NULL)")
        db.execute("CREATE INDEX IF NOT EXISTS termine_ts ON termine (ts)")
        db.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value REAL NOT NULL)")


def record_termin(wer: str, unternehmen: str) -> None:
    try:
        with closing(sqlite3.connect(DB_PATH)) as db, db:
            db.execute("INSERT INTO termine VALUES (?, ?, ?)", (time.time(), wer, unternehmen))
    except Exception:
        log.exception("Termin konnte nicht für die Statistik gespeichert werden")


def state_get(key: str) -> float | None:
    with closing(sqlite3.connect(DB_PATH)) as db:
        row = db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def state_set(key: str, value: float) -> None:
    with closing(sqlite3.connect(DB_PATH)) as db, db:
        db.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, value))


def last_report_time(now: datetime, weekday: int | None = None) -> datetime:
    """Letzter planmäßiger Report-Zeitpunkt <= now (täglich, oder wöchentlich am weekday)."""
    due = now.replace(hour=REPORT_HOUR, minute=REPORT_MINUTE, second=0, microsecond=0)
    if weekday is not None:
        due -= timedelta(days=(due.weekday() - weekday) % 7)
    if due > now:
        due -= timedelta(days=1 if weekday is None else 7)
    return due


def plural(n: int) -> str:
    return f"{n} Termin" if n == 1 else f"{n} Termine"


def build_report(kind: str, start: datetime, end: datetime) -> str | None:
    """Rangliste der Termine im Zeitraum (start, end]; None, wenn niemand einen Termin gelegt hat."""
    with closing(sqlite3.connect(DB_PATH)) as db:
        rows = db.execute(
            "SELECT wer FROM termine WHERE ts > ? AND ts <= ? ORDER BY ts", (start.timestamp(), end.timestamp())
        ).fetchall()
    counts: dict[str, list] = {}  # casefold(name) -> [Anzeigename, Anzahl]
    for (wer,) in rows:
        counts.setdefault(wer.casefold(), [wer, 0])[1] += 1
    if not counts:
        return None

    ranking = sorted(counts.values(), key=lambda e: (-e[1], e[0].casefold()))
    if kind == "daily":
        lines = [f"📊 *TAGESBILANZ* · {WEEKDAYS_DE[end.weekday()]}, {end:%d.%m.}", ""]
    else:
        lines = [f"🏆 *WOCHENBILANZ* · KW {end.isocalendar().week}", ""]
    for name, n in ranking:
        place = 1 + sum(1 for _, other in ranking if other > n)
        lines.append(f"{MEDALS.get(place, f'{place}.')} *{name}*: {plural(n)}")
    lines += ["", f"📅 *Gesamt: {plural(sum(n for _, n in ranking))}* 🚀"]
    return "\n".join(lines)


async def check_reports() -> None:
    now = datetime.now(TZ)
    for kind, weekday, days in (("daily", None, 1), ("weekly", WEEKLY_WEEKDAY, 7)):
        due = last_report_time(now, weekday)
        last = state_get(kind)
        if last is not None and last >= due.timestamp():
            continue
        # Beim allerersten Start nur den Zeitpunkt merken, nichts senden
        if last is not None and WAHA_GROUP_ID:
            text = build_report(kind, due - timedelta(days=days), due)
            if text is None:
                log.info("%s-Report übersprungen (keine Termine)", kind)
            elif await send_whatsapp(text):
                log.info("%s-Report gesendet", kind)
            elif now - due < timedelta(hours=1):
                continue  # in 30 s erneut versuchen
            else:
                log.error("%s-Report konnte nicht gesendet werden – übersprungen", kind)
        state_set(kind, due.timestamp())


async def report_loop() -> None:
    while True:
        try:
            await check_reports()
        except Exception:
            log.exception("Fehler im Report-Zeitplan")
        await asyncio.sleep(30)


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
        record_termin(wer, unternehmen)
        log.info("Gesendet: %s / %s", wer, unternehmen)
        return {"status": "sent"}

    _recent.pop(key, None)  # fehlgeschlagen -> erneuter Webhook darf es wieder versuchen
    log.error("502 – Senden endgültig fehlgeschlagen: %s / %s", wer, unternehmen)
    return error(502, "WhatsApp-Nachricht konnte nicht gesendet werden.")


@app.post("/report/{kind}")
async def report_now(kind: str, request: Request):
    """Zwischenstand seit dem letzten planmäßigen Report senden (?preview=1: nur anzeigen)."""
    if not token_ok(request):
        return error(401, "Ungültiger oder fehlender Token.")
    if kind not in ("daily", "weekly"):
        return error(404, "Erlaubt: /report/daily oder /report/weekly")
    now = datetime.now(TZ)
    text = build_report(kind, last_report_time(now, WEEKLY_WEEKDAY if kind == "weekly" else None), now)
    if text is None:
        return {"status": "empty"}
    if request.query_params.get("preview"):
        return {"status": "preview", "text": text}
    if not WAHA_GROUP_ID:
        return error(503, "WhatsApp-Gruppe ist nicht konfiguriert.")
    if await send_whatsapp(text):
        log.info("%s-Report manuell gesendet", kind)
        return {"status": "sent"}
    return error(502, "WhatsApp-Nachricht konnte nicht gesendet werden.")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("Unerwarteter Fehler")
    return error(500, "Interner Fehler.")
