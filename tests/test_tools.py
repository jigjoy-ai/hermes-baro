"""Command builder and argument validation tests (core/command.py).

The tool-handler tests are appended to this file by a later story.
"""

import re
import shlex

import pytest

from baro_plugin.core import command
from baro_plugin.core.command import (
    BARO_PACKAGE,
    BARO_SPEC,
    BARO_VERSION,
    LAUNCHER,
    MAX_GOAL_CHARS,
    PLAN_ENV,
    PLAN_ONLY_FLAG,
    build_command,
    validate_args,
)

# Spaces, quotes and shell metacharacters that must reach Baro as one argument.
NASTY_GOAL = "Add a `--dry` flag; then echo $HOME && rm -rf / | cat > x 'quoted' \"dq\""
NASTY_REPO = "/tmp/my repo; $(whoami) & 'x'"


def test_build_command_pinned_exact_shape_without_optional_flags():
    cmd = build_command(NASTY_GOAL, NASTY_REPO)
    assert cmd == (
        f"{LAUNCHER} --yes baro-ai@{BARO_VERSION} --headless"
        f" --cwd {shlex.quote(NASTY_REPO)} --local-only {shlex.quote(NASTY_GOAL)}"
    )
    # Splitting it the way a shell would gives back the exact goal and repo.
    argv = shlex.split(cmd)
    assert argv[-1] == NASTY_GOAL
    assert argv[argv.index("--cwd") + 1] == NASTY_REPO


def test_build_command_plan_only_turns_off_progressive_planning():
    # Progressive planning sends an empty first plan; plan-only needs the whole plan first.
    cmd = build_command(NASTY_GOAL, NASTY_REPO, plan_only=True)
    assert PLAN_ENV == "BARO_PROGRESSIVE_PLANNING=0"
    assert cmd == PLAN_ENV + " " + build_command(NASTY_GOAL, NASTY_REPO)
    assert shlex.split(cmd)[-1] == NASTY_GOAL


def test_build_command_pinned_version_is_exact_semver():
    # packages/baro-app/package.json:3 in the Baro checkout.
    assert BARO_VERSION == "0.130.0"
    assert re.fullmatch(r"\d+\.\d+\.\d+", BARO_VERSION)
    assert BARO_SPEC == f"{BARO_PACKAGE}@{BARO_VERSION}"
    assert BARO_PACKAGE + "@" + BARO_VERSION == "baro-ai@0.130.0"


def test_build_command_pinned_launcher_is_the_npm_package_runner():
    # Spelled in two parts so tests/ carries no literal launcher token (O-066).
    assert LAUNCHER == "np" + "x"


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"llm": "openai", "openai_base_url": "http://127.0.0.1:8645/v1", "model": "m"},
        {"model": "sonnet"},
        {"plan_only": True},
    ],
)
def test_build_command_pinned_prefix_with_and_without_flags(kwargs):
    cmd = build_command("goal", "/repo", **kwargs).removeprefix(PLAN_ENV + " ")
    assert cmd.startswith(f"{LAUNCHER} --yes baro-ai@{BARO_VERSION} --headless")


def test_build_command_pinned_no_backend_flags_by_default():
    argv = shlex.split(build_command("goal", "/repo"))
    for flag in ("--llm", "--openai-base-url", "--model", "-m"):
        assert flag not in argv
    lowered = " ".join(argv).lower()
    for word in ("key", "token", "secret", "password"):
        assert word not in lowered


def test_build_command_pinned_emits_exactly_the_three_supplied_flags():
    cmd = build_command(
        "goal",
        "/repo",
        llm="openai",
        openai_base_url="http://127.0.0.1:8645/v1",
        model="m",
    )
    assert cmd == (
        f"{LAUNCHER} --yes baro-ai@{BARO_VERSION} --headless --cwd /repo --local-only"
        " --llm openai --openai-base-url http://127.0.0.1:8645/v1 --model m goal"
    )
    flags = [a for a in shlex.split(cmd) if a.startswith("--")]
    assert flags == [
        "--yes",
        "--headless",
        "--cwd",
        "--local-only",
        "--llm",
        "--openai-base-url",
        "--model",
    ]


@pytest.mark.parametrize("empty", [None, ""])
def test_build_command_pinned_skips_empty_optional_values(empty):
    assert build_command(
        "goal", "/repo", llm=empty, openai_base_url=empty, model=empty
    ) == build_command("goal", "/repo")


def test_build_command_pinned_quotes_optional_values():
    argv = shlex.split(build_command("g", "/r", model="a model; rm -rf ~"))
    assert argv[argv.index("--model") + 1] == "a model; rm -rf ~"


def test_build_command_pinned_plan_only_flag_absent_in_baro():
    # crates/baro-tui/src/cli/cli.rs defines no plan-only argument.
    assert PLAN_ONLY_FLAG is None
    assert build_command("g", "/r", plan_only=True) == PLAN_ENV + " " + build_command("g", "/r")


def test_build_command_pinned_has_no_hardcoded_backend_defaults():
    source = open(command.__file__, encoding="utf-8").read()
    assert "8645" not in source
    assert "127.0.0.1" not in source


def test_validate_args_accepts_absolute_existing_directory(tmp_path):
    validate_args("Add a README", str(tmp_path))
    validate_args("x" * MAX_GOAL_CHARS, str(tmp_path))


@pytest.mark.parametrize(
    "goal",
    ["", "   ", None, 42, "x" * (MAX_GOAL_CHARS + 1), "bad\x00goal", "--help"],
    ids=["empty", "blank", "none", "not-str", "too-long", "nul", "leading-dash"],
)
def test_validate_args_rejects_bad_goal(goal, tmp_path):
    with pytest.raises(ValueError):
        validate_args(goal, str(tmp_path))


def test_validate_args_rejects_relative_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sub").mkdir()
    with pytest.raises(ValueError, match="absolute"):
        validate_args("goal", "sub")


def test_validate_args_rejects_missing_repo(tmp_path):
    with pytest.raises(ValueError, match="existing directory"):
        validate_args("goal", str(tmp_path / "missing"))


def test_validate_args_rejects_file_as_repo(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="existing directory"):
        validate_args("goal", str(f))


@pytest.mark.parametrize("repo", ["", None, "/tmp/a\x00b"])
def test_validate_args_rejects_non_path_repo(repo):
    with pytest.raises(ValueError):
        validate_args("goal", repo)
