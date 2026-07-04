import re

from cacli.providers import list_providers
from cacli.providers.devin import DevinProvider


def test_devin_provider_is_registered():
    assert "devin" in list_providers()


def test_build_devin_command_defaults_to_sandboxed_print_mode():
    provider = DevinProvider()
    cmd = provider.build_devin_command("fix the tests")

    assert cmd == "devin --sandbox --permission-mode dangerous --print 'fix the tests'"


def test_build_devin_command_sandbox_can_be_disabled():
    provider = DevinProvider()
    cmd = provider.build_devin_command("fix the tests", sandbox=False)

    assert cmd == "devin --permission-mode dangerous --print 'fix the tests'"


def test_build_devin_command_forwards_model_and_config():
    provider = DevinProvider()
    cmd = provider.build_devin_command(
        "summarize",
        model="opus",
        settings="/tmp/devin config.json",
        sandbox=False,
    )

    assert (
        cmd
        == "devin --permission-mode dangerous --model opus "
        "--config '/tmp/devin config.json' --print summarize"
    )


def test_build_command_wraps_devin_with_stream_converter():
    provider = DevinProvider()
    cmd = provider.build_command("fix the tests")

    assert re.search(
        r"devin-stream --export \S*cacli-devin-[0-9a-f]{12}\.json"
        r" -- devin --sandbox --permission-mode dangerous --print 'fix the tests'$",
        cmd,
    )


def test_build_command_forwards_no_sandbox():
    provider = DevinProvider()
    cmd = provider.build_command("fix the tests", sandbox=False)

    assert " -- devin --permission-mode dangerous --print 'fix the tests'" in cmd
    assert "--sandbox" not in cmd.split(" -- ")[1]


def test_build_pr_description_command_is_unwrapped():
    provider = DevinProvider()
    cmd = provider.build_pr_description_command("describe changes")

    assert (
        cmd
        == "devin --sandbox --permission-mode dangerous --print 'describe changes'"
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


def test_parse_output_reads_stream_json_result_line():
    provider = DevinProvider()

    result = provider.parse_output(
        '{"type": "system", "subtype": "init", "provider": "devin"}\n'
        '{"type": "assistant", "message": {"role": "assistant", "content": '
        '[{"type": "text", "text": "Working on it"}]}}\n'
        '{"type": "result", "subtype": "success", "is_error": false, '
        '"result": "All done"}\n'
    )

    assert result.result_message == "All done"
