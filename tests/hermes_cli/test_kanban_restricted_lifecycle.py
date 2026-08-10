"""Security and lifecycle contracts for restricted native Kanban workers."""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import struct
import sys
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_lifecycle as lifecycle
from tools import kanban_tools


class _DummyChannel:
    def close(self):
        pass


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _claimed_binding(
    conn, tmp_path: Path, *, assignee: str = "builder", scratch: bool = False,
):
    workspace = tmp_path / "workspace"
    if not scratch:
        workspace.mkdir(exist_ok=True)
    tid = kb.create_task(
        conn,
        title="restricted task",
        assignee=assignee,
        max_runtime_seconds=300,
        workspace_kind="scratch" if scratch else "dir",
        workspace_path=None if scratch else str(workspace),
    )
    if scratch:
        workspace = kb.resolve_workspace(kb.get_task(conn, tid))
        kb.set_workspace_path(conn, tid, str(workspace))
    claimed = kb.claim_task(conn, tid, claimer="host:restricted")
    assert claimed is not None
    pid = 424242
    kb._set_worker_pid(conn, tid, pid)
    current = kb.get_task(conn, tid)
    binding = kb.RestrictedWorkerBinding(
        pid=pid,
        task_id=tid,
        run_id=current.current_run_id,
        profile=assignee,
        workspace_path=str(workspace),
        workspace=os.path.realpath(workspace),
        board_db_path=kb._connection_main_db_path(conn),
        claim_lock=current.claim_lock,
        expected_uid=12345,
        channel=_DummyChannel(),
    )
    return tid, workspace, binding


def test_restricted_worker_public_connect_and_import_guard_fails_closed(
    kanban_home, monkeypatch,
):
    """Direct DB-layer imports cannot restore writable board authority."""
    monkeypatch.setenv(lifecycle.RESTRICTED_WORKER_ENV, "1")
    with pytest.raises(PermissionError, match="cannot open the board database"):
        kb.connect()
    with sqlite3.connect(kb.kanban_db_path()) as raw:
        raw.row_factory = sqlite3.Row
        with pytest.raises(PermissionError, match="restricted Kanban workers"):
            kb.create_task(raw, title="forbidden")


def test_restricted_tool_surface_is_lifecycle_only(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_own")
    monkeypatch.setenv(lifecycle.RESTRICTED_WORKER_ENV, "1")
    assert kanban_tools._check_kanban_mode() is True
    assert kanban_tools._check_kanban_direct_mode() is False
    assert kanban_tools._check_kanban_orchestrator_mode() is False


def test_restricted_complete_emits_identity_free_bounded_result(
    monkeypatch,
):
    emitted = []
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_own")
    monkeypatch.setenv(lifecycle.RESTRICTED_WORKER_ENV, "1")
    monkeypatch.setattr(
        lifecycle,
        "emit_result",
        lambda action, payload: emitted.append({"action": action, "payload": payload}),
    )
    response = json.loads(kanban_tools._handle_complete({"summary": "done"}))
    assert response["ok"] is True
    assert response["status"] == "pending_dispatcher"
    record = emitted[0]
    assert record == {
        "action": "complete",
        "payload": {
            "summary": "done",
            "result": None,
            "metadata": None,
            "created_cards": [],
            "artifacts": [],
        },
    }
    assert "task" not in record and "run" not in record and "claim" not in record


def test_restricted_complete_rejects_created_cards_before_handoff(monkeypatch):
    emitted = []
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_own")
    monkeypatch.setenv(lifecycle.RESTRICTED_WORKER_ENV, "1")
    monkeypatch.setattr(
        lifecycle,
        "emit_result",
        lambda action, payload: emitted.append((action, payload)),
    )

    response = json.loads(kanban_tools._handle_complete({
        "summary": "done",
        "created_cards": ["t_sibling"],
    }))

    assert "cannot claim created cards" in response["error"]
    assert emitted == []


def test_claim_bound_complete_reuses_canonical_artifact_cleanup_and_redaction(
    kanban_home, tmp_path, monkeypatch,
):
    hooks = []
    monkeypatch.setattr(
        kb,
        "_fire_kanban_lifecycle_hook",
        lambda event, task_id, **fields: hooks.append((event, task_id, fields)),
    )
    with kb.connect() as conn:
        tid, workspace, binding = _claimed_binding(conn, tmp_path, scratch=True)
        artifact = workspace / "result.txt"
        artifact.write_text("deliverable", encoding="utf-8")
        ok, error = kb._apply_restricted_worker_result(
            conn,
            binding,
            {
                "action": "complete",
                "payload": {
                    "summary": "finished sk-secret-value",
                    "metadata": {"token": "sk-secret-value"},
                    "artifacts": [str(artifact)],
                },
            },
        )
        assert ok is True and error is None
        task = kb.get_task(conn, tid)
        run = kb.latest_run(conn, tid)
        completed = [e for e in kb.list_events(conn, tid) if e.kind == "completed"][-1]
        preserved = Path(completed.payload["artifacts"][0])
        assert task.status == "done"
        assert run.status == "done" and run.outcome == "completed"
        assert "sk-secret-value" not in (run.summary or "")
        assert "sk-secret-value" not in json.dumps(run.metadata)
        assert [
            (event, task_id)
            for event, task_id, _fields in hooks
            if event == "kanban_task_completed"
        ] == [
            ("kanban_task_completed", tid),
        ]
    assert not workspace.exists()
    assert preserved.read_text(encoding="utf-8") == "deliverable"


def test_artifact_preservation_failure_refuses_result_without_aborting_tick(
    kanban_home, tmp_path, monkeypatch,
):
    credential_kind = getattr(socket, "SCM_CREDENTIALS", 0x02)
    monkeypatch.setattr(socket, "SCM_CREDENTIALS", credential_kind, raising=False)

    with kb.connect() as conn:
        tid, workspace, original = _claimed_binding(conn, tmp_path, scratch=True)
        record = {
            "action": "complete",
            "payload": {
                "summary": "finished",
                "artifacts": [str(workspace / "missing.txt")],
            },
        }

        class CredentialChannel:
            def recvmsg(self, *_args):
                return (
                    json.dumps(record).encode(),
                    [(socket.SOL_SOCKET, credential_kind,
                      struct.pack("3i", binding.pid, binding.expected_uid, 1))],
                    0,
                    None,
                )

            def recv(self, _size):
                raise BlockingIOError

            def close(self):
                pass

        values = dict(original.__dict__)
        values["channel"] = CredentialChannel()
        binding = kb.RestrictedWorkerBinding(**values)
        kb._restricted_worker_bindings[binding.pid] = binding
        monkeypatch.setattr(
            kb,
            "_classify_worker_exit",
            lambda pid: (
                ("clean_exit", 0)
                if pid == binding.pid
                else ("unknown", None)
            ),
        )

        assert kb.process_restricted_worker_results(conn) == []
        assert kb.get_task(conn, tid).status == "running"
        assert workspace.exists()
        assert binding.pid not in kb._restricted_worker_bindings
        refusal = [
            event for event in kb.list_events(conn, tid)
            if event.kind == "lifecycle_result_refused"
        ][-1]
        assert "missing.txt" in refusal.payload["reason"]


@pytest.mark.parametrize(
    ("kind", "expected_status", "event_kind"),
    [
        ("needs_input", "blocked", "blocked"),
        ("dependency", "todo", "dependency_wait"),
    ],
)
def test_claim_bound_block_reuses_typed_routing(
    kanban_home, tmp_path, kind, expected_status, event_kind,
):
    with kb.connect() as conn:
        tid, _workspace, binding = _claimed_binding(conn, tmp_path)
        ok, error = kb._apply_restricted_worker_result(
            conn,
            binding,
            {"action": "block", "payload": {"reason": "waiting", "kind": kind}},
        )
        assert ok is True and error is None
        assert kb.get_task(conn, tid).status == expected_status
        assert kb.latest_run(conn, tid).outcome == "blocked"
        assert event_kind in [e.kind for e in kb.list_events(conn, tid)]


@pytest.mark.parametrize(
    "forgery",
    ["task", "run", "profile", "workspace", "claim", "pid", "expired"],
)
def test_foreign_stale_or_forged_binding_is_refused_without_mutation(
    kanban_home, tmp_path, forgery,
):
    with kb.connect() as conn:
        tid, workspace, binding = _claimed_binding(conn, tmp_path)
        values = dict(binding.__dict__)
        if forgery == "task":
            values["task_id"] = "t_foreign"
        elif forgery == "run":
            values["run_id"] += 1
        elif forgery == "profile":
            values["profile"] = "foreign"
        elif forgery == "workspace":
            values["workspace"] = str(tmp_path / "foreign")
        elif forgery == "claim":
            values["claim_lock"] = "foreign:claim"
        elif forgery == "pid":
            values["pid"] += 1
        else:
            conn.execute(
                "UPDATE tasks SET claim_expires = ? WHERE id = ?",
                (int(time.time()) - 1, tid),
            )
            conn.commit()
        forged = kb.RestrictedWorkerBinding(**values)
        ok, error = kb._apply_restricted_worker_result(
            conn,
            forged,
            {"action": "complete", "payload": {"summary": "forged"}},
        )
        assert ok is False and error
        assert kb.get_task(conn, tid).status == "running"


def test_claim_expiry_is_rechecked_inside_finalizer_cas(
    kanban_home, tmp_path, monkeypatch,
):
    with kb.connect() as conn:
        tid, _workspace, binding = _claimed_binding(conn, tmp_path)
        task = kb.get_task(conn, tid)
        monkeypatch.setattr(
            kb,
            "_validate_restricted_binding",
            lambda _conn, _binding: (task, None),
        )
        expired = int(time.time()) - 1
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (expired, tid),
        )
        conn.execute(
            "UPDATE task_runs SET claim_expires = ? WHERE id = ?",
            (expired, binding.run_id),
        )
        conn.commit()
        ok, error = kb._apply_restricted_worker_result(
            conn,
            binding,
            {"action": "complete", "payload": {"summary": "too late"}},
        )
        assert ok is False and "CAS refused" in error
        assert kb.get_task(conn, tid).status == "running"


def test_live_pid_extends_claim_without_worker_db_heartbeat(
    kanban_home, tmp_path, monkeypatch,
):
    with kb.connect() as conn:
        tid, _workspace, binding = _claimed_binding(conn, tmp_path)
        expired = int(time.time()) - 5
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (expired, tid),
        )
        conn.execute(
            "UPDATE task_runs SET claim_expires = ? WHERE id = ?",
            (expired, binding.run_id),
        )
        conn.commit()
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid == binding.pid)
        monkeypatch.setattr(kb, "_claimer_id", lambda: "host:dispatcher")
        assert kb.release_stale_claims(conn) == 0
        task = kb.get_task(conn, tid)
        assert task.status == "running"
        assert task.claim_expires > int(time.time())
        assert "claim_extended" in [e.kind for e in kb.list_events(conn, tid)]


def test_live_restricted_binding_is_not_false_stale_reclaimed(
    kanban_home, tmp_path, monkeypatch,
):
    with kb.connect() as conn:
        tid, _workspace, binding = _claimed_binding(conn, tmp_path)
        old = int(time.time()) - 10_000
        conn.execute(
            "UPDATE tasks SET started_at = ?, last_heartbeat_at = NULL WHERE id = ?",
            (old, tid),
        )
        conn.execute(
            "UPDATE task_runs SET started_at = ?, last_heartbeat_at = NULL WHERE id = ?",
            (old, binding.run_id),
        )
        conn.commit()
        kb._restricted_worker_bindings[binding.pid] = binding
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid == binding.pid)
        try:
            assert kb.detect_stale_running(conn, stale_timeout_seconds=60) == []
            assert kb.get_task(conn, tid).status == "running"
        finally:
            kb._restricted_worker_bindings.pop(binding.pid, None)


def test_dispatcher_restart_loses_binding_but_runtime_bound_allows_retry(
    kanban_home, tmp_path, monkeypatch,
):
    """Process-local authority loss cannot leave a restricted run unbounded."""
    with kb.connect() as conn:
        tid, _workspace, binding = _claimed_binding(conn, tmp_path)
        kb._restricted_worker_bindings[binding.pid] = binding
        # A restarted dispatcher has no socket/binding. The durable task/run
        # retain only ordinary supervision state and a finite max runtime.
        lost = kb._restricted_worker_bindings.pop(binding.pid)
        lost.channel.close()
        old = int(time.time()) - 600
        conn.execute("UPDATE tasks SET started_at = ? WHERE id = ?", (old, tid))
        conn.execute(
            "UPDATE task_runs SET started_at = ? WHERE id = ?",
            (old, binding.run_id),
        )
        conn.commit()
        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
        monkeypatch.setattr(kb, "_claimer_id", lambda: "host:dispatcher")
        assert kb.enforce_max_runtime(conn, signal_fn=lambda *_args: None) == [tid]
        assert kb.get_task(conn, tid).status == "ready"
        assert kb.latest_run(conn, tid).outcome == "timed_out"
        assert kb.claim_task(conn, tid, claimer="host:retry") is not None
        assert kb.get_task(conn, tid).current_run_id != binding.run_id


def test_result_parser_refuses_generic_or_multiple_mutations():
    invalid = json.dumps(
        {"action": "create", "payload": {"title": "sibling"}}
    ).encode()
    assert lifecycle.parse_result(invalid)[0] is None
    assert lifecycle.parse_result(b"")[0] is None


def test_dispatcher_consumes_clean_exit_result_before_crash_accounting(
    kanban_home, tmp_path, monkeypatch,
):
    with kb.connect() as conn:
        tid, _workspace, binding = _claimed_binding(conn, tmp_path)
        record = {
            "action": "complete",
            "payload": {"summary": "trusted parent finalized"},
        }
        credential_kind = getattr(socket, "SCM_CREDENTIALS", 0x02)
        monkeypatch.setattr(socket, "SCM_CREDENTIALS", credential_kind, raising=False)

        class CredentialChannel:
            def __init__(self):
                self.read = False

            def recvmsg(self, *_args):
                self.read = True
                return (
                    json.dumps(record).encode(),
                    [(socket.SOL_SOCKET, credential_kind,
                      struct.pack("3i", binding.pid, binding.expected_uid, 1))],
                    0,
                    None,
                )

            def recv(self, _size):
                raise BlockingIOError

            def close(self):
                pass

        values = dict(binding.__dict__)
        values["channel"] = CredentialChannel()
        binding = kb.RestrictedWorkerBinding(**values)
        kb._restricted_worker_bindings[binding.pid] = binding
        monkeypatch.setattr(
            kb,
            "_classify_worker_exit",
            lambda pid: ("clean_exit", 0) if pid == binding.pid else ("unknown", None),
        )
        assert kb.process_restricted_worker_results(conn) == [tid]
        assert kb.get_task(conn, tid).status == "done"
        assert tid not in kb.detect_crashed_workers(conn)
        assert binding.pid not in kb._restricted_worker_bindings


def test_dispatcher_processes_restricted_results_only_for_matching_board(
    kanban_home, tmp_path, monkeypatch,
):
    kb.init_db(board="board-a")
    kb.init_db(board="board-b")
    credential_kind = getattr(socket, "SCM_CREDENTIALS", 0x02)
    monkeypatch.setattr(socket, "SCM_CREDENTIALS", credential_kind, raising=False)

    class CredentialChannel:
        def __init__(self):
            self.read = False

        def recvmsg(self, *_args):
            self.read = True
            return (
                json.dumps({
                    "action": "complete",
                    "payload": {"summary": "board B done"},
                }).encode(),
                [(socket.SOL_SOCKET, credential_kind,
                  struct.pack("3i", binding.pid, binding.expected_uid, 1))],
                0,
                None,
            )

        def recv(self, _size):
            raise BlockingIOError

        def close(self):
            pass

    with kb.connect(board="board-b") as conn_b:
        tid, _workspace, original = _claimed_binding(conn_b, tmp_path)
        values = dict(original.__dict__)
        channel = CredentialChannel()
        values["channel"] = channel
        binding = kb.RestrictedWorkerBinding(**values)
        kb._restricted_worker_bindings[binding.pid] = binding

    monkeypatch.setattr(
        kb,
        "_classify_worker_exit",
        lambda pid: ("clean_exit", 0) if pid == binding.pid else ("unknown", None),
    )
    try:
        with kb.connect(board="board-a") as conn_a:
            assert kb.process_restricted_worker_results(conn_a) == []
        assert channel.read is False
        assert kb._restricted_worker_bindings[binding.pid] is binding

        with kb.connect(board="board-b") as conn_b:
            assert kb.process_restricted_worker_results(conn_b) == [tid]
            assert kb.get_task(conn_b, tid).status == "done"
        assert channel.read is True
        assert binding.pid not in kb._restricted_worker_bindings
    finally:
        kb._restricted_worker_bindings.pop(binding.pid, None)


def test_control_channel_refuses_sibling_sender_pid(monkeypatch, tmp_path):
    credential_kind = getattr(socket, "SCM_CREDENTIALS", 0x02)
    monkeypatch.setattr(socket, "SCM_CREDENTIALS", credential_kind, raising=False)
    record = json.dumps(
        {"action": "complete", "payload": {"summary": "forged"}}
    ).encode()

    class SiblingChannel:
        def recvmsg(self, *_args):
            return (
                record,
                [(socket.SOL_SOCKET, credential_kind,
                  struct.pack("3i", 999, 12345, 1))],
                0,
                None,
            )

    binding = kb.RestrictedWorkerBinding(
        pid=111,
        task_id="t_own",
        run_id=1,
        profile="builder",
        workspace_path=str(tmp_path),
        workspace=str(tmp_path),
        board_db_path=str(tmp_path / "kanban.db"),
        claim_lock="claim",
        expected_uid=12345,
        channel=SiblingChannel(),
    )
    parsed, error = kb._read_restricted_worker_result(binding)
    assert parsed is None
    assert "PID/UID" in error


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="SCM_CREDENTIALS is a Linux restricted-runtime contract",
)
def test_linux_control_channel_authenticates_actual_sender(monkeypatch, tmp_path):
    parent, child = kb._create_restricted_control_channel()
    record = {"action": "block", "payload": {"reason": "bounded"}}
    child.send(json.dumps(record).encode())
    binding = kb.RestrictedWorkerBinding(
        pid=os.getpid(),
        task_id="t_own",
        run_id=1,
        profile="builder",
        workspace_path=str(tmp_path),
        workspace=str(tmp_path),
        board_db_path=str(tmp_path / "kanban.db"),
        claim_lock="claim",
        expected_uid=os.getuid(),
        channel=parent,
    )
    try:
        parsed, error = kb._read_restricted_worker_result(binding)
        assert error is None
        assert parsed == record
    finally:
        parent.close()
        child.close()


def test_restricted_spawn_scrubs_board_authority_and_registers_parent_binding(
    kanban_home, tmp_path, monkeypatch,
):
    captured = {}

    class FakeProc:
        pid = 515151

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        return FakeProc()

    launcher = tmp_path / "restricted-launcher"
    launcher.write_text("launcher", encoding="utf-8")
    launcher.chmod(0o755)
    monkeypatch.setenv("HERMES_BIN", str(launcher))
    monkeypatch.setattr(
        kb, "restricted_worker_config", lambda: (True, "restricted-test"),
    )
    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr(kb, "_retag_legacy_worker_sessions", lambda _path: None)
    monkeypatch.setattr(kb, "_resolve_worker_cli_toolsets", lambda _home: None)
    monkeypatch.setattr(
        kb, "_resolve_restricted_worker_uid", lambda _path, _user: 12345,
    )

    parent_channel, child_channel = socket.socketpair()
    monkeypatch.setattr(
        kb,
        "_create_restricted_control_channel",
        lambda: (parent_channel, child_channel),
    )

    with kb.connect() as conn:
        tid, workspace, _binding = _claimed_binding(conn, tmp_path)
        task = kb.get_task(conn, tid)
        assert kb._default_spawn(
            task,
            str(workspace),
            worker_context="trusted context",
        ) == FakeProc.pid
        env = captured["env"]
        assert env[lifecycle.RESTRICTED_WORKER_ENV] == "1"
        assert env[lifecycle.CONTEXT_ENV] == "trusted context"
        for key in (
            "HERMES_KANBAN_DB",
            "HERMES_KANBAN_HOME",
            "HERMES_KANBAN_WORKSPACES_ROOT",
            "HERMES_KANBAN_ATTACHMENTS_ROOT",
            "HERMES_KANBAN_BOARD",
            "HERMES_KANBAN_CLAIM_LOCK",
            "HERMES_KANBAN_RUN_ID",
        ):
            assert key not in env
        registered = kb._restricted_worker_bindings.pop(FakeProc.pid)
        assert registered.task_id == tid
        assert registered.run_id == task.current_run_id
        assert registered.profile == task.assignee
        assert registered.workspace == os.path.realpath(workspace)
        assert registered.board_db_path == os.path.realpath(kb.kanban_db_path())
        assert registered.expected_uid == 12345
        registered.channel.close()


def test_restricted_spawn_refuses_without_absolute_os_launcher(
    kanban_home, tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        kb, "restricted_worker_config", lambda: (True, "restricted-test"),
    )
    monkeypatch.delenv("HERMES_BIN", raising=False)
    monkeypatch.setattr(kb, "_retag_legacy_worker_sessions", lambda _path: None)
    with kb.connect() as conn:
        _tid, workspace, binding = _claimed_binding(conn, tmp_path)
        task = kb.get_task(conn, binding.task_id)
        with pytest.raises(RuntimeError, match="operator-owned HERMES_BIN"):
            kb._default_spawn(task, str(workspace), worker_context="context")


def test_restricted_launcher_requires_immutable_root_owned_ancestry(monkeypatch):
    launcher = "/opt/hermes/bin/restricted-launcher"
    monkeypatch.setattr(os.path, "realpath", lambda value: value)
    monkeypatch.setattr(os.path, "isfile", lambda _value: True)
    monkeypatch.setattr(
        "pwd.getpwnam",
        lambda _name: SimpleNamespace(pw_uid=os.geteuid() + 1000),
    )

    def unsafe_parent(path):
        mode = 0o40775 if path == "/opt/hermes" else 0o40755
        return SimpleNamespace(st_uid=0, st_mode=mode)

    monkeypatch.setattr(os, "lstat", unsafe_parent)
    with pytest.raises(RuntimeError, match="every parent"):
        kb._resolve_restricted_worker_uid(launcher, "worker")


def test_restricted_worker_mode_is_config_yaml_gated():
    assert kb.restricted_worker_config({}) == (False, "")
    assert kb.restricted_worker_config({
        "restricted_workers": {"enabled": True, "os_user": "worker"},
    }) == (True, "worker")


@pytest.mark.parametrize("value", ["false", "0", "off", "no"])
def test_false_like_restricted_env_does_not_select_restricted_prompt(
    monkeypatch, value,
):
    from agent.system_prompt import _is_restricted_kanban_worker

    monkeypatch.setenv(lifecycle.RESTRICTED_WORKER_ENV, value)
    assert _is_restricted_kanban_worker() is False


def test_restricted_spawn_requires_bounded_runtime(
    kanban_home, tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        kb, "restricted_worker_config", lambda: (True, "restricted-test"),
    )
    monkeypatch.setenv("HERMES_BIN", "/usr/local/sbin/hermes-worker")
    monkeypatch.setattr(kb, "_retag_legacy_worker_sessions", lambda _path: None)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="unbounded", assignee="builder")
        workspace = tmp_path / "unbounded"
        workspace.mkdir()
        kb.set_workspace_path(conn, tid, str(workspace))
        kb.claim_task(conn, tid, claimer="host:restricted")
        task = kb.get_task(conn, tid)
        with pytest.raises(RuntimeError, match="positive max_runtime_seconds"):
            kb._default_spawn(task, str(workspace), worker_context="context")


def test_restricted_worker_does_not_attempt_auto_heartbeat(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_own")
    monkeypatch.setenv(lifecycle.RESTRICTED_WORKER_ENV, "1")
    assert kanban_tools.heartbeat_current_worker_from_env() is False
