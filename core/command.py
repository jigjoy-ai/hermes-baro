"""Build the pinned Baro command line and validate tool arguments.

Sources, all in the Baro repository (paths relative to its root):

- Version pin: packages/baro-app/package.json:2-3 publishes the CLI package
  with ``"version": "0.121.0"`` (crates/baro-tui/Cargo.toml:3 carries the same
  version for the Rust host). Its ``bin`` entry ``baro`` at
  packages/baro-app/package.json:6-8 is what ``npx`` runs. That launcher
  forwards argv unchanged to the native binary
  (packages/baro-app/bin/baro:13 and :34). It skips its bundled updater
  whenever ``--headless`` is in argv (the ``skipReason`` check at line 31 of the
  updater module in packages/baro-app/bin/), so a pinned headless run stays on
  the pinned version.
- Flags: clap definitions in crates/baro-tui/src/cli/cli.rs:
    positional goal                   cli.rs:15-16
    --cwd <dir> (default ".")         cli.rs:39-40
    --local-only                      cli.rs:61-63
    --model / -m <name>               cli.rs:106-109
    --llm <backend> (default claude)  cli.rs:221-222
    --openai-base-url <url>           cli.rs:224-227
    --headless                        cli.rs:247-250
  No plan-only flag exists. cli.rs:7-269 (the whole Cli struct) has no such argument and no
  subcommand. ``--headless`` auto-confirms the plan and goes on to execution
  (cli.rs:247-248). So PLAN_ONLY_FLAG is None, and the plan tool must stop the
  process itself after the first plan event (the ``init`` event the
  orchestrator emits, packages/baro-orchestrator/src/tui-protocol.ts:93).

No backend, base URL or model is ever defaulted here. Baro then uses whatever
backend its user configured (cli.rs:221 defaults ``--llm`` itself).
"""

from __future__ import annotations

import os
import shlex

# packages/baro-app/package.json:3 (exact version, no range).
BARO_VERSION = "0.121.0"
# The single pinned npm spec. BARO_PACKAGE is derived from it, so no unpinned
# package literal exists anywhere in the plugin.
BARO_SPEC = "baro-ai@" + BARO_VERSION
BARO_PACKAGE = BARO_SPEC.split("@")[0]

# crates/baro-tui/src/cli/cli.rs has no plan-only argument (see module docstring).
PLAN_ONLY_FLAG: str | None = None

MAX_GOAL_CHARS = 4000

# Flag spellings, confirmed at crates/baro-tui/src/cli/cli.rs (lines above).
_FLAG_HEADLESS = "--headless"  # cli.rs:249
_FLAG_CWD = "--cwd"  # cli.rs:39
_FLAG_LOCAL_ONLY = "--local-only"  # cli.rs:62 (clap kebab-cases local_only)
_FLAG_LLM = "--llm"  # cli.rs:221
_FLAG_BASE_URL = "--openai-base-url"  # cli.rs:226 (kebab-cased openai_base_url)
_FLAG_MODEL = "--model"  # cli.rs:108


def build_command(
    goal: str,
    repo: str,
    *,
    llm: str | None = None,
    openai_base_url: str | None = None,
    model: str | None = None,
    plan_only: bool = False,
) -> str:
    """Return the shell command that runs the pinned Baro headless on ``repo``.

    Shape: ``npx --yes <BARO_SPEC> --headless --cwd <repo> --local-only
    [--llm X] [--openai-base-url Y] [--model Z] [PLAN_ONLY_FLAG] <goal>``.
    Every value is ``shlex.quote``d. Each optional flag is emitted only when its
    value is a non-empty string. ``plan_only`` adds PLAN_ONLY_FLAG only when Baro
    has one (it does not, so it is currently a no-op).
    """
    parts = [
        "npx",
        "--yes",
        BARO_SPEC,
        _FLAG_HEADLESS,
        _FLAG_CWD,
        shlex.quote(repo),
        _FLAG_LOCAL_ONLY,
    ]
    for flag, value in (
        (_FLAG_LLM, llm),
        (_FLAG_BASE_URL, openai_base_url),
        (_FLAG_MODEL, model),
    ):
        if isinstance(value, str) and value:
            parts += [flag, shlex.quote(value)]
    if plan_only and PLAN_ONLY_FLAG:
        parts.append(PLAN_ONLY_FLAG)
    parts.append(shlex.quote(goal))
    return " ".join(parts)


def validate_args(goal: object, repo: object) -> None:
    """Raise ``ValueError`` unless ``goal`` and ``repo`` are acceptable.

    ``goal``: a non-empty string of at most MAX_GOAL_CHARS characters, with no
    NUL and not starting with ``-``. Baro takes the goal as a positional clap
    argument (cli.rs:15-16), so a leading ``-`` would be parsed as a flag.
    ``repo``: an absolute path to an existing directory.
    """
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("goal must be a non-empty string")
    if len(goal) > MAX_GOAL_CHARS:
        raise ValueError(f"goal must be at most {MAX_GOAL_CHARS} characters")
    if "\x00" in goal:
        raise ValueError("goal must not contain a NUL character")
    if goal.startswith("-"):
        raise ValueError("goal must not start with '-' (Baro would read it as a flag)")
    if not isinstance(repo, str) or not repo:
        raise ValueError("repo must be a non-empty string")
    if "\x00" in repo:
        raise ValueError("repo must not contain a NUL character")
    if not os.path.isabs(repo):
        raise ValueError(f"repo must be an absolute path, got {repo!r}")
    if not os.path.isdir(repo):
        raise ValueError(f"repo must be an existing directory, got {repo!r}")
