"""HTTP-Clients: GitHub (Code des Bots), Coolify (Deploy, Logs, Variablen) und der Bot selbst."""

import base64
from typing import Any

import httpx

from mcp.server.mcpserver.exceptions import ToolError


def _message(r: httpx.Response) -> str:
    try:
        data = r.json()
    except ValueError:
        return r.text[:500]
    if isinstance(data, dict):
        text = str(data.get("message") or data.get("error") or "")
        if data.get("errors"):
            text += f" {data['errors']}"
        return text[:500]
    return str(data)[:500]


class UpstreamError(ToolError):
    def __init__(self, service: str, status: int, message: str):
        super().__init__(f"{service}: HTTP {status} – {message}")
        self.status = status


async def _request(http: httpx.AsyncClient, service: str, method: str, path: str, **kwargs) -> Any:
    try:
        r = await http.request(method, path, **kwargs)
    except httpx.HTTPError as e:
        raise ToolError(f"{service} nicht erreichbar: {type(e).__name__}: {e}") from e
    if r.status_code >= 400:
        raise UpstreamError(service, r.status_code, _message(r))
    if not r.content:
        return None
    try:
        return r.json()
    except ValueError:
        return r.text


class RefConflict(Exception):
    """Der Branch hat sich zwischen Lesen und Schreiben geändert."""


class GitHub:
    def __init__(self, token: str, repo: str, branch: str, api_url: str = "https://api.github.com"):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "whatsapp-bot-connector",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.http = httpx.AsyncClient(base_url=f"{api_url.rstrip('/')}/repos/{repo}", headers=headers, timeout=30)
        self.repo = repo
        self.branch = branch
        self._blobs: dict[str, bytes] = {}

    async def _req(self, method: str, path: str, **kwargs) -> Any:
        try:
            return await _request(self.http, "GitHub", method, path, **kwargs)
        except UpstreamError as e:
            if e.status == 401:
                raise ToolError("GitHub: Token ungültig oder abgelaufen (GITHUB_TOKEN des Connectors erneuern).") from e
            if e.status == 403 and method != "GET":
                raise ToolError(
                    "GitHub: Der Token darf nicht schreiben (Fine-grained Token braucht 'Contents: Read and write' "
                    f"für {self.repo}). {e}"
                ) from e
            raise

    async def head(self) -> tuple[str, str]:
        """(Commit-SHA, Tree-SHA) des Branches."""
        ref = await self._req("GET", f"/git/ref/heads/{self.branch}")
        commit_sha = ref["object"]["sha"]
        commit = await self.git_commit(commit_sha)
        return commit_sha, commit["tree"]["sha"]

    async def git_commit(self, sha: str) -> dict:
        return await self._req("GET", f"/git/commits/{sha}")

    async def resolve(self, ref: str) -> str:
        """Kurzen SHA (oder Branch) in den vollen Commit-SHA auflösen."""
        data = await self._req("GET", f"/commits/{ref}", headers={"Accept": "application/vnd.github.sha"})
        return str(data).strip()

    async def tree(self, tree_sha: str) -> dict[str, dict]:
        """Alle Dateien: Pfad -> {sha, mode, size}."""
        data = await self._req("GET", f"/git/trees/{tree_sha}", params={"recursive": "1"})
        if data.get("truncated"):
            raise ToolError("GitHub hat den Dateibaum gekürzt (Repo zu groß).")
        return {e["path"]: e for e in data["tree"] if e["type"] == "blob"}

    async def blob(self, sha: str) -> bytes:
        if sha not in self._blobs:
            data = await self._req("GET", f"/git/blobs/{sha}")
            self._blobs[sha] = base64.b64decode(data["content"])
        return self._blobs[sha]

    async def text(self, sha: str) -> str | None:
        """Inhalt als Text, None bei Binärdateien."""
        raw = await self.blob(sha)
        if b"\x00" in raw:
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None

    async def commit(self, parent: str, base_tree: str, entries: list[dict], message: str) -> str:
        """Neuen Commit auf dem Branch anlegen. RefConflict, wenn der Branch inzwischen weiter ist."""
        tree = await self._req("POST", "/git/trees", json={"base_tree": base_tree, "tree": entries})
        commit = await self._req("POST", "/git/commits", json={"message": message, "tree": tree["sha"], "parents": [parent]})
        try:
            await self._req("PATCH", f"/git/refs/heads/{self.branch}", json={"sha": commit["sha"], "force": False})
        except UpstreamError as e:
            if e.status in (409, 422):
                raise RefConflict() from e
            raise
        return commit["sha"]

    async def commits(self, count: int) -> list[dict]:
        return await self._req("GET", "/commits", params={"sha": self.branch, "per_page": count})

    async def commit_details(self, sha: str) -> dict:
        return await self._req("GET", f"/commits/{sha}")


class Coolify:
    def __init__(self, url: str, token: str, app_uuid: str):
        self.http = httpx.AsyncClient(
            base_url=f"{url.rstrip('/')}/api/v1",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=30,
        )
        self.app = app_uuid

    async def _req(self, method: str, path: str, **kwargs) -> Any:
        try:
            return await _request(self.http, "Coolify", method, path, **kwargs)
        except UpstreamError as e:
            if e.status == 401:
                raise ToolError("Coolify: Token ungültig (BOT_COOLIFY_TOKEN des Connectors prüfen).") from e
            if e.status == 403:
                raise ToolError(
                    "Coolify: Zugriff verweigert. Ist die API in Coolify aktiviert (Settings → Advanced → API Access) "
                    f"und hat der Token die Rechte read, read:sensitive, write und deploy? ({e})"
                ) from e
            raise

    async def application(self) -> dict:
        return await self._req("GET", f"/applications/{self.app}")

    async def deploy(self) -> dict:
        try:
            return await self._req("POST", "/deploy", params={"uuid": self.app})
        except UpstreamError as e:
            if e.status != 405:
                raise
            return await self._req("GET", "/deploy", params={"uuid": self.app})  # ältere Coolify-Versionen

    async def deployment(self, deployment_uuid: str) -> dict:
        return await self._req("GET", f"/deployments/{deployment_uuid}")

    async def deployments(self, take: int = 5) -> list[dict]:
        data = await self._req("GET", f"/deployments/applications/{self.app}", params={"skip": 0, "take": take})
        if isinstance(data, dict):
            data = data.get("deployments") or []
        return data if isinstance(data, list) else []

    async def logs(self, service: str, lines: int) -> str:
        data = await self._req("GET", f"/applications/{self.app}/logs", params={"lines": lines, "service_name": service})
        if isinstance(data, dict):
            return str(data.get("logs") or "")
        return str(data or "")

    async def envs(self) -> list[dict]:
        data = await self._req("GET", f"/applications/{self.app}/envs")
        return [e for e in data or [] if not e.get("is_preview")]

    async def create_env(self, key: str, value: str) -> None:
        await self._req("POST", f"/applications/{self.app}/envs", json={"key": key, "value": value})

    async def update_env(self, key: str, value: str) -> None:
        await self._req("PATCH", f"/applications/{self.app}/envs", json={"key": key, "value": value})

    async def delete_env(self, env_uuid: str) -> None:
        await self._req("DELETE", f"/applications/{self.app}/envs/{env_uuid}")


class Bot:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token
        self.http = httpx.AsyncClient(base_url=self.url, timeout=30, follow_redirects=False)

    async def request(
        self, method: str, path: str, body: Any = None, query: dict | None = None, with_token: bool = True
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.token}"} if with_token else {}
        kwargs: dict[str, Any] = {"headers": headers, "params": query or None}
        if body is not None:
            kwargs["json"] = body
        try:
            return await self.http.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise ToolError(f"Bot nicht erreichbar: {type(e).__name__}: {e}") from e
