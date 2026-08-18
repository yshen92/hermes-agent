"""Hermes never adopts a pre-existing branch when materializing a worktree.

``_ensure_git_worktree`` used to fall back to ``git worktree add <target>
<branch>`` whenever the expected task branch already existed, so a stale or
attacker-planted ``wt/<task-id>`` was silently adopted at dispatch and the
task ran on history nobody chose. The same arm made a vanished worktree with
a surviving branch look like a legitimate resume.

The invariant under test: reusing this task's existing linked worktree still
works, but materializing a NEW one fails closed when a Hermes-DERIVED branch
name already exists — refusing before any filesystem side effect and leaving
the pre-existing ref untouched. Derived means the task row alone implies the
name: a project-linked ``<slug>/<task-id…>``, or the ``wt/<task-id>`` fallback
— which stays derived after the dispatcher persists the resolved branch back
to the row, so re-dispatch cannot launder it into a caller's choice. A branch
the caller named explicitly on a non-project task (``kanban create --branch
<name>``) is still adopted, which is what that flag has always meant.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import projects_db as pdb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git", "-C", str(cwd),
            "-c", "user.name=Test User",
            "-c", "user.email=test@example.com",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        check=True, capture_output=True, text=True,
    )
    return result.stdout


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True, capture_output=True, text=True,
    )
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    return repo


def _worktree_paths(repo: Path) -> list[str]:
    out = _git(repo, "worktree", "list", "--porcelain")
    return [
        line.split(" ", 1)[1]
        for line in out.splitlines()
        if line.startswith("worktree ")
    ]


def _make_task_anchored_at_repo(repo: Path) -> kb.Task:
    """Create a worktree-kind task anchored on ``repo``'s root."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="run on the task branch",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        return kb.get_task(conn, tid)


def _plant_branch_off_head(repo: Path, branch: str) -> None:
    """Point ``branch`` at a commit that is NOT the repo's current HEAD."""
    _git(repo, "branch", branch)
    (repo / "main-only.txt").write_text("advanced\n", encoding="utf-8")
    _git(repo, "add", "main-only.txt")
    _git(repo, "commit", "-m", "advance main past the planted branch")


def test_materialize_refuses_preexisting_branch(kanban_home, tmp_path):
    repo = _make_repo(tmp_path)
    task = _make_task_anchored_at_repo(repo)
    _plant_branch_off_head(repo, f"wt/{task.id}")

    with pytest.raises(RuntimeError, match="refusing"):
        kb._resolve_worktree_workspace(task)

    # The refusal happens before any filesystem side effect: no .worktrees
    # directory, and git still knows only the main checkout.
    assert not (repo / ".worktrees").exists()
    assert _worktree_paths(repo) == [str(repo.resolve())]


def test_refusal_preserves_preexisting_branch_ref(kanban_home, tmp_path):
    repo = _make_repo(tmp_path)
    task = _make_task_anchored_at_repo(repo)
    branch = f"wt/{task.id}"
    _plant_branch_off_head(repo, branch)

    sha_before = _git(repo, "rev-parse", f"refs/heads/{branch}")
    show_ref_before = _git(repo, "show-ref", branch)

    with pytest.raises(RuntimeError, match="refusing"):
        kb._resolve_worktree_workspace(task)

    assert _git(repo, "rev-parse", f"refs/heads/{branch}") == sha_before
    assert _git(repo, "show-ref", branch) == show_ref_before


def test_materialize_succeeds_when_branch_absent(kanban_home, tmp_path):
    repo = _make_repo(tmp_path)
    task = _make_task_anchored_at_repo(repo)

    workspace, branch = kb._resolve_worktree_workspace(task)

    assert workspace == (repo / ".worktrees" / task.id).resolve()
    assert branch == f"wt/{task.id}"
    assert _git(workspace, "branch", "--show-current").strip() == branch
    porcelain = _git(repo, "worktree", "list", "--porcelain")
    assert str(workspace) in _worktree_paths(repo)
    assert f"branch refs/heads/{branch}" in porcelain


def test_existing_task_worktree_on_expected_branch_resumes(kanban_home, tmp_path):
    repo = _make_repo(tmp_path)
    task = _make_task_anchored_at_repo(repo)
    branch = f"wt/{task.id}"

    workspace, _ = kb._resolve_worktree_workspace(task)

    # The branch exists now — only fresh materialization refuses, so both
    # reuse paths must still resolve to the same checkout.
    again, again_branch = kb._resolve_worktree_workspace(task)
    assert (again, again_branch) == (workspace, branch)

    with kb.connect() as conn:
        kb.set_workspace_path(conn, task.id, workspace)
        resumed_task = kb.get_task(conn, task.id)

    resumed, resumed_branch = kb._resolve_worktree_workspace(resumed_task)
    assert (resumed, resumed_branch) == (workspace, branch)


def test_vanished_worktree_with_surviving_branch_fails_closed(kanban_home, tmp_path):
    repo = _make_repo(tmp_path)
    task = _make_task_anchored_at_repo(repo)
    branch = f"wt/{task.id}"

    # Legitimate first dispatch: Hermes materializes the worktree itself.
    workspace, resolved_branch = kb._resolve_worktree_workspace(task)

    # …and the dispatcher writes the resolution back to the row, so the
    # derived wt/<task-id> is now PERSISTED branch_name (kanban_db.py:9057).
    with kb.connect() as conn:
        kb.set_workspace_path(conn, task.id, str(workspace))
        kb.set_branch_name(conn, task.id, resolved_branch)
        task = kb.get_task(conn, task.id)
    assert task.branch_name == branch

    # The checkout goes away (manual cleanup, pruned disk, restored backup)
    # but the branch survives — the headline L9a scenario.
    _git(repo, "worktree", "remove", "--force", str(workspace))
    assert not workspace.exists()
    assert str(workspace) not in _worktree_paths(repo)
    sha_before = _git(repo, "rev-parse", f"refs/heads/{branch}")

    # Re-dispatch must not read the orphaned branch as a resume. Persistence
    # must not launder a derived name into a caller-chosen one.
    with pytest.raises(RuntimeError, match="refusing"):
        kb._resolve_worktree_workspace(task)

    assert _git(repo, "rev-parse", f"refs/heads/{branch}") == sha_before


def test_project_linked_task_refuses_planted_derived_branch(kanban_home, tmp_path):
    repo = _make_repo(tmp_path)
    with pdb.connect_closing() as pconn:
        pid = pdb.create_project(pconn, name="Widget Service", primary_path=str(repo))
        project = pdb.get_project(pconn, pid)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="harden the dispatcher",
            workspace_kind="worktree",
            project_id=pid,
        )
        task = kb.get_task(conn, tid)

    # The branch is the one create_task derived, not one we hand-built.
    assert task.project_id == pid
    branch = task.branch_name
    assert branch and branch.startswith(f"{project.slug}/{tid}")

    _plant_branch_off_head(repo, branch)
    sha_before = _git(repo, "rev-parse", f"refs/heads/{branch}")

    with pytest.raises(RuntimeError, match="refusing"):
        kb._resolve_worktree_workspace(task)

    assert _git(repo, "rev-parse", f"refs/heads/{branch}") == sha_before
    assert not Path(task.workspace_path).parent.exists()
    assert _worktree_paths(repo) == [str(repo.resolve())]


def test_nonproject_explicit_existing_branch_still_materializes(kanban_home, tmp_path):
    repo = _make_repo(tmp_path)
    _plant_branch_off_head(repo, "feature-x")
    planted = _git(repo, "rev-parse", "refs/heads/feature-x").strip()

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="keep working the feature branch",
            workspace_kind="worktree",
            workspace_path=str(repo),
            branch_name="feature-x",
        )
        task = kb.get_task(conn, tid)

    workspace, branch = kb._resolve_worktree_workspace(task)

    assert workspace == (repo / ".worktrees" / tid).resolve()
    assert branch == "feature-x"
    assert _git(workspace, "branch", "--show-current").strip() == "feature-x"
    # HEAD is the planted commit, not the repo's HEAD: the caller's branch was
    # checked out, not re-created from HEAD under the same name.
    assert _git(workspace, "rev-parse", "HEAD").strip() == planted
    assert _git(repo, "rev-parse", "refs/heads/feature-x").strip() == planted


def test_branch_appearing_after_check_fails_closed(kanban_home, tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    branch = "wt/t_race"
    _plant_branch_off_head(repo, branch)
    sha_before = _git(repo, "rev-parse", f"refs/heads/{branch}")

    # Model the branch racing in after the existence check: the gate sees
    # nothing, so the derived name reaches `git worktree add -b`.
    monkeypatch.setattr(kb, "_git_branch_exists", lambda *a, **kw: False)
    target = repo / ".worktrees" / "t_race"
    with pytest.raises(RuntimeError) as excinfo:
        kb._ensure_git_worktree(repo, target, branch)

    # Loud git failure, not the refusal gate and not a silent adoption.
    assert "git worktree add failed" in str(excinfo.value)
    assert "refusing" not in str(excinfo.value)
    assert not target.exists()
    assert _git(repo, "rev-parse", f"refs/heads/{branch}") == sha_before
    assert _worktree_paths(repo) == [str(repo.resolve())]
