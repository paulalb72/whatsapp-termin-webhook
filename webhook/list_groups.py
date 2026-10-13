"""Listet alle WhatsApp-Gruppen der WAHA-Session mit Name und ID (…@g.us).

Aufruf auf dem Server:  docker compose exec webhook python list_groups.py
"""

import json
import os
import sys
import urllib.error
import urllib.request

url = (os.environ.get("WAHA_URL") or "http://waha:3000").rstrip("/")
session = os.environ.get("WAHA_SESSION") or "default"
request = urllib.request.Request(
    f"{url}/api/{session}/groups",
    headers={"X-Api-Key": os.environ.get("WAHA_API_KEY", ""), "Accept": "application/json"},
)

try:
    with urllib.request.urlopen(request, timeout=60) as response:
        data = json.load(response)
except urllib.error.HTTPError as e:
    sys.exit(f"WAHA antwortet mit HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")
except OSError as e:
    sys.exit(f"WAHA nicht erreichbar: {e}")


def group_id(group: dict) -> str:
    # Je nach WAHA-Engine ist die ID ein String oder ein Objekt
    gid = group.get("id") or group.get("JID") or ""
    if isinstance(gid, dict):
        gid = gid.get("_serialized") or f"{gid.get('user', '')}@{gid.get('server', '')}"
    return str(gid)


def group_name(group: dict) -> str:
    return (
        group.get("name")
        or group.get("subject")
        or group.get("Name")
        or (group.get("groupMetadata") or {}).get("subject")
        or "(ohne Namen)"
    )


items = data.values() if isinstance(data, dict) else data
groups = sorted(
    ((group_name(g), group_id(g)) for g in items if isinstance(g, dict)),
    key=lambda g: g[0].casefold(),
)

if not groups:
    sys.exit("Keine Gruppen gefunden. Ist die Session verbunden (Status WORKING) und die Nummer Mitglied einer Gruppe?")

width = max(len(name) for name, _ in groups)
for name, gid in groups:
    print(f"{name:<{width}}  {gid}")
