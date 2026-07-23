from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict

from miniagent.domain import ToolExecutionBatch, ToolUsePart
from miniagent.ports import Cancellation
from miniagent.tools.artifacts import MemoryTraceSink
from miniagent.tools.executor import ToolExecutor
from miniagent.tools.models import ExecutionTraits, ResultPolicy, RetryPolicy, ToolExecutionError, ToolProtocolError, ToolSpec
from miniagent.tools.registry import ToolRegistry
from miniagent.trace import TraceEventType


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    value: str


def make_spec(name, handler, *, safe=True, attempts=1, threshold=50 * 1024):
    return ToolSpec(
        name, Input, handler,
        classify=lambda args, targets: ExecutionTraits(safe),
        retry_policy=RetryPolicy(attempts),
        result_policy=ResultPolicy(threshold_bytes=threshold, hard_limit_bytes=50 * 1024),
    )


def call(name="tool", call_id="call", **values):
    body = {"value": "ok", "correction_of_tool_use_id": None, **values}
    return ToolUsePart(name, json.dumps(body), call_id)


def executor(tmp_path, specs, trace=None):
    registry = ToolRegistry(specs)
    registry.freeze()
    return ToolExecutor(registry.enabled_view(), tmp_path, "session", trace_sink=trace or MemoryTraceSink())


def batch(*calls):
    return ToolExecutionBatch(uuid4(), uuid4(), calls)


async def test_validation_failures_do_not_run_handler_and_one_correction_succeeds(tmp_path):
    runs = 0
    async def handler(args, ctx):
        nonlocal runs
        runs += 1
        return args.value

    subject = executor(tmp_path, [make_spec("tool", handler)])
    first = batch(ToolUsePart("tool", '{"value": 1, "correction_of_tool_use_id": null}', "bad"))
    failed = (await subject.submit_batch(first, Cancellation()))[0]
    assert failed.failure.code == "invalid_arguments" and failed.failure.correctable and runs == 0
    fixed = call("tool", "fixed", correction_of_tool_use_id="bad")
    success = (await subject.submit_batch(batch(fixed), Cancellation()))[0]
    assert success.content == "ok" and runs == 1
    second = call("tool", "second", correction_of_tool_use_id="bad")
    rejected = (await subject.submit_batch(batch(second), Cancellation()))[0]
    assert rejected.failure.code == "correction_not_allowed" and runs == 1


async def test_correction_cannot_reference_failure_from_same_batch(tmp_path):
    async def handler(args, ctx): return "unexpected"
    subject = executor(tmp_path, [make_spec("tool", handler)])
    bad = ToolUsePart("tool", '{"value": 1, "correction_of_tool_use_id": null}', "bad")
    fixed = call("tool", "fixed", correction_of_tool_use_id="bad")
    results = await subject.submit_batch(batch(bad, fixed), Cancellation())
    assert results[0].failure.correctable
    assert results[1].failure.code == "correction_not_allowed"


async def test_unknown_malformed_missing_extra_and_duplicate_ids(tmp_path):
    async def handler(args, ctx): return "never"
    subject = executor(tmp_path, [make_spec("tool", handler)])
    calls = (
        ToolUsePart("missing", "{}", "unknown"),
        ToolUsePart("tool", "{", "malformed"),
        ToolUsePart("tool", json.dumps({"correction_of_tool_use_id": None}), "missing-field"),
        ToolUsePart("tool", json.dumps({"value": "x", "extra": 1, "correction_of_tool_use_id": None}), "extra"),
    )
    results = await subject.submit_batch(batch(*calls), Cancellation())
    assert [result.failure.code for result in results] == ["unknown_tool", "malformed_arguments", "invalid_arguments", "invalid_arguments"]
    with pytest.raises(ToolProtocolError, match="重复"):
        await subject.submit_batch(batch(call(call_id="unknown")), Cancellation())


async def test_transient_retries_but_ordinary_failure_does_not(tmp_path):
    transient_runs = ordinary_runs = 0
    async def transient(args, ctx):
        nonlocal transient_runs
        transient_runs += 1
        if transient_runs < 3:
            raise ToolExecutionError("later", transient=True)
        return "ok"
    async def ordinary(args, ctx):
        nonlocal ordinary_runs
        ordinary_runs += 1
        raise ToolExecutionError("bad")
    trace = MemoryTraceSink()
    subject = executor(tmp_path, [make_spec("retry", transient, attempts=3), make_spec("ordinary", ordinary, attempts=3)], trace)
    results = await subject.submit_batch(batch(call("retry", "r"), call("ordinary", "o")), Cancellation())
    assert results[0].content == "ok" and results[0].attempts == 3 and transient_runs == 3
    assert results[1].is_error and results[1].attempts == 1 and ordinary_runs == 1
    assert len([event for event in trace.events if event.event_type is TraceEventType.RETRY_SCHEDULED]) == 2


async def test_safe_segments_overlap_but_barrier_preserves_order(tmp_path):
    active = 0
    overlap = False
    timeline = []
    async def safe(args, ctx):
        nonlocal active, overlap
        active += 1
        overlap |= active > 1
        timeline.append(f"start-{ctx.tool_use_id}")
        await asyncio.sleep(0.02)
        timeline.append(f"end-{ctx.tool_use_id}")
        active -= 1
        return ctx.tool_use_id
    async def unsafe(args, ctx):
        assert active == 0
        timeline.append("barrier")
        return "barrier"
    specs = [make_spec("safe", safe), make_spec("unsafe", unsafe, safe=False)]
    subject = executor(tmp_path, specs)
    results = await subject.submit_batch(batch(call("safe", "a"), call("safe", "b"), call("unsafe", "c"), call("safe", "d")), Cancellation())
    assert overlap and timeline.index("barrier") > timeline.index("end-b")
    assert [result.tool_use_id for result in results] == ["a", "b", "c", "d"]


async def test_timeout_cancellation_and_large_result(tmp_path):
    async def slow(args, ctx):
        await asyncio.sleep(1)
        return "late"
    async def large(args, ctx):
        return "x" * 101
    timed = make_spec("timed", slow)
    timed = ToolSpec(**{**{field: getattr(timed, field) for field in timed.__dataclass_fields__ if field != "function_schema"}, "timeout_seconds": 0.01})
    subject = executor(tmp_path, [timed, make_spec("large", large, threshold=100)])
    results = await subject.submit_batch(batch(call("timed", "t"), call("large", "l")), Cancellation())
    assert results[0].failure.code == "timeout"
    assert results[1].artifact is not None
    assert (tmp_path / results[1].artifact.path).read_text() == "x" * 101

    cancelled = Cancellation(); cancelled.cancel()
    cancelled_trace = MemoryTraceSink()
    subject2 = executor(tmp_path, [make_spec("safe", slow), make_spec("unsafe", slow, safe=False)], cancelled_trace)
    results = await subject2.submit_batch(batch(call("safe", "cs"), call("unsafe", "cu")), cancelled)
    assert [result.failure.code for result in results] == ["cancelled", "outcome_unknown"]
    starts = [event for event in cancelled_trace.events if event.event_type is TraceEventType.SPAN_STARTED]
    finishes = [event for event in cancelled_trace.events if event.event_type is TraceEventType.SPAN_FINISHED]
    assert len(starts) == len(finishes) == 2


async def test_running_cancellation_stops_later_barrier(tmp_path):
    started = asyncio.Event()
    barrier_runs = 0
    async def running(args, ctx):
        started.set()
        while True:
            ctx.cancellation.raise_if_cancelled()
            await asyncio.sleep(0)
    async def barrier(args, ctx):
        nonlocal barrier_runs
        barrier_runs += 1
        return "unexpected"
    cancellation = Cancellation()
    subject = executor(tmp_path, [make_spec("safe", running), make_spec("unsafe", barrier, safe=False)])
    task = asyncio.create_task(subject.submit_batch(batch(call("safe", "a"), call("unsafe", "b")), cancellation))
    await started.wait()
    cancellation.cancel()
    results = await task
    assert [result.failure.code for result in results] == ["cancelled", "outcome_unknown"]
    assert barrier_runs == 0


async def test_cancellation_during_retry_delay_stops_next_attempt(tmp_path):
    attempts = 0
    retry_started = asyncio.Event()
    async def transient(args, ctx):
        nonlocal attempts
        attempts += 1
        retry_started.set()
        raise ToolExecutionError("later", transient=True)
    spec = make_spec("retry", transient, attempts=3)
    spec = ToolSpec(
        name=spec.name, input_model=spec.input_model, handler=spec.handler,
        classify=spec.classify, retry_policy=RetryPolicy(3, retry_delay_seconds=1),
        result_policy=spec.result_policy,
    )
    cancellation = Cancellation()
    subject = executor(tmp_path, [spec])
    task = asyncio.create_task(subject.submit_batch(batch(call("retry", "r")), cancellation))
    await retry_started.wait()
    cancellation.cancel()
    result = (await task)[0]
    assert result.failure.code == "cancelled" and attempts == 1
