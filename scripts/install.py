#!/usr/bin/env python3
"""Install and set up Surveillance Station Playback on a Home Assistant.

    export HA_TOKEN_FILE=~/.ha_token          # a long-lived token of an HA administrator
    scripts/install.py --ha-url http://homeassistant.local:8123 --check
    scripts/install.py --ha-url http://homeassistant.local:8123 --restart \\
        --ss-host nas.local --ss-user ha-playback --dashboard

It talks to Home Assistant's own API (REST and WebSocket), so it runs from any
machine that reaches HA; only --config-dir needs HA's files. Needs Python 3.9+
and nothing else. Every step looks first and changes only what is missing, so
running it again is safe. Steps, in order (each needs its flags, see --help):

1. Look: HA's version, that the token is an administrator's, what is there.
2. Files: through HACS if HA has it, or copied into --config-dir (from the
   checkout this script is in, else from the release on GitHub).
3. Restart HA (only with --restart, and only if step 2 changed something).
4. The NAS: the integration's config flow (--ss-host, --ss-user), unless one
   is set up already.
5. A dashboard with the card (--dashboard).
6. Options: Frigate detections, video transcoding (--frigate, ...).
7. Phone notifications from the blueprint (--notify-device).
8. Check what was set up.

Secrets never go on the command line (other users see it in `ps`, and the
shell keeps it in its history): the HA token comes from $HA_TOKEN or the file
$HA_TOKEN_FILE / --ha-token-file, the DSM password from $SS_PASSWORD or
--ss-password-file; either is asked for on a terminal otherwise.

Exit status: 0 done, 1 failed, 2 stopped for something only you can do.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import gzip
import http.client
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import struct
import sys
import tarfile
import tempfile
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import zlib

DOMAIN = "surveillance_station"
REPOSITORY = "Sraw/ha-surveillance-station"
MIN_HA = "2026.9.0"  # hacs.json's "homeassistant" (tests/test_install_script.py compares them)
MIN_FRIGATE = "0.14"  # the reviews topic
BLUEPRINT_URL = (
    f"https://github.com/{REPOSITORY}/blob/main/blueprints/automation/{DOMAIN}/detection_notification.yaml"
)
# Options the flags below set; anything else keeps its current value.
OPTION_FLAGS = ("frigate", "frigate_topic", "frigate_objects", "frigate_link", "frigate_url", "transcoder")

SETUP_ERRORS = {
    "cannot_connect": (
        "Home Assistant cannot reach Surveillance Station there. Check the host, the port (5001 for HTTPS, "
        "5000 for HTTP) and --ss-http, and that the machine HA runs on, not this one, reaches the NAS."
    ),
    "invalid_auth": (
        "DSM rejected the login. Check the user name and the password; DSM also blocks an address after "
        "several failed logins (Control Panel > Security > Protection)."
    ),
    "otp_required": (
        "This DSM account asks for a two-step verification code. Turn it off for this dedicated account, "
        "or exempt the account from a policy that enforces it."
    ),
    "no_cameras": (
        "Logged in, but the account sees no cameras. Give it a Surveillance Station privilege profile "
        "that may view and play back the cameras."
    ),
    "unsupported": (
        "This Surveillance Station lacks Web APIs the integration needs (Home Assistant's log names "
        "them). Update it to Surveillance Station 9."
    ),
    "unknown": "Surveillance Station refused the setup calls; Home Assistant's log has the error.",
}
OPTION_ERRORS = {
    "invalid_topic": "--frigate-topic: a topic prefix without wildcards (# or +).",
    "invalid_link": "--link: a path starting with /, such as /ss-playback/playback.",
    "no_objects": "--frigate-objects: at least one Frigate label.",
    "invalid_url": "--frigate-url: an http:// or https:// URL without a login in it, such as http://frigate:5000.",
    "duplicate_camera": "--camera-map: a Frigate camera can go to one Surveillance Station camera only.",
}


class Fail(Exception):
    """The step can't go on; the text says what to change."""


class NeedsUser(Exception):
    """Stopped for something only the user can do (exit status 2)."""


class Report:
    def __init__(self) -> None:
        self.todo: list[str] = []
        self.warnings = 0

    def line(self, mark: str, text: str) -> None:
        print(f"[{mark}] {text}", flush=True)

    def ok(self, text: str) -> None:
        self.line("ok", text)

    def skip(self, text: str) -> None:
        self.line("skip", text)

    def warn(self, text: str) -> None:
        self.warnings += 1
        self.line("warn", text)

    def later(self, text: str) -> None:
        """Something left for the user, listed again at the end."""
        self.todo.append(text)


def version_tuple(text: str) -> tuple[int, ...]:
    """"2026.10.0b1" -> (2026, 10, 0): the leading number of each dotted part."""
    out = []
    for part in str(text).split("."):
        digits = re.match(r"\d+", part)
        if not digits:
            break
        out.append(int(digits.group()))
    return tuple(out)


def camera_key(name: str) -> str:
    """How the integration matches camera names: without case, spaces or punctuation."""
    return re.sub(r"[\W_]", "", name.casefold())


def read_secret(env: str, path: str | None, flag: str, prompt: str, strip: bool = True) -> str:
    """A secret from the environment, a file (its first line), or the terminal.

    ``strip=False`` keeps the spaces around it: a password may begin or end with one. Messages name
    ``flag``, not the path: the secret itself is sometimes passed where its file was asked for.
    """
    if value := os.environ.get(env, ""):
        if value.strip():
            return value.strip() if strip else value
    if path:
        try:
            lines = Path(path).expanduser().read_text(encoding="utf-8-sig").splitlines()
        except OSError as err:
            raise Fail(f"{flag}: cannot read that file ({err.strerror}).") from None
        except UnicodeError:
            raise Fail(f"{flag}: that file is not text (UTF-8).") from None
        if lines and lines[0].strip():
            return lines[0].strip() if strip else lines[0]
        raise Fail(f"{flag}: that file is empty.")
    if sys.stdin.isatty():
        if value := getpass.getpass(f"{prompt}: "):
            if value.strip():
                return value.strip() if strip else value
    raise Fail(f"{prompt}: set ${env}, or pass the file that holds it as {flag}.")


class WebSocket:
    """A client for Home Assistant's WebSocket API: text messages, unfragmented sends."""

    def __init__(self, url: str, context: ssl.SSLContext | None, timeout: float) -> None:
        parts = urllib.parse.urlsplit(url)
        secure = parts.scheme == "https"
        port = parts.port or (443 if secure else 80)
        sock = socket.create_connection((parts.hostname, port), timeout=timeout)
        if secure:
            sock = (context or ssl.create_default_context()).wrap_socket(sock, server_hostname=parts.hostname)
        self._sock = sock
        self._file = sock.makefile("rb")
        key = base64.b64encode(os.urandom(16)).decode()
        path = parts.path.rstrip("/") + "/api/websocket"
        sock.sendall(
            (
                f"GET {path} HTTP/1.1\r\nHost: {parts.netloc}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        status = self._file.readline()
        if b" 101 " not in status:
            raise OSError(f"no WebSocket there ({status.decode(errors='replace').strip()[:80]})")
        while self._file.readline() not in (b"\r\n", b"\n", b""):
            pass

    def _frame(self, opcode: int, payload: bytes) -> None:
        head = bytes([0x80 | opcode])
        size = len(payload)
        if size < 126:
            head += bytes([0x80 | size])
        elif size < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", size)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", size)
        mask = os.urandom(4)
        self._sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send(self, text: str) -> None:
        self._frame(0x1, text.encode())

    def _read(self, size: int) -> bytes:
        data = self._file.read(size)
        if data is None or len(data) < size:
            raise OSError("connection closed")
        return data

    def recv(self, deadline: float) -> str:
        """The next message, or ``socket.timeout`` once ``deadline`` (monotonic) has passed.

        A deadline, not a timeout per read: Home Assistant's pings would keep renewing that one.
        """
        message = b""
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise socket.timeout("no answer in time")
            self._sock.settimeout(left)
            first, second = self._read(2)
            size = second & 0x7F
            if size == 126:
                (size,) = struct.unpack(">H", self._read(2))
            elif size == 127:
                (size,) = struct.unpack(">Q", self._read(8))
            mask = self._read(4) if second & 0x80 else b""
            payload = self._read(size)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            opcode = first & 0x0F
            if opcode == 0x8:
                raise OSError("connection closed")
            if opcode == 0x9:
                self._frame(0xA, payload)
            elif opcode in (0x0, 0x1, 0x2):
                message += payload
                if first & 0x80:
                    return message.decode()

    def close(self) -> None:
        for part in (self._file, self._sock):  # the socket stays open for as long as its file is
            try:
                part.close()
            except OSError:
                pass


IDLE_SECONDS = 30


class CommandError(Exception):
    """A WebSocket command Home Assistant refused."""

    def __init__(self, command: str, code: str, message: str) -> None:
        super().__init__(f"{command}: {message or code}")
        self.code = code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is reported, not followed: following one would hand the token to wherever it points."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class HomeAssistant:
    """Home Assistant's REST and WebSocket APIs under one token.

    Answers are never printed as they come: a config flow's form echoes what
    was entered, the DSM password included.
    """

    def __init__(self, url: str, token: str, insecure: bool = False, timeout: float = 30) -> None:
        try:
            parts = urllib.parse.urlsplit(url)
            plain = parts.scheme in ("http", "https") and parts.hostname and parts.port != 0
        except ValueError:  # brackets or a port that are none
            plain = False
        if not plain or parts.username or parts.password:
            # Not repeated in the message: an address with a login in it is a secret.
            raise Fail(
                "--ha-url: an http:// or https:// URL without a login in it, such as "
                "http://homeassistant.local:8123."
            )
        self.url = url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._context: ssl.SSLContext | None = None
        if insecure:
            self._context = ssl.create_default_context()
            self._context.check_hostname = False
            self._context.verify_mode = ssl.CERT_NONE
        # No proxy from the environment: the WebSocket below connects directly, and the two must agree.
        handlers: list[Any] = [NoRedirect(), urllib.request.ProxyHandler({})]
        if self._context is not None:
            handlers.append(urllib.request.HTTPSHandler(context=self._context))
        self._opener = urllib.request.build_opener(*handlers)
        self._ws: WebSocket | None = None
        self._ws_used = 0.0
        self._ids = 0

    def request(self, method: str, path: str, body: Any = None, timeout: float | None = None) -> tuple[int, Any]:
        """(status, the JSON answer or None). Raises OSError if HA doesn't answer."""
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.url + path, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self._token}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(request, timeout=timeout or self._timeout) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as err:
            status, raw = err.code, err.read()
        except urllib.error.URLError as err:
            raise OSError(str(err.reason)) from None
        except http.client.HTTPException as err:  # cut off mid-answer, as by a restart
            raise OSError(type(err).__name__) from None
        try:
            return status, json.loads(raw) if raw else None
        except ValueError:
            return status, None

    def get(self, path: str) -> Any:
        """A GET that must succeed."""
        try:
            status, data = self.request("GET", path)
        except OSError as err:
            raise Fail(f"Home Assistant does not answer at {self.url} ({err}).") from None
        if status == 401:
            raise Fail(
                "Home Assistant rejected the token, or it is not an administrator's. Create a long-lived "
                "access token as an administrator (your profile > Security)."
            )
        if status in (301, 302, 303, 307, 308):
            raise Fail(f"{self.url} redirects elsewhere (HTTP {status}): pass the address it leads to as --ha-url.")
        if status != 200:
            raise Fail(f"GET {path}: HTTP {status}.")
        return data

    def post(self, path: str, body: Any = None, timeout: float | None = None) -> Any:
        try:
            status, data = self.request("POST", path, body or {}, timeout)
        except OSError as err:
            raise Fail(f"Home Assistant does not answer at {self.url} ({err}).") from None
        if status not in (200, 201):
            # Only the names of what a form rejected: its messages may quote what was entered.
            errors = data.get("errors") if isinstance(data, dict) else None
            fields = f" (rejected: {', '.join(sorted(errors))})" if isinstance(errors, dict) and errors else ""
            raise Fail(f"POST {path}: HTTP {status}{fields}.")
        return data

    def call(self, command: str, timeout: float | None = None, **params: Any) -> Any:
        """A WebSocket command's result."""
        # Home Assistant pings every 55 s and drops a client that didn't answer; this one answers only
        # while it waits for a result, so a connection left idle (a REST-only step) is opened anew.
        if self._ws is not None and time.monotonic() - self._ws_used > IDLE_SECONDS:
            self.close()
        try:
            if self._ws is None:
                self._ws = self._connect()
            self._ids += 1
            deadline = time.monotonic() + (timeout or self._timeout)
            self._ws.send(json.dumps({"id": self._ids, "type": command, **params}))
            while True:
                message = json.loads(self._ws.recv(deadline))
                if message.get("id") == self._ids and message.get("type") == "result":
                    break
        except (OSError, ValueError) as err:
            self.close()
            why = "no answer in time" if isinstance(err, socket.timeout) else err
            raise Fail(f"Home Assistant's WebSocket failed during {command} ({why}).") from None
        self._ws_used = time.monotonic()
        if not message.get("success"):
            error = message.get("error") or {}
            raise CommandError(command, str(error.get("code")), str(error.get("message") or ""))
        return message.get("result")

    def _connect(self) -> WebSocket:
        ws = WebSocket(self.url, self._context, self._timeout)
        try:
            deadline = time.monotonic() + self._timeout
            if json.loads(ws.recv(deadline)).get("type") != "auth_required":
                raise OSError("unexpected greeting")
            ws.send(json.dumps({"type": "auth", "access_token": self._token}))
            if json.loads(ws.recv(deadline)).get("type") != "auth_ok":
                raise OSError("token rejected")
        except BaseException:
            ws.close()
            raise
        return ws

    def close(self) -> None:
        if self._ws is not None:
            self._ws.close()
            self._ws = None


# --- 1. Look -----------------------------------------------------------------


def entries(ha: HomeAssistant, domain: str = DOMAIN) -> list[dict[str, Any]]:
    return ha.get(f"/api/config/config_entries/entry?domain={domain}") or []


def installed_version(ha: HomeAssistant) -> str | None:
    """The version Home Assistant has loaded, or None if it doesn't know the integration."""
    handlers = ha.get("/api/config/config_entries/flow_handlers") or []
    if DOMAIN not in handlers:
        return None
    try:
        return str(ha.call("manifest/get", integration=DOMAIN).get("version") or "?")
    except CommandError:
        return "?"


def look(ha: HomeAssistant, args: argparse.Namespace, report: Report) -> dict[str, Any]:
    config = ha.get("/api/config")
    if not isinstance(config, dict) or "version" not in config:
        raise Fail(f"{ha.url} does not answer like Home Assistant's API; check --ha-url.")
    version = str(config.get("version", ""))
    if version_tuple(version) < version_tuple(MIN_HA):
        raise Fail(f"Home Assistant {version} is too old: the integration needs {MIN_HA} or newer.")
    components = set(config.get("components") or [])
    entries(ha)  # administrators only: a plain user's token stops here
    report.ok(f"Home Assistant {version} at {ha.url}, administrator token")
    state = {
        "components": components,
        "hacs": "hacs" in components,
        "mqtt": any(e.get("state") == "loaded" for e in entries(ha, "mqtt")),
        "installed": installed_version(ha),
    }
    if state["installed"]:
        report.ok(f"integration files: version {state['installed']}")
    if args.frigate and not state["mqtt"]:
        raise Fail(
            "Home Assistant has no working MQTT integration, and Frigate's detections come over MQTT. Add it "
            "first (Settings > Devices & services > Add integration > MQTT), connected to the broker Frigate "
            "publishes to."
        )
    if args.notify_device and "mobile_app" not in components:
        raise Fail("--notify-device: no phone with the Home Assistant Companion app is connected to this HA.")
    return state


# --- 2. Files ----------------------------------------------------------------


def latest_release() -> str:
    request = urllib.request.Request(f"https://api.github.com/repos/{REPOSITORY}/releases/latest")
    request.add_header("Accept", "application/vnd.github+json")
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            return str(json.load(resp)["tag_name"]).removeprefix("v")
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as err:
        why = str(err) or type(err).__name__
        raise Fail(f"Cannot ask GitHub for the latest release ({why}); pass --version.") from None


def checkout() -> Path | None:
    """The integration in the checkout this script is part of, if it is in one."""
    try:
        root = Path(__file__).resolve().parent.parent
    except NameError:  # piped into python
        return None
    here = root / "custom_components" / DOMAIN
    # hacs.json too: a script saved into HA's config directory has the installed copy next to it.
    return here if (here / "manifest.json").is_file() and (root / "hacs.json").is_file() else None


def unpack_release(version: str, into: Path) -> Path:
    """Download a release and unpack its integration folder under ``into``."""
    url = f"https://github.com/{REPOSITORY}/archive/refs/tags/v{version}.tar.gz"
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            archive = resp.read()
    except (OSError, http.client.HTTPException) as err:
        raise Fail(f"Cannot download release {version} ({str(err) or type(err).__name__}).") from None
    target = into / DOMAIN
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            for member in tar:
                # <repository>-<version>/custom_components/<domain>/<file>: plain files, by plain names.
                parts = member.name.split("/")
                rest = parts[3:]
                if parts[1:3] != ["custom_components", DOMAIN] or not rest or not member.isfile():
                    continue
                if any(part in ("", ".", "..", "__pycache__") for part in rest):
                    continue
                path = target.joinpath(*rest)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(tar.extractfile(member).read())
    except (tarfile.TarError, EOFError, gzip.BadGzipFile, zlib.error) as err:
        raise Fail(f"Release {version} did not download as an archive ({type(err).__name__}).") from None
    if not (target / "manifest.json").is_file():
        raise Fail(f"Release {version} has no custom_components/{DOMAIN}.")
    return target


def manifest_version(folder: Path) -> str:
    try:
        return str(json.loads((folder / "manifest.json").read_text())["version"])
    except (OSError, ValueError, KeyError, TypeError):
        raise Fail(f"{folder / 'manifest.json'} does not say a version.") from None


def same_files(source: Path, dest: Path) -> bool:
    """Whether ``dest`` already holds exactly ``source``'s files (Python's caches aside)."""

    def listing(root: Path) -> dict[str, bytes]:
        return {
            str(p.relative_to(root)): p.read_bytes()
            for p in root.rglob("*")
            if p.is_file() and "__pycache__" not in p.relative_to(root).parts
        }

    try:
        return listing(source) == listing(dest)
    except OSError:
        return False


def sync_tree(source: Path, dest: Path) -> None:
    """Make ``dest``'s files those of ``source``.

    In place, and around Python's caches: Home Assistant writes __pycache__
    there, often as another user (root in a container), so the folder can't
    always be moved away or emptied by whoever runs this.
    """
    wanted = set()
    if not dest.parent.exists():
        dest.parent.mkdir(parents=True)
        dest.parent.chmod(0o755)
    for path in sorted(source.rglob("*")):
        rel = path.relative_to(source)
        if "__pycache__" in rel.parts or not path.is_file():
            continue
        wanted.add(rel)
        target = dest / rel
        for folder in reversed([p for p in target.parents if dest in (p, *p.parents) and not p.exists()]):
            folder.mkdir()
            folder.chmod(0o755)
        partial = target.with_name(f".{target.name}.part")
        shutil.copyfile(path, partial)
        partial.chmod(0o644)  # whatever this user's umask: Home Assistant may run as another user
        os.replace(partial, target)  # each file whole, and over one this user may not write to
    for path in sorted(dest.rglob("*"), reverse=True):
        rel = path.relative_to(dest)
        if "__pycache__" in rel.parts:
            continue
        if path.is_dir() and not path.is_symlink():
            try:
                path.rmdir()  # only if what it held is gone
            except OSError:
                pass
        elif rel not in wanted:
            path.unlink()


def copy_files(args: argparse.Namespace, report: Report) -> str:
    """Put the integration into <config-dir>/custom_components; the version copied."""
    config = Path(args.config_dir).expanduser()
    if not (config / "configuration.yaml").is_file() and not (config / ".storage").is_dir():
        raise Fail(f"{config} is not a Home Assistant config directory (no configuration.yaml there).")
    dest = config / "custom_components" / DOMAIN
    source = None if args.version else checkout()
    if source is not None and source.resolve() == dest.resolve():
        raise Fail("This script is in that config directory: --config-dir would copy the folder onto itself.")
    with tempfile.TemporaryDirectory() as scratch:
        if source is None:
            source = unpack_release((args.version or latest_release()).removeprefix("v"), Path(scratch))
        try:
            sync_tree(source, dest)
        except OSError as err:
            raise Fail(
                f"Cannot write to {dest}: {err.strerror or err}. Run this as a user who may write there, then "
                "run it again: the copy stopped halfway."
            ) from None
    version = manifest_version(dest)
    report.ok(f"copied version {version} to {dest}")
    return version


def hacs_repository(ha: HomeAssistant) -> dict[str, Any] | None:
    for repo in ha.call("hacs/repositories/list", timeout=120, categories=["integration"]) or []:
        if str(repo.get("full_name", "")).casefold() == REPOSITORY.casefold():
            return repo
    return None


def hacs_download(ha: HomeAssistant, args: argparse.Namespace, report: Report) -> str:
    """Add the repository to HACS if it isn't there and download it; the version downloaded."""
    repo = hacs_repository(ha)
    if repo is None:
        ha.call("hacs/repositories/add", timeout=120, repository=REPOSITORY, category="integration")
        deadline = time.monotonic() + 120  # HACS answers at once and registers it afterwards
        while (repo := hacs_repository(ha)) is None:
            if time.monotonic() > deadline:
                raise Fail(
                    f"HACS did not add {REPOSITORY} (its log says why; GitHub's rate limit is the usual reason)."
                )
            time.sleep(3)
        report.ok(f"added {REPOSITORY} to HACS as a custom repository")
    # HACS takes the tag as it is named, and this repository's tags are v<version>.
    params = {"version": "v" + args.version.removeprefix("v")} if args.version else {}
    try:
        ha.call("hacs/repository/download", timeout=600, repository=str(repo["id"]), **params)
    except CommandError as err:
        raise Fail(f"HACS could not download the integration ({err}).") from None
    repo = hacs_repository(ha) or {}
    version = str(repo.get("installed_version") or args.version or "?").removeprefix("v")
    report.ok(f"downloaded version {version} through HACS")
    return version


def files(ha: HomeAssistant, args: argparse.Namespace, state: dict[str, Any], report: Report) -> str | None:
    """Install or upgrade the files; the version Home Assistant must restart to load, if any."""
    have = state["installed"]
    if have and not args.upgrade and not args.version:
        report.skip("files: already installed (--upgrade replaces them with the latest)")
        return None
    source = checkout() if args.config_dir and not args.version else None
    if have and args.version:
        if version_tuple(have) == version_tuple(args.version.removeprefix("v")):
            report.skip(f"files: version {have} is already installed")
            return None
    elif have and source is not None:
        # From a checkout, the checkout is what gets installed: its files decide, not its version.
        want = manifest_version(source)
        if version_tuple(want) < version_tuple(have):
            raise Fail(
                f"This checkout is version {want}, older than the installed {have}: update it (git pull), "
                f"or pass --version {want} to go back to that release."
            )
        if same_files(source, Path(args.config_dir).expanduser() / "custom_components" / DOMAIN):
            if version_tuple(want) == version_tuple(have):
                report.skip(f"files: this checkout (version {want}) is what is installed")
                return None
            # Copied by an earlier run that could not restart.
            report.skip(f"files: version {want} is in place, Home Assistant still runs {have}")
            return None if args.check else want
    elif have and version_tuple(have) >= version_tuple(want := latest_release()):
        report.skip(f"files: version {have} is installed, the latest release is {want}")
        return None
    if args.check:
        report.skip(f"files: version {have} would be replaced" if have else "files: not installed yet")
        return None
    if args.config_dir:
        return copy_files(args, report)
    if state["hacs"]:
        return hacs_download(ha, args, report)
    raise Fail(
        ("Upgrading needs a way to place the files" if have else "The integration is not installed")
        + ", and this Home Assistant has no HACS. Either install HACS (https://hacs.xyz), or run this "
        "where Home Assistant's config directory is and pass --config-dir."
    )


# --- 3. Restart --------------------------------------------------------------


def restart(ha: HomeAssistant, args: argparse.Namespace, report: Report, placed: str | None = None) -> None:
    if not args.restart:
        raise NeedsUser("Restart Home Assistant to load the new files, then run this again (or pass --restart).")

    def running() -> bool:
        try:
            status, config = ha.request("GET", "/api/config", timeout=10)
        except OSError:
            return False
        return status == 200 and isinstance(config, dict) and config.get("state") == "RUNNING"

    # A Home Assistant that is still starting answers the restart call and ignores it.
    deadline = time.monotonic() + args.restart_timeout
    while not running():
        if time.monotonic() > deadline:
            raise Fail("Home Assistant is still starting; run this again once it is up.")
        time.sleep(3)
    result = ha.post("/api/config/core/check_config")
    if result.get("result") != "valid":
        raise Fail("Home Assistant's own configuration check fails; not restarting it. Fix that first.")
    try:
        status, _ = ha.request("POST", "/api/services/homeassistant/restart", {}, timeout=60)
    except OSError:
        status = 200  # it may go down before it answers
    if 400 <= status < 500:
        raise Fail(f"Home Assistant refused to restart (HTTP {status}); restart it yourself and run this again.")
    ha.close()
    report.line("...", "restarting Home Assistant")

    # Down first: until it stops answering, "running" is still the old process with the old files.
    deadline = time.monotonic() + 180
    while running():
        if time.monotonic() > deadline:
            raise Fail("Home Assistant took the restart call but kept running; restart it yourself and run this again.")
        time.sleep(1)
    deadline = time.monotonic() + args.restart_timeout
    while not running():
        if time.monotonic() > deadline:
            raise Fail(f"Home Assistant did not come back within {args.restart_timeout} s.")
        time.sleep(3)
    version = installed_version(ha)
    if version is None:
        raise Fail("Home Assistant restarted but does not see the integration; its log says why.")
    # Both known: HACS or Home Assistant may not say a version ("?"), which is not a mismatch.
    if placed and version_tuple(placed) and version_tuple(version) and version_tuple(version) != version_tuple(placed):
        raise Fail(
            f"Home Assistant restarted and runs version {version}, not the {placed} just placed: "
            + ("is --config-dir the folder this Home Assistant uses?" if args.config_dir else "its log says why.")
        )
    report.ok(f"Home Assistant restarted, integration version {version}")


# --- 4. The NAS --------------------------------------------------------------


def flow_step(ha: HomeAssistant, path: str, body: dict[str, Any]) -> dict[str, Any]:
    # A step that logs in to the NAS waits for it: an address nothing answers at takes a minute to fail.
    result = ha.post(path, body, timeout=180)
    if not isinstance(result, dict) or "type" not in result:
        raise Fail(f"POST {path}: not a flow step.")
    return result


def abandon(ha: HomeAssistant, kind: str, flow: dict[str, Any]) -> None:
    try:
        ha.request("DELETE", f"/api/config/config_entries/{kind}/{flow.get('flow_id')}")
    except OSError:
        pass


def wait_loaded(ha: HomeAssistant, entry_id: str, seconds: float = 90) -> str:
    """The entry's state once it has settled (or what it is after ``seconds``)."""
    for left in range(int(seconds // 2), -1, -1):
        state = next((e.get("state") for e in entries(ha) if e.get("entry_id") == entry_id), "gone")
        if state in ("loaded", "setup_error", "migration_error", "gone") or not left:
            break
        time.sleep(2)
    return str(state)


def nas(ha: HomeAssistant, args: argparse.Namespace, report: Report) -> str | None:
    """Set up the NAS if asked; the id of the entry the later steps work on."""
    existing = {e["entry_id"]: e for e in entries(ha)}
    listed = ", ".join(f"{i} = {e.get('title')}" for i, e in existing.items())
    if args.entry_id and args.entry_id not in existing:
        raise Fail(f"--entry-id {args.entry_id}: no such entry" + (f" ({listed})." if listed else "."))
    chosen = args.entry_id or (next(iter(existing)) if len(existing) == 1 else None)
    # A NAS that is set up is not logged in to again: the password may be gone from where it was read,
    # and DSM blocks an address after repeated failed logins.
    if existing and not args.ss_add:
        if chosen is None:
            raise Fail(f"Several NASes are set up: pass --entry-id ({listed}).")
        report.skip(f"NAS: already set up ({existing[chosen].get('title')})" + (
            "; --ss-add adds another" if args.ss_host else ""))
        return chosen
    if not args.ss_host:
        raise NeedsUser("No NAS is set up yet: pass --ss-host and --ss-user (see --help).")
    if not args.ss_user:
        raise Fail("--ss-host needs --ss-user.")
    if args.check:
        report.skip(f"NAS: {args.ss_host} would be set up")
        return chosen
    password = read_secret(
        "SS_PASSWORD", args.ss_password_file, "--ss-password-file", f"DSM password of {args.ss_user}", strip=False
    )
    port = args.ss_port or (5000 if args.ss_http else 5001)
    flow = flow_step(ha, "/api/config/config_entries/flow", {"handler": DOMAIN, "show_advanced_options": False})
    if flow["type"] != "form":
        raise Fail(f"The config flow did not start ({flow.get('reason') or flow['type']}).")
    result = flow_step(
        ha,
        f"/api/config/config_entries/flow/{flow['flow_id']}",
        {
            "host": args.ss_host, "port": port, "ssl": not args.ss_http, "verify_ssl": args.ss_verify_ssl,
            "username": args.ss_user, "password": password,
        },
    )
    if result["type"] == "form":
        abandon(ha, "flow", result)
        error = (result.get("errors") or {}).get("base", "unknown")
        raise Fail(SETUP_ERRORS.get(error, f"Setup failed ({error})."))
    if result["type"] == "abort":
        if result.get("reason") != "already_configured":
            raise Fail(f"Setup stopped ({result.get('reason')}).")
        if chosen is None:
            raise Fail("That NAS is already set up, next to others: pass --entry-id to say which to work on.")
        report.skip("NAS: this one is already set up")
        return chosen
    entry_id = result["result"]["entry_id"]
    state = wait_loaded(ha, entry_id)
    if state != "loaded":
        raise Fail(f"The NAS was added but its entry is '{state}'; Home Assistant's log says why.")
    report.ok(f"NAS set up: {result.get('title')}")
    return entry_id


# --- 5. Dashboard ------------------------------------------------------------


def dashboard(ha: HomeAssistant, args: argparse.Namespace, report: Report) -> None:
    path = args.dashboard
    if not re.fullmatch(r"[a-z0-9_]+(-[a-z0-9_]+)+", path):
        raise Fail("--dashboard: lower-case words joined by hyphens, such as ss-playback (HA wants a hyphen).")
    if any(d.get("url_path") == path for d in ha.call("lovelace/dashboards/list")):
        report.skip(f"dashboard /{path}: already there, left as it is")
        if args.timelapse:
            try:
                has_view = "custom:ss-timelapse-card" in json.dumps(ha.call("lovelace/config", url_path=path))
            except CommandError:  # no cards yet
                has_view = False
            if not has_view:
                report.warn(
                    "--timelapse adds its view only to a dashboard this makes. To this one, add a card of "
                    "type custom:ss-timelapse-card yourself."
                )
        return
    if args.check:
        report.skip(f"dashboard /{path} would be made")
        return
    views = [{
        "title": "Playback", "path": "playback", "icon": "mdi:cctv", "panel": True,
        "cards": [{"type": "custom:ss-timeline-card"}],
    }]
    if args.timelapse:
        views.append({
            "title": "Time-lapse", "path": "timelapse", "icon": "mdi:timelapse", "panel": True,
            "cards": [{"type": "custom:ss-timelapse-card"}],
        })
    made = ha.call(
        "lovelace/dashboards/create", url_path=path, title=args.dashboard_title, icon="mdi:cctv",
        mode="storage", show_in_sidebar=True, require_admin=False,
    )
    try:
        ha.call("lovelace/config/save", url_path=path, config={"title": args.dashboard_title, "views": views})
    except (CommandError, Fail):
        try:  # not left empty: the next run would find it "already there"
            ha.call("lovelace/dashboards/delete", dashboard_id=made["id"])
        except (CommandError, Fail, KeyError, TypeError):
            pass
        raise
    report.ok(f"dashboard /{path}/playback made")


# --- 6. Options --------------------------------------------------------------


def fetch(url: str, timeout: float = 5) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        raw = resp.read(8 * 1024 * 1024)
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode(errors="replace").strip()


def frigate_look(args: argparse.Namespace, report: Report) -> dict[str, Any] | None:
    """What Frigate's own API says, if this machine reaches it: its config, checked against what is needed."""
    url = (args.frigate_check_url or args.frigate_url or "").rstrip("/")
    if not url:
        return None
    try:
        parts = urllib.parse.urlsplit(url)
        plain = parts.scheme in ("http", "https") and parts.hostname and parts.port != 0
    except ValueError:
        plain = False
    if not plain or parts.username or parts.password:
        # Never fetched, never printed: a URL with a login in it is a secret.
        flag = "--frigate-check-url" if args.frigate_check_url else "--frigate-url"
        raise Fail(OPTION_ERRORS["invalid_url"].replace("--frigate-url", flag))
    no_login = "Port 8971 asks for a login the integration cannot give: use Frigate's internal port 5000."
    try:
        version, config = fetch(f"{url}/api/version"), fetch(f"{url}/api/config")
    except urllib.error.HTTPError as err:
        hint = f" {no_login}" if err.code in (401, 403) else ""
        report.warn(f"Frigate at {url} answers HTTP {err.code}, so its settings were not checked.{hint}")
        return None
    except (OSError, ValueError, http.client.HTTPException) as err:
        report.warn(
            f"Frigate does not answer this machine at {url} ({getattr(err, 'reason', err)}), so its settings "
            "were not checked. Home Assistant is what must reach --frigate-url; if this machine reaches "
            "Frigate elsewhere, pass --frigate-check-url."
        )
        return None
    if not isinstance(config, dict) or not isinstance(config.get("cameras"), dict):
        report.warn(f"{url} did not answer like Frigate's API. {no_login}")
        return None
    if version_tuple(str(version)) < version_tuple(MIN_FRIGATE):
        raise Fail(f"Frigate {version} is too old: its review topic came with {MIN_FRIGATE}.")
    report.ok(f"Frigate {version} at {url}, {len(config['cameras'])} cameras")
    mqtt = config.get("mqtt") or {}
    if mqtt.get("enabled") is False:
        report.warn("Frigate's MQTT is off (mqtt.enabled): it publishes no reviews, so nothing gets bookmarked.")
    prefix = str(mqtt.get("topic_prefix") or "frigate")
    if args.frigate_topic is None:
        args.frigate_topic = prefix
    elif args.frigate_topic.strip("/") != prefix:
        report.warn(f"--frigate-topic is {args.frigate_topic}, but Frigate's mqtt.topic_prefix is {prefix}.")
    for name, camera in sorted(config["cameras"].items()):
        if camera.get("enabled") is False:
            continue
        if not (camera.get("record") or {}).get("enabled"):
            report.warn(f"Frigate camera {name}: record.enabled is off, so Frigate makes no reviews for it.")
        if args.frigate_url and not (camera.get("snapshots") or {}).get("enabled"):
            report.warn(
                f"Frigate camera {name}: snapshots.enabled is off, so its notifications show Surveillance "
                "Station's frame instead of Frigate's snapshot."
            )
    return config


def field_values(schema: list[dict[str, Any]]) -> dict[str, Any]:
    """What a flow's form shows: each field's current value, else its default."""
    values = {}
    for field in schema:
        suggested = (field.get("description") or {}).get("suggested_value")
        if suggested is not None:
            values[field["name"]] = suggested
        elif "default" in field:
            values[field["name"]] = field["default"]
    return values


def camera_mapping(
    args: argparse.Namespace, ss_names: list[str], current: dict[str, str], frigate: dict[str, Any] | None,
    report: Report,
) -> dict[str, str]:
    """The Frigate camera(s) of each SS camera whose name differs: --camera-map over what is set."""
    mapping = dict(current)
    by_key = {camera_key(n): n for n in ss_names}
    for item in args.camera_map:
        ss_name, sep, frigate_names = item.partition("=")
        name = by_key.get(camera_key(ss_name))
        if not sep or name is None:
            raise Fail(
                f"--camera-map {item!r}: write 'SS camera=frigate_camera'. Surveillance Station's cameras: "
                + ", ".join(ss_names) + "."
            )
        mapping[name] = frigate_names.strip()
    if frigate is not None:
        mapped = {camera_key(n) for names in mapping.values() for n in names.split(",")}
        for name, camera in sorted(frigate["cameras"].items()):
            if camera.get("enabled") is not False and camera_key(name) not in by_key and camera_key(name) not in mapped:
                report.warn(
                    f"Frigate camera {name} matches no Surveillance Station camera ({', '.join(ss_names)}): "
                    f"its detections are ignored. Map it with --camera-map 'SS camera={name}'."
                )
    return {name: names for name, names in mapping.items() if names}


def options(ha: HomeAssistant, args: argparse.Namespace, entry_id: str, report: Report) -> None:
    wanted = {name: getattr(args, name) for name in OPTION_FLAGS if getattr(args, name) is not None}
    if not wanted and not args.camera_map:
        return
    if args.check:
        report.skip("options would be set: " + ", ".join(sorted(wanted) + (["camera map"] if args.camera_map else [])))
        return
    flow = flow_step(ha, "/api/config/config_entries/options/flow", {"handler": entry_id})
    try:
        current = field_values(flow.get("data_schema") or [])
        if args.frigate_link is None and not current.get("frigate_link") and args.dashboard and wanted.get("frigate"):
            wanted["frigate_link"] = f"/{args.dashboard}/playback"  # so notifications open the card just made
        values = {**current, **wanted}
        frigate = frigate_look(args, report) if values.get("frigate") else None
        if "frigate_topic" not in wanted and args.frigate_topic is not None:
            values["frigate_topic"] = args.frigate_topic  # Frigate's own prefix, just read from it
        flow = flow_step(ha, f"/api/config/config_entries/options/flow/{flow['flow_id']}", values)
        if flow["type"] == "form" and flow.get("step_id") == "cameras":
            schema = flow.get("data_schema") or []
            mapping = camera_mapping(args, [f["name"] for f in schema], field_values(schema), frigate, report)
            flow = flow_step(ha, f"/api/config/config_entries/options/flow/{flow['flow_id']}", mapping)
        elif args.camera_map and flow["type"] != "form":
            report.warn("--camera-map was not applied: it needs Frigate detections on and Surveillance Station up.")
        if flow["type"] == "form":
            errors = sorted(set((flow.get("errors") or {}).values()))
            raise Fail(" ".join(OPTION_ERRORS.get(e, f"Options refused ({e}).") for e in errors) or "Options refused.")
    except Fail:
        if flow["type"] == "form":
            abandon(ha, "options/flow", flow)
        raise
    if flow["type"] != "create_entry":
        raise Fail(f"The options were not saved ({flow.get('reason') or flow['type']}).")
    state = wait_loaded(ha, entry_id)  # saving them reloads the entry
    if state != "loaded":
        raise Fail(f"The options were saved but the entry is '{state}'; Home Assistant's log says why.")
    report.ok("options saved: " + ", ".join(
        f"{name}={values[name]}" for name in OPTION_FLAGS if name in values and values[name] != ""))


# --- 7. Notifications --------------------------------------------------------


def phones(ha: HomeAssistant) -> dict[str, str]:
    """The Companion-app devices: name -> device id."""
    out = {}
    for device in ha.call("config/device_registry/list"):
        if any(pair and pair[0] == "mobile_app" for pair in device.get("identifiers") or []):
            out[str(device.get("name_by_user") or device.get("name"))] = device["id"]
    return out


def blueprint(ha: HomeAssistant) -> str:
    """The detection blueprint's path in Home Assistant, imported from GitHub if it isn't there."""
    for path, found in (ha.call("blueprint/list", domain="automation") or {}).items():
        if path.endswith("/detection_notification.yaml") and "surveillance_station_detection" in json.dumps(found):
            return str(path)
    try:
        imported = ha.call("blueprint/import", timeout=60, url=BLUEPRINT_URL)
        path = str(imported["suggested_filename"])
        path = path if path.endswith(".yaml") else f"{path}.yaml"
        if not imported.get("exists"):
            ha.call(
                "blueprint/save", domain="automation", path=path, yaml=imported["raw_data"], source_url=BLUEPRINT_URL
            )
    except CommandError as err:
        raise Fail(f"Home Assistant could not import the blueprint from GitHub ({err}).") from None
    return path


def notifications(ha: HomeAssistant, args: argparse.Namespace, report: Report) -> None:
    devices = phones(ha)
    wanted = args.notify_device
    device_id = next((i for n, i in devices.items() if wanted in (i, n) or n.casefold() == wanted.casefold()), None)
    if device_id is None:
        raise Fail(f"--notify-device {wanted!r}: no such phone. Phones: {', '.join(sorted(devices)) or 'none'}.")
    automation = f"{DOMAIN}_detection_{device_id[:12]}"
    try:
        status, _ = ha.request("GET", f"/api/config/automation/config/{automation}")
    except OSError as err:
        raise Fail(f"Home Assistant does not answer at {ha.url} ({err}).") from None
    if status == 200:
        report.skip("notifications: this phone's automation is already there")
        return
    if args.check:
        report.skip("notifications: the blueprint would be imported and an automation made")
        return
    path = blueprint(ha)
    name = next(n for n, i in devices.items() if i == device_id)
    ha.post(
        f"/api/config/automation/config/{automation}",
        {
            "alias": f"Surveillance Station detections to {name}",
            "description": "Made by scripts/install.py from the integration's blueprint.",
            "use_blueprint": {"path": path, "input": {"notify_device": device_id}},
        },
    )
    report.ok(f"notifications: automation 'Surveillance Station detections to {name}' made from the blueprint")


# --- 8. Check ----------------------------------------------------------------


def check(ha: HomeAssistant, entry_id: str, report: Report) -> None:
    state = wait_loaded(ha, entry_id, seconds=30)  # saved options reload it a moment later
    if state != "loaded":
        raise Fail(f"The entry is '{state}'; Home Assistant's log and Settings > Repairs say why.")
    try:
        answer = ha.call("surveillance_station/cameras", entry_id=entry_id)
    except CommandError as err:
        raise Fail(f"Surveillance Station does not answer Home Assistant ({err}).") from None
    cameras = answer.get("cameras") or []
    report.ok(f"Surveillance Station answers: {len(cameras)} cameras ({', '.join(c['name'] for c in cameras)})")
    try:
        resources = ha.call("lovelace/resources")
    except CommandError:
        resources = []
    if any("/ss-timeline-card.js" in str(r.get("url")) for r in resources):
        report.ok("the card is registered as a dashboard resource")
    else:
        report.warn(
            "The card is not among the dashboard resources (dashboards in YAML mode keep their own list); "
            "it is loaded on every page instead, which works."
        )
    try:
        status, diagnostics = ha.request("GET", f"/api/diagnostics/config_entry/{entry_id}")
    except OSError:
        status, diagnostics = 0, None
    data = diagnostics.get("data") if status == 200 and isinstance(diagnostics, dict) else None
    bridge = data.get("frigate") if isinstance(data, dict) else None
    if isinstance(bridge, dict) and bridge:
        if not bridge.get("subscribed"):
            report.warn(f"Frigate detections are on but {bridge.get('topic')} is not subscribed yet (MQTT down?).")
        elif bridge.get("frigate_available") == "offline":
            report.warn("Frigate reports itself offline on MQTT.")
        else:
            report.ok(
                f"Frigate detections: listening on {bridge.get('topic')}"
                + (", snapshots from Frigate's API" if bridge.get("frigate_api") else ", images from SS")
            )
        if isinstance(error := bridge.get("last_error"), dict):
            report.warn(f"Frigate detections: last error {error.get('error')}")
    try:
        issues = ha.call("repairs/list_issues").get("issues") or []
    except CommandError:
        issues = []
    for issue in issues:
        if issue.get("domain") == DOMAIN and not issue.get("ignored"):
            report.warn(f"Repairs has an issue for the integration: {issue.get('issue_id')}")


# --- Main --------------------------------------------------------------------


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:
        """One [FAIL] line and exit 1; argparse would repeat a mistyped flag's value, and exit 2."""
        if message.startswith("unrecognized arguments:"):
            # Only what is shaped like a flag: a value may begin with a dash too.
            words = (word.split("=")[0] for word in message.split()[2:])
            flags = [word for word in words if re.fullmatch(r"--[a-z][a-z0-9-]*", word)]
            message = "unrecognized arguments" + (": " + " ".join(flags) if flags else "")
        elif "'" in message:  # argparse quotes the value it could not take
            message = message.split(": ")[0] + ": not a value it takes"
        Report().line("FAIL", f"{message} (--help lists the flags).")
        self.exit(1)


def parser() -> argparse.ArgumentParser:
    p = Parser(
        description=__doc__.split("\n\n")[0], epilog="Steps, secrets and exit status: the head of this file.",
        allow_abbrev=False,  # or --ha-token <the token> would pass as --ha-token-file
    )
    p.add_argument("--ha-url", default=os.environ.get("HA_URL"), help="Home Assistant's address ($HA_URL)")
    p.add_argument("--ha-token-file", default=os.environ.get("HA_TOKEN_FILE"),
                   help="file holding a long-lived token of an administrator ($HA_TOKEN_FILE; or $HA_TOKEN)")
    p.add_argument("--insecure", action="store_true", help="don't verify Home Assistant's certificate")
    # Taken only to say no without repeating them (argparse would echo an unknown argument's value).
    p.add_argument("--ha-token", "--token", "--ss-password", "--password", dest="secret_given",
                   help=argparse.SUPPRESS)
    p.add_argument("--check", action="store_true", help="look and report only; change nothing")

    g = p.add_argument_group("files")
    g.add_argument("--config-dir", help="Home Assistant's config directory, to copy the files there (else HACS); "
                   "they come from this checkout, or from the release when the script is on its own")
    g.add_argument("--version", help="the release to install, such as 0.24.3 (default: the latest)")
    g.add_argument("--upgrade", action="store_true",
                   help="replace an installed version with the latest release (from a checkout: with the checkout)")
    g.add_argument("--restart", action="store_true", help="restart Home Assistant when new files need it")
    g.add_argument("--restart-timeout", type=int, default=600, help=argparse.SUPPRESS)

    g = p.add_argument_group("the NAS (a dedicated DSM account: SS playback rights, no two-step verification)")
    g.add_argument("--ss-host", help="the NAS as Home Assistant reaches it")
    g.add_argument("--ss-port", type=int, help="default 5001, or 5000 with --ss-http")
    g.add_argument("--ss-http", action="store_true", help="plain HTTP: the password crosses the network unencrypted")
    g.add_argument("--ss-verify-ssl", action="store_true", help="verify DSM's certificate (not its self-signed one)")
    g.add_argument("--ss-user", help="the DSM account")
    g.add_argument("--ss-password-file", help="file holding its password (or $SS_PASSWORD)")
    g.add_argument("--ss-add", action="store_true", help="add this NAS next to the one(s) already set up")
    g.add_argument("--entry-id", help="which NAS to work on, when several are set up")

    g = p.add_argument_group("dashboard")
    g.add_argument("--dashboard", nargs="?", const="ss-playback", metavar="URL-PATH",
                   help="make a dashboard with the card (default path: ss-playback); an existing one is left alone")
    g.add_argument("--dashboard-title", default="Cameras", help=argparse.SUPPRESS)
    g.add_argument("--timelapse", action="store_true", help="add a view with the time-lapse card to it")

    g = p.add_argument_group("options (what is not given keeps its value)")
    g.add_argument("--frigate", dest="frigate", action="store_true", default=None,
                   help="bookmark Frigate's detections (needs HA's MQTT integration on Frigate's broker)")
    g.add_argument("--no-frigate", dest="frigate", action="store_false", help="stop bookmarking them")
    g.add_argument("--frigate-url", help="Frigate's API as Home Assistant reaches it: http://frigate:5000")
    g.add_argument("--frigate-check-url", help="Frigate's API as this machine reaches it, if that differs")
    g.add_argument("--frigate-topic", help="Frigate's MQTT topic prefix (default: read from Frigate, else frigate)")
    g.add_argument("--frigate-objects", type=lambda v: [o.strip() for o in v.split(",") if o.strip()],
                   metavar="LABELS", help="Frigate labels to bookmark, comma-separated (default: person,car,dog,cat)")
    g.add_argument("--camera-map", action="append", default=[], metavar="'SS camera=frigate_camera'",
                   help="for a camera named differently in Frigate (repeat per camera)")
    g.add_argument("--link", dest="frigate_link", help="dashboard path notifications open (default: --dashboard's)")
    g.add_argument("--transcoder", choices=("auto", "gpu", "cpu"), help="what transcodes the time-lapse")

    g = p.add_argument_group("notifications")
    g.add_argument("--notify-device", metavar="PHONE",
                   help="a phone with the Companion app (its device name): make its detection automation")
    return p


def run(args: argparse.Namespace, report: Report) -> None:
    if args.secret_given is not None:
        raise Fail(
            "Secrets are not taken on the command line (other users see it, the shell keeps it): put the "
            "token in a file for --ha-token-file, the password in one for --ss-password-file."
        )
    if args.timelapse and not args.dashboard:
        report.warn("--timelapse does nothing without --dashboard: its view goes on the dashboard that one makes.")
    if not args.ha_url:
        raise Fail("--ha-url (or $HA_URL): Home Assistant's address, such as http://homeassistant.local:8123.")
    token = read_secret("HA_TOKEN", args.ha_token_file, "--ha-token-file", "Home Assistant long-lived access token")
    ha = HomeAssistant(args.ha_url, token, args.insecure)
    try:
        state = look(ha, args, report)
        placed = files(ha, args, state, report)
        if placed:
            restart(ha, args, report, placed)
        elif not state["installed"]:
            raise NeedsUser("The integration is not installed; run this without --check to install it.")
        entry_id = nas(ha, args, report)
        if args.dashboard:
            dashboard(ha, args, report)
        if entry_id is None:
            return
        options(ha, args, entry_id, report)
        if args.notify_device:
            notifications(ha, args, report)
        check(ha, entry_id, report)
    finally:
        ha.close()


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    report = Report()
    try:
        run(args, report)
    except NeedsUser as stop:
        report.line("stop", str(stop))
        return 2
    except Fail as err:
        report.line("FAIL", str(err))
        return 1
    except CommandError as err:
        report.line("FAIL", f"Home Assistant refused {err}.")
        return 1
    except KeyboardInterrupt:
        report.line("FAIL", "Interrupted; run it again to go on from here.")
        return 1
    except Exception as err:  # noqa: BLE001
        # Its type and place only: the text of an unforeseen error may quote an answer of Home Assistant's.
        trace = err.__traceback__
        while trace is not None and trace.tb_next is not None:
            trace = trace.tb_next
        where = f"{trace.tb_frame.f_code.co_name}, line {trace.tb_lineno}" if trace is not None else "?"
        report.line("FAIL", f"Unexpected {type(err).__name__} in {where}. This is a bug in the script: please report it.")
        return 1
    if not args.check:
        report.later("Reload open dashboards once without the browser's cache, so they load the card.")
    for text in report.todo:
        report.line("todo", text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
