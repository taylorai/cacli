from cacli.providers.cursor import CursorProvider


def test_resolve_model_passes_through_any_model():
    provider = CursorProvider()
    assert provider.resolve_model("composer-2.5") == "composer-2.5"
    assert provider.resolve_model("future-model") == "future-model"


def test_build_command_includes_composer_2_5():
    provider = CursorProvider()
    cmd = provider.build_command("hello", model="composer-2.5")
    assert "--model composer-2.5" in cmd
