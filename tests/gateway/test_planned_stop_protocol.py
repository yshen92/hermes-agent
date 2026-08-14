"""Tests for the shared planned-stop marker protocol.

Two things are under test here:

1. **Wire compatibility.** ``gateway/planned_stop_protocol.py`` now owns the
   marker format that ``gateway/status.py`` used to define inline. A marker
   written by the new code must still be consumed by the *old* code, because
   during a rollout the stopper and the running gateway are different
   versions. The old code is not paraphrased — the exact historical
   ``gateway/status.py`` blob is read out of git and executed.

2. **The standalone stop path** (``run_planned_stop``), driven entirely
   through fixture hosts. Nothing in this file touches the real ``~/.hermes``,
   a real systemd, a real ``systemctl``, or any process this test did not
   spawn itself.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from gateway import planned_stop_protocol as protocol
from gateway import status


REPO_ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_PATH = REPO_ROOT / "gateway" / "planned_stop_protocol.py"

# gateway/status.py as of R0 (01edcadbd194f81bd7eceb9ca267737830ce24c0) — the
# deployed revision whose markers the new writer must remain compatible with.
R0_STATUS_BLOB = "ce02648a958f9a281303dd825ad45b2fdc8eb046"

MARKER_NAME = ".gateway-planned-stop.json"
FAKE_UID = 4242


# ── loading the code under test ───────────────────────────────────────


@pytest.fixture
def module():
    """A private copy of the protocol module, loaded the way the standalone
    executor would: straight from the file, stdlib only.

    Private so tests can rebind its module-level ``subprocess`` without
    touching the ``gateway.planned_stop_protocol`` that ``gateway.status``
    imports.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_planned_stop_protocol_under_test", PROTOCOL_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _r0_status_source():
    """Return the exact R0 ``gateway/status.py`` source, or None."""
    proc = subprocess.run(
        ["git", "cat-file", "blob", R0_STATUS_BLOB],
        cwd=str(REPO_ROOT),
        capture_output=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8")


def _load_r0_status(hermes_home: Path) -> SimpleNamespace:
    """Execute the R0 ``gateway/status.py`` in a throwaway namespace.

    Only the two Hermes imports it does at module level are stubbed, and only
    for the duration of the ``exec`` — everything the test then exercises is
    the historical code itself.
    """
    source = _r0_status_source()
    if source is None:
        pytest.skip(
            f"R0 gateway/status.py blob {R0_STATUS_BLOB} is not present in this "
            "repository, so marker compatibility with the deployed revision "
            "cannot be proven here"
        )

    constants_stub = ModuleType("hermes_constants")
    constants_stub.get_hermes_home = lambda: hermes_home
    constants_stub._get_platform_default_hermes_home = lambda: hermes_home

    def _atomic_json_write(path, data, *, indent=2, mode=None, **dump_kwargs):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".r0tmp")
        tmp.write_text(
            json.dumps(data, indent=indent, ensure_ascii=False, **dump_kwargs),
            encoding="utf-8",
        )
        os.replace(tmp, path)

    utils_stub = ModuleType("utils")
    utils_stub.atomic_json_write = _atomic_json_write

    saved = {name: sys.modules.get(name) for name in ("hermes_constants", "utils")}
    sys.modules["hermes_constants"] = constants_stub
    sys.modules["utils"] = utils_stub
    namespace: dict = {"__name__": "r0_gateway_status"}
    try:
        exec(
            compile(source, f"<git blob {R0_STATUS_BLOB} gateway/status.py>", "exec"),
            namespace,
        )
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
    return SimpleNamespace(**namespace)


# ── fixture host: a fake 'hermes' account that exists only in tmp_path ─


FAKE_SYSTEMCTL = """#!/bin/sh
# Stand-in for systemctl. Records every invocation (argv + environment) and
# answers `show` from a file the test controls, so a test can prove which
# scope was queried and that no other verb was ever run.
LOG="{log}"
DIR="{dir}"
printf 'ARGV\\t%s\\n' "$*" >> "$LOG"
env | sed 's/^/ENV\\t/' >> "$LOG"

SCOPE=system
VERB=""
for arg in "$@"; do
    case "$arg" in
        --user) SCOPE=user ;;
        --system) SCOPE=system ;;
        show|stop|start|restart|kill|reload|is-active|daemon-reload)
            if [ -z "$VERB" ]; then VERB="$arg"; fi
            ;;
    esac
done

case "$VERB" in
    show)
        if [ -f "$DIR/show-$SCOPE.txt" ]; then cat "$DIR/show-$SCOPE.txt"; fi
        exit 0
        ;;
    stop)
        if [ -f "$DIR/stop-sleep" ]; then sleep "$(cat "$DIR/stop-sleep")"; fi
        RC=0
        if [ -f "$DIR/stop-rc" ]; then RC="$(cat "$DIR/stop-rc")"; fi
        if [ "$RC" != "0" ]; then echo "fake systemctl: stop refused" >&2; fi
        exit "$RC"
        ;;
esac
echo "fake systemctl: unexpected invocation: $*" >&2
exit 64
"""


def unit_text(description: str, hermes_home) -> str:
    """A user-scope unit shaped like the one the installer writes.

    Only its *existence* is checked by the helper — the HERMES_HOME binding
    comes from the service manager, not this file — but keeping the fixture
    realistic keeps the two views of the deployment honest with each other.
    """
    return (
        "[Unit]\n"
        f"Description={description}\n"
        "\n"
        "[Service]\n"
        'Environment="PATH=/usr/bin:/bin"\n'
        'Environment="VIRTUAL_ENV=/opt/hermes/.venv"\n'
        f'Environment="HERMES_HOME={hermes_home}"\n'
        "Restart=always\n"
    )


def show_environment(hermes_home) -> str:
    """The ``Environment`` property value ``systemctl show`` reports.

    One line of space-separated ``KEY=VALUE`` tokens, unquoted — systemd only
    quotes a token whose value contains spaces, and nothing the installer
    writes does. This is the merged view of the fragment plus any drop-in,
    which is why the helper trusts it over the fragment's own text.
    """
    return f"PATH=/usr/bin:/bin VIRTUAL_ENV=/opt/hermes/.venv HERMES_HOME={hermes_home}"


def _fake_stat_line(pid: int, start_time: int) -> str:
    """A ``/proc/<pid>/stat`` line whose field 22 is ``start_time``."""
    fields = [str(pid), "(python3)", "S"] + ["0"] * 18 + [str(start_time)] + ["0"] * 30
    assert fields[21] == str(start_time)
    return " ".join(fields) + "\n"


class Sandbox:
    """A self-contained fake host tree plus the host dicts built from it."""

    def __init__(self, tmp_path: Path):
        self.root = tmp_path
        self.home = tmp_path / "home" / "hermes"
        self.hermes_home = self.home / ".hermes"
        self.unit_path = (
            self.home / ".config" / "systemd" / "user" / "hermes-gateway.service"
        )
        self.runtime_dir_root = tmp_path / "run" / "user"
        self.runtime_dir = self.runtime_dir_root / str(FAKE_UID)
        self.proc_root = tmp_path / "proc"
        self.system_unit_path = (
            tmp_path / "etc" / "systemd" / "system" / "hermes-gateway.service"
        )
        self.control_dir = tmp_path / "systemctl-control"
        self.log = self.control_dir / "calls.log"
        self.systemctl = self.control_dir / "systemctl"

        for directory in (
            self.hermes_home,
            self.unit_path.parent,
            self.runtime_dir,
            self.proc_root,
            self.system_unit_path.parent,
            self.control_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        self.unit_path.write_text(
            unit_text("fake hermes gateway (user)", self.hermes_home),
            encoding="utf-8",
        )
        # A plausible system-scope unit. Nothing may ever consult it.
        self.system_unit_path.write_text(
            unit_text("fake hermes gateway (system)", self.hermes_home),
            encoding="utf-8",
        )
        self.systemctl.write_text(
            FAKE_SYSTEMCTL.format(log=self.log, dir=self.control_dir), encoding="utf-8"
        )
        self.systemctl.chmod(0o755)

        self.marker_path = self.hermes_home / MARKER_NAME
        self._children: list[subprocess.Popen] = []

    # -- live processes -------------------------------------------------

    def spawn_child(self) -> int:
        """Start a disposable child process and return its PID.

        Only ever this process's own children are signalled by these tests.
        """
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._children.append(child)
        return child.pid

    def spawn_dead_pid(self) -> int:
        """A PID that the kernel agrees is not a live process.

        Spawning and reaping a child is not enough on its own: between the
        reap and the assertion the OS may hand that number to something else,
        and the test would then be probing a stranger. Confirm with a
        signal-0 probe and retry if the number came back into use.
        """
        for _ in range(5):
            child = subprocess.Popen(
                [sys.executable, "-c", "pass"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            pid = child.pid
            child.wait(timeout=10)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return pid
            except PermissionError:
                continue
        pytest.fail("could not obtain a PID that stays dead")

    def reap(self) -> None:
        for child in self._children:
            try:
                child.stdin.close()
            except OSError:
                pass
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)

    # -- fake /proc and fake systemctl answers ---------------------------

    def write_stat(self, pid: int, start_time: int = 987654) -> int:
        entry = self.proc_root / str(pid)
        entry.mkdir(parents=True, exist_ok=True)
        (entry / "stat").write_text(_fake_stat_line(pid, start_time), encoding="utf-8")
        return start_time

    def set_show(
        self,
        *,
        scope: str = "user",
        main_pid,
        active_state: str = "active",
        sub_state: str = "running",
        fragment_path=None,
        environment=None,
    ) -> None:
        if fragment_path is None:
            fragment_path = (
                self.unit_path if scope == "user" else self.system_unit_path
            )
        if environment is None:
            environment = show_environment(self.hermes_home)
        (self.control_dir / f"show-{scope}.txt").write_text(
            f"MainPID={main_pid}\n"
            f"ActiveState={active_state}\n"
            f"SubState={sub_state}\n"
            f"FragmentPath={fragment_path}\n"
            f"Environment={environment}\n",
            encoding="utf-8",
        )

    def set_stop_rc(self, rc: int) -> None:
        (self.control_dir / "stop-rc").write_text(str(rc), encoding="utf-8")

    def set_stop_sleep(self, seconds: float) -> None:
        (self.control_dir / "stop-sleep").write_text(str(seconds), encoding="utf-8")

    # -- host dicts ------------------------------------------------------

    def host(self, **overrides) -> dict:
        host = {
            "platform": "linux",
            "euid": FAKE_UID,
            "account_exists": True,
            "pw_dir": str(self.home),
            "pw_uid": FAKE_UID,
            "env": {
                "HOME": str(self.home),
                "HERMES_HOME": str(self.hermes_home),
                "XDG_RUNTIME_DIR": str(self.runtime_dir),
                "DBUS_SESSION_BUS_ADDRESS": f"unix:path={self.runtime_dir}/bus",
            },
            "account": "hermes",
            "home": str(self.home),
            "hermes_home": str(self.hermes_home),
            "unit": "hermes-gateway.service",
            "unit_path": str(self.unit_path),
            "systemctl": str(self.systemctl),
            "proc_root": str(self.proc_root),
            "runtime_dir_root": str(self.runtime_dir_root),
            "show_timeout_s": 30,
            "stop_timeout_s": 90,
        }
        host.update(overrides)
        assert_fake_systemctl(host, self.root)
        return host

    # -- observation -----------------------------------------------------

    def systemctl_argv(self) -> list[str]:
        if not self.log.exists():
            return []
        return [
            line.split("\t", 1)[1]
            for line in self.log.read_text(encoding="utf-8").splitlines()
            if line.startswith("ARGV\t")
        ]

    def systemctl_env_lines(self) -> list[str]:
        if not self.log.exists():
            return []
        return [
            line.split("\t", 1)[1]
            for line in self.log.read_text(encoding="utf-8").splitlines()
            if line.startswith("ENV\t")
        ]


def assert_fake_systemctl(host: dict, sandbox_root: Path) -> None:
    """No test may ever be one typo away from driving the real service manager."""
    assert_systemctl_not_real(host, sandbox_root)
    assert Path(str(host["systemctl"])).is_file()


def assert_systemctl_not_real(host: dict, sandbox_root: Path) -> None:
    """The weaker check that holds even for deliberately broken host dicts.

    A binding that is absent, empty, or relative is fine — ``run_planned_stop``
    rejects each at the bindings stage and never spawns anything. Any
    *absolute* binding is one that could really be executed, so it must point
    inside the sandbox.
    """
    configured = host.get("systemctl")
    if not isinstance(configured, str) or not os.path.isabs(configured):
        return
    assert configured != "/usr/bin/systemctl"
    assert configured.startswith(str(sandbox_root)), configured


def run_stop(module, sandbox, host=None):
    """Call ``run_planned_stop``, re-checking the systemctl binding first.

    Every call in this file goes through here. ``Sandbox.host()`` checks the
    dict it builds, but tests mutate those dicts afterwards to drive
    rejections — this is the check that cannot be skipped by a later
    ``host[...] = ...``.
    """
    if host is None:
        host = sandbox.host()
    assert_systemctl_not_real(host, sandbox.root)
    return module.run_planned_stop(host)


# The live-system guard in tests/conftest.py refuses any subprocess whose
# command line mentions systemctl, a hermes unit, and a mutating verb. It reads
# the command as a string, so it cannot see that argv[0] here is a shell script
# inside tmp_path that only cats files and exits — ``assert_fake_systemctl``
# proves that for every host dict this file builds, and it runs before every
# call. Tests that actually reach the ``stop`` verb therefore opt out of the
# string check. The only ``os.kill`` these tests reach is a signal-0 liveness
# probe of a child the test itself spawned.
runs_fake_stop = pytest.mark.live_system_guard_bypass


@pytest.fixture
def sandbox(tmp_path):
    box = Sandbox(tmp_path)
    try:
        yield box
    finally:
        box.reap()


def _snapshot(root: Path) -> dict[str, object]:
    """Byte-level snapshot of a directory tree, for 'nothing was touched'."""
    snap: dict[str, object] = {}
    for path in sorted(root.rglob("*")):
        key = str(path.relative_to(root))
        snap[key] = path.read_bytes() if path.is_file() else "<dir>"
    return snap


# ── A. marker compatibility with the deployed (R0) gateway ────────────


class TestR0MarkerCompatibility:
    """The R0 consumer must accept markers the new writer produces."""

    def test_r0_consumes_marker_written_by_current_status(self, tmp_path, monkeypatch):
        home = tmp_path / "hermes-home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        r0 = _load_r0_status(home)

        assert status.write_planned_stop_marker(os.getpid()) is True
        marker = home / MARKER_NAME
        assert marker.exists()

        assert r0.consume_planned_stop_marker_for_self() is True
        assert not marker.exists()

    def test_r0_consumes_marker_built_by_protocol_module(self, tmp_path, monkeypatch):
        """The standalone stopper builds its record with the protocol helpers
        and never imports Hermes — those bytes must land the same way."""
        home = tmp_path / "hermes-home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        r0 = _load_r0_status(home)

        record = protocol.build_planned_stop_record(
            os.getpid(),
            # Observed exactly the way R0 observes it.
            r0._get_process_start_time(os.getpid()),
            os.getpid(),
        )
        marker = home / protocol.PLANNED_STOP_MARKER_FILENAME
        protocol.write_marker_atomic(marker, record)

        assert r0.consume_planned_stop_marker_for_self() is True
        assert not marker.exists()

    def test_marker_bytes_match_what_r0_would_have_written(self, tmp_path, monkeypatch):
        """The write path changed (no ``utils.atomic_json_write``); the bytes
        it lays down must not have."""
        home = tmp_path / "hermes-home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        r0 = _load_r0_status(home)
        marker = home / MARKER_NAME

        assert r0.write_planned_stop_marker(os.getpid()) is True
        r0_bytes = marker.read_bytes()
        marker.unlink()

        assert status.write_planned_stop_marker(os.getpid()) is True
        new_bytes = marker.read_bytes()

        r0_payload = json.loads(r0_bytes)
        new_payload = json.loads(new_bytes)
        # Same fields in the same order, and — ``written_at`` aside — the same
        # values. Both sides observed this process's start time independently.
        assert list(new_payload) == list(r0_payload)
        assert {k: v for k, v in new_payload.items() if k != "written_at"} == {
            k: v for k, v in r0_payload.items() if k != "written_at"
        }
        # Same encoding: compact separators, no indent, no trailing newline.
        # Comparing lengths would be flaky (``isoformat`` drops ``.ffffff``
        # when microseconds land on zero), so re-encode instead.
        for payload, raw in ((r0_payload, r0_bytes), (new_payload, new_bytes)):
            assert raw == json.dumps(
                payload, indent=None, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")

    def test_current_consumer_accepts_a_marker_r0_wrote(self, tmp_path, monkeypatch):
        """The other rollout direction: an old CLI stopping a new gateway."""
        home = tmp_path / "hermes-home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        r0 = _load_r0_status(home)

        assert r0.write_planned_stop_marker(os.getpid()) is True
        marker = home / MARKER_NAME
        assert marker.exists()

        assert status.planned_stop_marker_targets_self() is True
        assert marker.exists()  # the probe never unlinks a match
        assert status.consume_planned_stop_marker_for_self() is True
        assert not marker.exists()

    def test_r0_rejects_stale_marker(self, tmp_path, monkeypatch):
        home = tmp_path / "hermes-home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        r0 = _load_r0_status(home)

        record = protocol.build_planned_stop_record(
            os.getpid(), r0._get_process_start_time(os.getpid()), os.getpid()
        )
        stale = datetime.now(timezone.utc) - timedelta(
            seconds=protocol.PLANNED_STOP_MARKER_TTL_S + 30
        )
        record["written_at"] = stale.isoformat()
        marker = home / MARKER_NAME
        protocol.write_marker_atomic(marker, record)

        assert r0.consume_planned_stop_marker_for_self() is False
        assert not marker.exists()

    def test_r0_rejects_marker_for_another_pid(self, tmp_path, monkeypatch):
        home = tmp_path / "hermes-home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        r0 = _load_r0_status(home)

        record = protocol.build_planned_stop_record(os.getpid() + 1, 12345, os.getpid())
        marker = home / MARKER_NAME
        protocol.write_marker_atomic(marker, record)

        assert r0.consume_planned_stop_marker_for_self() is False

    @pytest.mark.skipif(
        not sys.platform.startswith("linux"), reason="needs a real /proc"
    )
    def test_start_time_observation_matches_r0(self, tmp_path, monkeypatch):
        """The fingerprint only works if both sides read /proc identically."""
        home = tmp_path / "hermes-home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        r0 = _load_r0_status(home)

        pid = os.getpid()
        assert protocol.linux_process_start_time(pid) == r0._get_process_start_time(pid)


# ── B. the current consumer accepts the same markers ──────────────────


class TestCurrentTargetCompatibility:
    def test_current_consume_accepts_protocol_marker(self, tmp_path, monkeypatch):
        home = tmp_path / "hermes-home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))

        record = protocol.build_planned_stop_record(
            os.getpid(), status._get_process_start_time(os.getpid()), os.getpid()
        )
        marker = home / MARKER_NAME
        protocol.write_marker_atomic(marker, record)

        assert status.consume_planned_stop_marker_for_self() is True
        assert not marker.exists()

    def test_probe_observes_without_unlinking(self, tmp_path, monkeypatch):
        """The watcher's probe must leave a matching marker for the shutdown
        handler's authoritative consume."""
        home = tmp_path / "hermes-home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))

        record = protocol.build_planned_stop_record(
            os.getpid(), status._get_process_start_time(os.getpid()), os.getpid()
        )
        marker = home / MARKER_NAME
        protocol.write_marker_atomic(marker, record)

        assert status.planned_stop_marker_targets_self() is True
        assert marker.exists()
        assert status.planned_stop_marker_targets_self() is True
        assert marker.exists()

        assert status.consume_planned_stop_marker_for_self() is True
        assert not marker.exists()

    def test_write_does_not_create_hermes_home(self, tmp_path, monkeypatch):
        """A missing HERMES_HOME fails the write closed rather than creating
        a directory no gateway is reading."""
        missing = tmp_path / "not-a-home"
        monkeypatch.setenv("HERMES_HOME", str(missing))

        assert status.write_planned_stop_marker(os.getpid()) is False
        assert not missing.exists()


# ── K. marker content ─────────────────────────────────────────────────


class TestMarkerContent:
    @runs_fake_stop
    def test_record_has_exactly_the_protocol_fields(self, sandbox, module):
        pid = sandbox.spawn_child()
        start_time = sandbox.write_stat(pid)
        sandbox.set_show(main_pid=pid)

        exit_code, result = run_stop(module, sandbox)

        assert exit_code == 0, result
        assert sandbox.marker_path == sandbox.hermes_home / MARKER_NAME
        assert result["marker_path"] == str(sandbox.marker_path)

        payload = json.loads(sandbox.marker_path.read_text(encoding="utf-8"))
        assert set(payload) == {
            "target_pid",
            "target_start_time",
            "stopper_pid",
            "written_at",
        }
        assert payload["target_pid"] == pid
        assert payload["target_start_time"] == start_time
        assert isinstance(payload["target_start_time"], int)
        assert payload["stopper_pid"] == os.getpid()
        assert isinstance(payload["written_at"], str)

        written_at = datetime.fromisoformat(payload["written_at"])
        age = (datetime.now(timezone.utc) - written_at).total_seconds()
        assert 0 <= age < protocol.PLANNED_STOP_MARKER_TTL_S
        assert not protocol.marker_is_stale(
            payload["written_at"], protocol.PLANNED_STOP_MARKER_TTL_S
        )

    def test_marker_bytes_are_compact_json(self, sandbox, module, tmp_path):
        """Same encoding the previous ``utils.atomic_json_write`` call produced."""
        record = {"target_pid": 1, "target_start_time": 2, "stopper_pid": 3, "written_at": "x"}
        path = tmp_path / "marker.json"
        module.write_marker_atomic(path, record)

        assert path.read_bytes() == json.dumps(
            record, indent=None, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")

    def test_write_marker_atomic_never_creates_parent(self, module, tmp_path):
        target = tmp_path / "missing" / "marker.json"
        with pytest.raises(OSError):
            module.write_marker_atomic(target, {"a": 1})
        assert not target.parent.exists()


# ── C(in-process)/G. scope validation ─────────────────────────────────


class TestHostValidation:
    """Every rejection is read-only: see TestNoPreMarkerMutation for proof."""

    @pytest.fixture(autouse=True)
    def _live_unit(self, sandbox):
        pid = sandbox.spawn_child()
        sandbox.write_stat(pid)
        sandbox.set_show(main_pid=pid)
        self.pid = pid

    def _reject(self, module, sandbox, expected_stage, **overrides):
        exit_code, result = run_stop(module, sandbox, sandbox.host(**overrides))
        assert exit_code != 0
        assert result["ok"] is False
        assert result["stage"] == expected_stage, result
        assert not sandbox.marker_path.exists()
        assert all("stop" not in argv.split() for argv in sandbox.systemctl_argv())
        return result

    @runs_fake_stop
    def test_accepts_the_bound_shape(self, module, sandbox):
        exit_code, result = run_stop(module, sandbox)
        assert exit_code == 0, result
        assert result["ok"] is True
        assert result["stage"] == "stopped"
        assert result["target_pid"] == self.pid

    def test_rejects_non_linux(self, module, sandbox):
        self._reject(module, sandbox, "platform", platform="darwin")
        self._reject(module, sandbox, "platform", platform="win32")

    def test_rejects_missing_account(self, module, sandbox):
        self._reject(module, sandbox, "account", account_exists=False)

    def test_rejects_wrong_account_home(self, module, sandbox):
        self._reject(module, sandbox, "account", pw_dir="/home/someone-else")

    def test_rejects_euid_mismatch(self, module, sandbox):
        self._reject(module, sandbox, "account", euid=FAKE_UID + 1)
        self._reject(module, sandbox, "account", euid=0)
        self._reject(module, sandbox, "account", euid=None)

    def test_rejects_wrong_home_env(self, module, sandbox):
        env = dict(sandbox.host()["env"], HOME="/root")
        self._reject(module, sandbox, "environment", env=env)

    def test_rejects_wrong_hermes_home_env(self, module, sandbox):
        env = dict(sandbox.host()["env"], HERMES_HOME="/home/hermes/.hermes-other")
        self._reject(module, sandbox, "environment", env=env)

    @runs_fake_stop
    def test_allows_unset_hermes_home_env(self, module, sandbox):
        """HERMES_HOME is optional; when present it must agree."""
        env = dict(sandbox.host()["env"])
        env["HERMES_HOME"] = None
        exit_code, result = run_stop(module, sandbox, sandbox.host(env=env))
        assert exit_code == 0, result

    def test_rejects_wrong_runtime_dir(self, module, sandbox):
        env = dict(sandbox.host()["env"], XDG_RUNTIME_DIR="/run/user/0")
        self._reject(module, sandbox, "environment", env=env)

    def test_rejects_wrong_dbus_address(self, module, sandbox):
        env = dict(
            sandbox.host()["env"],
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/0/bus",
        )
        self._reject(module, sandbox, "environment", env=env)

    def test_rejects_missing_runtime_dir(self, module, sandbox):
        sandbox.runtime_dir.rmdir()
        self._reject(module, sandbox, "paths")

    def test_rejects_missing_unit_file(self, module, sandbox):
        sandbox.unit_path.unlink()
        self._reject(module, sandbox, "paths")

    def test_rejects_missing_hermes_home(self, module, sandbox):
        sandbox.hermes_home.rmdir()
        self._reject(module, sandbox, "paths")

    @runs_fake_stop
    def test_accepts_the_managers_hermes_home(self, module, sandbox):
        """The bound home, as the service manager reports it."""
        exit_code, result = run_stop(module, sandbox)
        assert exit_code == 0, result

    def test_rejects_unit_bound_to_another_hermes_home(self, module, sandbox):
        """A gateway installed against a custom home is still called
        hermes-gateway.service — the marker would land where nobody reads it."""
        sandbox.set_show(
            main_pid=self.pid,
            environment=show_environment("/opt/data/.hermes"),
        )

        result = self._reject(module, sandbox, "unit_home")

        assert "/opt/data/.hermes" in result["detail"]

    def test_rejects_unit_with_no_hermes_home(self, module, sandbox):
        sandbox.set_show(main_pid=self.pid, environment="PATH=/usr/bin:/bin")
        self._reject(module, sandbox, "unit_home")

    def test_rejects_empty_environment_property(self, module, sandbox):
        sandbox.set_show(main_pid=self.pid, environment="")
        self._reject(module, sandbox, "unit_home")

    def test_rejects_conflicting_hermes_homes(self, module, sandbox):
        """systemd's multi-assignment form: a later token overrides an earlier
        one, so a fragment-text parse that stopped at the first correct line
        would sail past this. Two values, no way to be sure — refuse."""
        sandbox.set_show(
            main_pid=self.pid,
            environment=(
                f"PATH=/usr/bin HERMES_HOME={sandbox.hermes_home} "
                "HERMES_HOME=/opt/data/.hermes"
            ),
        )
        result = self._reject(module, sandbox, "unit_home")
        assert "/opt/data/.hermes" in result["detail"]

    def test_rejects_quoted_environment_token(self, module, sandbox):
        """A quoted token means a value with spaces, so a plain space split no
        longer finds token boundaries. Refuse rather than guess."""
        sandbox.set_show(
            main_pid=self.pid,
            environment=f'"FOO=a b" HERMES_HOME={sandbox.hermes_home}',
        )
        result = self._reject(module, sandbox, "unit_home")
        assert "not parseable" in result["detail"]

    def test_drop_in_home_is_what_counts(self, module, sandbox):
        """The manager reports the merged fragment + drop-in environment. The
        fragment on disk still says the bound home; the manager says otherwise;
        the manager wins."""
        assert f"HERMES_HOME={sandbox.hermes_home}" in sandbox.unit_path.read_text(
            encoding="utf-8"
        )
        sandbox.set_show(
            main_pid=self.pid,
            environment=show_environment("/opt/data/.hermes"),
        )
        self._reject(module, sandbox, "unit_home")

    def test_rejects_relative_path_bindings(self, module, sandbox):
        for name in (
            "home",
            "hermes_home",
            "unit_path",
            "systemctl",
            "proc_root",
            "runtime_dir_root",
        ):
            host = sandbox.host()
            host[name] = "relative/path"
            exit_code, result = run_stop(module, sandbox, host)
            assert exit_code != 0 and result["stage"] == "bindings", name
        assert not sandbox.marker_path.exists()

    def test_rejects_unexpected_fragment_path(self, module, sandbox):
        sandbox.set_show(
            main_pid=self.pid, fragment_path=sandbox.system_unit_path
        )
        self._reject(module, sandbox, "unit_identity")

    def test_rejects_missing_fragment_path(self, module, sandbox):
        (sandbox.control_dir / "show-user.txt").write_text(
            f"MainPID={self.pid}\nActiveState=active\nSubState=running\n",
            encoding="utf-8",
        )
        self._reject(module, sandbox, "unit_identity")

    def test_rejects_inactive_unit(self, module, sandbox):
        sandbox.set_show(main_pid=self.pid, active_state="failed")
        self._reject(module, sandbox, "unit_state")

    def test_rejects_non_running_substate(self, module, sandbox):
        sandbox.set_show(main_pid=self.pid, sub_state="start-pre")
        self._reject(module, sandbox, "unit_state")

    def test_rejects_zero_main_pid(self, module, sandbox):
        sandbox.set_show(main_pid=0)
        self._reject(module, sandbox, "unit_pid")

    def test_rejects_negative_main_pid(self, module, sandbox):
        sandbox.set_show(main_pid=-1)
        self._reject(module, sandbox, "unit_pid")

    def test_rejects_non_numeric_main_pid(self, module, sandbox):
        sandbox.set_show(main_pid="not-a-pid")
        self._reject(module, sandbox, "unit_pid")

    def test_rejects_pid_shapes_int_would_have_accepted(self, module, sandbox):
        """``int()`` is lenient; a MainPID systemd never emits must not slip
        through and hand us some other process to fingerprint."""
        for forged in (f" {self.pid} ", f"+{self.pid}", "1_000", "١٢٣"):
            sandbox.set_show(main_pid=forged)
            self._reject(module, sandbox, "unit_pid")

    def test_rejects_forged_property_line(self, module, sandbox):
        """A property value carrying a newline must not be able to answer for
        a property we never got a real answer to."""
        (sandbox.control_dir / "show-user.txt").write_text(
            f"MainPID={self.pid}\n"
            "ActiveState=active\n"
            "SubState=running\n"
            f"FragmentPath=/tmp/evil\nFragmentPath={sandbox.unit_path}\n",
            encoding="utf-8",
        )
        self._reject(module, sandbox, "unit_show")

    def test_rejects_unknown_property_in_output(self, module, sandbox):
        (sandbox.control_dir / "show-user.txt").write_text(
            f"MainPID={self.pid}\n"
            "ActiveState=active\n"
            "SubState=running\n"
            f"FragmentPath={sandbox.unit_path}\n"
            "ExecMainPID=1\n",
            encoding="utf-8",
        )
        self._reject(module, sandbox, "unit_show")

    def test_rejects_incomplete_host_bindings(self, module, sandbox):
        for name in (
            "account",
            "home",
            "hermes_home",
            "unit",
            "unit_path",
            "systemctl",
            "proc_root",
            "runtime_dir_root",
        ):
            host = sandbox.host()
            host[name] = None
            exit_code, result = run_stop(module, sandbox, host)
            assert exit_code != 0 and result["stage"] == "bindings", name
        for name in ("show_timeout_s", "stop_timeout_s"):
            host = sandbox.host()
            host[name] = 0
            exit_code, result = run_stop(module, sandbox, host)
            assert exit_code != 0 and result["stage"] == "bindings", name
        assert not sandbox.marker_path.exists()


# ── H. system scope is never consulted ────────────────────────────────


class TestUserScopeOnly:
    @runs_fake_stop
    def test_every_invocation_is_user_scoped(self, module, sandbox):
        pid = sandbox.spawn_child()
        sandbox.write_stat(pid)
        sandbox.set_show(main_pid=pid)
        # A system-scope answer that would sail through validation if it were
        # ever consulted — different PID, so using it is instantly visible.
        sandbox.set_show(scope="system", main_pid=999999)
        system_unit_before = sandbox.system_unit_path.read_bytes()

        exit_code, result = run_stop(module, sandbox)

        assert exit_code == 0, result
        assert result["target_pid"] == pid

        invocations = sandbox.systemctl_argv()
        assert len(invocations) == 2
        for argv in invocations:
            assert "--user" in argv.split()
            assert "--system" not in argv.split()
            assert str(sandbox.system_unit_path) not in argv
        assert invocations[0].split()[:3] == ["--user", "show", "hermes-gateway.service"]
        assert invocations[1].split() == ["--user", "stop", "hermes-gateway.service"]
        # The system unit was left completely alone.
        assert sandbox.system_unit_path.read_bytes() == system_unit_before
        assert "(system)" in sandbox.system_unit_path.read_text(encoding="utf-8")


# ── I. PID and start-time checks ──────────────────────────────────────


class TestTargetProcessIdentity:
    @runs_fake_stop
    def test_accepts_live_child_with_matching_stat(self, module, sandbox):
        pid = sandbox.spawn_child()
        start_time = sandbox.write_stat(pid, start_time=555111)
        sandbox.set_show(main_pid=pid)

        exit_code, result = run_stop(module, sandbox)

        assert exit_code == 0, result
        assert result["target_pid"] == pid
        assert result["target_start_time"] == start_time

    def test_rejects_dead_main_pid(self, module, sandbox):
        # A PID confirmed dead, not merely reaped — see spawn_dead_pid.
        pid = sandbox.spawn_dead_pid()
        sandbox.write_stat(pid)
        sandbox.set_show(main_pid=pid)  # systemd still claims it

        exit_code, result = run_stop(module, sandbox)

        assert exit_code != 0
        assert result["stage"] == "process"
        assert not sandbox.marker_path.exists()

    def test_rejects_missing_proc_entry(self, module, sandbox):
        pid = sandbox.spawn_child()
        sandbox.set_show(main_pid=pid)  # alive, but no <proc_root>/<pid>/stat

        exit_code, result = run_stop(module, sandbox)

        assert exit_code != 0
        assert result["stage"] == "start_time"
        assert not sandbox.marker_path.exists()

    def test_rejects_malformed_stat(self, module, sandbox):
        pid = sandbox.spawn_child()
        entry = sandbox.proc_root / str(pid)
        entry.mkdir(parents=True)
        (entry / "stat").write_text("garbage without enough fields\n", encoding="utf-8")
        sandbox.set_show(main_pid=pid)

        exit_code, result = run_stop(module, sandbox)

        assert exit_code != 0
        assert result["stage"] == "start_time"
        assert not sandbox.marker_path.exists()

    def test_start_time_reads_the_configured_proc_root(self, module, sandbox):
        assert module.linux_process_start_time(1, str(sandbox.proc_root)) is None
        sandbox.write_stat(1, start_time=42)
        assert module.linux_process_start_time(1, str(sandbox.proc_root)) == 42


# ── J. nothing is mutated before the marker ───────────────────────────


class TestNoPreMarkerMutation:
    """A rejected run must leave the gateway's runtime files untouched."""

    @pytest.fixture
    def populated(self, sandbox):
        contents = {
            "gateway.pid": b'{"pid":4321,"kind":"hermes-gateway"}',
            "gateway.lock": b"lock-owner-bytes",
            "gateway_state.json": b'{"state":"running"}',
        }
        for name, blob in contents.items():
            (sandbox.hermes_home / name).write_bytes(blob)
        return contents

    def _assert_untouched(self, sandbox, populated, before):
        assert not sandbox.marker_path.exists()
        for name, blob in populated.items():
            assert (sandbox.hermes_home / name).read_bytes() == blob
        assert _snapshot(sandbox.hermes_home) == before
        assert all("stop" not in argv.split() for argv in sandbox.systemctl_argv())

    def test_environment_mismatch_changes_nothing(self, module, sandbox, populated):
        pid = sandbox.spawn_child()
        sandbox.write_stat(pid)
        sandbox.set_show(main_pid=pid)
        before = _snapshot(sandbox.hermes_home)

        env = dict(sandbox.host()["env"], XDG_RUNTIME_DIR="/run/user/0")
        exit_code, result = run_stop(module, sandbox, sandbox.host(env=env))

        assert exit_code != 0 and result["stage"] == "environment"
        self._assert_untouched(sandbox, populated, before)
        # Never even asked systemctl anything.
        assert sandbox.systemctl_argv() == []

    def test_inactive_unit_changes_nothing(self, module, sandbox, populated):
        pid = sandbox.spawn_child()
        sandbox.write_stat(pid)
        sandbox.set_show(main_pid=pid, active_state="inactive", sub_state="dead")
        before = _snapshot(sandbox.hermes_home)

        exit_code, result = run_stop(module, sandbox)

        assert exit_code != 0 and result["stage"] == "unit_state"
        self._assert_untouched(sandbox, populated, before)
        assert len(sandbox.systemctl_argv()) == 1

    def test_dead_target_changes_nothing(self, module, sandbox, populated):
        pid = sandbox.spawn_dead_pid()
        sandbox.write_stat(pid)
        sandbox.set_show(main_pid=pid)
        before = _snapshot(sandbox.hermes_home)

        exit_code, result = run_stop(module, sandbox)

        assert exit_code != 0 and result["stage"] == "process"
        self._assert_untouched(sandbox, populated, before)

    def test_whole_sandbox_tree_is_unchanged_on_rejection(
        self, module, sandbox, populated
    ):
        pid = sandbox.spawn_child()
        sandbox.write_stat(pid)
        sandbox.set_show(main_pid=pid)
        # Snapshot everything except the systemctl call log, which the
        # rejected `show` legitimately appends to.
        before = {
            key: value
            for key, value in _snapshot(sandbox.root).items()
            if not key.startswith("systemctl-control")
        }

        exit_code, _ = run_stop(module, sandbox, sandbox.host(pw_dir="/home/nobody"))

        assert exit_code != 0
        after = {
            key: value
            for key, value in _snapshot(sandbox.root).items()
            if not key.startswith("systemctl-control")
        }
        assert after == before


# ── L. the stop invocation itself ─────────────────────────────────────


class TestStopInvocation:
    @staticmethod
    def _fake_subprocess(sandbox, calls, *, show_stdout, stop_result):
        def run(argv, **kwargs):
            calls.append((list(argv), kwargs))
            if "show" in argv:
                return SimpleNamespace(returncode=0, stdout=show_stdout, stderr=b"")
            if isinstance(stop_result, BaseException):
                raise stop_result
            return stop_result

        return SimpleNamespace(
            run=run,
            TimeoutExpired=subprocess.TimeoutExpired,
        )

    def _show_stdout(self, sandbox, pid):
        return (
            f"MainPID={pid}\nActiveState=active\nSubState=running\n"
            f"FragmentPath={sandbox.unit_path}\n"
            f"Environment={show_environment(sandbox.hermes_home)}\n"
        ).encode("utf-8")

    def test_stop_argv_env_and_timeout_are_exact(self, module, sandbox, monkeypatch):
        pid = sandbox.spawn_child()
        sandbox.write_stat(pid)
        calls: list = []
        monkeypatch.setattr(
            module,
            "subprocess",
            self._fake_subprocess(
                sandbox,
                calls,
                show_stdout=self._show_stdout(sandbox, pid),
                stop_result=SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
            ),
        )

        exit_code, result = run_stop(module, sandbox)

        assert exit_code == 0, result
        assert len(calls) == 2

        expected_env = {
            "HOME": str(sandbox.home),
            "XDG_RUNTIME_DIR": str(sandbox.runtime_dir),
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={sandbox.runtime_dir}/bus",
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        }

        show_argv, show_kwargs = calls[0]
        assert show_argv == [
            str(sandbox.systemctl),
            "--user",
            "show",
            "hermes-gateway.service",
            "--property=MainPID,ActiveState,SubState,FragmentPath,Environment",
        ]
        assert show_kwargs["timeout"] == 30
        assert show_kwargs["env"] == expected_env
        assert show_kwargs["capture_output"] is True

        stop_argv, stop_kwargs = calls[1]
        assert stop_argv == [
            str(sandbox.systemctl),
            "--user",
            "stop",
            "hermes-gateway.service",
        ]
        assert stop_kwargs["timeout"] == 90
        assert set(stop_kwargs["env"]) == set(expected_env)
        assert stop_kwargs["env"] == expected_env
        assert stop_kwargs["capture_output"] is True

    def test_stop_timeout_is_reported_and_marker_kept(
        self, module, sandbox, monkeypatch
    ):
        pid = sandbox.spawn_child()
        sandbox.write_stat(pid)
        calls: list = []
        monkeypatch.setattr(
            module,
            "subprocess",
            self._fake_subprocess(
                sandbox,
                calls,
                show_stdout=self._show_stdout(sandbox, pid),
                stop_result=subprocess.TimeoutExpired(cmd="systemctl", timeout=90),
            ),
        )

        exit_code, result = run_stop(module, sandbox)

        assert exit_code != 0
        assert result["stage"] == "stop"
        assert "timed out" in result["detail"]
        assert len(calls) == 2  # no retry
        # The marker stays: it is idempotent and its own TTL retires it.
        assert sandbox.marker_path.exists()


# ── M. a failing stop ─────────────────────────────────────────────────


class TestStopFailure:
    @runs_fake_stop
    def test_non_zero_stop_reports_failure_without_retry(self, module, sandbox):
        pid = sandbox.spawn_child()
        sandbox.write_stat(pid)
        sandbox.set_show(main_pid=pid)
        sandbox.set_stop_rc(1)

        exit_code, result = run_stop(module, sandbox)

        assert exit_code != 0
        assert result["ok"] is False
        assert result["stage"] == "stop"
        assert result["target_pid"] == pid
        assert "exited 1" in result["detail"]

        invocations = sandbox.systemctl_argv()
        assert [argv.split()[1] for argv in invocations] == ["show", "stop"]
        # No retry, and no other verb was reached for.
        assert len(invocations) == 2

        # The marker is deliberately left behind; the TTL bounds it.
        assert sandbox.marker_path.exists()
        payload = json.loads(sandbox.marker_path.read_text(encoding="utf-8"))
        assert payload["target_pid"] == pid

    @runs_fake_stop
    def test_stop_that_overruns_the_timeout_fails_closed(self, module, sandbox):
        pid = sandbox.spawn_child()
        sandbox.write_stat(pid)
        sandbox.set_show(main_pid=pid)
        sandbox.set_stop_sleep(5)

        started = time.monotonic()
        exit_code, result = run_stop(module, sandbox, sandbox.host(stop_timeout_s=1))
        elapsed = time.monotonic() - started

        assert exit_code != 0
        assert result["stage"] == "stop"
        assert "timed out" in result["detail"]
        assert elapsed < 5
        assert len(sandbox.systemctl_argv()) == 2

    def test_missing_systemctl_fails_closed(self, module, sandbox):
        pid = sandbox.spawn_child()
        sandbox.write_stat(pid)
        sandbox.set_show(main_pid=pid)
        host = sandbox.host()
        gone = sandbox.control_dir / "systemctl-gone"
        gone.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        gone.chmod(0o755)
        host["systemctl"] = str(gone)
        gone.unlink()

        exit_code, result = run_stop(module, sandbox, host)

        assert exit_code != 0
        assert result["stage"] == "unit_show"
        assert not sandbox.marker_path.exists()


# ── protocol helpers in isolation ─────────────────────────────────────


class TestProtocolHelpers:
    def test_marker_is_stale_boundaries(self, module):
        fresh = datetime.now(timezone.utc).isoformat()
        assert module.marker_is_stale(fresh, 60) is False

        old = (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()
        assert module.marker_is_stale(old, 60) is True

        assert module.marker_is_stale("not-a-timestamp", 60) is True
        assert module.marker_is_stale(None, 60) is True
        # Naive timestamps cannot be compared to an aware "now" — treat as stale.
        assert module.marker_is_stale("2030-01-01T00:00:00", 60) is True

    def test_pid_marker_matches_rules(self, module):
        assert module.pid_marker_matches(10, 5, 10, 5) is True
        assert module.pid_marker_matches(10, 5, 10, 6) is False
        assert module.pid_marker_matches(10, 5, 11, 5) is False
        # Either side unknown: PID equality alone (macOS / Windows).
        assert module.pid_marker_matches(10, None, 10, 5) is True
        assert module.pid_marker_matches(10, 5, 10, None) is True
        assert module.pid_marker_matches(10, None, 10, None) is True
        assert module.pid_marker_matches(10, None, 11, None) is False

    def test_environment_hermes_homes_shapes(self, module):
        parse = module.environment_hermes_homes

        assert parse(show_environment("/home/hermes/.hermes")) == [
            "/home/hermes/.hermes"
        ]
        # Every assignment is reported, so the caller can refuse a conflict.
        assert parse("HERMES_HOME=/a PATH=/bin HERMES_HOME=/b") == ["/a", "/b"]
        # Values containing '=' survive intact.
        assert parse("HERMES_HOME=/opt/a=b") == ["/opt/a=b"]
        # Not us: other variables, and one that merely ends in the name.
        assert parse("PATH=/usr/bin VIRTUAL_ENV=/v") == []
        assert parse("OTHER_HERMES_HOME=/z") == []
        assert parse("") == []
        # Unparseable, not "no assignments": a quoted token means a value with
        # spaces, so the space split no longer finds boundaries.
        assert parse('"FOO=a b" HERMES_HOME=/x') is None
        assert parse('HERMES_HOME=/x "FOO=a b"') is None
        assert parse(None) is None

    def test_python_version_gate(self, module):
        assert module.MINIMUM_PYTHON == (3, 11)
        assert module.python_version_supported((3, 9, 6)) is False
        assert module.python_version_supported((3, 10, 14)) is False
        assert module.python_version_supported((3, 11, 0)) is True
        assert module.python_version_supported((3, 13, 1)) is True
        # The interpreter running these tests must itself clear the floor.
        assert module.python_version_supported(sys.version_info) is True

    def test_status_uses_the_shared_helpers(self):
        """The refactor's whole point: one definition, not two that agree today."""
        assert status._marker_is_stale is protocol.marker_is_stale
        assert status._PLANNED_STOP_MARKER_FILENAME == protocol.PLANNED_STOP_MARKER_FILENAME
        assert status._PLANNED_STOP_MARKER_TTL_S == protocol.PLANNED_STOP_MARKER_TTL_S

    def test_module_still_executes_on_an_old_interpreter(self):
        """The version gate can only report *why* it refused if the file gets
        far enough to reach it, so everything above the gate has to stay
        runnable on the interpreters it exists to turn away.

        Two distinct hazards, both checked here because the end-to-end
        version-gate test skips on a machine with no old interpreter:
        syntax an old parser rejects outright, and ``X | Y`` annotations —
        valid 3.9 syntax, but a TypeError when the ``def`` executes.
        """
        import ast

        source = PROTOCOL_PATH.read_text(encoding="utf-8")
        ast.parse(source, feature_version=(3, 9))

        tree = ast.parse(source)

        def _annotations(node):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.returns is not None:
                    yield node.returns
                args = node.args
                for arg in (
                    *args.posonlyargs,
                    *args.args,
                    *args.kwonlyargs,
                    args.vararg,
                    args.kwarg,
                ):
                    if arg is not None and arg.annotation is not None:
                        yield arg.annotation
            elif isinstance(node, ast.AnnAssign):
                yield node.annotation

        for node in ast.walk(tree):
            for annotation in _annotations(node):
                for part in ast.walk(annotation):
                    assert not (
                        isinstance(part, ast.BinOp) and isinstance(part.op, ast.BitOr)
                    ), f"`X | Y` annotation breaks pre-3.10: {ast.unparse(annotation)}"

    def test_module_imports_only_stdlib(self):
        """No Hermes, no third-party imports anywhere in the file."""
        import ast

        tree = ast.parse(PROTOCOL_PATH.read_text(encoding="utf-8"))
        allowed = {
            "json",
            "os",
            "subprocess",
            "sys",
            "tempfile",
            "datetime",
            "pathlib",
            "typing",
            "pwd",
        }
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    pytest.fail("relative import would break standalone execution")
                imported.add((node.module or "").split(".")[0])
        assert imported <= allowed, imported - allowed
