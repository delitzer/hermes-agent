from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "install-e2e.yml"
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
LEG_ACTION = ROOT / ".github" / "actions" / "install-e2e-leg" / "action.yml"
DEV_SANDBOX = ROOT / "scripts" / "dev-sandbox.sh"

# Every matrix leg checks the repo out itself (the leg action runs from that
# checkout), so each checkout step in the workflow must carry the same inputs.
MATRIX_JOBS = ("update", "installer")


def _checkout_inputs() -> list[str]:
    text = WORKFLOW.read_text()
    matches = re.findall(
        r"(?ms)^\s*- uses: actions/checkout@[^\n]+\n"
        r"\s+with:\n"
        r"((?:\s{10}[^\n]+\n)+)",
        text,
    )
    assert len(matches) == len(MATRIX_JOBS), (
        f"expected one checkout step per matrix job ({MATRIX_JOBS}), found {len(matches)}"
    )
    return matches


def _logical_shell_commands(path: Path) -> list[str]:
    commands: list[str] = []
    buffer = ""
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        buffer = f"{buffer} {line}".strip()
        if buffer.endswith("\\"):
            buffer = buffer[:-1].rstrip()
            continue
        commands.append(buffer)
        buffer = ""
    if buffer:
        commands.append(buffer)
    return commands


def test_install_e2e_checkout_does_not_fetch_all_history():
    """Matrix legs must not fetch every branch and commit from the fork."""
    for inputs in _checkout_inputs():
        assert re.search(
            r"(?m)^\s*fetch-depth:\s*1\s*$", inputs
        ), "matrix checkout must use bounded history"


def test_install_e2e_checkout_keeps_release_tags_available():
    """The local sandbox upstream must still resolve each selected release."""
    for inputs in _checkout_inputs():
        assert re.search(
            r"(?m)^\s*fetch-tags:\s*true\s*$", inputs
        ), "matrix checkout must fetch release tags for the local sandbox upstream"


def test_install_e2e_leg_fails_early_when_install_ref_is_missing():
    """A missing selected tag must fail before the expensive sandbox setup."""
    action = LEG_ACTION.read_text()
    assert "name: Verify selected install ref" in action
    assert 'git rev-parse --verify "${INSTALL_REF}^{commit}"' in action
    assert "git rev-parse --is-shallow-repository" in action
    assert "git --version" in action


def test_install_e2e_matrix_jobs_use_local_action_not_reusable_workflow():
    """Startup must not depend on fetching a second workflow file.

    GitHub resolves ``jobs.<id>.uses: ./.github/workflows/...`` when the run
    starts, and that fetch fails intermittently: the run is marked failed with
    zero jobs and nothing to retry. A local action is read from the job's own
    checkout instead.
    """
    workflow = WORKFLOW.read_text()
    assert not re.search(
        r"(?m)^\s+uses:\s*\./\.github/workflows/", workflow
    ), "install-e2e legs must not call a reusable workflow"
    assert workflow.count("uses: ./.github/actions/install-e2e-leg") == len(MATRIX_JOBS)
    assert not (WORKFLOWS_DIR / "install-e2e-run.yml").exists()


def test_dev_sandbox_preflights_git_remote_builder():
    """The extracted remote builder must exist in repo and Nix sandbox modes."""
    assert "prepare-git-remote.sh" in DEV_SANDBOX.read_text().split("done", 1)[0]


def test_dev_sandbox_accepts_shallow_local_upstreams():
    """All local-source fetches must propagate shallow boundaries."""
    local_sources = (
        '"$UPSTREAM_URL"',
        '"$UPSTREAM_REPO"',
        '"$GIT_ROOT"',
        '"$FAKE_REPO"',
        '"$SOURCE_REPO"',
    )
    fetches = [
        command
        for command in _logical_shell_commands(DEV_SANDBOX)
        if " fetch " in command and any(source in command for source in local_sources)
    ]

    assert fetches, "expected dev-sandbox local-source fetch commands"
    missing = [command for command in fetches if "--update-shallow" not in command]
    assert not missing, "local shallow fetches need --update-shallow:\n" + "\n".join(missing)
