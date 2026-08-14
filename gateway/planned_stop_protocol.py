"""Planned-stop marker protocol — one implementation, two deployment shapes.

The gateway exits non-zero on an unexpected SIGTERM so a service manager can
revive it.  A deliberate stop sends the same SIGTERM, so whoever stops the
gateway first drops a short-lived marker naming the target PID (plus its
start-time fingerprint) into ``HERMES_HOME``; the target reads the marker in
its shutdown path and exits 0 instead.  That is the whole protocol.

This module is the single definition of it.  Three callers share these bytes:

1. ``gateway/status.py`` imports the helpers below, so the in-process
   writer/consumer/probe all agree by construction rather than by three
   parallel edits staying in sync.
2. ``hermes_cli/update_cmd.py`` writes the same marker into a specific
   profile home before pausing that profile's gateway for an update.
3. A deployment executor that has no Hermes checkout to import from streams
   this exact file to a fresh interpreter on stdin and lets ``main()`` stop
   the production gateway.

That third caller is why the module is **stdlib-only, unconditionally**, has
no import-time side effects, and never touches ``__file__``, the cwd, or a
PATH-resolved executable.

Recommended invocation::

    /usr/bin/python3 -I -S -B -

``-I`` (isolated) is the load-bearing flag: it implies ``-E`` (ignore every
``PYTHON*`` environment variable, ``PYTHONPATH`` above all), ``-s`` (no user
site directory), and ``-P`` (no cwd on ``sys.path``).  Without ``-E`` a
``PYTHONPATH`` entry shadows stdlib modules by name — a planted ``json.py``
is imported before the real one, and every guarantee below is void.  ``-I``
does **not** imply ``-S``, so ``-S`` is still passed to skip ``site``
entirely (no ``site-packages``, no ``.pth`` execution, no ``sitecustomize``).
``-B`` keeps the run from writing bytecode.  Use an absolute interpreter
path; ``python3`` off ``PATH`` is chosen by the ambient environment.

``-P -S -B`` is *also* sufficient, but only when the caller constructs the
child's environment from empty so no ``PYTHONPATH`` exists to honour.  That
is an invariant of the caller, not of this file, so the file does not rely on
it and the recommended envelope closes the hole at the interpreter instead.

``-I`` has existed since Python 3.4, so — unlike ``-P``, which is 3.11+ — it
does **not** incidentally reject an old interpreter.  Hermes itself requires
3.11, so a stopper running under anything older is not the runtime this
protocol was verified against.  :func:`main` therefore checks the version
itself rather than leaning on a flag to do it, and everything above that
check is kept syntactically valid on old interpreters so the check is
actually reached and can report why it refused.

Everything above the "standalone executor" banner is shared protocol.
Everything below it is the standalone path and is never reached on import.
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    # POSIX-only. The standalone executor (Linux) needs it; the shared
    # protocol half must still import on Windows, where gateway/status.py
    # imports this module for the marker write/consume path.
    import pwd
except ImportError:  # pragma: no cover - Windows
    pwd = None  # type: ignore[assignment]


# ── shared protocol ───────────────────────────────────────────────────

PLANNED_STOP_MARKER_FILENAME = ".gateway-planned-stop.json"
PLANNED_STOP_MARKER_TTL_S = 60


def utc_now_iso() -> str:
    """Timestamp shape used for the marker's ``written_at`` field."""
    return datetime.now(timezone.utc).isoformat()


def marker_is_stale(written_at: Any, ttl_s: int) -> bool:
    """Return True when ``written_at`` is older than ``ttl_s`` or unparseable.

    An unparseable timestamp counts as stale so a corrupt marker is dropped
    rather than trusted.  ``TypeError`` also covers the naive-vs-aware
    subtraction a hand-edited marker can produce.
    """
    try:
        written_dt = datetime.fromisoformat(written_at)
        age = (datetime.now(timezone.utc) - written_dt).total_seconds()
        return age > ttl_s
    except (TypeError, ValueError):
        return True


def linux_process_start_time(pid: int, proc_root: str = "/proc") -> Optional[int]:
    """Return field 22 of ``<proc_root>/<pid>/stat`` (start time in ticks).

    Paired with the PID this fingerprints a specific process, so a recycled
    PID never matches a marker written for its predecessor.

    The whitespace-``split()`` parse is deliberately kept as-is: a process
    whose comm field contains a space or a parenthesis shifts the fields and
    yields a "wrong" number.  Both sides of the protocol observe the value the
    same way on the same host, so wire compatibility depends on the two
    observations matching each other — not on either being correct.  Making
    this parse smarter here would silently break marker compatibility with a
    peer still running the old one.
    """
    stat_path = Path(proc_root) / str(pid) / "stat"
    try:
        return int(stat_path.read_text(encoding="utf-8").split()[21])
    except (FileNotFoundError, IndexError, PermissionError, ValueError, OSError):
        return None


def build_planned_stop_record(
    target_pid: int,
    target_start_time: Any,
    stopper_pid: int,
) -> dict[str, Any]:
    """Build the marker payload.

    Exactly these four fields.  The consumer applies a HERMES_HOME guard when
    a ``target_hermes_home`` / ``replacer_hermes_home`` field is present (a
    takeover-marker feature), so adding one here would change how planned-stop
    markers are matched.
    """
    return {
        "target_pid": target_pid,
        "target_start_time": target_start_time,
        "stopper_pid": stopper_pid,
        "written_at": utc_now_iso(),
    }


def pid_marker_matches(
    target_pid: Any,
    target_start_time: Any,
    our_pid: int,
    our_start_time: Any,
) -> bool:
    """Return True when a marker's identity fields name us.

    Start-time is a PID-reuse guard and is only meaningful when both sides
    have one: ``/proc`` does not exist on macOS or native Windows — the very
    platforms the planned-stop watcher exists for — so requiring a non-None
    match there would reject every legitimate stop, and the gateway would be
    misclassified as an unexpected exit and revived by its service manager.
    So: both known → they must be equal; either unknown → PID equality alone,
    bounded by the marker's short TTL.
    """
    if target_pid != our_pid:
        return False
    if target_start_time is not None and our_start_time is not None:
        return target_start_time == our_start_time
    return True


def write_marker_atomic(path: Path, record: dict[str, Any]) -> None:
    """Write ``record`` to ``path`` atomically, in the marker's wire encoding.

    Temp file in the destination directory, fsync, ``os.replace`` — a reader
    racing the write sees either the old file or the whole new one, never a
    truncated marker.  The JSON encoding (compact separators, no indent,
    ``ensure_ascii=False``) matches what ``utils.atomic_json_write`` produced
    for these markers, so the bytes on disk are unchanged.

    Deliberately does **not** create ``path.parent``.  A missing HERMES_HOME
    means we are not looking at the installation we think we are; raising
    fails the stop closed instead of repairing the host into a shape that
    makes the mistake invisible.
    """
    path = Path(path)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.stem}_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=None, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        # BaseException so the temp file is still cleaned up when a
        # KeyboardInterrupt/SystemExit lands mid-write.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ── standalone executor ───────────────────────────────────────────────
#
# Reached only via ``__main__`` (i.e. this file fed to a fresh interpreter on
# stdin).  Importing the module never runs any of it.
#
# Scope is deliberately one shape and nothing else: the Linux **user** systemd
# service of the production ``hermes`` account.  Every other platform, scope,
# account, or unit fails closed rather than degrading to a "best effort" stop
# of something we did not positively identify.

# Hermes requires >=3.11 (pyproject ``requires-python``), so the gateway on
# the other end of this marker is always 3.11+. An older interpreter here is
# not the runtime the protocol was verified against, and nothing in the
# invocation rejects it for us: ``-I`` predates 3.11 by seven releases.
MINIMUM_PYTHON = (3, 11)

STANDALONE_ACCOUNT = "hermes"
STANDALONE_HOME = "/home/hermes"
STANDALONE_HERMES_HOME = "/home/hermes/.hermes"
STANDALONE_UNIT = "hermes-gateway.service"
STANDALONE_UNIT_PATH = "/home/hermes/.config/systemd/user/hermes-gateway.service"
# Absolute, never PATH-resolved: the whole point of the bound environment is
# that nothing the ambient environment says can redirect which binary runs.
STANDALONE_SYSTEMCTL = "/usr/bin/systemctl"
STANDALONE_PROC_ROOT = "/proc"
STANDALONE_RUNTIME_DIR_ROOT = "/run/user"
# `systemctl --user stop` blocks for the unit's own stop timeout; 90s matches
# the CLI's `hermes gateway stop` budget. `show` is a local D-Bus round trip —
# 30s is already generous and only exists so a wedged bus cannot hang us.
STANDALONE_STOP_TIMEOUT_S = 90
STANDALONE_SHOW_TIMEOUT_S = 30
STANDALONE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

# The only environment variables we read, and each one is verified against a
# value derived from the account entry before it is used.
VERIFIED_ENV_VARS = (
    "HOME",
    "HERMES_HOME",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
)

_SHOW_PROPERTIES = (
    "MainPID",
    "ActiveState",
    "SubState",
    "FragmentPath",
    "Environment",
)


def environment_hermes_homes(environment: Any) -> Optional[list[str]]:
    """Return every ``HERMES_HOME`` value in a ``systemctl show`` Environment.

    The property is one line of space-separated ``KEY=VALUE`` tokens, already
    merged by the service manager from the unit fragment *and* any drop-in::

        Environment=PATH=/usr/bin HERMES_HOME=/home/hermes/.hermes

    Returns ``None`` — meaning "unparseable, refuse" — when any token starts
    with a double quote.  systemd quotes a token whose value contains spaces,
    and once quoting is in play a plain space split no longer identifies token
    boundaries.  Nothing on the bound deployment legitimately needs a quoted
    value, so rather than grow a shell-style tokenizer whose edge cases we
    would then have to be right about, refuse the answer.
    """
    if not isinstance(environment, str):
        return None
    values: list[str] = []
    for token in environment.split(" "):
        if token.startswith('"'):
            return None
        if token.startswith("HERMES_HOME="):
            values.append(token.split("=", 1)[1])
    return values


def python_version_supported(version_info) -> bool:
    """Whether ``version_info`` clears :data:`MINIMUM_PYTHON`."""
    return tuple(version_info[:2]) >= MINIMUM_PYTHON


def _gather_default_host() -> dict[str, Any]:
    """Collect raw host facts plus the production bindings.

    Facts only — no decisions.  Everything that decides lives in
    :func:`run_planned_stop`, so a test can drive the identical logic by
    handing it a fixture host instead of the real machine.
    """
    entry = None
    if pwd is not None:
        try:
            entry = pwd.getpwnam(STANDALONE_ACCOUNT)
        except KeyError:
            entry = None

    return {
        "platform": sys.platform,
        "euid": os.geteuid() if hasattr(os, "geteuid") else None,
        "account_exists": entry is not None,
        "pw_dir": entry.pw_dir if entry is not None else None,
        "pw_uid": entry.pw_uid if entry is not None else None,
        "env": {name: os.environ.get(name) for name in VERIFIED_ENV_VARS},
        "account": STANDALONE_ACCOUNT,
        "home": STANDALONE_HOME,
        "hermes_home": STANDALONE_HERMES_HOME,
        "unit": STANDALONE_UNIT,
        "unit_path": STANDALONE_UNIT_PATH,
        "systemctl": STANDALONE_SYSTEMCTL,
        "proc_root": STANDALONE_PROC_ROOT,
        "runtime_dir_root": STANDALONE_RUNTIME_DIR_ROOT,
        "show_timeout_s": STANDALONE_SHOW_TIMEOUT_S,
        "stop_timeout_s": STANDALONE_STOP_TIMEOUT_S,
    }


def _decode(raw: Any) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw if isinstance(raw, str) else ""


def _parse_systemctl_properties(text: str) -> Optional[dict[str, str]]:
    """Parse ``systemctl show`` output, or None when it looks forged.

    Accepts any *subset* of the properties we asked for, each appearing at
    most once — systemd may omit one whose value is empty, and a missing key
    is caught downstream by the comparison that reads it (a ``None`` never
    equals ``"active"``, ``"running"``, or the unit path).  So this function
    enforces shape, not completeness.

    What it rejects is output we cannot trust at all: an unknown key, a
    repeated key, or a line without ``=``.  A property value is free-form
    text, so one carrying an embedded newline would otherwise look like an
    extra property line.  That is what makes the duplicate-key rule
    load-bearing: forging a property systemd *did* emit is rejected as a
    duplicate, and forging one it omitted only helps for a property the
    caller then requires to equal a known value — which means controlling
    ``FragmentPath`` or ``Environment`` content, i.e. already owning the unit
    the check exists to identify.
    """
    props: dict[str, str] = {}
    for line in text.splitlines():
        if not line:
            continue
        key, sep, value = line.partition("=")
        if not sep or key not in _SHOW_PROPERTIES or key in props:
            return None
        props[key] = value
    return props


def run_planned_stop(host: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Verify ``host`` is the bound gateway, mark the stop, then stop it.

    Returns ``(exit_code, result)``.  Every check up to and including step 7
    is read-only: nothing is created, cleaned up, repaired, or rewritten
    before the marker, so a rejected run leaves the machine exactly as it
    found it.

    Residual race, stated plainly: reading ``MainPID``, writing the marker,
    and invoking ``stop`` are three separate steps, and systemd offers no
    atomic bind-and-stop primitive.  If the gateway exits and its PID is
    recycled between step 5 and step 9, the marker names a process that is no
    longer ours.  Two things bound that: the marker carries the start-time
    fingerprint observed at step 7, so the consumer rejects a recycled PID
    outright, and the 60s TTL retires a marker nobody consumed.
    """

    def fail(stage: str, detail: str, **extra: Any) -> tuple[int, dict[str, Any]]:
        result: dict[str, Any] = {"ok": False, "stage": stage, "detail": detail}
        result.update(extra)
        return 1, result

    # 1. Platform. The standalone path speaks systemd user units and /proc;
    #    there is no fallback for anything else.
    if host.get("platform") != "linux":
        return fail("platform", f"unsupported platform: {host.get('platform')!r}")

    # Host bindings must all be present. A fixture (or a future caller) that
    # forgets one must fail here rather than fall through to a comparison
    # against None.
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
        if not isinstance(host.get(name), str) or not host[name]:
            return fail("bindings", f"missing or invalid host binding: {name}")
    # Every path-like binding must be absolute. A relative one would resolve
    # against the cwd — which this program deliberately knows nothing about,
    # and which is the caller's, not the account's.
    for name in (
        "home",
        "hermes_home",
        "unit_path",
        "systemctl",
        "proc_root",
        "runtime_dir_root",
    ):
        if not os.path.isabs(host[name]):
            return fail("bindings", f"host binding {name} is not absolute: {host[name]!r}")
    for name in ("show_timeout_s", "stop_timeout_s"):
        value = host.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            return fail("bindings", f"missing or invalid host binding: {name}")

    account = host["account"]
    home = host["home"]
    hermes_home = host["hermes_home"]
    unit = host["unit"]
    unit_path = host["unit_path"]
    systemctl = host["systemctl"]
    proc_root = host["proc_root"]

    # 2. Account. The uid comes from the passwd entry — never guessed, never
    #    taken from the environment — and we must actually be running as it.
    if not host.get("account_exists"):
        return fail("account", f"account {account!r} does not exist")
    if host.get("pw_dir") != home:
        return fail(
            "account",
            f"account {account!r} home is {host.get('pw_dir')!r}, expected {home!r}",
        )
    uid = host.get("pw_uid")
    if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
        return fail("account", f"account {account!r} has no usable uid")
    euid = host.get("euid")
    if isinstance(euid, bool) or not isinstance(euid, int):
        return fail("account", "effective uid unavailable")
    if euid != uid:
        return fail("account", f"running as euid {euid}, expected {uid} ({account})")

    # 3. Environment cross-check. Mismatch means this shell is not the
    #    account's own session, so its D-Bus/runtime pointers cannot be
    #    trusted to reach the right user manager. The values we then *use*
    #    are the derived expectations, never the ambient strings.
    env = host.get("env")
    if not isinstance(env, dict):
        return fail("environment", "host environment snapshot missing")
    runtime_dir = f"{host['runtime_dir_root']}/{uid}"
    dbus_address = f"unix:path={runtime_dir}/bus"
    if env.get("HOME") != home:
        return fail("environment", f"HOME is {env.get('HOME')!r}, expected {home!r}")
    env_hermes_home = env.get("HERMES_HOME")
    if env_hermes_home is not None and env_hermes_home != hermes_home:
        return fail(
            "environment",
            f"HERMES_HOME is {env_hermes_home!r}, expected {hermes_home!r}",
        )
    if env.get("XDG_RUNTIME_DIR") != runtime_dir:
        return fail(
            "environment",
            f"XDG_RUNTIME_DIR is {env.get('XDG_RUNTIME_DIR')!r}, "
            f"expected {runtime_dir!r}",
        )
    if env.get("DBUS_SESSION_BUS_ADDRESS") != dbus_address:
        return fail(
            "environment",
            f"DBUS_SESSION_BUS_ADDRESS is {env.get('DBUS_SESSION_BUS_ADDRESS')!r}, "
            f"expected {dbus_address!r}",
        )

    # 4. The three paths we are about to rely on must already exist. We check
    #    them; we do not create them.
    if not Path(runtime_dir).is_dir():
        return fail("paths", f"runtime directory {runtime_dir} is missing")
    if not Path(unit_path).is_file():
        return fail("paths", f"unit file {unit_path} is missing")
    if not Path(hermes_home).is_dir():
        return fail("paths", f"HERMES_HOME {hermes_home} is missing")

    bound_env = {
        "HOME": home,
        "XDG_RUNTIME_DIR": runtime_dir,
        "DBUS_SESSION_BUS_ADDRESS": dbus_address,
        "PATH": STANDALONE_PATH,
    }

    # 5. Ask the *user* manager about the unit. Every invocation carries
    #    --user; system scope is never inspected and never fallen back to.
    show_argv = [
        systemctl,
        "--user",
        "show",
        unit,
        "--property=" + ",".join(_SHOW_PROPERTIES),
    ]
    try:
        shown = subprocess.run(
            show_argv,
            env=bound_env,
            timeout=host["show_timeout_s"],
            capture_output=True,
        )
    except subprocess.TimeoutExpired:
        return fail("unit_show", f"systemctl show timed out after {host['show_timeout_s']}s")
    except OSError as exc:
        return fail("unit_show", f"could not run {systemctl}: {exc}")
    if shown.returncode != 0:
        return fail(
            "unit_show",
            f"systemctl show exited {shown.returncode}: "
            f"{_decode(shown.stderr).strip()}",
        )
    props = _parse_systemctl_properties(_decode(shown.stdout))
    if props is None:
        return fail("unit_show", "systemctl show output was not the expected property set")

    if props.get("ActiveState") != "active":
        return fail("unit_state", f"ActiveState is {props.get('ActiveState')!r}, expected 'active'")
    if props.get("SubState") != "running":
        return fail("unit_state", f"SubState is {props.get('SubState')!r}, expected 'running'")
    if props.get("FragmentPath") != unit_path:
        return fail(
            "unit_identity",
            f"FragmentPath is {props.get('FragmentPath')!r}, expected {unit_path!r}",
        )

    # The unit must run out of the HERMES_HOME we are about to write into.
    # Knowing *which unit file* we are stopping is not enough: a gateway
    # installed against a custom home (say /opt/data) is still called
    # hermes-gateway.service, and a marker dropped in the default home would
    # be read by nobody while we reported success.
    #
    # Ask the manager rather than reading the fragment. The manager's answer
    # is the environment the running service actually has: it already merged
    # every drop-in under hermes-gateway.service.d/, and it reflects the unit
    # as *loaded*, so a fragment edited without `daemon-reload` cannot talk us
    # into the wrong home. Same authority we take MainPID from.
    homes = environment_hermes_homes(props.get("Environment"))
    if homes is None:
        return fail(
            "unit_home",
            f"Environment is not parseable: {props.get('Environment')!r}",
        )
    if not homes:
        return fail("unit_home", f"{unit} sets no HERMES_HOME")
    mismatched = [value for value in homes if value != hermes_home]
    if mismatched:
        return fail(
            "unit_home",
            f"{unit} sets HERMES_HOME to {mismatched!r}, expected {hermes_home!r}",
        )
    # Plain ASCII decimal only — ``int()`` alone would also accept " 12 ",
    # "+12", "1_000" and non-ASCII digits, none of which systemd emits and all
    # of which would mean we are not reading what we think we are.
    raw_main_pid = props.get("MainPID")
    if (
        not isinstance(raw_main_pid, str)
        or not raw_main_pid.isascii()
        or not raw_main_pid.isdigit()
    ):
        return fail("unit_pid", f"MainPID is not a decimal integer: {raw_main_pid!r}")
    main_pid = int(raw_main_pid)
    if main_pid <= 0:
        return fail("unit_pid", f"MainPID is {main_pid}, expected a live process")

    # 6. The reported main process must actually exist right now.
    try:
        os.kill(main_pid, 0)
    except ProcessLookupError:
        return fail("process", f"MainPID {main_pid} is not running")
    except PermissionError:
        return fail("process", f"MainPID {main_pid} is not signalable by us")
    except OSError as exc:
        return fail("process", f"could not probe MainPID {main_pid}: {exc}")

    # 7. Fingerprint it, so the marker cannot be honoured by a recycled PID.
    start_time = linux_process_start_time(main_pid, proc_root)
    if start_time is None:
        return fail(
            "start_time",
            f"no start time for MainPID {main_pid} under {proc_root}",
        )

    # 8. First and only write. Everything above was read-only.
    marker_path = Path(hermes_home) / PLANNED_STOP_MARKER_FILENAME
    record = build_planned_stop_record(main_pid, start_time, os.getpid())
    try:
        write_marker_atomic(marker_path, record)
    except OSError as exc:
        return fail(
            "marker",
            f"could not write {marker_path}: {exc}",
            target_pid=main_pid,
            target_start_time=start_time,
        )

    # 9. Stop it. One verb, one attempt. A failure is reported, not retried
    #    and not rolled back: the marker is idempotent and expires on its own
    #    TTL, while a second systemctl verb here would be an action nothing
    #    above verified the need for.
    stop_argv = [systemctl, "--user", "stop", unit]
    outcome: dict[str, Any] = {
        "unit": unit,
        "target_pid": main_pid,
        "target_start_time": start_time,
        "marker_path": str(marker_path),
    }
    try:
        stopped = subprocess.run(
            stop_argv,
            env=bound_env,
            timeout=host["stop_timeout_s"],
            capture_output=True,
        )
    except subprocess.TimeoutExpired:
        return fail(
            "stop",
            f"systemctl stop timed out after {host['stop_timeout_s']}s",
            **outcome,
        )
    except OSError as exc:
        return fail("stop", f"could not run {systemctl}: {exc}", **outcome)
    if stopped.returncode != 0:
        return fail(
            "stop",
            f"systemctl stop exited {stopped.returncode}: "
            f"{_decode(stopped.stderr).strip()}",
            **outcome,
        )

    return 0, {"ok": True, "stage": "stopped", **outcome}


def main() -> int:
    """Run the standalone stop and print a single JSON line to stdout."""
    # ``PLANNED_STOP_TEST_HOST`` is a test seam, not a privilege hole: this
    # path only runs when the file is fed to an interpreter on stdin, and
    # whoever writes that stdin already chooses every byte the interpreter
    # executes — a prelude that defines the name could equally well have
    # replaced this function. Production streams the file with no prelude, so
    # the name is simply absent and the real host facts are gathered.
    # Version floor first, before any host fact is gathered: on an
    # interpreter Hermes itself does not support, we have no standing to
    # claim this file behaves the way the gateway expects.
    if not python_version_supported(sys.version_info):
        running = ".".join(str(part) for part in sys.version_info[:3])
        wanted = ".".join(str(part) for part in MINIMUM_PYTHON)
        exit_code, result = 1, {
            "ok": False,
            "stage": "python",
            "detail": f"needs Python {wanted}+, running {running}",
        }
    else:
        host = globals().get("PLANNED_STOP_TEST_HOST") or _gather_default_host()
        exit_code, result = run_planned_stop(host)
    # ensure_ascii=True (the default) here, unlike the marker file: a path or
    # systemd message can carry non-ASCII, and stdout's encoding depends on
    # the ambient locale of whoever is capturing us. Escaping keeps this line
    # readable and parseable under any of them. The marker file keeps
    # ensure_ascii=False for byte compatibility with the in-process writer.
    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()
    return exit_code


if __name__ == "__main__":
    # Three ways to arrive here, told apart by ``__spec__``:
    #   stdin / script  -> __main__.__spec__ is None
    #   -m              -> __main__.__spec__ is a ModuleSpec
    #   bare exec()     -> the name is absent from the namespace entirely
    # The -m path is not the supported entry: it means a Hermes package was
    # on sys.path and imported, exactly the bootstrap this file exists to
    # avoid, and it says nothing about the interpreter's isolation flags. The
    # other two must run. Read through ``globals()`` rather than the bare
    # name, because a bare name would fall through to ``builtins.__spec__``
    # (a real ModuleSpec) and refuse a legitimate exec.
    if globals().get("__spec__") is not None:
        sys.stderr.write(
            "refusing to run via -m; stream the file to "
            "python -I -S -B - instead\n"
        )
        sys.exit(2)
    sys.exit(main())
