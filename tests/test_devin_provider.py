from cacli.providers import list_providers
from cacli.providers.devin import DevinProvider


def test_devin_provider_is_registered():
    assert "devin" in list_providers()


def test_build_command_uses_print_mode_and_dangerous_permissions():
    provider = DevinProvider()
    cmd = provider.build_command("fix the tests")

    assert cmd == "devin --permission-mode dangerous --print 'fix the tests'"


def test_build_command_forwards_model_and_config():
    provider = DevinProvider()
    cmd = provider.build_command(
        "summarize",
        model="opus",
        settings="/tmp/devin config.json",
    )

    assert (
        cmd
        == "devin --permission-mode dangerous --model opus "
        "--config '/tmp/devin config.json' --print summarize"
    )


def test_parse_output_returns_plain_stdout():
    provider = DevinProvider()

    result = provider.parse_output("\nHere is the answer.\n")

    assert result.result_message == "Here is the answer."


def test_parse_output_accepts_json_result_shapes():
    provider = DevinProvider()

    result = provider.parse_output('{"type": "result", "result": "Done"}')

    assert result.result_message == "Done"


def test_parse_output_accepts_jsonl_message_shapes():
    provider = DevinProvider()

    result = provider.parse_output(
        '{"type": "status", "message": "Thinking"}\n'
        '{"type": "assistant", "message": {"content": "Done"}}\n'
    )

    assert result.result_message == "Done"
