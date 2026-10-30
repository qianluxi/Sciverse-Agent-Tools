from research_app import ai_translation


class FakeClient:
    model = "test-model"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, messages, *, max_tokens=2048):
        self.calls.append((messages, max_tokens))
        return self.responses.pop(0)


def test_needs_translation_skips_chinese_titles():
    assert ai_translation.needs_translation("Shear wall behavior") is True
    assert ai_translation.needs_translation("剪力墙抗震性能") is False
    assert ai_translation.needs_translation("") is False


def test_translate_titles_parses_json_and_preserves_batch_indices():
    client = FakeClient(
        [
            '{"translations":[{"index":1,"title":"剪力墙性能"},'
            '{"index":2,"title":"钢板剪力墙"}]}'
        ]
    )

    result = ai_translation.translate_titles(
        client,
        ["Shear wall performance", "Steel plate shear wall"],
    )

    assert result == {
        "Shear wall performance": "剪力墙性能",
        "Steel plate shear wall": "钢板剪力墙",
    }
    assert client.calls[0][1] == 4096


def test_translation_cache_is_model_scoped(monkeypatch, tmp_path):
    monkeypatch.setenv("SCIVERSE_TRANSLATION_CACHE", str(tmp_path / "translations.json"))
    ai_translation.save_translations({"Shear wall": "剪力墙"}, "model-a")

    assert ai_translation.cached_translations(["Shear wall"], "model-a") == {
        "Shear wall": "剪力墙"
    }
    assert ai_translation.cached_translations(["Shear wall"], "model-b") == {}


def test_translate_abstracts_parses_single_translation():
    client = FakeClient(['{"translation":"这是一个翻译后的摘要。"}'])

    result = ai_translation.translate_abstracts(client, ["This is an abstract."])

    assert result == {"This is an abstract.": "这是一个翻译后的摘要。"}
    assert client.calls[0][1] == 4096


def test_abstract_translation_cache_is_model_scoped(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "SCIVERSE_ABSTRACT_TRANSLATION_CACHE",
        str(tmp_path / "abstracts.json"),
    )
    ai_translation.save_abstract_translations(
        {"Abstract text": "摘要译文"},
        "model-a",
    )

    assert ai_translation.cached_abstract_translations(
        ["Abstract text"], "model-a"
    ) == {"Abstract text": "摘要译文"}
    assert ai_translation.cached_abstract_translations(
        ["Abstract text"], "model-b"
    ) == {}
