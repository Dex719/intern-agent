import pytest

from intern_agent import llm


def test_build_prompt_contains_inputs():
    prompt = llm.build_prompt("РЕЗЮМЕ ТЕСТ", "ВАКАНСИЯ ТЕСТ")
    assert "РЕЗЮМЕ ТЕСТ" in prompt
    assert "ВАКАНСИЯ ТЕСТ" in prompt
    assert "match_score" in prompt


def _payload(text):
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def test_parse_response_plain_json():
    result = llm.parse_response(_payload('{"match_score": 70, "verdict": "ок"}'))
    assert result["match_score"] == 70


def test_parse_response_fenced_json():
    result = llm.parse_response(_payload('```json\n{"match_score": 55}\n```'))
    assert result["match_score"] == 55


def test_parse_response_clamps_score():
    assert llm.parse_response(_payload('{"match_score": 250}'))["match_score"] == 100
    assert llm.parse_response(_payload('{"match_score": -5}'))["match_score"] == 0


def test_parse_response_empty():
    with pytest.raises(llm.LLMError):
        llm.parse_response({})


def test_parse_response_blocked():
    with pytest.raises(llm.LLMError, match="SAFETY"):
        llm.parse_response({"promptFeedback": {"blockReason": "SAFETY"}})


def test_parse_response_bad_json():
    with pytest.raises(llm.LLMError):
        llm.parse_response(_payload("это не json"))


def test_resolve_config_providers(monkeypatch):
    from intern_agent import config

    monkeypatch.setattr(config, "GEMINI_API_KEY", "env-key")
    cfg = llm.resolve_config({})
    assert cfg == {"provider": "gemini", "api_key": "env-key", "model": config.GEMINI_MODEL}

    cfg = llm.resolve_config(
        {"llm_provider": "openrouter", "llm_api_key": "or-key", "llm_model": "qwen/qwen3"}
    )
    assert cfg == {"provider": "openrouter", "api_key": "or-key", "model": "qwen/qwen3"}
    # свой ключ не подменяется env-ключом для других провайдеров
    assert llm.resolve_config({"llm_provider": "openai"})["api_key"] == ""


def test_screen_accepts_openai_object_wrapper():
    items = llm._validate_screen({"items": [{"id": 5, "score": 55, "reason": "ок"}]})
    assert items == [{"id": "5", "score": 55, "reason": "ок"}]


def test_resolve_config_new_providers():
    for provider, model in [
        ("anthropic", "claude-sonnet-4-5"),
        ("groq", "llama-3.3-70b-versatile"),
        ("deepseek", "deepseek-chat"),
        ("mistral", "mistral-small-latest"),
    ]:
        cfg = llm.resolve_config({"llm_provider": provider, "llm_api_key": "k"})
        assert cfg["provider"] == provider
        assert cfg["model"] == model


def test_resolve_config_unknown_provider_falls_back():
    cfg = llm.resolve_config({"llm_provider": "nope", "llm_api_key": "k"})
    assert cfg["provider"] == "gemini"


# ---------- скорость и устойчивость Gemini ----------


def test_gemini_body_disables_thinking_for_25_flash():
    body = llm.gemini_body("привет", "gemini-2.5-flash", None, 0.3)
    assert body["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}
    body = llm.gemini_body("привет", "gemini-2.5-flash-lite", llm.RESPONSE_SCHEMA, 0.3)
    assert body["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}
    assert body["generationConfig"]["responseSchema"] == llm.RESPONSE_SCHEMA


def test_gemini_body_keeps_thinking_for_other_models():
    for model in ("gemini-2.0-flash", "gemini-2.5-pro"):
        body = llm.gemini_body("привет", model, None, 0.3)
        assert "thinkingConfig" not in body["generationConfig"]


@pytest.mark.anyio
async def test_gemini_fallback_model_used_on_error(monkeypatch):
    calls = []

    async def fake_post(url, *, headers, params, body):
        calls.append(url)
        if "gemini-2.5-flash" in url:
            raise llm.LLMError("Gemini перегружен")
        return {"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}

    monkeypatch.setattr(llm, "_post_with_retry", fake_post)
    monkeypatch.setattr(llm.config, "GEMINI_FALLBACK_MODEL", "gemini-2.0-flash")
    payload = await llm._call_gemini("привет", "key", "gemini-2.5-flash", None, 0.3)
    assert "candidates" in payload
    assert len(calls) == 2
    assert "gemini-2.0-flash" in calls[1]


@pytest.mark.anyio
async def test_gemini_no_fallback_when_disabled(monkeypatch):
    async def fake_post(url, *, headers, params, body):
        raise llm.LLMError("Gemini перегружен")

    monkeypatch.setattr(llm, "_post_with_retry", fake_post)
    monkeypatch.setattr(llm.config, "GEMINI_FALLBACK_MODEL", "")
    with pytest.raises(llm.LLMError):
        await llm._call_gemini("привет", "key", "gemini-2.5-flash", None, 0.3)
