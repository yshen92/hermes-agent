"""Tests for the planned-stop marker watcher thread (gateway/run.py).

The watcher is the Windows-fallback path for the v0.13.0 session-resume
feature — on Windows ``asyncio.add_signal_handler`` raises
NotImplementedError, so the SIGTERM signal handler never runs and the
shutdown drain (which writes ``resume_pending=True``) is skipped. The
watcher closes this gap by polling for the planned-stop marker file
and translating its existence into the same shutdown-handler call a
real SIGTERM would have produced.

See issue #33778 for the original Windows session-loss bug report.
"""

import asyncio
import json
import os
import signal
import threading
import time
from unittest.mock import MagicMock

import pytest

from gateway.config import GatewayConfig
import gateway.run as gateway_run
from gateway.run import _run_planned_stop_watcher
from gateway import status as status_mod


def _write_self_marker(marker, *, stale: bool = False):
    """Write a planned-stop marker that targets the CURRENT process.

    The watcher only fires for markers naming our PID + start_time (the
    fix for issue #34597), so tests that expect a fire must write a
    self-targeting marker. Pass ``stale=True`` to backdate ``written_at``
    past the TTL.
    """
    written_at = "2000-01-01T00:00:00+00:00" if stale else status_mod._utc_now_iso()
    record = {
        "target_pid": os.getpid(),
        "target_start_time": status_mod._get_process_start_time(os.getpid()),
        "stopper_pid": os.getpid(),
        "written_at": written_at,
    }
    marker.write_text(json.dumps(record), encoding="utf-8")


class _FakeRunner:
    """Stand-in for GatewayRunner — only exposes the two flags the watcher reads."""

    def __init__(self, *, running: bool = True, draining: bool = False):
        self._running = running
        self._draining = draining


def _make_loop_capturing_calls():
    """Build a fake asyncio loop whose call_soon_threadsafe records its args."""
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    loop._captured = []

    def fake_call_soon_threadsafe(fn, *args):
        loop._captured.append((fn, args))

    loop.call_soon_threadsafe = fake_call_soon_threadsafe
    return loop


def test_watcher_fires_shutdown_when_marker_appears(tmp_path, monkeypatch):
    """When a marker targeting THIS process exists, fire the shutdown handler."""
    marker = tmp_path / ".gateway-planned-stop.json"

    # Patch the marker-path resolver so the watcher polls our temp location.
    monkeypatch.setattr(status_mod, "_get_planned_stop_marker_path", lambda: marker)

    runner = _FakeRunner(running=True, draining=False)
    loop = _make_loop_capturing_calls()
    shutdown_handler = MagicMock(name="shutdown_signal_handler")
    stop_event = threading.Event()

    # Drop a self-targeting marker before the thread starts.
    _write_self_marker(marker)

    watcher = threading.Thread(
        target=_run_planned_stop_watcher,
        args=(stop_event, runner, loop, shutdown_handler),
        kwargs={"poll_interval": 0.05},
        daemon=True,
    )
    watcher.start()
    watcher.join(timeout=10.0)

    assert not watcher.is_alive(), "Watcher should exit after firing"
    assert len(loop._captured) == 1, (
        f"Expected exactly one shutdown invocation, got {loop._captured}"
    )
    fn, args = loop._captured[0]
    assert fn is shutdown_handler
    # The handler must be called with signal=None (planned stop sentinel).
    assert args == (None,)


def test_watcher_tolerates_marker_path_resolution_errors(tmp_path, monkeypatch, caplog):
    """If _get_planned_stop_marker_path() raises, the watcher logs and continues."""
    from gateway import status as status_mod

    call_count = [0]
    def explode():
        call_count[0] += 1
        # First call (the one outside the loop, at thread start) is fine —
        # but subsequent .exists() calls on a corrupt Path could explode.
        if call_count[0] == 1:
            return tmp_path / "nonexistent"
        raise OSError("filesystem failed")

    monkeypatch.setattr(status_mod, "_get_planned_stop_marker_path", explode)

    runner = _FakeRunner(running=True, draining=False)
    loop = _make_loop_capturing_calls()
    stop_event = threading.Event()

    watcher = threading.Thread(
        target=_run_planned_stop_watcher,
        args=(stop_event, runner, loop, MagicMock()),
        kwargs={"poll_interval": 0.05},
        daemon=True,
    )
    watcher.start()
    time.sleep(0.2)
    stop_event.set()
    watcher.join(timeout=10.0)

    assert not watcher.is_alive(), "Watcher should still honour stop_event after errors"
    # No shutdown fired because the marker never reported existence.
    assert loop._captured == []


# ---------------------------------------------------------------------------
# Regression coverage for issue #34597:
# A marker left behind by a PREVIOUS gateway instance (different PID, or
# past its TTL) must NOT crash the freshly booted gateway. The watcher
# only fires when the marker targets the current process, and self-heals
# by cleaning up stale/malformed markers.
# ---------------------------------------------------------------------------


def test_watcher_does_not_fire_for_foreign_pid_marker(tmp_path, monkeypatch):
    """A marker naming a DIFFERENT process must not trigger our shutdown.

    This is the core #34597 regression: a stale marker from a prior
    gateway instance was firing the handler, driving the new gateway into
    a false "Received UNKNOWN" shutdown and a watchdog crash loop.
    """
    marker = tmp_path / ".gateway-planned-stop.json"
    # Foreign PID + a start_time that cannot match ours, freshly written
    # so the TTL does NOT remove it — the watcher must still decline.
    record = {
        "target_pid": os.getpid() + 1,
        "target_start_time": -1,
        "stopper_pid": os.getpid() + 1,
        "written_at": status_mod._utc_now_iso(),
    }
    marker.write_text(json.dumps(record), encoding="utf-8")

    monkeypatch.setattr(status_mod, "_get_planned_stop_marker_path", lambda: marker)

    runner = _FakeRunner(running=True, draining=False)
    loop = _make_loop_capturing_calls()
    shutdown_handler = MagicMock(name="shutdown_signal_handler")
    stop_event = threading.Event()

    watcher = threading.Thread(
        target=_run_planned_stop_watcher,
        args=(stop_event, runner, loop, shutdown_handler),
        kwargs={"poll_interval": 0.05},
        daemon=True,
    )
    watcher.start()
    time.sleep(0.2)  # several poll cycles
    stop_event.set()
    watcher.join(timeout=10.0)

    assert not watcher.is_alive()
    assert loop._captured == [], (
        f"Watcher fired on a foreign-PID marker (#34597 regression): {loop._captured}"
    )
    shutdown_handler.assert_not_called()
    # Foreign (but live) marker is left in place — it may still belong to
    # the process it names.
    assert marker.exists()


def test_planned_stop_marker_targets_self_probe_is_non_destructive(tmp_path, monkeypatch):
    """The probe returns True for a self-marker WITHOUT unlinking it.

    The shutdown handler performs the authoritative consume on its own
    thread, so the watcher's probe must leave a matching marker intact.
    """
    marker = tmp_path / ".gateway-planned-stop.json"
    _write_self_marker(marker)
    monkeypatch.setattr(status_mod, "_get_planned_stop_marker_path", lambda: marker)

    assert status_mod.planned_stop_marker_targets_self() is True
    assert marker.exists(), "Probe must not consume a matching marker"
    # Idempotent: still True on a second call.
    assert status_mod.planned_stop_marker_targets_self() is True


async def _run_gateway_signal_sequence(
    monkeypatch, tmp_path, *, planned_first, late_planned=False
):
    """Drive start_gateway through its real signal handler and exit decision."""
    marker = tmp_path / ".gateway-planned-stop.json"
    handlers = {}
    runners = []

    class _Runner:
        def __init__(self, config):
            self.config = config
            self.adapters = {}
            self._running = False
            self._draining = False
            self._external_drain_active = False
            self._restart_requested = False
            self._restart_via_service = False
            self._signal_initiated_shutdown = False
            self.should_exit_cleanly = False
            self.should_exit_with_failure = False
            self.exit_reason = None
            self.exit_code = None
            self._shutdown = asyncio.Event()
            self._stop_calls = 0
            runners.append(self)

        async def start(self):
            self._running = True
            if not planned_first:
                asyncio.get_running_loop().call_soon(handlers[signal.SIGTERM])
            return True

        async def stop(self):
            self._stop_calls += 1
            self._running = False
            self._draining = True
            if planned_first and self._stop_calls == 1:
                # Reproduce the live ordering: the watcher consumed the marker,
                # then systemd delivered its SIGTERM before shutdown completed.
                handlers[signal.SIGTERM]()
            elif late_planned and self._stop_calls == 1:
                # Classification is monotonic in both directions: a marker
                # arriving after a genuine unexpected signal cannot clean it.
                _write_self_marker(marker)
                handlers[signal.SIGTERM]()
            self._shutdown.set()

        async def wait_for_shutdown(self):
            await self._shutdown.wait()

    class _CronProvider:
        def start(self, stop_event, **kwargs):
            return None

        def stop(self):
            return None

    loop = asyncio.get_running_loop()

    def capture_signal_handler(sig, handler, *args):
        handlers[sig] = lambda: handler(*args)

    monkeypatch.setattr(loop, "add_signal_handler", capture_signal_handler)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "GatewayRunner", _Runner)
    monkeypatch.setattr(gateway_run, "_start_gateway_housekeeping", lambda *a, **k: None)
    monkeypatch.setattr(gateway_run, "_ensure_windows_gateway_venv_imports", lambda: None)
    monkeypatch.setattr("gateway.code_skew.record_boot_fingerprint", lambda: None)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr("gateway.status.acquire_gateway_runtime_lock", lambda: True)
    monkeypatch.setattr("gateway.status.write_pid_file", lambda: None)
    monkeypatch.setattr("gateway.status.remove_pid_file", lambda: None)
    monkeypatch.setattr("gateway.status.release_gateway_runtime_lock", lambda: None)
    monkeypatch.setattr("gateway.status._get_planned_stop_marker_path", lambda: marker)
    monkeypatch.setattr("gateway.lifecycle_ledger.record_startup", lambda: None)
    monkeypatch.setattr("gateway.shutdown_forensics.snapshot_shutdown_context", lambda sig: None)
    monkeypatch.setattr("tools.skills_sync.sync_skills", lambda quiet=True: None)
    monkeypatch.setattr("hermes_logging.setup_logging", lambda **kwargs: tmp_path)
    monkeypatch.setattr("hermes_cli.security_audit_startup.log_startup_security_warnings", lambda **kwargs: None)
    monkeypatch.setattr("hermes_cli.nous_auth_keepalive.start_nous_auth_keepalive", lambda: None)
    monkeypatch.setattr("hermes_cli.nous_auth_keepalive.stop_nous_auth_keepalive", lambda: None)
    monkeypatch.setattr("tools.mcp_tool.discover_mcp_tools", lambda: None)
    monkeypatch.setattr("tools.mcp_tool.shutdown_mcp_servers", lambda: None)
    monkeypatch.setattr("cron.scheduler_provider.resolve_cron_scheduler", lambda: _CronProvider())

    if planned_first:
        _write_self_marker(marker)

    result = await asyncio.wait_for(
        gateway_run.start_gateway(
            config=GatewayConfig(), replace=False, verbosity=None
        ),
        timeout=5,
    )
    if planned_first:
        assert not marker.exists(), "The watcher path must consume the marker"
    return result, runners[0]


@pytest.mark.asyncio
async def test_watcher_first_then_sigterm_remains_planned(tmp_path, monkeypatch):
    """A duplicate SIGTERM cannot downgrade a watcher-classified planned stop."""
    result, runner = await _run_gateway_signal_sequence(
        monkeypatch, tmp_path, planned_first=True
    )

    assert result is True
    assert runner._signal_initiated_shutdown is False


@pytest.mark.asyncio
async def test_unmarked_sigterm_remains_unexpected(tmp_path, monkeypatch):
    """A first-arrival SIGTERM without a marker still requests revival."""
    result, runner = await _run_gateway_signal_sequence(
        monkeypatch, tmp_path, planned_first=False
    )

    assert result is False
    assert runner._signal_initiated_shutdown is True


@pytest.mark.asyncio
async def test_unexpected_shutdown_cannot_be_reclassified_planned(
    tmp_path, monkeypatch
):
    """A marker arriving after an unmarked SIGTERM cannot clean the exit."""
    result, runner = await _run_gateway_signal_sequence(
        monkeypatch, tmp_path, planned_first=False, late_planned=True
    )

    assert result is False
    assert runner._signal_initiated_shutdown is True
