from cacli.providers.codex import CodexProvider


def test_build_command_defaults_to_workspace_write_sandbox():
    provider = CodexProvider()
    cmd = provider.build_command("fix the tests")

    assert "--sandbox workspace-write" in cmd
    assert "--config sandbox_workspace_write.network_access=true" in cmd
    assert "danger-full-access" not in cmd


def test_build_command_no_sandbox_uses_danger_full_access():
    provider = CodexProvider()
    cmd = provider.build_command("fix the tests", sandbox=False)

    assert "--sandbox danger-full-access" in cmd
    assert "workspace-write" not in cmd


def test_build_command_keeps_model_and_effort_defaults():
    provider = CodexProvider()
    cmd = provider.build_command("fix the tests")

    assert "--model gpt-5.4" in cmd
    assert "--config model_reasoning_effort=high" in cmd


def test_build_command_forwards_reasoning_effort_verbatim():
    provider = CodexProvider()

    max_cmd = provider.build_command(
        "solve it", model="gpt-5.6-sol", reasoning_effort="max"
    )
    future_cmd = provider.build_command(
        "solve it", model="future-model", reasoning_effort="future-effort"
    )

    assert "--model gpt-5.6-sol" in max_cmd
    assert "--config model_reasoning_effort=max" in max_cmd
    assert "model_reasoning_effort=xhigh" not in max_cmd
    assert "--model future-model" in future_cmd
    assert "--config model_reasoning_effort=future-effort" in future_cmd


def test_pr_description_command_uses_default_sandbox():
    provider = CodexProvider()
    cmd = provider.build_pr_description_command("describe changes")

    assert "--sandbox workspace-write" in cmd


def test_build_command_forwards_output_schema_path(tmp_path):
    path = tmp_path / "schema.json"
    path.write_text('{"type":"object"}')
    cmd = CodexProvider().build_command("answer", output_schema=path)
    assert f"--output-schema {path}" in cmd
