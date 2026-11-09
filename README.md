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

When Hermes uses Baro, Baro does the coding. Every failure other than
`invalid_argument` and `unknown_run`, and the status of a run that failed, was
cancelled or is still running, carries an `instruction` field that tells the
model so: do not write, edit or test code in the repository itself, report the
outcome and wait for the user.

### `baro_plan`

Asks Baro for a plan and returns it as stories with dependency edges. It never
executes a story. Its description tells the model to use it only when the user
wants to see a plan without running it: `baro_run` plans as part of the run, and
the plan appears on the Baro board.

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
  "baro_version": "0.130.0",
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
arrives within 360 seconds, the process is stopped and the tool returns
`plan_timeout`. That stays under Hermes's own deadline for one tool call
(`timeouts.tools.sequential_call`, 420 seconds by default).

### `baro_run`

Starts a Baro run in the background and returns at once. It does not wait for
any output. The process is started with `notify_on_complete`, so Hermes tells the
model when Baro exits; the result's `instruction` asks the model not to poll or
sleep in a loop meanwhile, while the user watches the Baro board. The goal is
passed on as the user stated it: the description asks the model not to add
methods or requirements of its own.

Parameters: the same as `baro_plan`.

Result:

```json
{
  "ok": true,
  "run_id": "baro-20261105T142233Z-a1b2c3",
  "status": "running",
  "baro_version": "0.130.0",
  "command": "npx --yes baro-ai@0.130.0 --headless --cwd /abs/path --local-only 'the goal'"
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
  "baro_version": "0.130.0",
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

## What the Desktop board shows

Besides the kanban columns, the board shows how many agents are working, how
many stories are done and how long the run has taken. While Baro is still
planning, before any story exists, it shows what the planner is on. Each card
shows its agent's model and latest step; click a card for that agent's steps
(the commands it ran, what it said, the files it changed, its tests and the
Critic's verdict). All of it is read from the run's mirrored event stream
(`GET /api/plugins/baro/runs/<id>/activity`, core/activity.py) and never
stored.

A **Team** strip shows what the agents tell each other while they work: Baro
runs them as concurrent participants on Mozaik, so a finding one agent
publishes ("server.mjs uses 4-space indent, no semicolons") or a request for
help reaches the others that are working at that moment, not when they next
start. It also lists the plan changing under them (stories added or rewired)
and a story pausing until the stories it needs are done. Cards count the notes
each agent sent and received.

From any page, a chip in the status bar shows the newest live run ("Baro · 3
working · 2/5 done", or "planning") and opens the board; ⌘K → "Baro: Open
board" does the same. To watch a run beside the chat, open the board in a split
next to the conversation.

## Disclosures

Read this before you install:

- **Coding agents execute shell commands in the target repo, without asking
  again.** `baro_run`, and `baro_plan` while it runs, start Baro in the `repo`
  you pass. Once you approve the launch, Baro's coding agents edit files and run
  shell commands there (builds, tests, git) on their own, inside the limits in
  [Autonomous by design, guarded by the harness](#autonomous-by-design-guarded-by-the-harness).
  The plugin never approves on your behalf and never turns a guard off.
- **Network calls to configured model providers.** Baro calls the model provider
  you configured for it (or the one you name with `llm` and `openai_base_url`,
  or the Hermes subscription proxy when that setting is on). `npx` also
  downloads the pinned Baro package from the npm registry on first use. The
  plugin itself makes one other kind of call: before it starts Baro against a
  base URL on this machine, it requests `<base URL>/models` there to check that
  something answers. It calls nothing off this machine and has no telemetry.
- **One long-running background process per run.** Each `baro_run` starts one
  background Baro process, managed by Hermes, which keeps running until the run
  ends or you call `baro_cancel`. Baro starts its own child agent processes
  under it.
- **Stored data.** Boards, run ids and process handles are kept only in the
  plugin's own Hermes plugin state, up to the 50 most recent runs. `baro_run`
  also pipes Baro's event stream through `tee` into
  `events/<run id>.jsonl` in the plugin's own data directory, so the Desktop
  board can follow the run live. The plugin reads no `.env`, `auth.json` or
  other credential files.

## Using the Hermes subscription proxy

Out of the box the plugin picks no backend, URL or model: Baro uses whatever
backend you configured for it.

To run Baro on your Hermes subscription, start the Hermes subscription proxy. It
serves an OpenAI-compatible API at `http://127.0.0.1:8645/v1`:

```bash
hermes proxy start
```

Then turn on **Settings → Plugins → Baro → Use my Hermes subscription** and pick
the model your tier serves (`nous/welcome` by default). Every `baro_run` and
`baro_plan` without a backend of its own then runs Baro on the proxy, so a
request can be as plain as "use Baro to add a CLI to my notes API". If nothing
answers at the proxy URL, the tool says to start the proxy instead of starting
Baro.

The same works per request, setting or not, with three tool arguments:

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
sends and attaches your real credential itself, so Baro needs no
`OPENAI_API_KEY` for it: for a base URL on this machine (`localhost`,
`127.0.0.1`, `::1`) Baro sends a placeholder, and any key you have set stays
unused.

`model` names every Baro phase, the Critic and the Surgeon included. Tested
with Baro 0.130.0 on the free `nous/welcome` tier, with no OpenAI key in the
environment: a small goal ran through planning, its stories, the Critic's
reviews and the final test run.

## Pinned Baro version

The plugin runs exactly one Baro release:
`BARO_VERSION = "0.130.0"` in `core/command.py`, run as
`npx --yes baro-ai@0.130.0`. There is no `latest` tag, no version range and no
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
