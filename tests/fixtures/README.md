# Recorded Baro headless event fixtures

Each `*.jsonl` file is a stdout stream of `baro --headless`: one JSON object per
line. The lines were reconstructed from Baro's own emit sites, not invented.
Every `type` and field name below appears in Baro's source. All paths are
relative to the Baro repository.

## How the lines are serialised

- The orchestrator writes each event as `JSON.stringify({ ts, ...event })`
  (`packages/baro-orchestrator/src/tui-protocol.ts:237-238`). The event shapes
  are the `BaroEvent` union at `packages/baro-orchestrator/src/tui-protocol.ts:88-230`.
- In headless mode the Rust host parses each orchestrator line and echoes it
  to stdout (`crates/baro-tui/src/orchestrator_client.rs:204-217`) through
  `stamped_jsonl`/`serde_json::Value::to_string`
  (`crates/baro-tui/src/events.rs:609-620`). `serde_json` is built without
  `preserve_order` (`crates/baro-tui/Cargo.toml:20`), so keys come out sorted
  and compact. The fixtures use that byte form.
- The host also prints lines of its own:
  - `architect_start` and `architect_complete` (`crates/baro-tui/src/main.rs:2172`, `:2178`);
  - `{"type":"plan_ready","stories":N}`, written by a raw `println!` with no `ts`
    (`crates/baro-tui/src/main.rs:1426`, `:2014`).

## Event sources

| type | shape | emitted at |
|---|---|---|
| `init` | tui-protocol.ts:93, StoryInfo at :19-23 | orchestrate.ts:1906-1918 |
| `gates` | tui-protocol.ts:90 | orchestrate.ts (after init); values copied from execution/gate-registry.ts:61-63 |
| `dag` | tui-protocol.ts:97 | orchestrate.ts:1929 |
| `story_start` | tui-protocol.ts:98 (title = agent id) | execution/forwarders/story-lifecycle.ts:198 |
| `story_log` | tui-protocol.ts:100 | many, e.g. integration/git-coordinator.ts:399 |
| `activity` | tui-protocol.ts:164-174 | planning/application/plan-events.ts:35, verification/verify.ts:1497 |
| `story_retry` | tui-protocol.ts:115 | execution/forwarders/story-lifecycle.ts:206 |
| `story_complete` | tui-protocol.ts:101-107 | execution/forwarders/story-lifecycle.ts:258 |
| `story_error` | tui-protocol.ts:108-114 (terminal; see docs/tui-protocol-v3.md §5) | execution/forwarders/story-lifecycle.ts:237 |
| `progress` | tui-protocol.ts:116 | execution/forwarders/progress.ts:279 |
| `level_started` / `level_completed` | tui-protocol.ts:188-195 | execution/forwarders/dag.ts |
| `replan` | tui-protocol.ts:177-184 | execution/forwarders/dag.ts:134-148 (`source` format `agent:<id>@graph-v<n>` from :118) |
| `done` | tui-protocol.ts:120-131 | orchestrate.ts:2167-2185 |

All paths in this table are under `packages/baro-orchestrator/src/` unless they
start with `docs/`. The replan line is laid out the same way as Baro's own
parser test (`crates/baro-tui/src/events.rs:420-424`).

## Fixtures

- **run_success.jsonl**: a complete successful run. The plan is
  S1 ← {S2, S3}. Each story starts, logs and completes. It ends with `done`
  and `success:true`.
- **run_replan_adds_prereq.jsonl**: S3 is planned with no dependencies, so it is
  *ready*. While S1 runs, S3 has not started. A runtime `replan` then adds S4
  and rewires S3 onto `["S4"]` (`rewired[].depends_on` is S3's full new
  dependency list). S4, then S3, run to completion, and the stream ends with a
  successful `done`.
- **run_failure_blocks_dependents.jsonl**: the plan is S1 ← S2 ← S3, plus an
  independent S4. S1 retries once (`story_retry`) and then fails terminally
  (`story_error`). S2 is its direct dependent and S3 its transitive dependent;
  neither ever starts. S4 completes. `level_completed` reports S2 and S3 as
  blocked, and the stream ends with `done` (`success:false` and an
  `abort_reason`).
- **plan_only.jsonl**: the architect and plan phase, then `init` with dependency
  edges (S2→S1, S3→S1 and S3→S2). The `dag` event follows, then the first
  `story_start` and a `story_log`, which mark the point where execution begins.

Timestamps, story titles, error text and the project name are illustrative
values. The types, field names and nesting are Baro's.
