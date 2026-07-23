import asyncio
import json
from uuid import uuid4

from miniagent.trace import (
    BestEffortTraceSink,
    JsonlTraceSink,
    TraceContext,
    TraceEvent,
    TraceEventType,
)


def event(index=0):
    return TraceEvent(
        TraceEventType.SPAN_STARTED,
        TraceContext(uuid4(), uuid4(), None, uuid4(), uuid4()),
        {"name": "agent.run", "index": index},
    )


async def test_jsonl_sink_writes_monotonic_sequences_and_rotates(tmp_path):
    sink = JsonlTraceSink(tmp_path, max_file_bytes=450, queue_capacity=8)
    for index in range(4):
        await sink.emit(event(index))
    await sink.close()

    files = sorted(tmp_path.glob("*.jsonl"))
    records = [json.loads(line) for path in files for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(files) >= 2
    assert [record["trace_sequence"] for record in records] == [1, 2, 3, 4]
    assert all(record["trace_schema_version"] == 1 for record in records)


async def test_queue_full_drops_without_waiting(tmp_path):
    gate = asyncio.Event()
    sink = JsonlTraceSink(tmp_path, queue_capacity=1, writer_gate=gate)
    await sink.emit(event(1))
    await sink.emit(event(2))
    assert sink.dropped_count == 1
    gate.set()
    await sink.close()


async def test_best_effort_wrapper_swallows_sink_failure():
    class BrokenSink:
        async def emit(self, value):
            raise OSError("blocked")

        async def close(self, drain_timeout=1.0):
            raise OSError("blocked")

    sink = BestEffortTraceSink(BrokenSink())
    await sink.emit(event())
    await sink.close()
    assert sink.failed_count == 2
