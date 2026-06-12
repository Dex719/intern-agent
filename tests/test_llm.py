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
