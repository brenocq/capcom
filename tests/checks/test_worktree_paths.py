"""Worktree folding is not tied to one worktree layout or branch naming."""

from __future__ import annotations

from pathlib import Path

from capcom.checks.rules import _repo_root, _strip_worktree


def test_claude_worktrees_fold_into_the_main_checkout() -> None:
    assert _strip_worktree("/r/p/.claude/worktrees/x/AGENTS.md") == "/r/p/AGENTS.md"
    assert _repo_root(Path("/r/p/.claude/worktrees/x")) == Path("/r/p")


def test_a_branch_name_without_a_known_prefix_folds() -> None:
    assert _strip_worktree("/r/p/.worktrees/team/ticket-7/CLAUDE.md") == "/r/p/CLAUDE.md"
    assert _strip_worktree("/r/p/.worktrees/solo/docs/x.md") == "/r/p/docs/x.md"
    assert _repo_root(Path("/r/p/.worktrees/team/ticket-7")) == Path("/r/p")


def test_the_gitdir_pointer_names_the_main_checkout(tmp_path: Path) -> None:
    main = tmp_path / "main"
    (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
    tree = tmp_path / "elsewhere" / "wt"
    tree.mkdir(parents=True)
    (tree / ".git").write_text(f"gitdir: {main}/.git/worktrees/wt\n")
    (tree / "sub").mkdir()
    assert _repo_root(tree) == main
    assert _repo_root(tree / "sub") == main
    assert _repo_root(main) == main
