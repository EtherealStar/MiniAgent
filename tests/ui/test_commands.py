from miniagent.ui.commands import CommandName, complete_command, parse_command


def test_only_an_exact_first_token_is_a_command():
    assert parse_command("/model") is CommandName.MODEL
    assert parse_command("/session later") is CommandName.SESSION
    assert parse_command("/modelish") is None
    assert parse_command("hello /model") is None


def test_completion_is_deterministic():
    assert complete_command("/s") == "/session"
    assert complete_command("/") is None

