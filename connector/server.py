"""Bot-Connector: Remote-MCP-Server, über den Claude den WhatsApp-Bot ändern, deployen und testen kann.

- Code: jede Änderung ist ein Commit im GitHub-Repo des Bots (Verlauf, Diff, Rücknahme).
- Betrieb: Deploy, Deploy-Status, Container-Logs und Umgebungsvariablen über die Coolify-API.
- Test: HTTP-Aufrufe an die öffentliche Bot-URL, auf Wunsch mit WEBHOOK_TOKEN.
- Zugang: OAuth mit Login-Seite (Name + Connector-Passwort), siehe oauth.py.
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Annotated, Any, Awaitable, Callable, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from backends import Bot, Coolify, GitHub, RefConflict
from oauth import DEFAULT_SCOPES, PasswordOAuth

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("connector")
logging.getLogger("httpx").setLevel(logging.WARNING)


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or "").strip() or default


PUBLIC_URL = env("PUBLIC_URL").rstrip("/")
CONNECTOR_PASSWORD = env("CONNECTOR_PASSWORD")
GITHUB_TOKEN = env("GITHUB_TOKEN")
GITHUB_REPO = env("GITHUB_REPO", "paulalb72/whatsapp-termin-webhook")
GITHUB_BRANCH = env("GITHUB_BRANCH", "main")
GITHUB_API_URL = env("GITHUB_API_URL", "https://api.github.com")
COOLIFY_URL = env("COOLIFY_URL").rstrip("/")
COOLIFY_TOKEN = env("COOLIFY_TOKEN")
COOLIFY_APP_UUID = env("COOLIFY_APP_UUID")
BOT_URL = env("BOT_URL").rstrip("/")
BOT_TOKEN = env("BOT_TOKEN")
DATA_DIR = env("DATA_DIR", "/data")
TIMEZONE = env("TIMEZONE", "Europe/Berlin")

# Der Connector darf sich nicht selbst kaputtmachen: connector/ ist über die Tools nur lesbar.
PROTECTED_PATHS = ("connector/", ".github/")
# Diese Variablen hängen am Akquise-Tool, an WAHA oder am Connector selbst und bleiben Paul vorbehalten.
PROTECTED_VARS = {"WEBHOOK_TOKEN", "WAHA_API_KEY", "WAHA_DASHBOARD_PASSWORD", "WHATSAPP_HOOK_HMAC_KEY"}
SECRET_NAME = re.compile(r"TOKEN|KEY|SECRET|PASSWORD|PASSWD|HMAC|CREDENTIAL", re.I)
SECRET_PATTERNS = [
    (re.compile(r"ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}"), "GitHub-Token"),
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), "Anthropic-API-Key"),
    (re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}"), "API-Key"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "privater Schlüssel"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS-Key"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "Slack-Token"),
]
MAX_FILE_BYTES = 500_000
MAX_OUTPUT = 40_000
TERMINAL_DEPLOY_STATES = {"finished", "failed", "cancelled-by-user", "cancelled-by-system", "cancelled"}
DEPLOY_ICONS = {"queued": "⏳", "in_progress": "🔄", "finished": "✅", "failed": "❌"}


def check_config() -> None:
    errors = []
    local = re.fullmatch(r"http://(localhost|127\.0\.0\.1)(:\d+)?", PUBLIC_URL)
    if not (PUBLIC_URL.startswith("https://") or local):
        errors.append("PUBLIC_URL fehlt oder beginnt nicht mit https:// (z. B. https://connector.example.de)")
    if PUBLIC_URL.endswith("/mcp"):
        errors.append("PUBLIC_URL ohne /mcp am Ende angeben")
    if len(CONNECTOR_PASSWORD) < 12:
        errors.append("CONNECTOR_PASSWORD fehlt oder ist kürzer als 12 Zeichen")
    for name in ("GITHUB_TOKEN", "COOLIFY_URL", "COOLIFY_TOKEN", "COOLIFY_APP_UUID", "BOT_URL", "BOT_TOKEN"):
        if not globals()[name]:
            errors.append(f"{name} fehlt")
    if "/" not in GITHUB_REPO:
        errors.append(f"GITHUB_REPO muss die Form besitzer/repo haben: {GITHUB_REPO!r}")
    try:
        ZoneInfo(TIMEZONE)
    except Exception:
        errors.append(f"TIMEZONE ungültig: {TIMEZONE!r}")
    for error in errors:
        log.critical("Konfigurationsfehler: %s", error)
    if errors:
        sys.exit(1)


check_config()
TZ = ZoneInfo(TIMEZONE)
KNOWN_SECRETS = [s for s in (CONNECTOR_PASSWORD, GITHUB_TOKEN, COOLIFY_TOKEN, BOT_TOKEN) if len(s) >= 8]

github = GitHub(GITHUB_TOKEN, GITHUB_REPO, GITHUB_BRANCH, GITHUB_API_URL)
coolify = Coolify(COOLIFY_URL, COOLIFY_TOKEN, COOLIFY_APP_UUID)
bot = Bot(BOT_URL, BOT_TOKEN)
last_deployment: str | None = None

INSTRUCTIONS = f"""\
Du arbeitest am WhatsApp-Termin-Bot (Python/FastAPI in webhook/main.py, WhatsApp über WAHA, \
deployt mit Coolify aus github.com/{GITHUB_REPO}, Branch {GITHUB_BRANCH}). Öffentliche Bot-URL: {BOT_URL}

Arbeitsablauf:
1. uebersicht aufrufen.
2. Vor jeder Änderung den betroffenen Code lesen (datei_lesen, suchen). Auch README.md und docker-compose.yml beachten.
3. Ändern: datei_ersetzen für gezielte Änderungen, datei_schreiben für neue oder komplett neue Dateien. \
Jede Änderung wird sofort ein Commit auf {GITHUB_BRANCH}, ist aber erst nach deployen live.
4. Dem Nutzer kurz sagen, was live geht. Dann deployen, danach deploy_status, bis der Status finished ist.
5. Prüfen: bot_aufrufen (z. B. GET /health) und logs. Bei Fehlern korrigieren, oder die Änderung mit \
aenderung_zuruecknehmen zurücknehmen und neu deployen.

Regeln:
- Das Repo ist öffentlich. Niemals Tokens, Passwörter oder Kundendaten in Dateien schreiben. Geheimnisse und \
Einstellungen sind Coolify-Variablen (variablen_liste, variable_setzen) und werden im Code mit os.environ gelesen.
- Neue Variable: in docker-compose.yml unter services.webhook.environment eintragen (NAME: ${{NAME:-default}}), \
mit variable_setzen setzen und deployen. Sonst kommt sie nicht im Container an.
- Neuer Endpoint in webhook/main.py: @app.get(...) / @app.post(...). Schutz mit \
`if not token_ok(request): return error(401, "Ungültiger oder fehlender Token.")`. Nachricht in die Gruppe mit \
`await send_whatsapp(text)`. Die WAHA-API ist intern unter WAHA_URL erreichbar (Header X-Api-Key: WAHA_API_KEY).
- Neue Python-Pakete mit fester Version in webhook/requirements.txt. Neue .py-Dateien im webhook/Dockerfile per COPY aufnehmen.
- README.md aktuell halten, wenn sich Verhalten, Endpoints oder Variablen ändern.
- Achtung: POST /webhook/termin und /report/daily|weekly ohne ?preview=1 senden echte Nachrichten in die \
WhatsApp-Gruppe und zählen in der Statistik.
- connector/ (dieser Connector) und die Variablen {', '.join(sorted(PROTECTED_VARS))} sind geschützt.
"""

provider = PasswordOAuth(os.path.join(DATA_DIR, "connector.db"), CONNECTOR_PASSWORD, PUBLIC_URL)
mcp = MCPServer(
    name="whatsapp-bot-connector",
    title="WhatsApp-Bot",
    version="1.0.0",
    instructions=INSTRUCTIONS,
    auth_server_provider=provider,
    auth=AuthSettings(
        issuer_url=PUBLIC_URL,
        resource_server_url=f"{PUBLIC_URL}/mcp",
        client_registration_options=ClientRegistrationOptions(enabled=True, default_scopes=DEFAULT_SCOPES),
        revocation_options=RevocationOptions(enabled=True),
        validate_token_resource=False,
    ),
)


def tool(title: str, read_only: bool = False, destructive: bool = False):
    return mcp.tool(
        structured_output=False,
        annotations=ToolAnnotations(title=title, read_only_hint=read_only, destructive_hint=destructive),
    )


# ---------- Hilfsfunktionen ----------


def user() -> str:
    token = get_access_token()
    return token.subject if token and token.subject else "unbekannt"


def mask(text: str) -> str:
    for secret in KNOWN_SECRETS:
        text = text.replace(secret, "•••")
    return text


def clip(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… (gekürzt, {len(text) - limit} Zeichen mehr)"


def local_time(value: str | None) -> str:
    if not value:
        return "?"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M")


def is_sha(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def is_protected(path: str) -> bool:
    return path.lower().startswith(PROTECTED_PATHS)


def norm_path(pfad: str) -> str:
    path = pfad.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    path = path.lstrip("/")
    if not path or any(part in ("", ".", "..") for part in path.split("/")):
        raise ToolError(f"Ungültiger Pfad {pfad!r}. Pfade sind relativ zum Repo, z. B. webhook/main.py")
    return path


def writable_path(pfad: str) -> str:
    path = norm_path(pfad)
    if is_protected(path):
        raise ToolError(
            f"{path} ist geschützt. Der Ordner gehört zum Connector selbst und kann nur direkt im Repo geändert werden."
        )
    return path


def first_line(message: str) -> str:
    return (message or "").splitlines()[0] if message else ""


class CheckError(Exception):
    pass


def check_secrets(content: str) -> None:
    for pattern, label in SECRET_PATTERNS:
        if pattern.search(content):
            raise ToolError(
                f"Abgelehnt: Der Inhalt enthält offenbar einen {label}. Das Repo ist öffentlich. Geheimnisse gehören "
                "in eine Coolify-Variable (variable_setzen) und werden im Code mit os.environ gelesen."
            )
    if any(secret in content for secret in KNOWN_SECRETS):
        raise ToolError("Abgelehnt: Der Inhalt enthält einen echten Token oder ein Passwort. Das Repo ist öffentlich.")


def check_syntax(path: str, content: str) -> list[str]:
    """Syntax von Python/YAML/JSON prüfen. CheckError bei Fehlern, Rückgabe: Warnungen."""
    name = path.rsplit("/", 1)[-1].lower()
    warnings: list[str] = []
    if name.endswith(".py"):
        try:
            compile(content, path, "exec", dont_inherit=True)
        except SyntaxError as e:
            raise CheckError(f"Python-Syntaxfehler in {path}, Zeile {e.lineno}: {e.msg}\n  {(e.text or '').strip()}") from e
        except ValueError as e:
            raise CheckError(f"{path}: {e}") from e
    elif name.endswith((".yml", ".yaml")):
        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError as e:
            raise CheckError(f"YAML-Fehler in {path}: {e}") from e
        if name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml") and path.count("/") == 0:
            services = data.get("services") if isinstance(data, dict) else None
            if not isinstance(services, dict) or not services:
                raise CheckError(f"{path}: Abschnitt 'services' fehlt oder ist leer.")
            for service in ("webhook", "waha"):
                if service not in services:
                    warnings.append(f"⚠ Service '{service}' fehlt in {path}.")
    elif name.endswith(".json"):
        try:
            json.loads(content)
        except ValueError as e:
            raise CheckError(f"JSON-Fehler in {path}: {e}") from e
    return warnings


def check_content(path: str, content: str) -> list[str]:
    if len(content.encode()) > MAX_FILE_BYTES:
        raise ToolError(f"Datei zu groß (max. {MAX_FILE_BYTES // 1000} kB).")
    check_secrets(content)
    try:
        return check_syntax(path, content)
    except CheckError as e:
        raise ToolError(f"{e}\nNichts gespeichert.") from e


async def read_text(files: dict[str, dict], path: str) -> str:
    if path not in files:
        raise ToolError(f"Die Datei {path} gibt es nicht. dateien_liste zeigt alle Dateien.")
    text = await github.text(files[path]["sha"])
    if text is None:
        raise ToolError(f"{path} ist eine Binärdatei und kann nicht als Text gelesen werden.")
    return text


async def head_files() -> tuple[str, dict[str, dict]]:
    head, tree_sha = await github.head()
    return head, await github.tree(tree_sha)


Changes = dict[str, str | None]  # Pfad -> neuer Inhalt, None = löschen


async def commit_change(beschreibung: str, make: Callable[[dict[str, dict]], Awaitable[tuple[Changes, list[str]]]]) -> str:
    """Änderung auf dem aktuellen Stand berechnen und als Commit speichern (bei Gleichzeitigkeit neu versuchen)."""
    summary = " ".join(beschreibung.split())
    if len(summary) < 3:
        raise ToolError("Bitte in 'beschreibung' kurz angeben, was und warum geändert wird (erscheint im Verlauf).")
    message = f"{summary}\n\nÜber Bot-Connector · {user()}"
    for _ in range(3):
        head, tree_sha = await github.head()
        files = await github.tree(tree_sha)
        changes, notes = await make(files)
        entries = []
        for path, content in changes.items():
            mode = files.get(path, {}).get("mode", "100644")
            if content is None:
                entries.append({"path": path, "mode": mode, "type": "blob", "sha": None})
            else:
                entries.append({"path": path, "mode": mode, "type": "blob", "content": content})
        try:
            sha = await github.commit(head, tree_sha, entries, message)
        except RefConflict:
            continue
        log.info("Commit %s von %s: %s (%s)", sha[:7], user(), summary, ", ".join(changes))
        lines = [f"✓ Gespeichert als Commit {sha[:7]}: {summary}"]
        for path, content in changes.items():
            state = "gelöscht" if content is None else ("geändert" if path in files else "neu")
            lines.append(f"  {path} ({state})")
        lines += ["", "Noch nicht live: weitere Änderungen machen oder mit deployen veröffentlichen.", *notes]
        return "\n".join(lines)
    raise ToolError("Das Repo wurde gerade gleichzeitig geändert. Bitte noch einmal versuchen.")


async def compose_variables(files: dict[str, dict]) -> set[str] | None:
    entry = files.get("docker-compose.yml")
    if not entry:
        return None
    text = await github.text(entry["sha"]) or ""
    return set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", text))


def deploy_log_lines(deployment: dict, limit: int) -> list[str]:
    raw = deployment.get("logs")
    if not raw:
        return []
    try:
        entries = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return [mask(line)[:300] for line in str(raw).splitlines()[-limit:]]
    lines: list[str] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or entry.get("hidden"):
            continue
        output = str(entry.get("output") or "").rstrip()
        if output:
            lines.extend(output.splitlines())
    return [mask(line)[:300] for line in lines[-limit:]]


async def check_repo() -> tuple[str, list[str], list[str]]:
    """Gesamten Stand vor dem Deploy prüfen: (HEAD, Fehler, Warnungen)."""
    head, files = await head_files()
    errors: list[str] = []
    warnings: list[str] = []
    for path, entry in files.items():
        if is_protected(path) or not path.lower().endswith((".py", ".yml", ".yaml", ".json")):
            continue
        text = await github.text(entry["sha"])
        if text is None:
            continue
        try:
            warnings += check_syntax(path, text)
        except CheckError as e:
            errors.append(str(e))
    dockerfile = files.get("webhook/Dockerfile")
    if dockerfile:
        text = await github.text(dockerfile["sha"]) or ""
        if not re.search(r"^\s*COPY\s+\.\s", text, re.M):
            for path in files:
                if path.startswith("webhook/") and path.endswith(".py") and path.count("/") == 1:
                    name = path.split("/", 1)[1]
                    if not re.search(rf"^\s*COPY\b.*\b{re.escape(name)}\b", text, re.M):
                        warnings.append(f"⚠ {path} wird im webhook/Dockerfile nicht per COPY ins Image übernommen.")
    return head, errors, warnings


# ---------- Tools: Überblick und Code lesen ----------


@tool("Überblick", read_only=True)
async def uebersicht() -> str:
    """Startpunkt: Stand des Bots auf einen Blick. Zeigt, ob der Bot läuft, den letzten Deploy, ob neuere
    Code-Änderungen noch nicht deployt sind, die letzten Änderungen und alle Dateien. Zuerst aufrufen."""
    commits, app, deployments, health, tree = await asyncio.gather(
        github.commits(8),
        coolify.application(),
        coolify.deployments(5),
        bot.request("GET", "/health", with_token=False),
        head_files(),
        return_exceptions=True,
    )
    lines = [f"WhatsApp-Bot · github.com/{GITHUB_REPO} (Branch {GITHUB_BRANCH}) · angemeldet als {user()}", ""]

    lines.append("Bot")
    if isinstance(app, Exception):
        lines.append(f"  Coolify: nicht abrufbar ({app})")
    else:
        lines.append(f"  Coolify-Status: {app.get('status') or '?'}")
    if isinstance(health, Exception):
        lines.append(f"  {BOT_URL}/health: {health}")
    else:
        lines.append(f"  {BOT_URL}/health: HTTP {health.status_code} {health.text[:100]}")

    deployed_sha = None
    lines += ["", "Deploy"]
    if isinstance(deployments, Exception):
        lines.append(f"  nicht abrufbar ({deployments})")
    elif not deployments:
        lines.append("  noch kein Deploy gefunden")
    else:
        latest = deployments[0]
        status = latest.get("status") or "?"
        lines.append(
            f"  Letzter Deploy: {DEPLOY_ICONS.get(status, '•')} {status} · {local_time(latest.get('created_at'))}"
            f" · Commit {str(latest.get('commit') or '?')[:7]} · ID {latest.get('deployment_uuid')}"
        )
        finished = next((d for d in deployments if d.get("status") == "finished"), None)
        if finished and is_sha(finished.get("commit")):
            deployed_sha = finished["commit"]

    lines += ["", "Letzte Änderungen (neueste zuerst)"]
    if isinstance(commits, Exception):
        lines.append(f"  nicht abrufbar ({commits})")
    else:
        shas = [c["sha"] for c in commits]
        for c in commits:
            live = "  ← live" if c["sha"] == deployed_sha else ""
            info = c["commit"]["author"]
            lines.append(f"  {c['sha'][:7]} · {local_time(info.get('date'))} · {info.get('name')} · {first_line(c['commit']['message'])}{live}")
        if deployed_sha and shas and shas[0] != deployed_sha:
            pending = shas.index(deployed_sha) if deployed_sha in shas else f"mehr als {len(shas)}"
            lines.append(f"  ⚠ {pending} neuere Commit(s) sind noch nicht live → deployen")

    lines += ["", "Dateien"]
    if isinstance(tree, Exception):
        lines.append(f"  nicht abrufbar ({tree})")
    else:
        for path, entry in sorted(tree[1].items()):
            note = " (geschützt)" if is_protected(path) else ""
            lines.append(f"  {path} · {entry.get('size', 0) / 1000:.1f} kB{note}")
    return mask("\n".join(lines))


@tool("Dateien auflisten", read_only=True)
async def dateien_liste() -> str:
    """Alle Dateien im Repo des Bots mit Größe."""
    _, files = await head_files()
    return "\n".join(
        f"{path} · {entry.get('size', 0) / 1000:.1f} kB{' (geschützt)' if is_protected(path) else ''}"
        for path, entry in sorted(files.items())
    )


@tool("Datei lesen", read_only=True)
async def datei_lesen(
    pfad: Annotated[str, Field(description="Pfad relativ zum Repo, z. B. webhook/main.py")],
    ab_zeile: Annotated[int, Field(description="Erste Zeile (ab 1)", ge=1)] = 1,
    bis_zeile: Annotated[int | None, Field(description="Letzte Zeile (leer = bis zum Ende)", ge=1)] = None,
    zeilennummern: Annotated[bool, Field(description="Zeilennummern voranstellen (nicht mit in datei_ersetzen kopieren)")] = False,
) -> str:
    """Inhalt einer Datei auf dem aktuellen Stand des Branches lesen."""
    path = norm_path(pfad)
    _, files = await head_files()
    text = await read_text(files, path)
    all_lines = text.splitlines()
    end = min(bis_zeile or len(all_lines), len(all_lines))
    selected = all_lines[ab_zeile - 1 : end]
    if zeilennummern:
        body = "\n".join(f"{n:>4}│ {line}" for n, line in enumerate(selected, start=ab_zeile))
    else:
        body = "\n".join(selected)
    header = f"{path} · {len(all_lines)} Zeilen"
    if ab_zeile > 1 or end < len(all_lines):
        header += f" · Ausschnitt {ab_zeile}–{end}"
    if len(body) > MAX_OUTPUT:
        body = clip(body) + "\n(Mit ab_zeile/bis_zeile den Rest lesen.)"
    return f"{header}\n\n{body}"


@tool("Im Code suchen", read_only=True)
async def suchen(
    text: Annotated[str, Field(description="Suchtext (Groß-/Kleinschreibung egal)")],
    regex: Annotated[bool, Field(description="text als regulären Ausdruck behandeln")] = False,
    pfad_beginnt_mit: Annotated[str | None, Field(description="Nur Dateien unter diesem Pfad, z. B. webhook/")] = None,
) -> str:
    """Text in allen Dateien des Repos suchen. Liefert Datei, Zeilennummer und Zeile."""
    try:
        pattern = re.compile(text if regex else re.escape(text), re.I)
    except re.error as e:
        raise ToolError(f"Ungültiger regulärer Ausdruck: {e}") from e
    prefix = norm_path(pfad_beginnt_mit) if pfad_beginnt_mit else ""
    _, files = await head_files()
    hits: list[str] = []
    for path, entry in sorted(files.items()):
        if not path.startswith(prefix) or entry.get("size", 0) > MAX_FILE_BYTES:
            continue
        content = await github.text(entry["sha"])
        for number, line in enumerate((content or "").splitlines(), start=1):
            if pattern.search(line):
                hits.append(f"{path}:{number}: {line.strip()[:200]}")
        if len(hits) >= 200:
            hits.append("… (mehr als 200 Treffer, Suche eingrenzen)")
            break
    return mask("\n".join(hits)) if hits else "Keine Treffer."


# ---------- Tools: Code ändern ----------


@tool("Datei schreiben")
async def datei_schreiben(
    pfad: Annotated[str, Field(description="Pfad relativ zum Repo, z. B. webhook/main.py")],
    inhalt: Annotated[str, Field(description="Kompletter neuer Inhalt der Datei")],
    beschreibung: Annotated[str, Field(description="Kurze Beschreibung der Änderung für den Verlauf, z. B. 'Endpoint /status ergänzt'")],
) -> str:
    """Datei neu anlegen oder komplett überschreiben (ein Commit). Für kleine Änderungen besser datei_ersetzen.
    Python-, YAML- und JSON-Dateien werden vorher auf Syntaxfehler geprüft. Erst nach deployen live."""
    path = writable_path(pfad)
    notes = check_content(path, inhalt)

    async def make(files: dict[str, dict]) -> tuple[Changes, list[str]]:
        if path in files and await github.text(files[path]["sha"]) == inhalt:
            raise ToolError(f"{path} hat bereits genau diesen Inhalt. Nichts geändert.")
        return {path: inhalt}, notes

    return await commit_change(beschreibung, make)


@tool("Text in Datei ersetzen")
async def datei_ersetzen(
    pfad: Annotated[str, Field(description="Pfad relativ zum Repo, z. B. webhook/main.py")],
    alt: Annotated[str, Field(description="Bisheriger Text, exakt wie in der Datei (inkl. Einrückung, ohne Zeilennummern)")],
    neu: Annotated[str, Field(description="Neuer Text")],
    beschreibung: Annotated[str, Field(description="Kurze Beschreibung der Änderung für den Verlauf")],
    alle_vorkommen: Annotated[bool, Field(description="Alle Vorkommen ersetzen statt genau eines")] = False,
) -> str:
    """Einen Textabschnitt in einer Datei ersetzen (ein Commit). 'alt' muss genau einmal vorkommen, sonst mehr
    umgebenden Text angeben oder alle_vorkommen=true. Syntax wird vorher geprüft. Erst nach deployen live."""
    path = writable_path(pfad)
    if not alt:
        raise ToolError("'alt' darf nicht leer sein. Für neue Dateien datei_schreiben verwenden.")
    if alt == neu:
        raise ToolError("'alt' und 'neu' sind gleich.")

    async def make(files: dict[str, dict]) -> tuple[Changes, list[str]]:
        content = await read_text(files, path)
        old, new = alt, neu
        if old not in content and "\r\n" in content:
            old, new = alt.replace("\n", "\r\n"), neu.replace("\n", "\r\n")
        count = content.count(old)
        if count == 0:
            raise ToolError(
                f"Der Text aus 'alt' kommt in {path} nicht vor. Genau so übernehmen wie in datei_lesen "
                "(Leerzeichen, Einrückung, Zeilenumbrüche), ohne Zeilennummern."
            )
        if count > 1 and not alle_vorkommen:
            raise ToolError(
                f"Der Text aus 'alt' kommt {count}-mal in {path} vor. Mehr umgebenden Text angeben oder alle_vorkommen=true."
            )
        updated = content.replace(old, new) if alle_vorkommen else content.replace(old, new, 1)
        notes = check_content(path, updated)
        if count > 1:
            notes.append(f"{count} Vorkommen ersetzt.")
        return {path: updated}, notes

    return await commit_change(beschreibung, make)


@tool("Datei löschen", destructive=True)
async def datei_loeschen(
    pfad: Annotated[str, Field(description="Pfad relativ zum Repo")],
    beschreibung: Annotated[str, Field(description="Kurze Begründung für den Verlauf")],
) -> str:
    """Datei aus dem Repo löschen (ein Commit, mit aenderung_zuruecknehmen wiederherstellbar). Erst nach deployen live."""
    path = writable_path(pfad)

    async def make(files: dict[str, dict]) -> tuple[Changes, list[str]]:
        if path not in files:
            raise ToolError(f"Die Datei {path} gibt es nicht.")
        return {path: None}, []

    return await commit_change(beschreibung, make)


@tool("Datei verschieben")
async def datei_verschieben(
    von: Annotated[str, Field(description="Bisheriger Pfad")],
    nach: Annotated[str, Field(description="Neuer Pfad")],
    beschreibung: Annotated[str, Field(description="Kurze Begründung für den Verlauf")],
) -> str:
    """Datei umbenennen oder verschieben (ein Commit). Verweise darauf (z. B. im Dockerfile) selbst anpassen."""
    source, target = writable_path(von), writable_path(nach)
    if source == target:
        raise ToolError("Quelle und Ziel sind gleich.")

    async def make(files: dict[str, dict]) -> tuple[Changes, list[str]]:
        content = await read_text(files, source)
        if target in files:
            raise ToolError(f"{target} gibt es schon.")
        return {target: content, source: None}, check_content(target, content)

    return await commit_change(beschreibung, make)


# ---------- Tools: Verlauf ----------


@tool("Änderungsverlauf", read_only=True)
async def aenderungen_liste(
    anzahl: Annotated[int, Field(description="Wie viele Commits (max. 50)", ge=1, le=50)] = 15,
) -> str:
    """Letzte Änderungen (Commits) am Bot, neueste zuerst, mit Kennung für aenderung_anzeigen / aenderung_zuruecknehmen."""
    commits = await github.commits(anzahl)
    return "\n".join(
        f"{c['sha'][:7]} · {local_time(c['commit']['author'].get('date'))} · {c['commit']['author'].get('name')} · "
        f"{first_line(c['commit']['message'])}"
        for c in commits
    ) or "Keine Commits."


@tool("Änderung anzeigen", read_only=True)
async def aenderung_anzeigen(
    commit: Annotated[str, Field(description="Commit-Kennung aus aenderungen_liste (z. B. 284e7e1)")],
) -> str:
    """Zeigt eine Änderung (Commit) mit Beschreibung und Diff aller Dateien."""
    data = await github.commit_details(commit.strip())
    info = data["commit"]
    lines = [
        f"Commit {data['sha'][:7]} · {local_time(info['author'].get('date'))} · {info['author'].get('name')}",
        info["message"].strip(),
        "",
    ]
    for f in data.get("files", []):
        renamed = f" (vorher {f['previous_filename']})" if f.get("previous_filename") else ""
        lines.append(f"=== {f['filename']}{renamed} · {f['status']} · +{f.get('additions', 0)} −{f.get('deletions', 0)}")
        lines.append(f.get("patch") or "(kein Text-Diff verfügbar)")
        lines.append("")
    return clip(mask("\n".join(lines)))


@tool("Änderung zurücknehmen", destructive=True)
async def aenderung_zuruecknehmen(
    commit: Annotated[str, Field(description="Commit-Kennung aus aenderungen_liste")],
    erzwingen: Annotated[
        bool, Field(description="Auch dann zurücknehmen, wenn die Dateien danach noch einmal geändert wurden (überschreibt diese späteren Änderungen)")
    ] = False,
) -> str:
    """Nimmt eine Änderung zurück: Alle Dateien dieses Commits bekommen wieder den Stand davor (als neuer Commit).
    Danach deployen, damit es live geht."""
    sha = await github.resolve(commit.strip())
    target = await github.git_commit(sha)
    parents = target.get("parents") or []
    if len(parents) != 1:
        raise ToolError("Dieser Commit kann nicht zurückgenommen werden (erster Commit oder Merge-Commit).")
    parent = await github.git_commit(parents[0]["sha"])
    target_tree, parent_tree = await asyncio.gather(github.tree(target["tree"]["sha"]), github.tree(parent["tree"]["sha"]))
    changed = sorted(p for p in set(target_tree) | set(parent_tree) if target_tree.get(p, {}).get("sha") != parent_tree.get(p, {}).get("sha"))
    if not changed:
        raise ToolError("Dieser Commit enthält keine Dateiänderungen.")
    protected = [p for p in changed if is_protected(p)]
    if protected:
        raise ToolError(f"Dieser Commit ändert geschützte Dateien ({', '.join(protected)}) und kann nur direkt im Repo zurückgenommen werden.")

    for _ in range(3):
        head, head_tree_sha = await github.head()
        head_tree = await github.tree(head_tree_sha)
        later = [p for p in changed if head_tree.get(p, {}).get("sha") != target_tree.get(p, {}).get("sha")]
        if later and not erzwingen:
            raise ToolError(
                "Diese Dateien wurden nach dem Commit noch einmal geändert: " + ", ".join(later) + ". "
                "Zurücknehmen würde auch die späteren Änderungen daran überschreiben. Entweder die späteren Commits zuerst "
                "zurücknehmen (neueste zuerst) oder mit erzwingen=true trotzdem den Stand vor diesem Commit herstellen."
            )
        entries = []
        for p in changed:
            if p in parent_tree:
                if head_tree.get(p, {}).get("sha") != parent_tree[p]["sha"]:
                    entries.append({"path": p, "mode": parent_tree[p]["mode"], "type": "blob", "sha": parent_tree[p]["sha"]})
            elif p in head_tree:
                entries.append({"path": p, "mode": head_tree[p]["mode"], "type": "blob", "sha": None})
        if not entries:
            return "Nichts zu tun: Die Dateien haben bereits den Stand vor diesem Commit."
        message = f"Zurückgenommen: {first_line(target['message'])}\n\nNimmt {sha[:7]} zurück.\n\nÜber Bot-Connector · {user()}"
        try:
            new_sha = await github.commit(head, head_tree_sha, entries, message)
        except RefConflict:
            continue
        log.info("Commit %s von %s: %s zurückgenommen", new_sha[:7], user(), sha[:7])
        files = ", ".join(e["path"] for e in entries)
        return (
            f"✓ {sha[:7]} „{first_line(target['message'])}“ zurückgenommen (Commit {new_sha[:7]}): {files}\n"
            "Noch nicht live → deployen"
        )
    raise ToolError("Das Repo wurde gerade gleichzeitig geändert. Bitte noch einmal versuchen.")


# ---------- Tools: Deploy und Betrieb ----------


@tool("Deployen")
async def deployen(
    trotz_fehlern: Annotated[bool, Field(description="Auch deployen, wenn die Prüfung Syntaxfehler findet (nicht empfohlen)")] = False,
) -> str:
    """Aktuellen Stand des Branches live schalten (Coolify baut und startet den Bot neu, meist 1–3 Minuten).
    Prüft vorher alle Python-/YAML-Dateien. Danach deploy_status aufrufen."""
    global last_deployment
    head, errors, warnings = await check_repo()
    if errors and not trotz_fehlern:
        raise ToolError("Deploy abgebrochen, der Code enthält Fehler:\n- " + "\n- ".join(errors))
    data = await coolify.deploy()
    data = data if isinstance(data, dict) else {}
    deployments = data.get("deployments") or []
    deployment = deployments[0] if deployments else {}
    last_deployment = deployment.get("deployment_uuid") or last_deployment
    log.info("Deploy %s gestartet von %s (Commit %s)", last_deployment, user(), head[:7])
    lines = [
        f"🚀 Deploy gestartet für Commit {head[:7]} (ID {deployment.get('deployment_uuid') or '?'}).",
        str(deployment.get("message") or data.get("message") or "").strip(),
        "Dauert meist 1–3 Minuten. Jetzt deploy_status aufrufen.",
        *warnings,
        *(f"⚠ {e}" for e in errors),
    ]
    return "\n".join(line for line in lines if line)


@tool("Deploy-Status", read_only=True)
async def deploy_status(
    deployment_id: Annotated[str | None, Field(description="ID aus deployen (leer = letzter Deploy)")] = None,
    warten_sekunden: Annotated[int, Field(description="So lange auf das Ende warten (0–50)", ge=0, le=50)] = 45,
) -> str:
    """Status eines Deploys mit den letzten Build-Logzeilen. Wartet bis zu warten_sekunden, bis er fertig ist."""
    uuid = (deployment_id or "").strip() or last_deployment
    if not uuid:
        latest = await coolify.deployments(1)
        if not latest:
            return "Noch kein Deploy gefunden."
        uuid = latest[0].get("deployment_uuid")
    deadline = time.monotonic() + warten_sekunden
    while True:
        deployment = await coolify.deployment(uuid)
        status = deployment.get("status") or "?"
        if status in TERMINAL_DEPLOY_STATES or time.monotonic() >= deadline:
            break
        await asyncio.sleep(5)

    lines = [
        f"{DEPLOY_ICONS.get(status, '•')} Deploy {uuid}: {status}",
        f"Commit {str(deployment.get('commit') or '?')[:7]} · gestartet {local_time(deployment.get('created_at'))}",
    ]
    log_lines = deploy_log_lines(deployment, 60 if status == "failed" else 25)
    if log_lines:
        lines += ["", "Build-Log (Ende):", *log_lines]
    if status == "finished":
        try:
            health = await bot.request("GET", "/health", with_token=False)
            lines += ["", f"/health: HTTP {health.status_code} {health.text[:100]}"]
        except ToolError as e:
            lines += ["", f"/health: {e}"]
        lines.append("Falls der Bot nicht antwortet, kurz warten oder logs prüfen.")
    elif status == "failed":
        lines += ["", "Build fehlgeschlagen, es läuft weiter der vorherige Stand. Fehler beheben und erneut deployen."]
    elif status not in TERMINAL_DEPLOY_STATES:
        lines += ["", "Läuft noch → deploy_status gleich noch einmal aufrufen."]
    return clip("\n".join(lines))


@tool("Container-Logs", read_only=True)
async def logs(
    dienst: Annotated[Literal["webhook", "waha"], Field(description="webhook = Bot, waha = WhatsApp-Gateway")] = "webhook",
    zeilen: Annotated[int, Field(description="Anzahl letzter Zeilen (10–1000)", ge=10, le=1000)] = 100,
    filter: Annotated[str | None, Field(description="Nur Zeilen mit diesem Text (Groß-/Kleinschreibung egal)")] = None,
) -> str:
    """Letzte Log-Zeilen eines Containers (Fehler, gesendete Nachrichten, Reports)."""
    text = mask(await coolify.logs(dienst, zeilen))
    if filter:
        needle = filter.casefold()
        text = "\n".join(line for line in text.splitlines() if needle in line.casefold())
    if not text.strip():
        return "Keine passenden Log-Zeilen."
    if len(text) > MAX_OUTPUT:
        text = "… (ältere Zeilen gekürzt)\n" + text[-MAX_OUTPUT:]
    return text


@tool("Variablen anzeigen", read_only=True)
async def variablen_liste() -> str:
    """Umgebungsvariablen des Bots in Coolify (Einstellungen wie MESSAGE_TEMPLATE, REPORT_TIME). Geheime Werte werden
    maskiert. Zeigt auch, welche Variablen docker-compose.yml verwendet."""
    envs, (_, files) = await asyncio.gather(coolify.envs(), head_files())
    used = await compose_variables(files) or set()
    lines = []
    keys = set()
    for e in sorted(envs, key=lambda e: e.get("key", "")):
        key = e.get("key", "")
        keys.add(key)
        value = e.get("value")
        if key in PROTECTED_VARS or SECRET_NAME.search(key):
            shown = "•••• (gesetzt)" if value else "(leer)"
        else:
            shown = mask(str(value if value is not None else ""))[:300] or "(leer)"
        flags = []
        if key in PROTECTED_VARS:
            flags.append("geschützt")
        if used and key not in used and not key.startswith(("SERVICE_", "COOLIFY_")):
            flags.append("nicht in docker-compose.yml verwendet")
        lines.append(f"{key} = {shown}{'  [' + ', '.join(flags) + ']' if flags else ''}")
    missing = sorted(used - keys)
    if missing:
        lines += ["", "In docker-compose.yml verwendet, in Coolify nicht gesetzt (Default greift): " + ", ".join(missing)]
    lines += ["", "Änderungen an Variablen werden erst mit dem nächsten deployen wirksam."]
    return "\n".join(lines)


@tool("Variable setzen")
async def variable_setzen(
    name: Annotated[str, Field(description="Name in Großbuchstaben, z. B. REPORT_TIME")],
    wert: Annotated[str, Field(description="Neuer Wert (\\n für Zeilenumbrüche in MESSAGE_TEMPLATE)")],
) -> str:
    """Umgebungsvariable des Bots in Coolify anlegen oder ändern. Wirksam nach dem nächsten deployen.
    Neue Variablen müssen zusätzlich in docker-compose.yml unter services.webhook.environment stehen."""
    key = name.strip()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
        raise ToolError("Variablennamen bestehen aus Großbuchstaben, Ziffern und _ (z. B. REPORT_TIME).")
    if key in PROTECTED_VARS:
        raise ToolError(f"{key} ist geschützt und kann nur in Coolify direkt geändert werden.")
    if any(secret in wert for secret in KNOWN_SECRETS):
        raise ToolError("Abgelehnt: Der Wert enthält einen Token oder ein Passwort des Connectors.")
    envs, (_, files) = await asyncio.gather(coolify.envs(), head_files())
    exists = any(e.get("key") == key for e in envs)
    if exists:
        await coolify.update_env(key, wert)
    else:
        await coolify.create_env(key, wert)
    log.info("Variable %s %s von %s", key, "geändert" if exists else "angelegt", user())
    lines = [f"✓ {key} {'geändert' if exists else 'angelegt'}. Wirksam nach dem nächsten deployen."]
    used = await compose_variables(files)
    if used is not None and key not in used:
        lines.append(
            f"⚠ {key} steht noch nicht in docker-compose.yml. Dort unter services.webhook.environment ergänzen "
            f"({key}: ${{{key}}}), sonst kommt der Wert nicht im Container an."
        )
    return "\n".join(lines)


@tool("Variable löschen", destructive=True)
async def variable_loeschen(
    name: Annotated[str, Field(description="Name der Variable")],
) -> str:
    """Umgebungsvariable des Bots in Coolify löschen (danach greift der Default aus docker-compose.yml). Wirksam nach deployen."""
    key = name.strip()
    if key in PROTECTED_VARS:
        raise ToolError(f"{key} ist geschützt und kann nur in Coolify direkt geändert werden.")
    env_entry = next((e for e in await coolify.envs() if e.get("key") == key), None)
    if not env_entry:
        raise ToolError(f"Die Variable {key} gibt es nicht.")
    await coolify.delete_env(env_entry["uuid"])
    log.info("Variable %s gelöscht von %s", key, user())
    return f"✓ {key} gelöscht. Wirksam nach dem nächsten deployen."


@tool("Bot-Endpoint aufrufen")
async def bot_aufrufen(
    methode: Annotated[Literal["GET", "POST", "PUT", "PATCH", "DELETE"], Field(description="HTTP-Methode")],
    pfad: Annotated[str, Field(description="Pfad mit / am Anfang, z. B. /health oder /report/daily?preview=1")],
    body: Annotated[Any, Field(description="JSON-Body (Objekt oder Liste), leer = kein Body")] = None,
    query: Annotated[dict[str, str] | None, Field(description="Query-Parameter")] = None,
    mit_token: Annotated[bool, Field(description="WEBHOOK_TOKEN als 'Authorization: Bearer' mitschicken")] = True,
) -> str:
    """HTTP-Anfrage an den laufenden Bot (öffentliche URL), um Endpoints zu testen. Achtung: POST /webhook/termin und
    /report/... ohne ?preview=1 senden echte Nachrichten in die WhatsApp-Gruppe."""
    path = pfad.strip()
    if not re.fullmatch(r"/[^\s#]*", path) or path.startswith("//") or "://" in path:
        raise ToolError("pfad muss mit / beginnen und darf nur den Pfad enthalten, z. B. /health")
    started = time.monotonic()
    r = await bot.request(methode, path, body=body, query=query, with_token=mit_token)
    elapsed = int((time.monotonic() - started) * 1000)
    content_type = r.headers.get("content-type", "")
    text = r.text
    if "json" in content_type:
        try:
            text = json.dumps(r.json(), ensure_ascii=False, indent=2)
        except ValueError:
            pass
    log.info("bot_aufrufen von %s: %s %s -> %d", user(), methode, path.split("?")[0], r.status_code)
    lines = [f"HTTP {r.status_code} {r.reason_phrase} · {elapsed} ms · {methode} {BOT_URL}{path}"]
    if content_type:
        lines.append(f"Content-Type: {content_type}")
    if r.headers.get("location"):
        lines.append(f"Location: {r.headers['location']}")
    lines += ["", clip(mask(text), 20_000) or "(leerer Body)"]
    return "\n".join(lines)


# ---------- HTTP-Routen außerhalb von MCP ----------


@mcp.custom_route("/login", methods=["GET", "POST"])
async def login(request: Request) -> Response:
    return await provider.login(request)


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> Response:
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/", methods=["GET"])
async def home(request: Request) -> Response:
    return PlainTextResponse(f"Bot-Connector läuft. MCP-URL für Claude: {PUBLIC_URL}/mcp\n")


app = mcp.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0")
log.info("Connector bereit: %s/mcp (Repo %s, Branch %s, Bot %s)", PUBLIC_URL, GITHUB_REPO, GITHUB_BRANCH, BOT_URL)
