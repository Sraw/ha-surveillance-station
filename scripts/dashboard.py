#!/usr/bin/env python3
"""Create (if missing) and write the test dashboard from dashboards/<file>.json.

    HA_URL=http://host:8123 python3 scripts/dashboard.py [dashboards/ss-playback.json]

Token: $HA_TOKEN, or the first line of ~/.ha_token. Needs websocket-client.
The dashboard's url_path is the file stem (ss-playback).
"""

import itertools
import json
import os
from pathlib import Path
import sys

import websocket

HERE = Path(__file__).resolve().parent.parent


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "dashboards" / "ss-playback.json"
    config = json.loads(path.read_text())
    url_path = path.stem
    base = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
    token = os.environ.get("HA_TOKEN") or (Path.home() / ".ha_token").read_text().split()[0]

    ws = websocket.create_connection(base.replace("http", "ws", 1) + "/api/websocket", timeout=30)
    ids = itertools.count(1)

    def call(**cmd):
        cmd["id"] = next(ids)
        ws.send(json.dumps(cmd))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == cmd["id"]:
                if not msg.get("success"):
                    raise SystemExit(f"{cmd['type']} failed: {msg.get('error')}")
                return msg.get("result")

    assert json.loads(ws.recv())["type"] == "auth_required"
    ws.send(json.dumps({"type": "auth", "access_token": token}))
    if json.loads(ws.recv())["type"] != "auth_ok":
        raise SystemExit("auth failed")

    existing = {d["url_path"] for d in call(type="lovelace/dashboards/list")}
    if url_path not in existing:
        call(
            type="lovelace/dashboards/create",
            url_path=url_path,
            title=config.get("title", url_path),
            icon="mdi:cctv",
            mode="storage",
            show_in_sidebar=True,
            require_admin=False,
        )
        print(f"created dashboard {url_path}")
    call(type="lovelace/config/save", url_path=url_path, config=config)
    print(f"saved {path.name} -> /{url_path}")
    ws.close()


if __name__ == "__main__":
    main()
