from cacli.harnesses.cursor import CursorHarness


def test_resolve_model_passes_through_any_model():
    provider = CursorHarness()
    assert provider.resolve_model("composer-2.5") == "composer-2.5"
    assert provider.resolve_model("future-model") == "future-model"


def test_build_command_includes_composer_2_5():
    provider = CursorHarness()
    cmd = provider.build_command("hello", model="composer-2.5")
    assert "--model composer-2.5" in cmd


def test_build_command_maps_sandbox_without_changing_force():
    enabled = CursorHarness().build_command("hello", sandbox=True)
    disabled = CursorHarness().build_command("hello", sandbox=False)
    default = CursorHarness().build_command("hello")
    assert "--force" in enabled
    assert "--sandbox enabled" in enabled
    assert "--sandbox disabled" in disabled
    assert "--sandbox" not in default
