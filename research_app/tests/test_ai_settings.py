import research_app.ai_settings as settings


def _use_path(monkeypatch, tmp_path):
    path = tmp_path / "ai-settings.json"
    monkeypatch.setattr(settings, "settings_path", lambda: path)
    return path


def test_default_profile_uses_opencode_url(monkeypatch, tmp_path):
    _use_path(monkeypatch, tmp_path)
    profile = settings.default_profile()
    assert profile["base_url"] == "https://opencode.ai/zen/go/v1"
    assert profile["model"] == "glm-5.3-flash"


def test_record_and_list_profiles_updates_recent_and_model(monkeypatch, tmp_path):
    _use_path(monkeypatch, tmp_path)
    settings.record_ai_profile("https://opencode.ai/zen/go/v1", "glm-5.3-flash")
    settings.record_ai_profile("https://api.openai.com/v1", "gpt-4o-mini")
    settings.record_ai_profile("https://opencode.ai/zen/go/v1/", "glm-5.3")

    profiles = settings.list_ai_profiles()

    assert [profile["base_url"] for profile in profiles] == [
        "https://opencode.ai/zen/go/v1",
        "https://api.openai.com/v1",
    ]
    assert profiles[0]["model"] == "glm-5.3"
    assert settings.default_profile()["model"] == "glm-5.3"


def test_invalid_settings_file_is_ignored(monkeypatch, tmp_path):
    path = _use_path(monkeypatch, tmp_path)
    path.write_text("not json", encoding="utf-8")
    assert settings.list_ai_profiles() == []
