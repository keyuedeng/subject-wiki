import pytest

from app.llm.cache import CallResult, LlmCache, request_key


def test_key_is_order_insensitive_and_content_sensitive():
    a = request_key({"model": "m", "input": "x", "params": {"effort": "low", "n": 1}})
    b = request_key({"params": {"n": 1, "effort": "low"}, "input": "x", "model": "m"})
    assert a == b and len(a) == 64
    assert a != request_key({"model": "m", "input": "y", "params": {"effort": "low", "n": 1}})
    assert a != request_key({"model": "m2", "input": "x", "params": {"effort": "low", "n": 1}})


def test_get_or_call_pays_once(session_factory):
    cache = LlmCache(session_factory)
    calls = []

    def call():
        calls.append(1)
        return CallResult({"text": "cleaned"}, input_tokens=100, output_tokens=20, cost_usd=0.001)

    fp = {"provider": "openai", "model": "m", "instructions": "clean", "input": "um, so"}
    first, hit1 = cache.get_or_call(fp, kind="transcript_clean", prompt_version="v1", call=call)
    second, hit2 = cache.get_or_call(fp, kind="transcript_clean", prompt_version="v1", call=call)
    assert (hit1, hit2) == (False, True) and len(calls) == 1
    assert second.response == {"text": "cleaned"} and second.cost_usd == 0.001


def test_embeddings_use_the_same_cache(session_factory):
    cache = LlmCache(session_factory)
    fp = {"provider": "openai", "model": "embed-m", "input": "Dijkstra's algorithm"}
    vec = [0.1, 0.2, 0.3]
    cache.get_or_call(fp, kind="embedding", prompt_version=None,
                      call=lambda: CallResult({"embedding": vec}, input_tokens=5))  # fmt: skip
    hit, cached = cache.get_or_call(fp, kind="embedding", prompt_version=None,
                                    call=lambda: pytest.fail("should be cached"))  # fmt: skip
    assert cached and hit.response == {"embedding": vec}


def test_failed_call_stores_nothing(session_factory):
    cache = LlmCache(session_factory)
    fp = {"provider": "openai", "model": "m", "input": "x"}

    def boom():
        raise RuntimeError("API down")

    with pytest.raises(RuntimeError):
        cache.get_or_call(fp, kind="k", prompt_version=None, call=boom)
    assert cache.get_many([request_key(fp)]) == {}
