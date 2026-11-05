# Recorded Baro headless event fixtures

Each `*.jsonl` file is a `baro --headless` stdout stream, one line per event.
Every line was taken from Baro's own repository, in one of two ways:

- **verbatim**: the cited source span contains exactly this event. It is
  either a JSON string the Baro tests feed to a consumer, or the object a Baro
  forwarder test captured from the real emitter's stdout (`captureStdout` →
  `assert.deepEqual(events, [...])`). The only change is that a JSON string
  split across lines in the source is joined into one line, with the
  line-break indentation removed. Keys, values and key order are unchanged.
- **projection**: Baro's emit code applied to Baro test data, both cited. Two
  lines in the whole set are projections, because no Baro test contains them
  as a literal.

No value was made up. Every id, title, project name, error text and number
comes from the cited span.

## How a headless stream is formed (why these shapes)

- The orchestrator writes `JSON.stringify({ ts, ...event })` per line
  (`packages/baro-orchestrator/src/tui-protocol.ts:237-238`). Event shapes are
  the `BaroEvent` union at `packages/baro-orchestrator/src/tui-protocol.ts:88-230`.
- `baro --headless` echoes each orchestrator line to stdout
  (`crates/baro-tui/src/orchestrator_client.rs:204-217`), and prints a few host
  lines of its own, e.g. `plan_ready` (`crates/baro-tui/src/main.rs:1426`).
- `ts` is absent from these fixtures because Baro's capture helper strips the
  wall-clock `ts` from every emitted line before tests assert on it
  (`packages/baro-orchestrator/test/execution/helpers.ts:65-77`, `captureStdout`).
  The v3 contract says the same (`docs/tui-protocol-v3.md`, section 1). The
  parser treats `ts` as optional.

## Per-line provenance

Paths are relative to the Baro repository root. `L` is the fixture line number.

| fixture | L | method | source |
|---|---|---|---|
| run_success.jsonl | 1 | verbatim | packages/baro-dsh/test/delegate-run.test.ts:54 |
| run_success.jsonl | 2 | verbatim | packages/baro-dsh/test/delegate-run.test.ts:55 |
| run_success.jsonl | 3 | verbatim | packages/baro-dsh/test/delegate-run.test.ts:56 |
| run_success.jsonl | 4 | verbatim | packages/baro-dsh/test/delegate-run.test.ts:57 |
| run_success.jsonl | 5 | verbatim | packages/baro-dsh/test/delegate-run.test.ts:58 |
| plan_only.jsonl | 1 | projection | crates/baro-tui/src/main.rs:1426 |
| plan_only.jsonl | 2 | verbatim | crates/baro-tui/src/screens/execute.rs:715-716 |
| plan_only.jsonl | 3 | verbatim | crates/baro-tui/src/screens/execute.rs:718 |
| plan_only.jsonl | 4 | verbatim | crates/baro-tui/src/screens/execute.rs:719 |
| plan_only.jsonl | 5 | verbatim | crates/baro-tui/src/screens/execute.rs:720 |
| plan_only.jsonl | 6 | verbatim | crates/baro-tui/src/screens/execute.rs:721 |
| run_replan_adds_prereq.jsonl | 1 | verbatim | crates/baro-tui/src/app.rs:2253-2254 |
| run_replan_adds_prereq.jsonl | 2 | verbatim | crates/baro-tui/src/app.rs:2258 |
| run_replan_adds_prereq.jsonl | 3 | verbatim | crates/baro-tui/src/app.rs:2363-2365 |
| run_replan_adds_prereq.jsonl | 4 | verbatim | packages/baro-orchestrator/test/execution/forwarders/story-lifecycle.test.ts:73-79 |
| run_replan_adds_prereq.jsonl | 5 | verbatim | packages/baro-orchestrator/test/execution/forwarders/dag.test.ts:73-80 |
| run_failure_blocks_dependents.jsonl | 1 | projection | packages/baro-orchestrator/src/orchestrate.ts:1905-1917; packages/baro-orchestrator/test/execution-mode.test.ts:124-140; packages/baro-orchestrator/test/execution-mode.test.ts:426-431; crates/baro-tui/src/screens/execute.rs:715 |
| run_failure_blocks_dependents.jsonl | 2 | verbatim | packages/baro-orchestrator/test/execution/forwarders/story-lifecycle.test.ts:71 |
| run_failure_blocks_dependents.jsonl | 3 | verbatim | packages/baro-orchestrator/test/execution/forwarders/story-lifecycle.test.ts:72 |
| run_failure_blocks_dependents.jsonl | 4 | verbatim | packages/baro-orchestrator/test/execution/forwarders/story-lifecycle.test.ts:73-79 |
| run_failure_blocks_dependents.jsonl | 5 | verbatim | packages/baro-orchestrator/test/execution/forwarders/story-lifecycle.test.ts:145 |
| run_failure_blocks_dependents.jsonl | 6 | verbatim | packages/baro-orchestrator/test/execution/forwarders/story-lifecycle.test.ts:101-107 |
| run_failure_blocks_dependents.jsonl | 7 | verbatim | packages/baro-orchestrator/test/operator/run-tracker.test.ts:110 |

## The two projections

- **plan_only.jsonl L1:** `println!(r#"{{"type":"plan_ready","stories":{}}}"#, stories.len())`
  (`crates/baro-tui/src/main.rs:1426`), where `stories.len()` = 2, the story
  count of the plan on L2.
- **run_failure_blocks_dependents.jsonl L1:** the `init` emit at
  `packages/baro-orchestrator/src/orchestrate.ts:1905-1917` applied to Baro's
  chained test PRD (`execution-mode.test.ts:426-431`, ids S1→S2→S3→S4, built by
  the `prd()` helper at `:124-140`: `project: "p"`, `title: s.id`). Key order
  follows the emit literal. `mode` and `mode_reason` are `undefined` for that
  PRD, so `JSON.stringify` drops them. `runner: hostname()` uses the runner
  name from Baro's own test stream (`crates/baro-tui/src/screens/execute.rs:715`, `"macbook"`).

## Scenarios

- **run_success.jsonl**: the scripted headless child stream that Baro's
  `DelegateRun` test reads to completion. It holds one story, a non-JSON
  `garbage line`, an `activity` with no story id, the completion, and `done`
  with `success:true`.
- **plan_only.jsonl**: the plan (S2 depends on S1), then the DAG, then the
  first `story_start`, after which execution has begun (`routed`, `activity`).
- **run_replan_adds_prereq.jsonl**: the plan is S1 ← S2. While S1 runs,
  Baro's sentry replan removes S2 and adds S3, which depends on S1 (app.rs
  replan test). S1 then completes, which makes **S3 ready while it has not
  started**. The surgeon replan that Baro's DAG forwarder actually emitted
  then adds S2a and rewires S3 onto `["S2a"]`. S3 is now blocked on a new
  prerequisite. Its `removed:["S2"]` names a story the first replan already
  removed, so consumers must treat removing an unknown id as a no-op. The
  stream ends mid-run, as `baro_status` sees a live run.
- **run_failure_blocks_dependents.jsonl**: the chain is S1 ← S2 ← S3 ← S4.
  The lifecycle forwarder emits that S1 starts, retries once and completes.
  S2 then starts and fails terminally (`story_error`, attempt 3 of 3). S3 is
  its direct dependent and S4 its transitive dependent, and neither starts.
  The run ends with `done`, `success:false` and an `abort_reason`.
