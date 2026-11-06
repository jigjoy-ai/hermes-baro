# hermes-baro

Baro for Hermes Agent: dependency-aware multi-agent execution with a live kanban.

## What it does

`hermes-baro` is a native Hermes Agent plugin named `baro`. You give it a
software goal and a repository. [Baro](https://github.com/jigjoy-ai/baro) plans
the goal as stories that depend on each other, then runs coding agents on those
stories in dependency order. The plugin turns Baro's headless event stream into
a board that you can follow live:

- a `baro` toolset with four tools: `baro_plan`, `baro_run`, `baro_status` and
  `baro_cancel`;
- a run-state core in plain Python (`core/`). It parses Baro's events (plan,
  story start, progress, completion, failure, replan, run end) into a board and
  saves it in the plugin's Hermes state (`ctx.state`);
- a Desktop kanban page (`desktop/plugin.js`), served by a small read-only
  backend (`dashboard/`).

## Autonomous by design, guarded by the harness

Baro is built to be fire and forget: you hand it a goal, and it plans, builds,
reviews and integrates the work on its own, normally ending with a pull request.
It does not stop to ask about each command, because a run executes hundreds of
them. Instead of a prompt per command, Baro's harness guards the run:

- **Isolated worktrees.** Every story runs in its own git worktree on its own
  branch, and work is integrated in a separate run worktree on a `baro/<goal>`
  branch, so your checkout is not where agents work.
- **An OS sandbox around each story.** With Baro's default Claude backend,
  every shell command a story runs, and every program it starts (a script, a
  test, an install), runs inside Claude Code's OS sandbox: Seatbelt on macOS,
  bubblewrap on Linux. It can write only to the story's worktree, the git
  metadata a commit needs, a Baro-owned package cache and the run's shared
  dependency folders. It cannot read credential stores (`~/.ssh`, `~/.aws`,
  `~/.config/gh`, `~/.npmrc` and similar), and it reaches the network only
  through package registries. If the sandbox cannot start (on Linux, when
  bubblewrap is not installed), the story does not run.
- **Guarded file edits.** Baro checks each file write and edit an agent makes
  and refuses those outside the story's worktree.
- **No publishing by agents.** `git push`, `gh`, changes to git config and
  remotes are refused in agent lanes, credentials are removed from agent
  environments, and push URLs are disabled. Through this plugin Baro itself
  never publishes either: the plugin always runs it with `--local-only`, so
  the run's result stays on your machine.
- **Owned files.** Each story writes the files it owns; a write to a file that
  another unfinished story holds is refused, and ownership is checked again
  when work is integrated.
- **Reviewed before it lands.** A story integrates only after the Critic accepts
  it, and the merged result is verified at the end of the run.
- **Nothing is lost.** The work of a failed or cancelled attempt is kept on a
  recovery branch.

**What this does not guarantee.** Agents run as your user account. Inside the
sandbox they can read your files outside the credential stores listed above
(another repository, for example), and they can download packages from public
registries, which run install scripts. With the OpenAI-compatible backend used
for Hermes Portal (`--llm openai`), the OS sandbox covers less: on macOS, shell
writes are confined to the worktree by Seatbelt, but reads and network are not
limited; on Linux there is no OS sandbox, and Baro's command guard, which
judges each command and refuses those that would leave the worktree, is the
only check. Treat a Baro run like a developer you let work in that repository:
approve it on repositories and machines where that is acceptable.

### What you approve in Hermes

Because Baro does not ask per command, the plugin asks you once, up front, for
the whole run. Its `pre_tool_call` hook sends every `baro_run` and `baro_plan`
call to Hermes's human-approval gate, whatever approval mode you use (`smart`
included), with a message that says what the run will do. If you deny, the
approval times out or the gate fails, nothing starts. Cron and unattended
sessions are denied by Hermes's own defaults, and the plugin refuses them itself
(see [Unattended contexts](#unattended-contexts)). The plugin never approves on
your behalf and never turns a Hermes guard off. `baro_status` only reads stored
state and needs no approval; `baro_cancel` stops a run through Hermes's
`terminal` tool, which applies its usual approval rules.

## Install

```bash
hermes plugins install https://github.com/jigjoy-ai/hermes-baro
hermes plugins enable baro
```

`<git-url>` is this repository's clone URL, for example
`https://github.com/jigjoy-ai/hermes-baro`. Hermes asks `Enable 'baro' now? [y/N]`
after the install. Pass `--enable` or `--no-enable` to skip that question. For a
reproducible install, pin a full commit with
`hermes plugins install https://github.com/jigjoy-ai/hermes-baro --ref <40-hex-sha>`.

Enabling turns on the agent half (the tools and the approval hook). Hermes
Desktop keeps the kanban page of a plugin installed from Git off until you turn
it on: open **Capabilities → Plugins → Baro** and switch on **Desktop**. A
**Baro** entry then appears in the sidebar.

Requirements:

- Hermes Agent `>=0.22.7` (`requires_hermes` in `plugin.yaml`).
- Node.js with `npx` on the `PATH` of the machine where Hermes runs terminal
  commands. The plugin runs the pinned Baro package through `npx`.
- A Baro backend that you have configured yourself (Claude by default, or
  another `--llm` backend). The plugin never supplies or reads credentials.

## Tools

All four tools are in the toolset `baro`. A tool returns a JSON object. A failure
always has the shape `{"ok": false, "error": <code>, "message": <text>}`, where
`<code>` is one of `invalid_argument`, `unattended_context`, `approval_denied`,
`dispatch_failed`, `unknown_run` or `plan_timeout`.

### `baro_plan`

Asks Baro for a plan and returns it as stories with dependency edges. It never
executes a story.

| Parameter | Type | Notes |
| --- | --- | --- |
| `goal` | string, required | Plain-language goal. Up to 4000 characters, no NUL, must not start with `-`. |
| `repo` | string, required | Absolute path of an existing directory. |
| `llm` | string, optional | Passed to Baro as `--llm`. Omit to use Baro's configured backend. |
| `openai_base_url` | string, optional | Passed to Baro as `--openai-base-url`. |
| `model` | string, optional | Passed to Baro as `--model`. |

Result:

```json
{
  "ok": true,
  "goal": "...",
  "repo": "/abs/path",
  "baro_version": "0.123.0",
  "stories": [{"id": "S1", "title": "...", "depends_on": []},
              {"id": "S2", "title": "...", "depends_on": ["S1"]}],
  "edges": [{"from": "S1", "to": "S2"}],
  "native_plan_only": false,
  "stories_started": 0
}
```

An edge always reads `{from: prerequisite, to: dependent}`. Baro has no
plan-only mode: `--headless` confirms the plan and goes on to execute it. So
`baro_plan` runs Baro with `BARO_PROGRESSIVE_PLANNING=0`, which makes Baro plan
the whole goal before it starts any story, reads the output until the first plan
event that holds stories, and stops the process at once
(`native_plan_only: false`). The stopped run leaves an empty `baro/<goal>`
branch in the repository. If Baro starts a story before it reports a
plan, the process is stopped and the tool returns `dispatch_failed`. If no plan
arrives within 600 seconds, the process is stopped and the tool returns
`plan_timeout`.

### `baro_run`

Starts a Baro run in the background and returns at once. It does not wait for
any output.

Parameters: the same as `baro_plan`.

Result:

```json
{
  "ok": true,
  "run_id": "baro-20261105T142233Z-a1b2c3",
  "status": "running",
  "baro_version": "0.123.0",
  "command": "npx --yes baro-ai@0.123.0 --headless --cwd /abs/path --local-only 'the goal'"
}
```

The command always has this shape. `--llm`, `--openai-base-url` and `--model`
are added only when you pass them. Every value is shell-quoted.

### `baro_status`

Reads the run's new output, applies it to the board, saves the board and returns
it. It only reads, so it also works in unattended contexts.

| Parameter | Type | Notes |
| --- | --- | --- |
| `run_id` | string, required | As returned by `baro_run`, matching `baro-YYYYMMDDTHHMMSSZ-xxxxxx`. |

Result: `{"ok": true, "board": <board>}`, where the board is:

```json
{
  "schema": 1,
  "run_id": "baro-20261105T142233Z-a1b2c3",
  "goal": "...",
  "repo": "/abs/path",
  "baro_version": "0.123.0",
  "status": "running",
  "started_at": "2026-11-05T14:22:33Z",
  "ended_at": null,
  "stories": [
    {
      "id": "S2",
      "title": "...",
      "state": "blocked",
      "depends_on": ["S1"],
      "waits_on": ["S1"],
      "blocks": ["S3"],
      "blocked_by_failure": false,
      "last_progress": null,
      "error": null
    }
  ],
  "replans": [
    {"index": 1, "at": "...", "added": ["S3"], "removed": ["S2"],
     "edges_changed": [{"from": "S1", "to": "S3"}]}
  ],
  "events_applied": 12,
  "unparsed_lines": 0
}
```

- `status` is one of `planning`, `running`, `completed`, `failed` or `cancelled`.
- A story's `state` is one of `blocked`, `ready`, `running`, `done` or `failed`.
- `waits_on` lists the prerequisites that are not done yet. `blocks` lists the
  direct dependents that are not done yet.
- `blocked_by_failure` is true when a direct or transitive prerequisite failed.
- `replans` records every replan Baro made, with the stories it added and
  removed and the edges it introduced.

If the Baro process exited without reporting the end of the run, the run is
marked `failed`.

### `baro_cancel`

Stops a run and its whole process tree, then marks the board `cancelled`.

| Parameter | Type | Notes |
| --- | --- | --- |
| `run_id` | string, required | As returned by `baro_run`. |

Result: `{"ok": true, "run_id": "...", "status": "cancelled", "already_finished": false}`.

If the run has already ended, nothing is stopped. The tool returns the existing
status with `"already_finished": true`. To stop the tree, the plugin uses
Hermes's `process_manage` kill. On some terminal backends that kill signals only
the root process, so the plugin also sends `pkill -TERM -P <pid>` and
`kill -TERM <pid>` through the gated `terminal` tool, and only while the process
is still running.

## Desktop panel

`desktop/plugin.js` adds a **Baro** page to the Hermes Desktop sidebar, at route
`/baro`. It imports only `@hermes/plugin-sdk` and `react`, and needs no build
step.

- A run picker lists the stored runs, newest first, and selects the newest run
  by default.
- The header shows the run status and a replans counter.
- Columns appear in this order: **Blocked**, **Ready**, **Running**, **Done** and
  **Failed**. Each story card sits in the column that matches its state.
- Each card shows the story title and id, `Waits on: <ids>` and `Blocks: <ids>`,
  with `—` when a list is empty. It also shows a "blocked by failure" badge and
  the latest progress text.
- The panel re-reads the board every 2000 ms while the run is `planning` or
  `running`, and stops polling once the run has ended.

The panel reads boards through the plugin's own read-only backend
(`dashboard/plugin_api.py`, mounted at `/api/plugins/baro/`):
`GET /runs` returns `{"runs": [...]}` and `GET /runs/<run_id>` returns
`{"board": ...}`. Both read the same plugin state that the tools write.

## Disclosures

Read this before you install:

- **Coding agents execute shell commands in the target repo, without asking
  again.** `baro_run`, and `baro_plan` while it runs, start Baro in the `repo`
  you pass. Once you approve the launch, Baro's coding agents edit files and run
  shell commands there (builds, tests, git) on their own, inside the limits in
  [Autonomous by design, guarded by the harness](#autonomous-by-design-guarded-by-the-harness).
  The plugin never approves on your behalf and never turns a guard off.
- **Network calls to configured model providers.** Baro calls the model provider
  you configured for it (or the one you name with `llm` and `openai_base_url`).
  `npx` also downloads the pinned Baro package from the npm registry on first
  use. The plugin itself makes no other network calls and has no telemetry.
- **One long-running background process per run.** Each `baro_run` starts one
  background Baro process, managed by Hermes, which keeps running until the run
  ends or you call `baro_cancel`. Baro starts its own child agent processes
  under it.
- **Stored data.** Boards, run ids and process handles are kept only in the
  plugin's own Hermes plugin state, up to the 50 most recent runs. The plugin
  reads no `.env`, `auth.json` or other credential files.

## Using the Hermes subscription proxy

This section is documentation only. The plugin never defaults a backend, a URL
or a model. Baro uses whatever backend you configured for it, unless you pass
these arguments.

To run Baro on your Hermes subscription, start the Hermes subscription proxy. It
serves an OpenAI-compatible API at `http://127.0.0.1:8645/v1`:

```bash
hermes proxy start
```

Then pass three tool arguments to `baro_plan` or `baro_run`:

```json
{
  "goal": "Add rate limiting to the API",
  "repo": "/abs/path/to/repo",
  "llm": "openai",
  "openai_base_url": "http://127.0.0.1:8645/v1",
  "model": "<portal model>"
}
```

Baro then runs with `--llm openai --openai-base-url http://127.0.0.1:8645/v1
--model <portal model>`. Use a model that your Portal tier serves, as listed by
the proxy's `/v1/models` route. The proxy ignores the API key that the client
sends and attaches your real credential itself. So whatever key Baro's `openai`
backend is set up with stays unused.

## Pinned Baro version

The plugin runs exactly one Baro release:
`BARO_VERSION = "0.123.0"` in `core/command.py`, run as
`npx --yes baro-ai@0.123.0`. There is no `latest` tag, no version range and no
update logic. `--headless` also turns off Baro's own updater and update check,
so a run makes no network call to npm beyond the `npx` download. A new
Baro version reaches users only through a reviewed change to `BARO_VERSION` and
a new plugin release.

## Why dependencies matter

Say the goal is "add a `/health` endpoint and document it". Baro plans:

```
S1  Add the /health route           depends_on: []
S2  Add a test for /health          depends_on: [S1]
S3  Document /health in the README  depends_on: [S1]
```

While S1 runs, S2 and S3 sit in **Blocked** with `Waits on: S1`. S1's card shows
`Blocks: S2, S3`. When S1 is done, both move to **Ready** and can run in
parallel. If S1 fails instead, S2 and S3 stay blocked with the "blocked by
failure" badge, so nobody waits for work that cannot start. If Baro replans and
adds a prerequisite (for example S4 "Add a config flag", now required by S2), S2
moves back to **Blocked** with `Waits on: S4`, and the header's replans counter
goes up. A flat to-do list cannot show any of this.

## Unattended contexts

Under cron jobs and the messaging gateway, nobody can answer an approval prompt.
There, `baro_plan`, `baro_run` and `baro_cancel` fail at once with
`{"ok": false, "error": "unattended_context", ...}` and dispatch nothing. They do
not hang and do not route around approval. `baro_status` stays available
because it only reads stored state and process output. Run a goal from an
interactive Hermes session.

## Development

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest
```

The tests use recorded Baro event lines in `tests/fixtures/` and fake Hermes
bridges. They make no network calls and no model calls.

## Validation

Delete any bytecode caches first, then run the check that catalog CI runs
(`website/docs/developer-guide/plugins/catalog-submission.md` in Hermes Agent):

```bash
find . -name __pycache__ -prune -exec rm -rf {} +
hermes plugins validate .
```

Output with Hermes Agent v0.22.7:

```
✓ manifest — plugin.yaml parses
✓ manifest fields — name, version, description present
✓ requires_hermes — spec '>=0.22.7' parses
✓ config schema — not declared
✓ requires_env — all entries UPPER_SNAKE
✓ oauth — not declared
✓ loadable — entry: __init__.py, desktop/plugin.js
✓ python dependencies — none declared
✓ capability probe — register() ran in isolation
✓ declared tools — matches registrations
✓ declared hooks — matches registrations
✓ declared middleware — matches registrations
✓ built-in tool collisions — no collisions
✓ security scan — safe
✓ no core override — no runtime rebinds of Hermes core
✓ desktop surface — stays inside the plugin SDK surface
◆ Isolation — runs in the plugin host (plugins.isolation: host)
  · dashboard backend API is served through the plugin host (buffered responses)

Validation passed.
```

`catalog/baro.yaml` is a draft catalog entry. Its `repo` and `sha` are
placeholders until the plugin is submitted.

## License

MIT. Copyright (c) 2026 JigJoy. See [LICENSE](LICENSE).
