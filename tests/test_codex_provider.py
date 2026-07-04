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


def test_pr_description_command_uses_default_sandbox():
    provider = CodexProvider()
    cmd = provider.build_pr_description_command("describe changes")

    assert "--sandbox workspace-write" in cmd
