from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PREPARE_REMOTE = ROOT / "scripts" / "sandbox" / "prepare-git-remote.sh"


def _run(*args: str | Path, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [str(arg) for arg in args],
        cwd=cwd,
        text=True,
        capture_output=True,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"command failed ({result.returncode}): {' '.join(map(str, args))}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run("git", "-C", repo, *args, check=check)


def _sha(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", ref).stdout.strip()


def _history(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "upstream"
    _run("git", "init", "-q", "-b", "main", repo)
    _git(repo, "config", "user.name", "Sandbox test")
    _git(repo, "config", "user.email", "sandbox-test@invalid")

    tracked = repo / "tracked.txt"
    tracked.write_text("release\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-q", "-m", "release")
    _git(repo, "tag", "-a", "v1.0.0", "-m", "release v1")
    release = _sha(repo, "v1.0.0^{commit}")

    tracked.write_text("middle\n")
    _git(repo, "commit", "-q", "-am", "middle")
    tracked.write_text("current\n")
    _git(repo, "commit", "-q", "-am", "current")
    return repo, release, _sha(repo, "HEAD")


def _bare_repo(path: Path) -> Path:
    _run("git", "init", "--bare", "-q", path)
    return path


def _prepare(
    *,
    source: Path,
    source_commit: str,
    upstream: Path,
    release: str,
    fake: Path,
    promote_file: Path,
) -> subprocess.CompletedProcess[str]:
    return _run(
        "bash",
        PREPARE_REMOTE,
        source,
        source_commit,
        upstream,
        release,
        "v1.0.0",
        fake,
        promote_file,
        check=False,
    )


def test_shallow_source_publishes_complete_fast_forward_update(tmp_path: Path) -> None:
    upstream, release, _ = _history(tmp_path)
    source = tmp_path / "source"
    _run("git", "clone", "-q", "--depth", "1", "--branch", "main", f"file://{upstream}", source)
    _git(
        source,
        "fetch",
        "-q",
        "--depth",
        "1",
        "origin",
        "refs/tags/v1.0.0:refs/tags/v1.0.0",
    )
    assert _sha(source, "--is-shallow-repository") == "true"
    source_head = _sha(source, "HEAD")
    assert _git(source, "cat-file", "-e", f"{source_head}^{{commit}}^", check=False).returncode != 0

    fake = _bare_repo(tmp_path / "fake.git")
    promote_file = tmp_path / "promote-main"
    result = _prepare(
        source=source,
        source_commit=source_head,
        upstream=upstream,
        release=release,
        fake=fake,
        promote_file=promote_file,
    )

    assert result.returncode == 0, result.stderr
    target = result.stdout.strip()
    assert target and target != source_head
    assert _sha(fake, "refs/heads/main") == release
    assert _sha(fake, "refs/hermes-sandbox/next") == target
    assert _sha(fake, f"{target}^") == release
    assert _sha(fake, f"{target}^{{tree}}") == _sha(source, f"{source_head}^{{tree}}")
    assert promote_file.read_text().strip() == target
    _git(fake, "rev-list", "--objects", f"{release}..{target}")

    installed = tmp_path / "installed"
    _run("git", "clone", "-q", "--branch", "main", f"file://{fake}", installed)
    assert _sha(installed, "HEAD") == release
    _git(fake, "update-ref", "refs/heads/main", target)
    _git(installed, "fetch", "-q", "origin", "main")
    assert _git(installed, "rev-list", "HEAD..origin/main", "--count").stdout.strip() == "1"
    _git(installed, "merge", "-q", "--ff-only", "origin/main")
    assert _sha(installed, "HEAD^{tree}") == _sha(source, "HEAD^{tree}")


def test_dirty_source_snapshot_keeps_worktree_and_release_parent(tmp_path: Path) -> None:
    source, release, source_head = _history(tmp_path)
    (source / "tracked.txt").write_text("dirty current\n")
    fake = _bare_repo(tmp_path / "fake.git")

    result = _prepare(
        source=source,
        source_commit=source_head,
        upstream=source,
        release=release,
        fake=fake,
        promote_file=tmp_path / "promote-main",
    )

    assert result.returncode == 0, result.stderr
    target = result.stdout.strip()
    assert target != source_head
    assert _sha(fake, f"{target}^") == release
    assert _git(fake, "show", f"{target}:tracked.txt").stdout == "dirty current\n"


def test_complete_source_preserves_real_head(tmp_path: Path) -> None:
    source, release, source_head = _history(tmp_path)
    fake = _bare_repo(tmp_path / "fake.git")

    result = _prepare(
        source=source,
        source_commit=source_head,
        upstream=source,
        release=release,
        fake=fake,
        promote_file=tmp_path / "promote-main",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == source_head
    assert _sha(fake, "refs/hermes-sandbox/next") == source_head


def test_invalid_source_commit_fails_before_publication(tmp_path: Path) -> None:
    source, release, _ = _history(tmp_path)
    fake = _bare_repo(tmp_path / "fake.git")
    promote_file = tmp_path / "promote-main"

    result = _prepare(
        source=source,
        source_commit="0" * 40,
        upstream=source,
        release=release,
        fake=fake,
        promote_file=promote_file,
    )

    assert result.returncode != 0
    assert "could not verify source commit" in result.stderr
    assert not promote_file.exists()
    assert _git(fake, "show-ref", "--verify", "refs/heads/main", check=False).returncode != 0
