"""Event parser and run-id tests.

Every Baro event input comes from the recorded fixtures via ``load_fixture``:
either a fixture line verbatim, or a fixture event with fields removed,
retyped or truncated to model malformed/older streams.
"""

import copy
import inspect
import json
import random
import re
import typing
from datetime import datetime, timedelta, timezone

import pytest

from baro_plugin.core import events
from baro_plugin.core.events import BaroEvent, LineBuffer, PlannedStory, parse_line
from baro_plugin.core.ids import RUN_ID_RE, new_run_id

FIXTURES = [
    "run_success.jsonl",
    "run_replan_adds_prereq.jsonl",
    "run_failure_blocks_dependents.jsonl",
    "plan_only.jsonl",
]
MAPPED_KINDS = {
    "plan",
    "story_start",
    "story_progress",
    "story_complete",
    "story_failed",
    "replan",
    "run_end",
}


def kinds(lines):
    return [e.kind for e in map(parse_line, lines) if e is not None]


def mapped(lines):
    return [e for e in map(parse_line, lines) if e is not None and e.kind != "unknown"]


def fixture_line(load_fixture, name, baro_type):
    """The first raw line of ``name`` whose Baro ``type`` is ``baro_type``."""
    for line in load_fixture(name):
        if json.loads(line).get("type") == baro_type:
            return line
    raise AssertionError(f"{name} has no {baro_type} line")


def fixture_event(load_fixture, name, baro_type):
    """A mutable copy of that fixture event."""
    return copy.deepcopy(json.loads(fixture_line(load_fixture, name, baro_type)))


def dumps(obj):
    return json.dumps(obj, separators=(",", ":"))


def mutated(load_fixture, name, baro_type, **changes):
    """Fixture event with fields replaced; a value of ``...`` deletes the field."""
    obj = fixture_event(load_fixture, name, baro_type)
    for key, value in changes.items():
        if value is ...:
            obj.pop(key, None)
        else:
            obj[key] = value
    return dumps(obj)


# --- _TYPE_MAP -------------------------------------------------------------


def test_type_map_covers_every_kind_and_cites_baro_source():
    assert {spec["kind"] for spec in events._TYPE_MAP.values()} == MAPPED_KINDS
    assert set(typing.get_args(events.EventKind)) == MAPPED_KINDS | {"unknown"}
    source = inspect.getsource(events)
    block = source[source.index("_TYPE_MAP: dict") : source.index("\n}\n")]
    for baro_type in events._TYPE_MAP:
        key_at = block.index(f'"{baro_type}": {{')
        preceding_comment = block[:key_at].rsplit("}", 1)[-1]
        assert "packages/baro-orchestrator/src/" in preceding_comment, baro_type
        assert re.search(r"\.ts:\d+", preceding_comment), baro_type


# --- fixtures through parse_line --------------------------------------------


@pytest.mark.parametrize("name", FIXTURES)
def test_every_fixture_line_is_a_json_object_that_parses(load_fixture, name):
    lines = load_fixture(name)
    assert lines
    for line in lines:
        event = parse_line(line)
        assert isinstance(event, BaroEvent), line
        assert isinstance(event.raw, dict)


def test_fixtures_exercise_every_mapped_kind(load_fixture):
    seen = set()
    for name in FIXTURES:
        seen.update(kinds(load_fixture(name)))
    assert MAPPED_KINDS <= seen


def test_run_success(load_fixture):
    evs = mapped(load_fixture("run_success.jsonl"))
    assert [e.kind for e in evs] == [
        "plan",
        "story_start",
        "story_progress",
        "story_progress",
        "story_complete",
        "story_start",
        "story_start",
        "story_progress",
        "story_complete",
        "story_complete",
        "run_end",
    ]
    plan = evs[0]
    assert plan.stories == (
        PlannedStory("S1", "Add Todo model", ()),
        PlannedStory("S2", "Add REST endpoints", ("S1",)),
        PlannedStory("S3", "Add model tests", ("S1",)),
    )
    assert plan.ts == "2026-11-05T14:22:03.111Z"
    assert evs[1].story_id == "S1"
    assert evs[2].story_id == "S1" and evs[2].message == "Creating src/models/todo.ts"
    assert evs[3].message == "src/models/todo.ts"  # activity text
    assert evs[4].story_id == "S1"
    end = evs[-1]
    assert (end.outcome, end.error) == ("completed", None)


def test_replan_adds_prerequisite(load_fixture):
    evs = mapped(load_fixture("run_replan_adds_prereq.jsonl"))
    plan = evs[0]
    assert plan.kind == "plan"
    s3 = next(s for s in plan.stories if s.id == "S3")
    assert s3.depends_on == ()  # S3 starts out ready
    replan = next(e for e in evs if e.kind == "replan")
    started_before = {e.story_id for e in evs[: evs.index(replan)] if e.kind == "story_start"}
    assert "S3" not in started_before
    assert replan.stories == (
        PlannedStory("S4", "Add full-text index migration", ()),
        PlannedStory("S3", "", ("S4",)),  # rewired: title not restated
    )
    assert replan.removed == ()
    assert replan.message == "search requires a full-text index migration first"
    assert replan.ts == "2026-11-05T14:22:07.259Z"
    assert evs[-1].kind == "run_end" and evs[-1].outcome == "completed"


def test_failure_blocks_dependents(load_fixture):
    lines = load_fixture("run_failure_blocks_dependents.jsonl")
    evs = mapped(lines)
    plan = evs[0]
    deps = {s.id: s.depends_on for s in plan.stories}
    assert deps == {"S1": (), "S2": ("S1",), "S3": ("S2",), "S4": ()}
    failed = [e for e in evs if e.kind == "story_failed"]
    assert len(failed) == 1
    assert failed[0].story_id == "S1"
    assert failed[0].error == "tsc: src/models/todo.ts(12,5): error TS2322"
    started = {e.story_id for e in evs if e.kind == "story_start"}
    assert started == {"S1", "S4"}  # dependents never start
    end = evs[-1]
    assert end.kind == "run_end"
    assert (end.outcome, end.error) == ("failed", "1 story failed: S1")
    # story_retry is not a terminal state: it stays unknown
    retry = parse_line(fixture_line(load_fixture, "run_failure_blocks_dependents.jsonl", "story_retry"))
    assert retry.kind == "unknown"


def test_plan_only_has_plan_with_edges_then_story_start(load_fixture):
    lines = load_fixture("plan_only.jsonl")
    ks = kinds(lines)
    assert ks.index("plan") < ks.index("story_start")
    plan = mapped(lines)[0]
    assert plan.kind == "plan"
    assert {(d, s.id) for s in plan.stories for d in s.depends_on} == {
        ("S1", "S2"),
        ("S1", "S3"),
        ("S2", "S3"),
    }
    # the host-printed plan_ready line has no ts: unknown, never None
    plan_ready = parse_line(fixture_line(load_fixture, "plan_only.jsonl", "plan_ready"))
    assert plan_ready.kind == "unknown" and plan_ready.ts is None


# --- LineBuffer --------------------------------------------------------------


@pytest.mark.parametrize("name", FIXTURES)
@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_linebuffer_reassembles_arbitrary_chunk_splits(load_fixture, name, seed):
    lines = load_fixture(name)
    text = "\n".join(lines) + "\n"
    rng = random.Random(seed)
    buf = LineBuffer()
    out = []
    i = 0
    while i < len(text):
        n = rng.randint(1, 97)
        out.extend(buf.feed(text[i : i + n]))
        i += n
    assert out == lines
    assert buf.flush() == []
    assert [parse_line(l) for l in out] == [parse_line(l) for l in lines]


def test_linebuffer_holds_back_partial_line_until_completed(load_fixture):
    lines = load_fixture("run_success.jsonl")
    first, second, third = lines[6], lines[7], lines[8]
    buf = LineBuffer()
    assert buf.feed(first[:10]) == []
    assert buf.feed(first[10:] + "\n" + second[:5]) == [first]
    assert buf.feed("") == []
    assert buf.feed(second[5:] + "\r\n") == [second]  # CRLF tolerated
    assert buf.feed(third) == []  # stream ended without a newline
    assert buf.flush() == [third]
    assert buf.flush() == []
    assert parse_line(third).kind == "story_progress"


def test_linebuffer_one_byte_at_a_time(load_fixture):
    lines = load_fixture("plan_only.jsonl")[:3]
    text = lines[0] + "\n\n" + lines[1] + "\n" + lines[2] + "\n"
    buf = LineBuffer()
    out = []
    for ch in text:
        out.extend(buf.feed(ch))
    assert out == [lines[0], "", lines[1], lines[2]]
    assert parse_line("") is None


# --- parse_line edge cases -----------------------------------------------------


def test_blank_non_json_and_non_object_lines_return_none(load_fixture):
    lines = load_fixture("run_success.jsonl")
    init = fixture_event(load_fixture, "run_success.jsonl", "init")
    candidates = [
        "",
        "   ",
        "\r",
        *(line[: len(line) // 2] for line in lines),  # clipped mid-line
        "[orchestrator] " + lines[0],  # non-JSON prefix
        dumps(init["stories"]),  # JSON array
        dumps(init["protocol"]),  # JSON number
        dumps(init["project"]),  # JSON string
        "null",
    ]
    for line in candidates:
        assert parse_line(line) is None, line


UNKNOWN_CASES = {
    "gates": lambda lf: fixture_line(lf, "run_success.jsonl", "gates"),
    "dag": lambda lf: fixture_line(lf, "run_success.jsonl", "dag"),
    "progress": lambda lf: fixture_line(lf, "run_success.jsonl", "progress"),
    "level_completed": lambda lf: fixture_line(lf, "run_failure_blocks_dependents.jsonl", "level_completed"),
    "architect_start": lambda lf: fixture_line(lf, "plan_only.jsonl", "architect_start"),
    "no type": lambda lf: mutated(lf, "run_success.jsonl", "story_start", type=...),
    "numeric type": lambda lf: mutated(lf, "run_success.jsonl", "story_start", type=42),
    "null type": lambda lf: mutated(lf, "run_success.jsonl", "story_start", type=None),
    "story_start without id": lambda lf: mutated(lf, "run_success.jsonl", "story_start", id=...),
    "story_complete numeric id": lambda lf: mutated(lf, "run_success.jsonl", "story_complete", id=7),
    "story_error empty id": lambda lf: mutated(lf, "run_failure_blocks_dependents.jsonl", "story_error", id=""),
}


@pytest.mark.parametrize("case", sorted(UNKNOWN_CASES))
def test_unmapped_or_malformed_objects_are_unknown(load_fixture, case):
    event = parse_line(UNKNOWN_CASES[case](load_fixture))
    assert event is not None and event.kind == "unknown"


def test_tolerates_missing_and_wrongly_typed_optional_fields(load_fixture):
    run = "run_success.jsonl"
    assert parse_line(mutated(load_fixture, run, "init", stories=...)).stories == ()
    init = fixture_event(load_fixture, run, "init")
    s1, s2 = init["stories"][0], init["stories"][1]
    plan = parse_line(
        mutated(
            load_fixture,
            run,
            "init",
            stories=[
                {"id": s1["id"]},  # title and depends_on missing
                "S9",  # not an object
                {"title": s1["title"]},  # no id
                {**s2, "title": 5, "depends_on": [*s2["depends_on"], 3, ""]},
            ],
        )
    )
    assert plan.kind == "plan"
    assert plan.stories == (PlannedStory("S1", "", ()), PlannedStory("S2", "", ("S1",)))

    rp = "run_replan_adds_prereq.jsonl"
    minimal = parse_line(dumps({"type": fixture_event(load_fixture, rp, "replan")["type"]}))
    assert (minimal.kind, minimal.stories, minimal.removed) == ("replan", (), ())
    removed = parse_line(mutated(load_fixture, rp, "replan", added=None, rewired=..., removed=["S3"]))
    assert (removed.stories, removed.removed) == ((), ("S3",))

    log = parse_line(mutated(load_fixture, run, "story_log", line=None, ts=5))
    assert (log.kind, log.story_id, log.message, log.ts) == ("story_progress", "S1", None, None)


DONE_CASES = [
    ("run_success.jsonl", {}, "completed", None),
    ("run_failure_blocks_dependents.jsonl", {}, "failed", "1 story failed: S1"),
    ("run_failure_blocks_dependents.jsonl", {"abort_reason": ...}, "failed", None),
    ("run_success.jsonl", {"success": ...}, "completed", None),  # pre-v3 stream
    ("run_failure_blocks_dependents.jsonl", {"success": ...}, "failed", "1 story failed: S1"),
]


@pytest.mark.parametrize("name,changes,outcome,error", DONE_CASES)
def test_run_end_outcome(load_fixture, name, changes, outcome, error):
    event = parse_line(mutated(load_fixture, name, "done", **changes))
    assert (event.kind, event.outcome, event.error) == ("run_end", outcome, error)


def test_parse_line_never_raises_on_hostile_input(load_fixture):
    rng = random.Random(7)
    samples = [None, 3, b"{}", object(), "\x00", "{" * 5000, "[" * 100000]
    samples.append(mutated(load_fixture, "run_success.jsonl", "init", stories={}))
    for name in FIXTURES:
        for line in load_fixture(name):
            samples.extend(line[:cut] for cut in range(len(line)))  # every truncation
            chars = list(line)
            for _ in range(5):  # random single-character corruption
                i = rng.randrange(len(chars))
                corrupt = chars[:i] + [rng.choice('{}[]":,0an')] + chars[i + 1 :]
                samples.append("".join(corrupt))
    for sample in samples:
        result = parse_line(sample)
        assert result is None or isinstance(result, BaroEvent)


# --- run ids --------------------------------------------------------------------


def test_new_run_id_matches_run_id_re():
    for _ in range(50):
        assert re.fullmatch(RUN_ID_RE, new_run_id())


def test_new_run_id_is_deterministic_with_injected_clock_and_rand():
    now = datetime(2026, 11, 5, 14, 22, 33, tzinfo=timezone.utc)
    assert new_run_id(now, lambda: "a1b2c3") == "baro-20261105T142233Z-a1b2c3"
    naive = datetime(2026, 11, 5, 14, 22, 33)
    assert new_run_id(naive, lambda: "a1b2c3") == "baro-20261105T142233Z-a1b2c3"
    cet = now.astimezone(timezone(timedelta(hours=1)))
    assert new_run_id(cet, lambda: "a1b2c3") == "baro-20261105T142233Z-a1b2c3"


@pytest.mark.parametrize("bad", ["A1B2C3", "a1b2c", "a1b2c3d", "zzzzzz", ""])
def test_new_run_id_rejects_bad_suffix(bad):
    with pytest.raises(ValueError):
        new_run_id(rand=lambda: bad)


@pytest.mark.parametrize(
    "candidate",
    ["baro-20261105T142233Z-A1B2C3", "baro-20261105T142233-a1b2c3", "x-baro-20261105T142233Z-a1b2c3",
     "baro-20261105T142233Z-a1b2c3\n/etc", "../baro-20261105T142233Z-a1b2c3"],
)
def test_run_id_re_rejects_malformed(candidate):
    assert re.fullmatch(RUN_ID_RE, candidate) is None
