PROMPT = """Purpose:
Replace the current session's in-memory todo list to track progress on a multi-step task.

Use when:
- The current task has several meaningful steps or needs explicit progress tracking.

Rules:
- Submit the complete desired list on every call; an empty list clears it.
- Keep item ids stable when updating the same task.
- Use at most one `in_progress` item and preserve the intended display order.

Returns:
- A summary and the complete structured todo list used by the session UI.
"""
