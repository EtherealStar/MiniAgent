from uuid import uuid4

from miniagent.trace import TraceContext, TraceEvent, TraceEventType, sanitize_error


def test_trace_event_contains_only_explicit_metadata():
    context = TraceContext(uuid4(), uuid4(), None, uuid4(), uuid4())
    event = TraceEvent(TraceEventType.SPAN_STARTED, context, {"name": "agent.run"})
    assert event.payload == {"name": "agent.run"}
    assert "prompt" not in event.payload


def test_sanitize_error_redacts_secrets_controls_and_limits_message():
    error = ValueError("Authorization: Bearer abc123\napi_key=secret " + "x" * 700)
    safe = sanitize_error(error)
    assert safe["type"] == "ValueError"
    assert "abc123" not in safe["message"] and "secret" not in safe["message"]
    assert "\n" not in safe["message"]
    assert len(safe["message"]) <= 512
