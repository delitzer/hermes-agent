from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "install-e2e-run.yml"
DEV_SANDBOX = ROOT / "scripts" / "dev-sandbox.sh"


def _checkout_inputs() -> str:
    text = WORKFLOW.read_text()
    match = re.search(
        r"(?ms)^\s*- uses: actions/checkout@[^\n]+\n"
        r"\s+with:\n"
        r"(?P<inputs>(?:\s{10}[^\n]+\n)+)",
        text,
    )
    assert match is not None, "reusable E2E workflow must have a checkout step"
    return match.group("inputs")


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


def test_reusable_install_e2e_checkout_does_not_fetch_all_history():
    """Matrix legs must not fetch every branch and commit from the fork."""
    assert re.search(
        r"(?m)^\s*fetch-depth:\s*1\s*$", _checkout_inputs()
    ), "matrix checkout must use bounded history"


def test_reusable_install_e2e_checkout_keeps_release_tags_available():
    """The local sandbox upstream must still resolve each selected release."""
    assert re.search(
        r"(?m)^\s*fetch-tags:\s*true\s*$", _checkout_inputs()
    ), "matrix checkout must fetch release tags for the local sandbox upstream"


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
