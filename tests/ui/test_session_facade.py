from miniagent.domain import AgentRunResult, Message, Role, StopReason
from miniagent.repository import SessionRepository
from miniagent.ui.session_facade import RuntimeSession


class CompletingLoop:
    async def run(self, initial_messages, user_message, system_prompt, max_turns, committer, cancellation, run_id):
        result = AgentRunResult(StopReason.COMPLETED, 0)
        await committer.finish_run(run_id, result)
        return result


async def test_start_persists_first_message_and_stop_releases_writer_lock(tmp_path):
    repository = SessionRepository(tmp_path)
    session, accepted = await RuntimeSession.start(
        repository,
        "first message",
        loop_factory=lambda: CompletingLoop(),
    )

    snapshot = await session.snapshot()
    assert snapshot.messages[0].message_id == accepted.message_id
    assert snapshot.messages[0].parts[0].content == "first message"

    session_id = session.session_id
    await session.stop("test")
    reopened = await repository.open_session(session_id)
    await reopened.close()


async def test_submit_returns_ids_allocated_by_the_engine(tmp_path):
    repository = SessionRepository(tmp_path)
    session, _ = await RuntimeSession.start(repository, "first", loop_factory=lambda: CompletingLoop())

    accepted = await session.submit("second")

    assert accepted.message_id != accepted.run_id
    await session.stop("test")

