"""Tests for running the planned-stop protocol as a standalone program.

The deployment executor has no Hermes checkout to import from: it streams the
exact bytes of ``gateway/planned_stop_protocol.py`` into

    python -I -S -B -

and lets ``main()`` do the work. These tests run that real invocation, with a
prelude that supplies a fixture host, and check the two properties that make
it safe to point at a production account:

* it stops what it was told to stop, and nothing else;
* it executes **only** those bytes — no Hermes package, no user plugin, no
  ``sitecustomize``, no ``.pth`` hook, and no ``PYTHONPATH`` module gets a
  chance to run first.

``TestExecutionEnvelope`` is where the flags themselves are under test:
``-P -S -B`` still honours ``PYTHONPATH``, so a planted ``json.py`` shadows
the stdlib; ``-I`` implies ``-E`` and closes that. Both control runs assert
their fixture actually fires before the isolated run asserts it does not.

Everything the child touches lives in ``tmp_path``: a fake HOME, a fake
HERMES_HOME, a fake ``/proc``, a fake ``systemctl`` shell script, and a
disposable child process this test spawned. The prelude re-asserts the fake
``systemctl`` binding inside the child, so even a broken fixture cannot reach
a real service manager.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.gateway.test_planned_stop_protocol import (  # noqa: E402
    MARKER_NAME,
    PROTOCOL_PATH,
    REPO_ROOT,
    Sandbox,
    assert_fake_systemctl,
)


HELPER_SOURCE = PROTOCOL_PATH.read_text(encoding="utf-8")

# Any import of these top-level names would mean the helper dragged in Hermes
# (or its dependency tree) instead of standing alone.
BANNED_IMPORT_ROOTS = (
    "hermes_cli",
    "gateway",
    "providers",
    "agent",
    "dotenv",
    "hermes_constants",
    "utils",
)


@pytest.fixture
def sandbox(tmp_path):
    box = Sandbox(tmp_path)
    try:
        yield box
    finally:
        box.reap()


def _prelude(host: dict, sandbox_root: Path, *, extra: str = "") -> str:
    """Python source that installs the fixture host before the helper runs."""
    return (
        "PLANNED_STOP_TEST_HOST = " + repr(host) + "\n"
        # Fail closed inside the child too: no fixture mistake may ever let
        # this run reach a real service manager or a real HERMES_HOME.
        f"_SANDBOX = {str(sandbox_root)!r}\n"
        "assert PLANNED_STOP_TEST_HOST['systemctl'].startswith(_SANDBOX)\n"
        "assert PLANNED_STOP_TEST_HOST['hermes_home'].startswith(_SANDBOX)\n"
        "assert PLANNED_STOP_TEST_HOST['unit_path'].startswith(_SANDBOX)\n"
        "assert PLANNED_STOP_TEST_HOST['proc_root'].startswith(_SANDBOX)\n"
        + extra
    )


# The envelope the module docstring recommends. ``-I`` is the load-bearing
# flag: it implies ``-E``, so a PYTHONPATH entry cannot shadow a stdlib module
# by name. ``-I`` does NOT imply ``-S``, hence both.
ISOLATED_FLAGS = ("-I", "-S", "-B")
# The weaker envelope. Safe only because the caller builds the child's
# environment from empty; ``TestExecutionEnvelope`` proves both halves.
NO_PATH_PREPEND_FLAGS = ("-P", "-S", "-B")

MINIMAL_ENV = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}


def _run_standalone(
    host: dict,
    sandbox_root: Path,
    *,
    extra_prelude: str = "",
    env=None,
    cwd=None,
    flags=ISOLATED_FLAGS,
    executable=None,
):
    """Execute the helper the way the deployment executor does."""
    stdin = _prelude(host, sandbox_root, extra=extra_prelude) + "\n" + HELPER_SOURCE
    return subprocess.run(
        [executable or sys.executable, *flags, "-"],
        input=stdin.encode("utf-8"),
        capture_output=True,
        # A constructed environment, never the test runner's own.
        env=dict(MINIMAL_ENV) if env is None else env,
        cwd=str(cwd) if cwd else None,
        timeout=120,
    )


def _interpreter_below_minimum():
    """An interpreter older than Hermes' floor, if this machine has one.

    macOS ships 3.9 at /usr/bin/python3 and many Linux images ship an older
    system python alongside the venv, which is exactly the mistake the in-band
    version gate exists to catch.
    """
    candidates = ["/usr/bin/python3", "/usr/bin/python3.9", "/usr/bin/python3.10"]
    for executable in candidates:
        if not Path(executable).exists():
            continue
        probe = subprocess.run(
            [executable, "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
            capture_output=True,
            timeout=60,
        )
        if probe.returncode != 0:
            continue
        try:
            major, minor = (int(part) for part in probe.stdout.split())
        except ValueError:
            continue
        if (major, minor) < (3, 11):
            return executable
    return None


def _sole_json_line(completed) -> dict:
    lines = completed.stdout.decode("utf-8").splitlines()
    assert len(lines) == 1, completed.stdout
    return json.loads(lines[0])


# ── C. it runs, stdlib-only, and stops the unit ───────────────────────


class TestStandaloneExecution:
    def test_stops_the_bound_unit(self, sandbox):
        pid = sandbox.spawn_child()
        start_time = sandbox.write_stat(pid, start_time=778899)
        sandbox.set_show(main_pid=pid)
        host = sandbox.host()
        assert_fake_systemctl(host, sandbox.root)

        completed = _run_standalone(host, sandbox.root)

        assert completed.returncode == 0, completed.stderr.decode("utf-8")
        result = _sole_json_line(completed)
        assert result["ok"] is True
        assert result["stage"] == "stopped"
        assert result["target_pid"] == pid
        assert result["target_start_time"] == start_time
        assert result["marker_path"] == str(sandbox.marker_path)

        payload = json.loads(sandbox.marker_path.read_text(encoding="utf-8"))
        assert set(payload) == {
            "target_pid",
            "target_start_time",
            "target_hermes_home",
            "replacer_pid",
            "replacer_hermes_home",
            "written_at",
        }
        assert payload["target_pid"] == pid
        assert payload["target_start_time"] == start_time
        # The stopper is the child interpreter, not this test process.
        assert payload["target_hermes_home"] == str(sandbox.hermes_home)
        assert payload["replacer_pid"] != os.getpid()
        assert payload["replacer_hermes_home"] == str(sandbox.hermes_home)

        invocations = sandbox.systemctl_argv()
        assert len(invocations) == 2
        assert invocations[0].split() == [
            "--user",
            "show",
            "hermes-gateway.service",
            "--property=MainPID,ActiveState,SubState,FragmentPath,Environment",
        ]
        assert invocations[1].split() == ["--user", "stop", "hermes-gateway.service"]

    def test_bound_environment_reaches_systemctl(self, sandbox):
        pid = sandbox.spawn_child()
        sandbox.write_stat(pid)
        sandbox.set_show(main_pid=pid)

        completed = _run_standalone(sandbox.host(), sandbox.root)
        assert completed.returncode == 0, completed.stderr.decode("utf-8")

        env_lines = sandbox.systemctl_env_lines()
        assert f"HOME={sandbox.home}" in env_lines
        assert f"XDG_RUNTIME_DIR={sandbox.runtime_dir}" in env_lines
        assert f"DBUS_SESSION_BUS_ADDRESS=unix:path={sandbox.runtime_dir}/bus" in env_lines
        assert "PATH=/usr/bin:/bin:/usr/sbin:/sbin" in env_lines
        # HERMES_HOME is verified, never forwarded.
        assert not any(line.startswith("HERMES_HOME=") for line in env_lines)

    def test_rejection_stops_nothing(self, sandbox):
        pid = sandbox.spawn_child()
        sandbox.write_stat(pid)
        sandbox.set_show(main_pid=pid, active_state="activating", sub_state="start")

        completed = _run_standalone(sandbox.host(), sandbox.root)

        assert completed.returncode != 0
        result = _sole_json_line(completed)
        assert result["ok"] is False
        assert result["stage"] == "unit_state"
        assert not sandbox.marker_path.exists()
        assert all("stop" not in argv.split() for argv in sandbox.systemctl_argv())


# ── the execution envelope itself ─────────────────────────────────────


class TestExecutionEnvelope:
    """``-P`` is not enough on its own; ``-I`` is what closes PYTHONPATH."""

    @staticmethod
    def _poisoned_pythonpath(sandbox) -> tuple[dict, Path]:
        """A PYTHONPATH holding a ``json.py`` that shadows the stdlib one."""
        sentinel = sandbox.root / "poisoned-json.sentinel"
        hook_dir = sandbox.root / "poison"
        hook_dir.mkdir()
        (hook_dir / "json.py").write_text(
            "import pathlib\n"
            f"pathlib.Path({str(sentinel)!r}).write_text('imported')\n"
            "def dumps(*a, **k): return '{}'\n"
            "def loads(*a, **k): return {}\n",
            encoding="utf-8",
        )
        return dict(MINIMAL_ENV, PYTHONPATH=str(hook_dir)), sentinel

    def test_pythonpath_shadows_the_stdlib_without_isolation(self, sandbox):
        """The control: ``-P -S -B`` leaves PYTHONPATH honoured, so a planted
        module wins over the stdlib. This is the hole ``-I`` exists to close;
        if this ever stops firing, the test below proves nothing."""
        env, sentinel = self._poisoned_pythonpath(sandbox)

        control = subprocess.run(
            [sys.executable, *NO_PATH_PREPEND_FLAGS, "-c", "import json"],
            capture_output=True,
            env=env,
            timeout=60,
        )

        assert control.returncode == 0, control.stderr.decode("utf-8")
        assert sentinel.exists(), "fixture is vacuous — the planted json.py never ran"

    def test_isolated_run_ignores_a_poisoned_pythonpath(self, sandbox):
        env, sentinel = self._poisoned_pythonpath(sandbox)
        pid = sandbox.spawn_child()
        sandbox.write_stat(pid)
        sandbox.set_show(main_pid=pid)

        completed = _run_standalone(sandbox.host(), sandbox.root, env=env)

        assert completed.returncode == 0, completed.stderr.decode("utf-8")
        assert _sole_json_line(completed)["ok"] is True
        assert not sentinel.exists()
        assert sandbox.marker_path.exists()

    def test_the_weaker_envelope_still_works_on_a_clean_environment(self, sandbox):
        """``-P -S -B`` remains correct when the caller builds the child's
        environment from empty — which is what makes it merely weaker, not
        wrong."""
        pid = sandbox.spawn_child()
        sandbox.write_stat(pid)
        sandbox.set_show(main_pid=pid)

        completed = _run_standalone(
            sandbox.host(), sandbox.root, flags=NO_PATH_PREPEND_FLAGS
        )

        assert completed.returncode == 0, completed.stderr.decode("utf-8")
        assert _sole_json_line(completed)["ok"] is True
        assert sandbox.marker_path.exists()

    def test_bare_blob_gathers_the_real_host_and_fails_closed(self, tmp_path):
        """The production path, with no prelude at all.

        Streams the unmodified file bytes so ``_gather_default_host`` and the
        ``__main__`` guard actually run. This machine is not the bound
        production account, so the run must refuse — at one of the read-only
        stages that happen before any subprocess, whatever the host OS.
        """
        completed = subprocess.run(
            [sys.executable, *ISOLATED_FLAGS, "-"],
            input=PROTOCOL_PATH.read_bytes(),
            capture_output=True,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(tmp_path)},
            timeout=120,
        )

        assert completed.returncode == 1, completed.stderr.decode("utf-8")
        result = _sole_json_line(completed)
        assert result["ok"] is False
        assert result["stage"] in {"platform", "account", "environment"}, result

    def test_dash_m_entry_is_refused(self, tmp_path):
        """``-m`` means a Hermes package was imported to find this file — the
        bootstrap the standalone path exists to avoid — and says nothing about
        the interpreter's isolation flags."""
        completed = subprocess.run(
            [sys.executable, "-m", "gateway.planned_stop_protocol"],
            capture_output=True,
            cwd=str(REPO_ROOT),
            # Constructed, not inherited: importing the gateway package reads
            # HERMES_HOME, and this test must not point it at the real one.
            env={
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "HOME": str(tmp_path),
                "HERMES_HOME": str(tmp_path / ".hermes"),
            },
            timeout=120,
        )

        assert completed.returncode == 2, completed.stderr.decode("utf-8")
        assert completed.stdout == b""
        assert b"refusing to run via -m" in completed.stderr

    def test_bare_exec_namespace_still_runs(self):
        """An in-process executor that ``exec``s the blob into a namespace of
        its own must not be mistaken for the ``-m`` path.

        Such a namespace has no ``__spec__`` at all, and a bare-name read
        would fall through to ``builtins.__spec__`` — a real ModuleSpec — and
        refuse. The host here fails at the first check, so this proves
        ``main()`` was reached without spawning anything.
        """
        import builtins
        import contextlib
        import io

        assert builtins.__spec__ is not None, "the trap this guards against"

        namespace = {
            "__name__": "__main__",
            "PLANNED_STOP_TEST_HOST": {"platform": "definitely-not-linux"},
        }
        assert "__spec__" not in namespace

        stdout = io.StringIO()
        exit_code = None
        try:
            with contextlib.redirect_stdout(stdout):
                exec(compile(HELPER_SOURCE, "<blob>", "exec"), namespace)
        except SystemExit as exc:
            exit_code = exc.code

        assert exit_code == 1
        result = json.loads(stdout.getvalue().strip())
        assert result["stage"] == "platform"

    def test_old_interpreter_is_refused(self):
        """``-I`` has existed since 3.4, so nothing in the invocation rejects
        an interpreter Hermes does not support. The file checks for itself."""
        old = _interpreter_below_minimum()
        if old is None:
            pytest.skip(
                "no interpreter older than Python 3.11 found here, so the "
                "in-band version gate cannot be exercised end to end"
            )

        completed = subprocess.run(
            [old, *ISOLATED_FLAGS, "-"],
            input=PROTOCOL_PATH.read_bytes(),
            capture_output=True,
            env=dict(MINIMAL_ENV),
            timeout=120,
        )

        assert completed.returncode == 1, completed.stderr.decode("utf-8")
        result = _sole_json_line(completed)
        assert result["ok"] is False
        assert result["stage"] == "python"
        assert "needs Python 3.11+" in result["detail"]


# ── M. a failing stop, end to end ─────────────────────────────────────


class TestStandaloneStopFailure:
    def test_non_zero_stop_exits_non_zero_and_keeps_marker(self, sandbox):
        pid = sandbox.spawn_child()
        sandbox.write_stat(pid)
        sandbox.set_show(main_pid=pid)
        sandbox.set_stop_rc(3)

        completed = _run_standalone(sandbox.host(), sandbox.root)

        assert completed.returncode != 0
        result = _sole_json_line(completed)
        assert result["ok"] is False
        assert result["stage"] == "stop"
        assert "exited 3" in result["detail"]
        assert result["target_pid"] == pid

        # No retry, no second verb.
        invocations = sandbox.systemctl_argv()
        assert [argv.split()[1] for argv in invocations] == ["show", "stop"]
        # Marker deliberately left in place; its TTL retires it.
        assert sandbox.marker_path.exists()


# ── D. no Hermes bootstrap ────────────────────────────────────────────


class TestNoHermesBootstrap:
    def test_imports_no_hermes_module_even_from_the_repo_root(self, sandbox):
        """Run with cwd at the repo root — where ``utils.py`` and ``gateway/``
        are sitting right there — and prove none of it is imported."""
        record = sandbox.root / "imports.log"
        pid = sandbox.spawn_child()
        sandbox.write_stat(pid)
        sandbox.set_show(main_pid=pid)

        observer = (
            "import sys as _sys\n"
            f"_BANNED = {BANNED_IMPORT_ROOTS!r}\n"
            f"_RECORD = {str(record)!r}\n"
            "class _ImportObserver:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name.split('.')[0] in _BANNED:\n"
            "            with open(_RECORD, 'a') as fh:\n"
            "                fh.write(name + chr(10))\n"
            "        return None\n"
            "_sys.meta_path.insert(0, _ImportObserver())\n"
        )

        completed = _run_standalone(
            sandbox.host(), sandbox.root, extra_prelude=observer, cwd=REPO_ROOT
        )

        assert completed.returncode == 0, completed.stderr.decode("utf-8")
        assert _sole_json_line(completed)["ok"] is True
        assert not record.exists(), record.read_text(encoding="utf-8")

    def test_observer_would_have_noticed(self, sandbox):
        """The observer is not vacuous: it records a banned import when one
        actually happens."""
        record = sandbox.root / "imports.log"
        pid = sandbox.spawn_child()
        sandbox.write_stat(pid)
        sandbox.set_show(main_pid=pid)

        observer = (
            "import sys as _sys\n"
            f"_BANNED = {BANNED_IMPORT_ROOTS!r}\n"
            f"_RECORD = {str(record)!r}\n"
            "class _ImportObserver:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name.split('.')[0] in _BANNED:\n"
            "            with open(_RECORD, 'a') as fh:\n"
            "                fh.write(name + chr(10))\n"
            "        return None\n"
            "_sys.meta_path.insert(0, _ImportObserver())\n"
            "try:\n"
            "    import hermes_constants\n"
            "except ImportError:\n"
            "    pass\n"
        )

        completed = _run_standalone(
            sandbox.host(), sandbox.root, extra_prelude=observer, cwd=REPO_ROOT
        )

        assert completed.returncode == 0, completed.stderr.decode("utf-8")
        assert record.exists()
        assert "hermes_constants" in record.read_text(encoding="utf-8")


# ── E. user plugins never execute ─────────────────────────────────────


class TestAdversarialUserPlugin:
    def test_plugin_in_hermes_home_is_never_executed(self, sandbox):
        sentinel = sandbox.root / "plugin-executed.sentinel"
        plugin = (
            sandbox.hermes_home / "plugins" / "model-providers" / "evil" / "__init__.py"
        )
        plugin.parent.mkdir(parents=True)
        plugin.write_text(
            "import pathlib\n"
            f"pathlib.Path({str(sentinel)!r}).write_text('executed')\n",
            encoding="utf-8",
        )

        pid = sandbox.spawn_child()
        sandbox.write_stat(pid)
        sandbox.set_show(main_pid=pid)

        completed = _run_standalone(sandbox.host(), sandbox.root)

        assert completed.returncode == 0, completed.stderr.decode("utf-8")
        assert not sentinel.exists()
        # The plugin file itself is untouched — the helper only reads and
        # writes the marker inside HERMES_HOME.
        assert "executed" in plugin.read_text(encoding="utf-8")
        assert sandbox.marker_path.exists()


# ── F. sitecustomize and .pth hooks are neutralised by -S ─────────────


def _user_site_interpreter(userbase: Path):
    """An interpreter whose ``site`` will scan ``userbase`` for ``.pth`` files.

    A venv interpreter disables user-site entirely, so fall back to the base
    interpreter it was built from. Returns ``(executable, user_site_dir)`` or
    ``None`` when neither will process user-site ``.pth`` files here.
    """
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONUSERBASE": str(userbase)}
    for executable in (sys.executable, getattr(sys, "_base_executable", None)):
        if not executable or not Path(executable).exists():
            continue
        probe = subprocess.run(
            [
                executable,
                "-c",
                "import site; print(site.ENABLE_USER_SITE); "
                "print(site.getusersitepackages())",
            ],
            capture_output=True,
            env=env,
            timeout=60,
        )
        lines = probe.stdout.decode("utf-8").splitlines()
        if probe.returncode == 0 and len(lines) == 2 and lines[0] == "True":
            return executable, Path(lines[1])
    return None


class TestSiteHooksNeutralised:
    """These runs deliberately use the weaker ``-P -S -B`` envelope.

    Under ``-I`` the interpreter discards ``PYTHONPATH`` and ``PYTHONUSERBASE``
    outright, so the fixtures below would never arm and the tests would pass
    for the wrong reason. Keeping ``-E`` out of the picture leaves ``-S`` as
    the only thing that can be doing the neutralising, which is the claim
    under test. ``TestExecutionEnvelope`` covers the ``-I`` dimension.
    """

    def test_sitecustomize_on_pythonpath_does_not_run(self, sandbox):
        sentinel = sandbox.root / "sitecustomize.sentinel"
        hook_dir = sandbox.root / "pythonpath"
        hook_dir.mkdir()
        (hook_dir / "sitecustomize.py").write_text(
            f"import pathlib\npathlib.Path({str(sentinel)!r}).write_text('fired')\n",
            encoding="utf-8",
        )
        env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONPATH": str(hook_dir),
        }

        # Control: without -S the hook demonstrably fires.
        control = subprocess.run(
            [sys.executable, "-c", "pass"], capture_output=True, env=env, timeout=60
        )
        assert control.returncode == 0, control.stderr.decode("utf-8")
        assert sentinel.exists(), "fixture is vacuous — sitecustomize never ran"
        sentinel.unlink()

        pid = sandbox.spawn_child()
        sandbox.write_stat(pid)
        sandbox.set_show(main_pid=pid)

        completed = _run_standalone(
            sandbox.host(), sandbox.root, env=env, flags=NO_PATH_PREPEND_FLAGS
        )

        assert completed.returncode == 0, completed.stderr.decode("utf-8")
        assert _sole_json_line(completed)["ok"] is True
        assert not sentinel.exists()

    def test_user_site_pth_does_not_run(self, sandbox):
        userbase = sandbox.root / "userbase"
        userbase.mkdir()
        found = _user_site_interpreter(userbase)
        if found is None:
            pytest.skip(
                "no interpreter here processes user-site .pth files "
                "(venv interpreters disable user-site), so the control run "
                "cannot prove the fixture fires"
            )
        executable, user_site = found
        user_site.mkdir(parents=True, exist_ok=True)

        sentinel = sandbox.root / "pth.sentinel"
        (user_site / "zzz_planned_stop_probe.pth").write_text(
            f"import pathlib; pathlib.Path({str(sentinel)!r}).write_text('fired')\n",
            encoding="utf-8",
        )
        env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONUSERBASE": str(userbase),
        }

        control = subprocess.run(
            [executable, "-c", "pass"], capture_output=True, env=env, timeout=60
        )
        assert control.returncode == 0, control.stderr.decode("utf-8")
        assert sentinel.exists(), "fixture is vacuous — the .pth never ran"
        sentinel.unlink()

        pid = sandbox.spawn_child()
        sandbox.write_stat(pid)
        sandbox.set_show(main_pid=pid)

        host = sandbox.host()
        stdin = _prelude(host, sandbox.root) + "\n" + HELPER_SOURCE
        completed = subprocess.run(
            [executable, *NO_PATH_PREPEND_FLAGS, "-"],
            input=stdin.encode("utf-8"),
            capture_output=True,
            env=env,
            timeout=120,
        )

        assert completed.returncode == 0, completed.stderr.decode("utf-8")
        assert _sole_json_line(completed)["ok"] is True
        assert not sentinel.exists()
