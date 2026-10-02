"""scripts/install.py: what it decides, what it sends, and that it never prints a secret.

The script only talks to Home Assistant's API, so most tests hand it a
HomeAssistant whose transport is scripted; one talks to a real socket for the
WebSocket framing. To run it against a real Home Assistant, see
docs/development.md, "The installer".
"""

from __future__ import annotations

import argparse
import ast
import base64
import gzip
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import sys
import tarfile
import threading
import time
from typing import Any
import zlib

import pytest

from custom_components.surveillance_station import config_flow, frigate

ROOT = Path(__file__).parent.parent
spec = importlib.util.spec_from_file_location("install_script", ROOT / "scripts" / "install.py")
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)

PASSWORD = "dsm-pass-9f3a"
TOKEN = "ha-token-7c1e"
ENTRY = {"entry_id": "E1", "title": "Surveillance Station (nas)", "state": "loaded"}
ENTRIES = "/api/config/config_entries/entry?domain=surveillance_station"
FLOW = "/api/config/config_entries/flow"
OPTIONS_FLOW = "/api/config/config_entries/options/flow"


class Scripted(install.HomeAssistant):
    """A HomeAssistant with scripted answers: {(method, path): (status, body) or a function of the body}."""

    def __init__(self, rest: dict | None = None, ws: dict | None = None) -> None:
        super().__init__("http://ha.test:8123", TOKEN)
        self.rest = {("GET", ENTRIES): (200, [ENTRY]), **(rest or {})}
        self.ws = ws or {}
        self.sent: list[tuple[str, str, Any]] = []

    def request(self, method: str, path: str, body: Any = None, timeout: float | None = None) -> tuple[int, Any]:
        self.sent.append((method, path, body))
        answer = self.rest.get((method, path), (404, None))
        return answer(body) if callable(answer) else answer

    def call(self, command: str, timeout: float | None = None, **params: Any) -> Any:
        self.sent.append(("WS", command, params))
        answer = self.ws[command]
        answer = answer(**params) if callable(answer) else answer
        if isinstance(answer, Exception):
            raise answer
        return answer

    def bodies(self, method: str, path: str) -> list[Any]:
        return [body for m, p, body in self.sent if (m, p) == (method, path)]


def arguments(*argv: str) -> argparse.Namespace:
    return install.parser().parse_args(argv)


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install.time, "sleep", lambda seconds: None)
    for name in ("HA_URL", "HA_TOKEN", "HA_TOKEN_FILE", "SS_PASSWORD"):
        monkeypatch.delenv(name, raising=False)


# --- What must stay in step with the integration ---------------------------------


def test_minimum_home_assistant_is_hacs_json() -> None:
    assert install.MIN_HA == json.loads((ROOT / "hacs.json").read_text())["homeassistant"]


def test_every_flow_error_is_explained() -> None:
    strings = json.loads((ROOT / "custom_components/surveillance_station/strings.json").read_text())
    assert set(install.SETUP_ERRORS) == set(strings["config"]["error"])
    assert set(install.OPTION_ERRORS) == set(strings["options"]["error"])


def test_flags_are_the_flows_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SS_PASSWORD", PASSWORD)
    ha = nas_flow({"type": "abort", "reason": "cannot_connect"})
    with pytest.raises(install.Fail):
        install.nas(ha, arguments(*NAS_ARGS), install.Report())
    assert set(ha.bodies("POST", f"{FLOW}/F1")[0]) == {str(key) for key in config_flow.SCHEMA.schema}
    assert set(install.OPTION_FLAGS) <= {str(key) for key in config_flow.OPTIONS_SCHEMA.schema}
    args = arguments()
    assert all(hasattr(args, name) for name in install.OPTION_FLAGS)


def test_script_runs_alone_on_python_3_9() -> None:
    """People download just this file: nothing to install, and an old Python."""
    source = (ROOT / "scripts" / "install.py").read_text()
    tree = ast.parse(source, feature_version=(3, 9))
    imported = {
        (node.module if isinstance(node, ast.ImportFrom) else alias.name).split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert imported - {"__future__"} <= sys.stdlib_module_names
    assert "from __future__ import annotations" in source  # `str | None` in a signature needs it there


@pytest.mark.parametrize("name", ["Drive Way", "drive_way", "BackyardPath", "前门", "Front-Door 2"])
def test_camera_key_is_the_integrations(name: str) -> None:
    assert install.camera_key(name) == frigate.camera_key(name)


def test_blueprint_url_is_the_shipped_blueprint() -> None:
    assert install.BLUEPRINT_URL.endswith("/blob/main/blueprints/automation/surveillance_station/detection_notification.yaml")
    assert (ROOT / install.BLUEPRINT_URL.split("/blob/main/")[1]).is_file()


# --- Helpers ---------------------------------------------------------------------


def test_version_tuple() -> None:
    assert install.version_tuple("2026.10.0b1") == (2026, 10, 0)
    assert install.version_tuple("0.18.0-77a66e7") == (0, 18, 0)
    assert install.version_tuple("2026.9.2") >= install.version_tuple("2026.9.0") > install.version_tuple("2026.8.9")
    assert install.version_tuple("dev") == ()


def test_read_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    file = tmp_path / "secret"
    file.write_text("from-file\nsecond line\n")
    assert install.read_secret("SS_PASSWORD", str(file), "--ss-password-file", "password") == "from-file"
    monkeypatch.setenv("SS_PASSWORD", " from-env ")
    assert install.read_secret("SS_PASSWORD", str(file), "--ss-password-file", "password") == "from-env"
    monkeypatch.delenv("SS_PASSWORD")
    file.write_text("\n")
    with pytest.raises(install.Fail, match="is empty") as failure:
        install.read_secret("SS_PASSWORD", str(file), "--ss-password-file", "password")
    assert str(failure.value) == "--ss-password-file: that file is empty."  # by the flag, not by the path
    # The secret itself where its file was asked for: said by the flag's name, not by what was passed.
    with pytest.raises(install.Fail, match="--ss-password-file: cannot read that file") as failure:
        install.read_secret("SS_PASSWORD", PASSWORD, "--ss-password-file", "password")
    assert PASSWORD not in str(failure.value)
    monkeypatch.setattr(install.sys.stdin, "isatty", lambda: False, raising=False)
    with pytest.raises(install.Fail, match=r"\$SS_PASSWORD"):
        install.read_secret("SS_PASSWORD", None, "--ss-password-file", "password")
    # A file saved with a byte-order mark; a password keeps its own spaces.
    file.write_bytes(b"\xef\xbb\xbf pass word \r\n")
    assert install.read_secret("HA_TOKEN", str(file), "--ha-token-file", "token") == "pass word"
    assert install.read_secret("SS_PASSWORD", str(file), "--ss-password-file", "password", strip=False) == " pass word "
    file.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(install.Fail, match="not text") as failure:
        install.read_secret("HA_TOKEN", str(file), "--ha-token-file", "token")
    assert str(failure.value) == "--ha-token-file: that file is not text (UTF-8)."


def test_field_values_prefers_what_is_set() -> None:
    schema = [
        {"name": "frigate", "default": False, "description": {"suggested_value": True}},
        {"name": "frigate_topic", "default": "frigate"},
        {"name": "frigate_url"},
    ]
    assert install.field_values(schema) == {"frigate": True, "frigate_topic": "frigate"}


# --- Home Assistant and the files ------------------------------------------------


def look_at(config: dict | None, **rest: Any) -> Scripted:
    return Scripted(
        {
            ("GET", "/api/config"): (200, config),
            ("GET", "/api/config/config_entries/entry?domain=mqtt"): (200, []),
            ("GET", "/api/config/config_entries/flow_handlers"): (200, ["mqtt"]),
            **rest,
        }
    )


def test_look_refuses_an_old_home_assistant() -> None:
    with pytest.raises(install.Fail, match="too old"):
        install.look(look_at({"version": "2026.8.4"}), arguments(), install.Report())


def test_look_refuses_what_is_not_home_assistant() -> None:
    with pytest.raises(install.Fail, match="does not answer like Home Assistant"):
        install.look(look_at(None), arguments(), install.Report())


def test_look_refuses_a_token_that_is_not_an_administrators() -> None:
    ha = look_at({"version": "2026.9.2", "components": []}, **{})
    ha.rest[("GET", ENTRIES)] = (401, None)
    with pytest.raises(install.Fail, match="administrator"):
        install.look(ha, arguments(), install.Report())


def test_look_wants_mqtt_before_frigate_and_a_phone_before_notifications() -> None:
    ha = look_at({"version": "2026.9.2", "components": ["hacs"]})
    with pytest.raises(install.Fail, match="MQTT"):
        install.look(ha, arguments("--frigate"), install.Report())
    with pytest.raises(install.Fail, match="Companion"):
        install.look(ha, arguments("--notify-device", "Phone"), install.Report())
    state = install.look(ha, arguments(), install.Report())
    assert state["hacs"] and not state["mqtt"] and state["installed"] is None


def test_files_decisions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ha, report = Scripted(), install.Report()
    done: list[str] = []
    monkeypatch.setattr(install, "copy_files", lambda args, report: done.append("copy") or "0.24.9")
    monkeypatch.setattr(install, "hacs_download", lambda ha, args, report: done.append("hacs") or "0.24.9")
    monkeypatch.setattr(install, "latest_release", lambda: "0.24.3")
    state = {"installed": "0.24.3", "hacs": True}

    assert not install.files(ha, arguments(), state, report)  # installed: left alone
    assert not install.files(ha, arguments("--upgrade"), state, report)  # already the latest
    assert not install.files(ha, arguments("--upgrade"), {**state, "installed": "0.25.0"}, report)  # past it
    assert not install.files(ha, arguments("--version", "v0.24.3"), state, report)
    assert done == []
    assert install.files(ha, arguments("--upgrade"), {**state, "installed": "0.24.2"}, report) and done == ["hacs"]
    assert install.files(ha, arguments("--version", "0.24.2"), state, report) and done == ["hacs", "hacs"]
    assert install.files(ha, arguments("--config-dir", "/config", "--version", "0.24.2"), state, report)
    assert done == ["hacs", "hacs", "copy"]  # --config-dir wins over HACS

    assert not install.files(ha, arguments("--check"), {"installed": None, "hacs": True}, report)
    assert not install.files(ha, arguments("--check", "--upgrade"), {**state, "installed": "0.24.2"}, report)
    assert done == ["hacs", "hacs", "copy"]  # --check changes nothing
    with pytest.raises(install.Fail, match="not installed, and this Home Assistant has no HACS"):
        install.files(ha, arguments(), {"installed": None, "hacs": False}, report)
    with pytest.raises(install.Fail, match="Upgrading needs a way to place the files"):
        install.files(ha, arguments("--upgrade"), {"installed": "0.24.2", "hacs": False}, report)


def test_files_from_a_checkout_go_by_its_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The checkout is what gets installed: unreleased changes under the same version are copied."""
    source = ROOT / "custom_components" / "surveillance_station"
    version = json.loads((source / "manifest.json").read_text())["version"]
    (tmp_path / "configuration.yaml").write_text("")
    args = arguments("--config-dir", str(tmp_path), "--upgrade")
    monkeypatch.setattr(install, "latest_release", lambda: pytest.fail("asked GitHub"))
    ha, report, state = Scripted(), install.Report(), {"installed": version, "hacs": False}

    assert install.files(ha, args, state, report) == version  # nothing there yet, whatever HA has loaded
    assert install.files(ha, args, state, report) is None  # the same files: nothing to do
    # Copied by a run that could not restart: this one still has to, and --check only says so.
    older = {**state, "installed": "0.0.1"}
    assert install.files(ha, args, older, report) == version
    assert install.files(ha, arguments("--config-dir", str(tmp_path), "--upgrade", "--check"), older, report) is None
    changed = tmp_path / "custom_components" / "surveillance_station" / "const.py"
    changed.write_text(changed.read_text() + "# edited\n")
    assert not install.files(ha, arguments("--config-dir", str(tmp_path), "--upgrade", "--check"), state, report)
    assert changed.read_text().endswith("# edited\n")
    assert install.files(ha, args, state, report) and not changed.read_text().endswith("# edited\n")
    # A stale checkout does not quietly take a newer installation back.
    with pytest.raises(install.Fail, match="older than the installed 99.0.0"):
        install.files(ha, args, {**state, "installed": "99.0.0"}, report)


def test_checkout_is_not_a_config_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The script saved as <config>/scripts/install.py has the installed copy where a checkout has its source."""
    installed = tmp_path / "custom_components" / "surveillance_station"
    installed.mkdir(parents=True)
    (installed / "manifest.json").write_text("{}")
    (tmp_path / "scripts").mkdir()
    monkeypatch.setattr(install, "__file__", str(tmp_path / "scripts" / "install.py"))
    assert install.checkout() is None
    (tmp_path / "hacs.json").write_text("{}")
    (tmp_path / "configuration.yaml").write_text("")
    assert install.checkout() == installed
    with pytest.raises(install.Fail, match="onto itself"):
        install.copy_files(arguments("--config-dir", str(tmp_path)), install.Report())


def test_copy_files_makes_the_folder_the_checkouts(tmp_path: Path, request: pytest.FixtureRequest) -> None:
    (tmp_path / "configuration.yaml").write_text("")
    old = os.umask(0o077)
    request.addfinalizer(lambda: os.umask(old))
    dest = tmp_path / "custom_components" / "surveillance_station"
    for stale in ("stale.py", "gone/old.js", "__pycache__/stale.cpython-314.pyc", "manifest.json"):
        (dest / stale).parent.mkdir(parents=True, exist_ok=True)
        (dest / stale).write_text("old")
    version = install.copy_files(arguments("--config-dir", str(tmp_path)), install.Report())
    source = ROOT / "custom_components" / "surveillance_station"
    assert version == json.loads((source / "manifest.json").read_text())["version"]

    def files(root: Path) -> dict[str, bytes]:
        return {
            str(p.relative_to(root)): p.read_bytes()
            for p in root.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        }

    assert files(dest) == files(source)
    assert not (dest / "gone").exists()
    # Home Assistant's caches are left alone: it may own them as another user.
    assert (dest / "__pycache__" / "stale.cpython-314.pyc").is_file()
    # Readable by Home Assistant whoever it runs as, whatever this user's umask.
    assert {p.stat().st_mode & 0o777 for p in dest.rglob("*.js")} == {0o644}
    assert (dest / "frontend").stat().st_mode & 0o777 == 0o755
    assert sorted(p.name for p in dest.parent.iterdir()) == ["surveillance_station"]  # nothing staged next to it


def test_copy_files_of_a_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".storage").mkdir()

    def unpack(version: str, into: Path) -> Path:
        (into / "x").mkdir()
        (into / "x" / "manifest.json").write_text(json.dumps({"version": version}))
        return into / "x"

    monkeypatch.setattr(install, "unpack_release", unpack)
    args = arguments("--config-dir", str(tmp_path), "--version", "v0.24.2")
    assert install.copy_files(args, install.Report()) == "0.24.2"
    assert [p.name for p in (tmp_path / "custom_components" / "surveillance_station").iterdir()] == ["manifest.json"]


def test_copy_files_refuses_a_directory_that_is_not_a_config(tmp_path: Path) -> None:
    with pytest.raises(install.Fail, match="not a Home Assistant config directory"):
        install.copy_files(arguments("--config-dir", str(tmp_path)), install.Report())
    assert not list(tmp_path.iterdir())


def test_unpack_release_takes_only_the_integration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    here = "repo-0.1.0/custom_components/surveillance_station"
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        for name in (
            "repo-0.1.0/README.md",
            f"{here}/manifest.json",
            f"{here}/frontend/card.js",
            f"{here}/../../../escaped.py",
            f"{here}/frontend/../../../../escaped2.py",
            f"{here}//absolute/escaped3.py",
            f"{here}/__pycache__/x.pyc",
            f"repo-0.1.0/tests/fixtures/custom_components/surveillance_station/not_the_integration.py",
            "repo-0.1.0/custom_components/other/theirs.py",
        ):
            info = tarfile.TarInfo(name)
            info.size = 2
            tar.addfile(info, io.BytesIO(b"{}"))
        link = tarfile.TarInfo(f"{here}/link.py")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tar.addfile(link)
    asked: list[str] = []

    def urlopen(url: str, timeout: float) -> io.BytesIO:
        asked.append(url)
        return io.BytesIO(archive.getvalue())

    monkeypatch.setattr(install.urllib.request, "urlopen", urlopen)
    # Several levels down, so that a path climbing out still lands under tmp_path, where it is seen.
    into = tmp_path / "a" / "b" / "c" / "d" / "unpack"
    target = install.unpack_release("0.1.0", into)
    assert asked == ["https://github.com/Sraw/ha-surveillance-station/archive/refs/tags/v0.1.0.tar.gz"]
    assert target == into / "surveillance_station"
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if not p.is_dir()) == [
        "a/b/c/d/unpack/surveillance_station/frontend/card.js",
        "a/b/c/d/unpack/surveillance_station/manifest.json",
    ]
    assert not Path("/absolute/escaped3.py").exists()

    monkeypatch.setattr(install.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(b"<html>not found</html>"))
    with pytest.raises(install.Fail, match="did not download as an archive"):
        install.unpack_release("0.1.0", tmp_path / "bad")


def test_latest_release_is_asked_of_github(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []

    def urlopen(request: Any, timeout: float) -> io.BytesIO:
        asked.append(request.full_url)
        return io.BytesIO(b'{"tag_name": "v0.24.3"}')

    monkeypatch.setattr(install.urllib.request, "urlopen", urlopen)
    assert install.latest_release() == "0.24.3"
    assert asked == ["https://api.github.com/repos/Sraw/ha-surveillance-station/releases/latest"]


@pytest.mark.parametrize(
    ("error", "said"),
    [
        (http.client.RemoteDisconnected(""), "RemoteDisconnected"),  # an error with no text still says what
        (http.client.IncompleteRead(b""), "IncompleteRead"),
        (OSError("unreachable"), "unreachable"),
    ],
)
def test_a_download_that_breaks_is_a_failure_that_says_why(
    error: Exception, said: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def urlopen(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(install.urllib.request, "urlopen", urlopen)
    with pytest.raises(install.Fail, match=rf"Cannot ask GitHub for the latest release \({said}.*\); pass --version"):
        install.latest_release()
    with pytest.raises(install.Fail, match=rf"Cannot download release 0\.1\.0 \({said}.*\)"):
        install.unpack_release("0.1.0", tmp_path)


@pytest.mark.parametrize("error", [zlib.error("bad data"), gzip.BadGzipFile("bad crc"), EOFError(), tarfile.ReadError("x")])
def test_an_archive_that_breaks_midway_is_a_failure(
    error: Exception, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(install.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(b""))

    def opened(**kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(install.tarfile, "open", opened)
    with pytest.raises(install.Fail, match=rf"did not download as an archive \({type(error).__name__}\)"):
        install.unpack_release("0.1.0", tmp_path)


def test_hacs_adds_the_repository_then_downloads_it() -> None:
    listed = iter([[], [], [{"id": "42", "full_name": "sraw/HA-Surveillance-Station"}]])
    last = [{"id": "42", "full_name": "Sraw/ha-surveillance-station", "installed_version": "v0.24.3"}]
    ha = Scripted(
        ws={
            "hacs/repositories/list": lambda **p: next(listed, last),
            "hacs/repositories/add": {},
            "hacs/repository/download": {},
        }
    )
    assert install.hacs_download(ha, arguments(), install.Report()) == "0.24.3"
    assert ("WS", "hacs/repositories/add", {"repository": "Sraw/ha-surveillance-station", "category": "integration"}) in ha.sent
    assert ("WS", "hacs/repository/download", {"repository": "42"}) in ha.sent
    # HACS takes the tag as it is named: v0.24.3, however the version was written.
    for written in ("0.24.3", "v0.24.3"):
        install.hacs_download(ha, arguments("--version", written), install.Report())
        assert ha.sent[-2] == ("WS", "hacs/repository/download", {"repository": "42", "version": "v0.24.3"})


def test_restart_is_asked_for_not_assumed() -> None:
    ha = Scripted()
    with pytest.raises(install.NeedsUser, match="--restart"):
        install.restart(ha, arguments(), install.Report())
    assert ha.sent == []


RUNNING = {("GET", "/api/config"): (200, {"state": "RUNNING"})}


def test_restart_refuses_a_configuration_that_does_not_check() -> None:
    ha = Scripted({**RUNNING, ("POST", "/api/config/core/check_config"): (200, {"result": "invalid", "errors": "x"})})
    with pytest.raises(install.Fail, match="configuration check"):
        install.restart(ha, arguments("--restart"), install.Report())
    assert not ha.bodies("POST", "/api/services/homeassistant/restart")


def test_restart_is_not_asked_of_one_that_is_still_starting() -> None:
    """Home Assistant answers a restart call while it starts, and ignores it."""
    ha = Scripted({("GET", "/api/config"): (200, {"state": "NOT_RUNNING"})})
    with pytest.raises(install.Fail, match="still starting"):
        install.restart(ha, arguments("--restart", "--restart-timeout", "0"), install.Report())
    assert {method for method, path, body in ha.sent} == {"GET"}


def test_restart_that_is_refused_is_not_waited_for() -> None:
    ha = Scripted(
        {
            **RUNNING,
            ("POST", "/api/config/core/check_config"): (200, {"result": "valid"}),
            ("POST", "/api/services/homeassistant/restart"): (400, {"message": "no"}),
        }
    )
    with pytest.raises(install.Fail, match=r"refused to restart \(HTTP 400\)"):
        install.restart(ha, arguments("--restart"), install.Report())


def test_restart_waits_for_it_to_go_down_and_come_back() -> None:
    states = iter(
        [
            (200, {"state": "NOT_RUNNING"}),  # still starting: not restarted yet
            (200, {"state": "RUNNING"}),
            (200, {"state": "RUNNING"}),  # the call is in, the old process still answers
            OSError("refused"),
            (200, {"state": "NOT_RUNNING"}),
            (200, {"state": "RUNNING"}),
        ]
    )

    def config(body: Any) -> tuple[int, Any]:
        answer = next(states)
        if isinstance(answer, Exception):
            raise answer
        return answer

    ha = Scripted(
        {
            ("POST", "/api/config/core/check_config"): (200, {"result": "valid"}),
            ("POST", "/api/services/homeassistant/restart"): (200, []),
            ("GET", "/api/config"): config,
            ("GET", "/api/config/config_entries/flow_handlers"): (200, ["surveillance_station"]),
        },
        ws={"manifest/get": {"version": "0.24.3"}},
    )
    install.restart(ha, arguments("--restart"), install.Report(), "0.24.3")
    assert next(states, None) is None  # it read every state, the old "RUNNING" included
    asked = [(method, path) for method, path, body in ha.sent]
    assert asked.index(("POST", "/api/services/homeassistant/restart")) == 3  # after the first "RUNNING"


# --- The NAS ---------------------------------------------------------------------


def nas_flow(result: dict) -> Scripted:
    return Scripted(
        {
            ("GET", ENTRIES): (200, []),
            ("POST", FLOW): (200, {"type": "form", "flow_id": "F1", "step_id": "user"}),
            ("POST", f"{FLOW}/F1"): (200, result),
            ("DELETE", f"{FLOW}/F1"): (200, None),
        }
    )


NAS_ARGS = ("--ss-host", "nas.local", "--ss-user", "ha-playback")


def test_nas_sends_the_form_and_waits_for_the_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SS_PASSWORD", PASSWORD)
    ha = nas_flow({"type": "create_entry", "title": "Surveillance Station (nas)", "result": {"entry_id": "E1"}})
    states = iter(["setup_in_progress", "loaded"])
    ha.rest[("GET", ENTRIES)] = lambda body: (200, [{**ENTRY, "state": next(states)}] if ha.bodies("POST", f"{FLOW}/F1") else [])
    assert install.nas(ha, arguments(*NAS_ARGS), install.Report()) == "E1"
    assert ha.bodies("POST", f"{FLOW}/F1") == [
        {"host": "nas.local", "port": 5001, "ssl": True, "verify_ssl": False, "username": "ha-playback", "password": PASSWORD}
    ]


def test_nas_over_http_defaults_to_port_5000(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SS_PASSWORD", " " + PASSWORD)
    ha = nas_flow({"type": "abort", "reason": "already_configured"})
    ha.rest[("GET", ENTRIES)] = (200, [ENTRY])
    assert install.nas(ha, arguments(*NAS_ARGS, "--ss-http", "--ss-add"), install.Report()) == "E1"
    sent = ha.bodies("POST", f"{FLOW}/F1")[0]
    assert sent["port"] == 5000 and not sent["ssl"]
    assert sent["password"] == " " + PASSWORD  # a password's own spaces are kept


def test_nas_that_is_set_up_is_not_logged_in_to_again(capsys: pytest.CaptureFixture[str]) -> None:
    """A rerun of the same command: no password needed, no login for DSM to count."""
    ha = Scripted()
    assert install.nas(ha, arguments(*NAS_ARGS), install.Report()) == "E1"
    assert [method for method, path, body in ha.sent] == ["GET"]
    assert "already set up" in capsys.readouterr().out


@pytest.mark.parametrize("error", sorted(install.SETUP_ERRORS))
def test_nas_error_is_explained_without_the_password(
    error: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Home Assistant's form comes back with what was entered; none of it may be printed."""
    monkeypatch.setenv("SS_PASSWORD", PASSWORD)
    monkeypatch.setenv("HA_TOKEN", TOKEN)
    form = {
        "type": "form", "flow_id": "F1", "step_id": "user", "errors": {"base": error},
        "data_schema": [{"name": "password", "description": {"suggested_value": PASSWORD}}],
    }
    ha = nas_flow(form)
    monkeypatch.setattr(install, "HomeAssistant", lambda url, token, insecure: ha)
    monkeypatch.setattr(install, "look", lambda ha, args, report: {"installed": "0.24.3", "hacs": False})
    assert install.main(["--ha-url", "http://ha.test:8123", *NAS_ARGS]) == 1
    out = capsys.readouterr()
    assert install.SETUP_ERRORS[error] in out.out
    assert PASSWORD not in out.out + out.err and TOKEN not in out.out + out.err
    assert ("DELETE", f"{FLOW}/F1", None) in ha.sent  # the half-filled flow is not left behind


def test_nas_without_flags() -> None:
    nothing = Scripted({("GET", ENTRIES): (200, [])})
    with pytest.raises(install.NeedsUser, match="--ss-host"):
        install.nas(nothing, arguments(), install.Report())
    with pytest.raises(install.Fail, match="--ss-user"):
        install.nas(nothing, arguments("--ss-host", "nas.local"), install.Report())
    assert install.nas(nothing, arguments(*NAS_ARGS, "--check"), install.Report()) is None
    assert not [1 for method, path, body in nothing.sent if method != "GET"]

    assert install.nas(Scripted(), arguments(), install.Report()) == "E1"
    two = Scripted({("GET", ENTRIES): (200, [ENTRY, {**ENTRY, "entry_id": "E2"}])})
    with pytest.raises(install.Fail, match="--entry-id"):
        install.nas(two, arguments(), install.Report())
    assert install.nas(two, arguments("--entry-id", "E2"), install.Report()) == "E2"
    with pytest.raises(install.Fail, match="--entry-id nope: no such entry"):
        install.nas(two, arguments("--entry-id", "nope"), install.Report())


# --- Dashboard, options, notifications -------------------------------------------


def test_dashboard_is_made_once_and_never_overwritten() -> None:
    ha = Scripted(ws={"lovelace/dashboards/list": [], "lovelace/dashboards/create": {}, "lovelace/config/save": None})
    install.dashboard(ha, arguments("--dashboard", "--timelapse"), install.Report())
    saved = [params for kind, command, params in ha.sent if command == "lovelace/config/save"]
    assert [view["cards"][0]["type"] for view in saved[0]["config"]["views"]] == [
        "custom:ss-timeline-card",
        "custom:ss-timelapse-card",
    ]
    assert saved[0]["url_path"] == "ss-playback"

    there = Scripted(ws={"lovelace/dashboards/list": [{"url_path": "ss-playback"}]})
    install.dashboard(there, arguments("--dashboard"), install.Report())
    assert [command for kind, command, params in there.sent] == ["lovelace/dashboards/list"]
    with pytest.raises(install.Fail, match="hyphen"):
        install.dashboard(there, arguments("--dashboard", "cameras"), install.Report())


def test_dashboard_that_cannot_be_filled_is_not_left_empty() -> None:
    ha = Scripted(
        ws={
            "lovelace/dashboards/list": [],
            "lovelace/dashboards/create": {"id": "ss_playback", "url_path": "ss-playback"},
            "lovelace/config/save": install.CommandError("lovelace/config/save", "unknown_error", "disk full"),
            "lovelace/dashboards/delete": None,
        }
    )
    with pytest.raises(install.CommandError):
        install.dashboard(ha, arguments("--dashboard"), install.Report())
    assert ha.sent[-1] == ("WS", "lovelace/dashboards/delete", {"dashboard_id": "ss_playback"})


def test_timelapse_view_that_is_not_made_is_said(capsys: pytest.CaptureFixture[str]) -> None:
    listed = {"lovelace/dashboards/list": [{"url_path": "ss-playback"}]}
    empty = install.CommandError("lovelace/config", "config_not_found", "No config found.")
    for config in ({"views": [{"cards": [{"type": "custom:ss-timeline-card"}]}]}, empty):
        there = Scripted(ws={**listed, "lovelace/config": config})
        install.dashboard(there, arguments("--dashboard", "--timelapse"), install.Report())
        assert "[warn] --timelapse adds its view only to a dashboard this makes" in capsys.readouterr().out
    # The same command run again: the view it made the first time is there.
    made = Scripted(ws={**listed, "lovelace/config": {"views": [{"cards": [{"type": "custom:ss-timelapse-card"}]}]}})
    install.dashboard(made, arguments("--dashboard", "--timelapse"), install.Report())
    assert "[warn]" not in capsys.readouterr().out
    assert made.sent[-1] == ("WS", "lovelace/config", {"url_path": "ss-playback"})


OPTIONS_FORM = {
    "type": "form", "flow_id": "O1", "step_id": "init",
    "data_schema": [
        {"name": "frigate", "default": False},
        {"name": "frigate_topic", "default": "frigate"},
        {"name": "frigate_objects", "default": ["person", "car", "dog", "cat"], "description": {"suggested_value": ["person"]}},
        {"name": "frigate_link", "description": {"suggested_value": "/mine/cams"}},
        {"name": "frigate_url"},
        {"name": "frigate_quiet_minutes", "default": 5, "description": {"suggested_value": 9}},
        {"name": "transcoder", "default": "auto"},
    ],
}
CAMERAS_FORM = {
    "type": "form", "flow_id": "O1", "step_id": "cameras",
    "data_schema": [{"name": "Drive Way"}, {"name": "Front Door", "description": {"suggested_value": "door"}}, {"name": "前门"}],
}


def options_flow(*steps: dict) -> Scripted:
    answers = iter(steps)
    return Scripted(
        {
            ("POST", OPTIONS_FLOW): (200, OPTIONS_FORM),
            ("POST", f"{OPTIONS_FLOW}/O1"): lambda body: (200, next(answers)),
            ("DELETE", f"{OPTIONS_FLOW}/O1"): (200, None),
        }
    )


def test_options_keep_what_is_set_and_map_cameras() -> None:
    ha = options_flow(CAMERAS_FORM, {"type": "create_entry"})
    args = arguments("--frigate", "--transcoder", "cpu", "--dashboard", "--camera-map", "drive way = driveway, garage")
    install.options(ha, args, "E1", install.Report())
    first, second = ha.bodies("POST", f"{OPTIONS_FLOW}/O1")
    assert first == {
        "frigate": True, "frigate_topic": "frigate", "frigate_objects": ["person"], "frigate_link": "/mine/cams",
        "frigate_quiet_minutes": 9, "transcoder": "cpu",
    }
    assert second == {"Drive Way": "driveway, garage", "Front Door": "door"}


def test_options_link_the_dashboard_just_made() -> None:
    form = {**OPTIONS_FORM, "data_schema": [f for f in OPTIONS_FORM["data_schema"] if f["name"] != "frigate_link"]}
    ha = options_flow({"type": "create_entry"})
    ha.rest[("POST", OPTIONS_FLOW)] = (200, form)
    install.options(ha, arguments("--frigate", "--dashboard", "my-cams"), "E1", install.Report())
    assert ha.bodies("POST", f"{OPTIONS_FLOW}/O1")[0]["frigate_link"] == "/my-cams/playback"


def test_options_do_nothing_without_flags() -> None:
    ha = Scripted()
    install.options(ha, arguments("--dashboard"), "E1", install.Report())
    assert ha.sent == []


def test_options_error_names_the_flag_and_drops_the_flow() -> None:
    ha = options_flow({**OPTIONS_FORM, "errors": {"frigate_link": "invalid_link"}})
    with pytest.raises(install.Fail, match="--link"):
        install.options(ha, arguments("--link", "cams"), "E1", install.Report())
    assert ("DELETE", f"{OPTIONS_FLOW}/O1", None) in ha.sent


def test_unknown_camera_in_the_map_lists_the_real_ones() -> None:
    ha = options_flow(CAMERAS_FORM)
    with pytest.raises(install.Fail, match="Drive Way, Front Door, 前门"):
        install.options(ha, arguments("--frigate", "--camera-map", "Garage=garage"), "E1", install.Report())
    assert ("DELETE", f"{OPTIONS_FLOW}/O1", None) in ha.sent


FRIGATE = {
    "mqtt": {"enabled": True, "topic_prefix": "nvr"},
    "cameras": {
        "drive_way": {"record": {"enabled": True}, "snapshots": {"enabled": True}},
        "garage": {"record": {"enabled": False}, "snapshots": {"enabled": False}},
        "old": {"enabled": False},
    },
}


def test_frigate_is_read_for_what_would_not_work(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    asked: list[str] = []

    def fetch(url: str, timeout: float = 5) -> Any:
        asked.append(url)
        return "0.18.0-77a66e7" if url.endswith("/version") else FRIGATE

    monkeypatch.setattr(install, "fetch", fetch)
    args, report = arguments("--frigate", "--frigate-url", "http://frigate:5000/"), install.Report()
    assert install.frigate_look(args, report) == FRIGATE
    assert asked == ["http://frigate:5000/api/version", "http://frigate:5000/api/config"]
    assert args.frigate_topic == "nvr"  # Frigate's own prefix, when none was given
    out = capsys.readouterr().out
    assert "garage: record.enabled is off" in out and "garage: snapshots.enabled is off" in out
    assert "drive_way" not in out and "old" not in out
    assert report.warnings == 2

    mapping = install.camera_mapping(args, ["Drive Way", "Porch"], {}, FRIGATE, report)
    assert mapping == {} and "Frigate camera garage matches no Surveillance Station camera" in capsys.readouterr().out
    args.camera_map = ["Porch=garage"]
    assert install.camera_mapping(args, ["Drive Way", "Porch"], {}, FRIGATE, report) == {"Porch": "garage"}
    assert capsys.readouterr().out == ""

    monkeypatch.setattr(install, "fetch", lambda url, timeout=5: "0.13.2" if url.endswith("/version") else FRIGATE)
    with pytest.raises(install.Fail, match="too old"):
        install.frigate_look(args, report)


@pytest.mark.parametrize("flag", ["--frigate-url", "--frigate-check-url"])
def test_frigate_url_with_a_login_is_neither_fetched_nor_printed(
    flag: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(install, "fetch", lambda *a, **k: pytest.fail("fetched"))
    with pytest.raises(install.Fail, match=flag) as failure:
        install.frigate_look(arguments(flag, "http://admin:hunter2@frigate:5000"), install.Report())
    assert "hunter2" not in str(failure.value) + capsys.readouterr().out


@pytest.mark.parametrize("url", ["http://[::1", "http://frigate:port", "frigate:5000", "http://"])
def test_frigate_url_that_is_none_is_said_plainly(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install, "fetch", lambda *a, **k: pytest.fail("fetched"))
    with pytest.raises(install.Fail, match="--frigate-url"):
        install.frigate_look(arguments("--frigate-url", url), install.Report())


def test_frigate_behind_a_login_points_to_port_5000(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def fetch(url: str, timeout: float = 5) -> Any:
        raise install.urllib.error.HTTPError(url, 401, "Unauthorized", None, None)

    monkeypatch.setattr(install, "fetch", fetch)
    assert install.frigate_look(arguments("--frigate-url", "https://frigate:8971"), install.Report()) is None
    assert "internal port 5000" in capsys.readouterr().out


PHONE = {"id": "abcdef0123456789", "name": "Pixel", "name_by_user": "My Phone", "identifiers": [["mobile_app", "x"]]}
AUTOMATION = "/api/config/automation/config/surveillance_station_detection_abcdef012345"


def notify_ha(**rest: Any) -> Scripted:
    return Scripted(
        {("POST", AUTOMATION): (200, {"result": "ok"}), **rest},
        ws={
            "config/device_registry/list": [PHONE, {"id": "tv", "name": "TV", "identifiers": [["cast", "y"]]}],
            "blueprint/list": {"homeassistant/motion_light.yaml": {"metadata": {}}},
            "blueprint/import": {
                "suggested_filename": "Sraw/detection_notification", "raw_data": "blueprint: {}", "exists": False,
            },
            "blueprint/save": {},
        },
    )


def test_notifications_import_the_blueprint_and_make_the_automation() -> None:
    ha = notify_ha()
    install.notifications(ha, arguments("--notify-device", "my phone"), install.Report())
    saved = [params for kind, command, params in ha.sent if command == "blueprint/save"]
    assert saved[0]["path"] == "Sraw/detection_notification.yaml" and saved[0]["source_url"] == install.BLUEPRINT_URL
    assert ha.bodies("POST", AUTOMATION)[0]["use_blueprint"] == {
        "path": "Sraw/detection_notification.yaml",
        "input": {"notify_device": "abcdef0123456789"},
    }


def test_notifications_use_a_blueprint_that_is_there_and_run_once() -> None:
    ha = notify_ha()
    ha.ws["blueprint/list"] = {
        "surveillance_station/detection_notification.yaml": {"metadata": {"input": {}}, "x": "surveillance_station_detection"}
    }
    install.notifications(ha, arguments("--notify-device", "abcdef0123456789"), install.Report())
    assert not [1 for kind, command, params in ha.sent if command.startswith("blueprint/import")]
    assert ha.bodies("POST", AUTOMATION)[0]["use_blueprint"]["path"] == "surveillance_station/detection_notification.yaml"

    again = notify_ha(**{})
    again.rest[("GET", AUTOMATION)] = (200, {"id": "x"})
    install.notifications(again, arguments("--notify-device", "My Phone"), install.Report())
    assert not again.bodies("POST", AUTOMATION)


def test_unknown_phone_lists_the_phones() -> None:
    with pytest.raises(install.Fail, match="Phones: My Phone"):
        install.notifications(notify_ha(), arguments("--notify-device", "TV"), install.Report())


# --- The check, and the whole run -------------------------------------------------


def checked(frigate_stats: dict | None, issues: list | None = None, resources: list | None = None) -> Scripted:
    return Scripted(
        {("GET", "/api/diagnostics/config_entry/E1"): (200, {"data": {"frigate": frigate_stats}})},
        ws={
            "surveillance_station/cameras": {"cameras": [{"id": 6, "name": "Drive Way"}]},
            "lovelace/resources": resources if resources is not None else [{"url": "/surveillance_station_static/ss-timeline-card.js?v=1"}],
            "repairs/list_issues": {"issues": issues or []},
        },
    )


def test_check_reports_what_works(capsys: pytest.CaptureFixture[str]) -> None:
    report = install.Report()
    stats = {"subscribed": True, "topic": "frigate/reviews", "frigate_available": "online", "frigate_api": True}
    install.check(checked(stats), "E1", report)
    out = capsys.readouterr().out
    assert "1 cameras (Drive Way)" in out and "listening on frigate/reviews, snapshots from Frigate's API" in out
    assert report.warnings == 0


def test_check_warns_about_what_does_not(capsys: pytest.CaptureFixture[str]) -> None:
    report = install.Report()
    stats = {"subscribed": True, "topic": "frigate/reviews", "frigate_available": "offline", "last_error": {"error": "boom"}}
    issues = [
        {"domain": "surveillance_station", "issue_id": "frigate_silent"},
        {"domain": "surveillance_station", "issue_id": "hidden", "ignored": True},
        {"domain": "other", "issue_id": "theirs"},
    ]
    install.check(checked(stats, issues, resources=[]), "E1", report)
    out = capsys.readouterr().out
    assert "offline" in out and "boom" in out and "frigate_silent" in out and "not among the dashboard resources" in out
    assert "hidden" not in out and "theirs" not in out
    assert report.warnings == 4


def test_check_fails_on_an_entry_that_did_not_load() -> None:
    ha = checked(None)
    ha.rest[("GET", ENTRIES)] = (200, [{**ENTRY, "state": "setup_retry"}])
    with pytest.raises(install.Fail, match="setup_retry"):
        install.check(ha, "E1", install.Report())


def test_main_exit_status(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert install.main([]) == 1 and "--ha-url" in capsys.readouterr().out
    token = tmp_path / "token"
    token.write_text(TOKEN + "\n")
    ha = look_at({"version": "2026.9.2", "components": []})
    made: list[tuple] = []
    monkeypatch.setattr(install, "HomeAssistant", lambda *a: made.append(a) or ha)
    # Not installed, --check: it says so and stops for the user.
    assert install.main(["--ha-url", "http://ha.test:8123", "--ha-token-file", str(token), "--check"]) == 2
    assert made == [("http://ha.test:8123", TOKEN, False)]
    out = capsys.readouterr().out
    assert "[stop]" in out and TOKEN not in out
    assert not [1 for method, path, body in ha.sent if method != "GET"]


def test_run_hands_the_restart_what_was_placed(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("HA_TOKEN", TOKEN)
    monkeypatch.setattr(install, "HomeAssistant", lambda *a: Scripted())
    monkeypatch.setattr(install, "look", lambda ha, args, report: {"installed": "0.24.2", "hacs": True})
    monkeypatch.setattr(install, "files", lambda ha, args, state, report: "0.24.9")
    placed: list[Any] = []

    def restart(ha: Any, args: Any, report: Any, version: Any = None) -> None:
        placed.append(version)
        raise install.Fail("as far as this test goes")

    monkeypatch.setattr(install, "restart", restart)
    assert install.main(["--ha-url", "http://ha.test:8123", "--upgrade", "--restart"]) == 1
    assert placed == ["0.24.9"] and "as far as this test goes" in capsys.readouterr().out


@pytest.mark.parametrize(("placed", "loaded"), [("?", "0.24.3"), ("0.24.3", None), (None, "0.24.3")])
def test_restart_with_a_version_unknown_is_not_a_mismatch(placed: str | None, loaded: str | None) -> None:
    """HACS, or Home Assistant, may not say which version: that alone is no failure."""
    states = iter([True, False, True])
    ha = Scripted(
        {
            ("GET", "/api/config"): lambda body: (200, {"state": "RUNNING" if next(states) else "STOPPING"}),
            ("POST", "/api/config/core/check_config"): (200, {"result": "valid"}),
            ("POST", "/api/services/homeassistant/restart"): (200, []),
            ("GET", "/api/config/config_entries/flow_handlers"): (200, ["surveillance_station"]),
        },
        ws={"manifest/get": {"version": loaded}},
    )
    install.restart(ha, arguments("--restart"), install.Report(), placed)


def test_restart_that_loads_another_version_is_a_failure() -> None:
    """Files placed where this Home Assistant does not look: it comes back with what it had."""
    ha = Scripted(
        {
            **RUNNING,
            ("POST", "/api/config/core/check_config"): (200, {"result": "valid"}),
            ("POST", "/api/services/homeassistant/restart"): (200, []),
            ("GET", "/api/config/config_entries/flow_handlers"): (200, ["surveillance_station"]),
        },
        ws={"manifest/get": {"version": "0.24.2"}},
    )
    states = iter([True, False, True])
    ha.rest[("GET", "/api/config")] = lambda body: (200, {"state": "RUNNING" if next(states) else "STOPPING"})
    with pytest.raises(install.Fail, match="runs version 0.24.2, not the 0.24.3 just placed: is --config-dir"):
        install.restart(ha, arguments("--restart", "--config-dir", "/config"), install.Report(), "0.24.3")


@pytest.mark.parametrize(
    "argv",
    [
        ["--pass", PASSWORD],
        [f"--pass={PASSWORD}"],
        [PASSWORD],
        ["--ss-port", "x", "--pass", PASSWORD],
        ["--ss-port", PASSWORD],
        ["--transcoder", PASSWORD],
        ["--pass", f"-{PASSWORD}"],  # a value that begins with a dash
        ["--pass", f"my -{PASSWORD} word"],
    ],
)
def test_a_mistyped_flag_is_one_line_without_its_value(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        install.main(["--ha-url", "http://ha.test:8123", *argv])
    assert stopped.value.code == 1  # 2 is "the person must do something", not a usage error
    out = capsys.readouterr()
    assert out.out.startswith("[FAIL] ") and out.out.count("\n") == 1 and out.err == ""
    assert PASSWORD not in out.out and PASSWORD[1:] not in out.out


def test_a_value_read_as_the_help_flag_is_not_repeated(capsys: pytest.CaptureFixture[str]) -> None:
    """"-h<value>": the help (Python 3.13+), or an error quoting the rest of it (before)."""
    with pytest.raises(SystemExit) as stopped:
        install.main(["--ha-url", "http://ha.test:8123", "--pass", "-h" + PASSWORD])
    out = capsys.readouterr()
    assert stopped.value.code in (0, 1) and PASSWORD not in out.out + out.err


@pytest.mark.parametrize("url", ["http://[::1", "http://ha.test:port", "ftp://ha.test", "http://"])
def test_an_address_that_is_none_is_said_plainly(
    url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HA_TOKEN", TOKEN)
    assert install.main(["--ha-url", url]) == 1
    assert capsys.readouterr().out.startswith("[FAIL] --ha-url: an http:// or https:// URL")
    with pytest.raises(install.Fail, match="--ha-url: an http:// or https:// URL"):
        install.HomeAssistant(url, TOKEN)


@pytest.mark.parametrize("flag", ["--ha-token", "--token", "--ss-password", "--password"])
def test_a_secret_on_the_command_line_is_refused_unrepeated(flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert install.main(["--ha-url", "http://ha.test:8123", flag, PASSWORD]) == 1
    out = capsys.readouterr()
    assert "not taken on the command line" in out.out and PASSWORD not in out.out + out.err


def test_an_unforeseen_error_is_one_line_without_its_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Whatever went wrong may quote Home Assistant's answer, so only its type and place are said."""
    monkeypatch.setenv("HA_TOKEN", TOKEN)

    def look(ha: Any, args: Any, report: Any) -> dict:
        raise KeyError(f"answer with {PASSWORD}")

    monkeypatch.setattr(install, "look", look)
    assert install.main(["--ha-url", "http://ha.test:8123"]) == 1
    out = capsys.readouterr()
    assert "[FAIL] Unexpected KeyError in look, line" in out.out
    assert PASSWORD not in out.out + out.err and "Traceback" not in out.out + out.err

    def refused(ha: Any, args: Any, report: Any) -> dict:
        raise install.CommandError("hacs/repositories/list", "unknown_command", "Unknown command.")

    monkeypatch.setattr(install, "look", refused)
    assert install.main(["--ha-url", "http://ha.test:8123"]) == 1
    assert "[FAIL] Home Assistant refused hacs/repositories/list: Unknown command." in capsys.readouterr().out


READ_ONLY = {
    "manifest/get", "hacs/repositories/list", "lovelace/dashboards/list", "lovelace/config", "lovelace/resources",
    "blueprint/list",
    "config/device_registry/list", "repairs/list_issues", "surveillance_station/cameras",
}


@pytest.mark.parametrize("installed", ["0.24.2", None])
def test_check_changes_nothing_at_any_step(
    installed: str | None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every flag at once, with --check: only reads go out."""
    monkeypatch.setenv("HA_TOKEN", TOKEN)
    monkeypatch.setenv("SS_PASSWORD", PASSWORD)
    monkeypatch.setattr(install, "latest_release", lambda: "0.24.3")
    monkeypatch.setattr(install, "fetch", lambda *a, **k: pytest.fail("asked Frigate"))
    ha = Scripted(
        {
            ("GET", "/api/config"): (200, {"version": "2026.9.2", "components": ["hacs", "mobile_app"]}),
            ("GET", "/api/config/config_entries/entry?domain=mqtt"): (200, [{"state": "loaded"}]),
            ("GET", "/api/config/config_entries/flow_handlers"): (200, ["surveillance_station"] if installed else []),
            ("GET", ENTRIES): (200, [ENTRY] if installed else []),
            ("GET", "/api/diagnostics/config_entry/E1"): (200, {"data": {}}),
        },
        ws={
            "manifest/get": {"version": installed},
            "hacs/repositories/list": [],
            "lovelace/dashboards/list": [],
            "lovelace/resources": [],
            "blueprint/list": {},
            "config/device_registry/list": [PHONE],
            "repairs/list_issues": {"issues": []},
            "surveillance_station/cameras": {"cameras": []},
        },
    )
    monkeypatch.setattr(install, "HomeAssistant", lambda *a: ha)
    status = install.main(
        [
            "--ha-url", "http://ha.test:8123", "--check", "--upgrade", "--restart", "--config-dir", "/nonexistent",
            *NAS_ARGS, "--ss-add", "--dashboard", "--timelapse", "--frigate", "--frigate-url", "http://frigate:5000",
            "--camera-map", "Drive Way=drive", "--transcoder", "cpu", "--notify-device", "My Phone",
        ]
    )  # fmt: skip
    assert status == (0 if installed else 2), capsys.readouterr().out
    assert {method for method, path, body in ha.sent} <= {"GET", "WS"}
    assert {path for method, path, body in ha.sent if method == "WS"} <= READ_ONLY
    out = capsys.readouterr().out
    assert PASSWORD not in out and TOKEN not in out and "Reload open dashboards" not in out


# --- The transport, on a real socket ----------------------------------------------


class Handler(BaseHTTPRequestHandler):
    """Enough of Home Assistant: one REST path, and a WebSocket that echoes commands."""

    def log_message(self, *args: Any) -> None:
        pass

    def frame(self, opcode: int, payload: bytes, final: bool = True) -> None:
        head = bytes([(0x80 if final else 0) | opcode])
        if len(payload) < 126:
            head += bytes([len(payload)])
        elif len(payload) < 65536:
            head += bytes([126]) + struct.pack(">H", len(payload))
        else:
            head += bytes([127]) + struct.pack(">Q", len(payload))
        self.wfile.write(head + payload)
        self.wfile.flush()

    def message(self) -> dict:
        head = self.rfile.read(2)
        if len(head) < 2:
            raise ConnectionResetError
        first, second = head
        assert first == 0x81 and second & 0x80, "clients send whole, masked text frames"
        size = second & 0x7F
        if size == 126:
            (size,) = struct.unpack(">H", self.rfile.read(2))
        elif size == 127:
            (size,) = struct.unpack(">Q", self.rfile.read(8))
        mask = self.rfile.read(4)
        return json.loads(bytes(b ^ mask[i % 4] for i, b in enumerate(self.rfile.read(size))))

    def do_GET(self) -> None:
        authorized = self.headers.get("Authorization") == f"Bearer {TOKEN}"
        if self.path.startswith("/moved"):
            self.server.after_redirect.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/moved/again")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/api/config":
            body = json.dumps({"version": "2026.9.2"}).encode() if authorized else b"401: Unauthorized"
            self.send_response(200 if authorized else 401)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        assert self.path == "/api/websocket" and base64.b64decode(self.headers["Sec-WebSocket-Key"])
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.end_headers()
        self.frame(0x1, b'{"type": "auth_required"}')
        ok = self.message() == {"type": "auth", "access_token": TOKEN}
        self.frame(0x1, json.dumps({"type": "auth_ok" if ok else "auth_invalid"}).encode())
        while ok:
            try:
                command = self.message()
            except ConnectionResetError:
                break
            if command["type"] == "silence":  # alive, pinging, and never answering
                try:
                    for _ in range(100):
                        self.frame(0x9, b"ping")
                        threading.Event().wait(0.1)
                except OSError:
                    pass
                break
            self.frame(0x9, b"ping")  # answered with a pong, which this server never reads as a command
            self.frame(0x1, json.dumps({"id": command["id"], "type": "event", "event": {}}).encode())
            if command["type"] == "fail":
                answer = {"success": False, "error": {"code": "not_found", "message": "No such thing"}}
            else:
                answer = {"success": True, "result": command}
            text = json.dumps({"id": command["id"], "type": "result", **answer}).encode()
            self.frame(0x1, text[:10], final=False)  # in two frames, as a server may
            self.frame(0x0, text[10:])
            pong = self.rfile.read(2)
            assert pong[0] == 0x8A, "the ping was answered"
            self.rfile.read(4 + (pong[1] & 0x7F))
        self.close_connection = True


@pytest.fixture
def server(socket_enabled: None) -> Any:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = False
    httpd.after_redirect = []
    thread = threading.Thread(target=httpd.serve_forever)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", httpd
    httpd.shutdown()
    httpd.server_close()  # joins the handler threads
    thread.join()


def test_rest_and_websocket_on_a_socket(server: tuple[str, Any]) -> None:
    server, httpd = server
    ha = install.HomeAssistant(server, TOKEN)
    try:
        assert ha.get("/api/config") == {"version": "2026.9.2"}
        assert ha.request("GET", "/api/config")[0] == 200
        for size in (10, 200, 70_000):  # the three frame-length encodings
            answer = ha.call("echo", text="é" * size)
            assert answer["type"] == "echo" and answer["text"] == "é" * size
        with pytest.raises(install.CommandError, match="No such thing") as refused:
            ha.call("fail")
        assert refused.value.code == "not_found"
        assert ha.call("echo", n=1)["id"] == 5  # ids go up, the connection is kept
        kept = ha._ws
        assert ha.call("echo")["id"] == 6 and ha._ws is kept
        # Idle past Home Assistant's ping (it drops a client that did not answer one): a new connection.
        ha._ws_used -= install.IDLE_SECONDS + 1
        assert ha.call("echo")["id"] == 7 and ha._ws is not kept
    finally:
        ha.close()

    # A redirect is not followed: the token would go wherever it points.
    with pytest.raises(install.Fail, match="redirects elsewhere"):
        ha.get("/moved")
    assert httpd.after_redirect == ["/moved"]

    wrong = install.HomeAssistant(server, "not-the-token")
    with pytest.raises(install.Fail, match="rejected the token"):
        wrong.get("/api/config")
    with pytest.raises(install.Fail, match="token rejected") as failure:
        wrong.call("echo")
    assert "not-the-token" not in str(failure.value)


def test_a_command_never_answered_ends_at_its_timeout(server: tuple[str, Any]) -> None:
    """Home Assistant's pings arrive all along: they must not keep the wait alive."""
    ha = install.HomeAssistant(server[0], TOKEN)
    started = time.monotonic()
    with pytest.raises(install.Fail, match=r"WebSocket failed during silence \(no answer in time\)"):
        ha.call("silence", timeout=0.5)
    assert time.monotonic() - started < 5


def test_a_handshake_that_fails_leaves_no_socket_open(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[Any] = []

    class Refusing:
        closed = False

        def __init__(self, *args: Any) -> None:
            made.append(self)
            self.answers = iter(['{"type": "auth_required"}', '{"type": "auth_invalid"}'])

        def send(self, text: str) -> None:
            pass

        def recv(self, deadline: float) -> str:
            return next(self.answers)

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(install, "WebSocket", Refusing)
    ha = install.HomeAssistant("http://ha.test:8123", TOKEN)
    with pytest.raises(install.Fail, match="token rejected"):
        ha.call("echo")
    assert len(made) == 1 and made[0].closed and ha._ws is None


def test_home_assistant_that_is_not_there(socket_enabled: None) -> None:
    ha = install.HomeAssistant("http://127.0.0.1:9", TOKEN, timeout=2)
    with pytest.raises(install.Fail, match="does not answer"):
        ha.get("/api/config")
    with pytest.raises(install.Fail, match="WebSocket failed"):
        ha.call("echo")
    with pytest.raises(install.Fail, match="--ha-url"):
        install.HomeAssistant("homeassistant.local:8123", TOKEN)
    with pytest.raises(install.Fail, match="without a login") as failure:
        install.HomeAssistant("http://admin:hunter2@homeassistant.local:8123", TOKEN)
    assert "hunter2" not in str(failure.value)
