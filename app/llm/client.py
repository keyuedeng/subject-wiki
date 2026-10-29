"""The model provider interface used by every phase, and cached helpers on top of it.

- `OpenAIProvider` makes raw calls: structured outputs (Responses API) and embeddings.
  The SDK client is created on the first real call, so cache lookups need no API key.
- `LlmClient` wraps a provider with the request-hash cache (app/llm/cache.py): callers
  describe a request, and only requests never made before are paid for.
- Streaming: `structured(..., on_text=...)` passes the raw JSON text to `on_text` as it
  arrives, when the provider supports it (`structured_stream`). A cache hit returns at
  once without calling `on_text`; the stored result is the same either way.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from app.llm.cache import CallResult, LlmCache, request_key
from app.parse.pricing import cost_usd

EMBED_BATCH = 96  # inputs per embeddings request


class ProviderError(Exception):
    pass


class OutputFiltered(ProviderError):
    """The provider's content filter stopped the output part-way (e.g. the model began
    reproducing a copyrighted comic). Retrying gives the same result, so callers treat it
    like a refusal."""

    def __init__(self, usage: "Usage"):
        super().__init__("Model response incomplete (content_filter)")
        self.usage = usage


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0  # includes reasoning tokens
    reasoning_tokens: int = 0


@dataclass(frozen=True)
class StructuredResponse:
    parsed: BaseModel | None  # None when the model refused
    usage: Usage
    refusal: str | None = None


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str | None, timeout: float = 180):
        self._api_key = api_key
        self._timeout = timeout
        self._client = None

    def client(self):
        if self._client is None:
            if not self._api_key:
                raise ProviderError("OPENAI_API_KEY is not set")
            from openai import OpenAI

            self._client = OpenAI(api_key=self._api_key, max_retries=4, timeout=self._timeout)
        return self._client

    def structured(
        self,
        *,
        model: str,
        instructions: str,
        content: list[dict],
        schema: type[BaseModel],
        effort: str,
        max_output_tokens: int,
        cache_hint: str | None = None,
    ) -> StructuredResponse:
        """One structured-output call. `content` is the user message's content parts.
        The status is checked before the output is parsed: the SDK would otherwise fail on
        cut-off JSON with a validation error that hides why it was cut off."""
        raw = self.client().responses.with_raw_response.parse(
            **_request(model, instructions, content, schema, effort, max_output_tokens, cache_hint)
        )
        body = raw.http_response.json()
        if body.get("status") == "incomplete":
            reason = (body.get("incomplete_details") or {}).get("reason") or "unknown"
            _raise_incomplete(reason, _usage_from_dict(body.get("usage") or {}))
        return _structured_result(raw.parse())

    def structured_stream(
        self,
        *,
        model: str,
        instructions: str,
        content: list[dict],
        schema: type[BaseModel],
        effort: str,
        max_output_tokens: int,
        on_text: Callable[[str], None],
        cache_hint: str | None = None,
    ) -> StructuredResponse:
        """Like `structured`, passing each piece of the output's JSON text to `on_text`."""
        request = _request(model, instructions, content, schema, effort, max_output_tokens,
                           cache_hint)  # fmt: skip
        with self.client().responses.stream(**request) as stream:
            for event in stream:
                if event.type == "response.output_text.delta":
                    on_text(event.delta)
                elif event.type == "response.incomplete":
                    r = event.response
                    reason = getattr(r.incomplete_details, "reason", None) or "unknown"
                    _raise_incomplete(reason, _usage(r.usage))
            response = stream.get_final_response()
        return _structured_result(response)

    def embed(self, *, model: str, texts: list[str], dimensions: int) -> tuple[list, int]:
        """Vectors in input order, and the input tokens used."""
        response = self.client().embeddings.create(model=model, input=texts, dimensions=dimensions)
        vectors = [d.embedding for d in sorted(response.data, key=lambda d: d.index)]
        return vectors, response.usage.prompt_tokens if response.usage else 0


def _request(model, instructions, content, schema, effort, max_output_tokens, cache_hint) -> dict:
    return {
        "model": model,
        "instructions": instructions,
        "input": [{"role": "user", "content": content}],
        "text_format": schema,
        "reasoning": {"effort": effort},
        "max_output_tokens": max_output_tokens,
        "prompt_cache_key": cache_hint,  # routes requests sharing a prefix to one cache
        "store": False,
    }


def _usage(u) -> Usage:
    return Usage(
        input_tokens=u.input_tokens if u else 0,
        cached_tokens=u.input_tokens_details.cached_tokens if u else 0,
        output_tokens=u.output_tokens if u else 0,
        reasoning_tokens=u.output_tokens_details.reasoning_tokens if u else 0,
    )


def _usage_from_dict(u: dict) -> Usage:
    return Usage(
        input_tokens=u.get("input_tokens") or 0,
        cached_tokens=(u.get("input_tokens_details") or {}).get("cached_tokens") or 0,
        output_tokens=u.get("output_tokens") or 0,
        reasoning_tokens=(u.get("output_tokens_details") or {}).get("reasoning_tokens") or 0,
    )


def _raise_incomplete(reason: str, usage: Usage):
    if reason == "content_filter":
        raise OutputFiltered(usage)
    raise ProviderError(f"Model response incomplete ({reason})")


def _structured_result(response) -> StructuredResponse:
    usage = _usage(response.usage)
    if response.status == "incomplete":
        reason = getattr(response.incomplete_details, "reason", None) or "unknown"
        _raise_incomplete(reason, usage)
    for item in response.output or []:
        for part in getattr(item, "content", None) or []:
            if getattr(part, "type", None) == "refusal":
                return StructuredResponse(None, usage, part.refusal or "refused")
    if response.output_parsed is None:
        raise ProviderError("Model returned no parseable output")
    return StructuredResponse(response.output_parsed, usage)


@dataclass(frozen=True)
class StructuredRequest:
    key: str
    kind: str
    prompt_version: str
    model: str
    instructions: str
    input_text: str
    schema: type[BaseModel]
    effort: str
    max_output_tokens: int


@dataclass
class Cached:
    """A cached structured result: parsed JSON plus what it cost when it was made."""

    response: dict
    call: CallResult
    cached: bool
    request_hash: str


class LlmClient:
    def __init__(self, provider_factory: Callable[[], Any], cache: LlmCache):
        self._provider_factory = provider_factory
        self._provider = None
        self.cache = cache

    @property
    def provider(self):
        if self._provider is None:
            self._provider = self._provider_factory()
        return self._provider

    def structured_request(
        self,
        *,
        kind: str,
        prompt_version: str,
        model: str,
        instructions: str,
        input_text: str,
        schema: type[BaseModel],
        effort: str,
        max_output_tokens: int,
    ) -> "StructuredRequest":
        """Describe a text-only structured call; its key is the hash of the request."""
        fingerprint = {
            "provider": self.provider.name,
            "api": "responses.parse",
            "model": model,
            "instructions": instructions,
            "input_text": input_text,
            "output_schema": schema.model_json_schema(),
            "reasoning_effort": effort,
            "max_output_tokens": max_output_tokens,
        }
        return StructuredRequest(
            key=request_key(fingerprint), kind=kind, prompt_version=prompt_version, model=model,
            instructions=instructions, input_text=input_text, schema=schema, effort=effort,
            max_output_tokens=max_output_tokens,
        )  # fmt: skip

    def call(
        self, req: "StructuredRequest", on_text: Callable[[str], None] | None = None
    ) -> CallResult:
        """Make the call (no cache, no database: safe to run in worker threads).
        Refusals raise ProviderError and are never cached, so a later run can retry.
        With `on_text`, the output streams in when the provider can stream."""
        kwargs = dict(
            model=req.model,
            instructions=req.instructions,
            content=[{"type": "input_text", "text": req.input_text}],
            schema=req.schema,
            effort=req.effort,
            max_output_tokens=req.max_output_tokens,
            cache_hint=req.prompt_version,
        )
        stream = getattr(self.provider, "structured_stream", None)
        if on_text is not None and stream is not None:
            result = stream(on_text=on_text, **kwargs)
        else:
            result = self.provider.structured(**kwargs)
        if result.parsed is None:
            raise ProviderError(f"Model refused: {result.refusal}")
        u = result.usage
        return CallResult(
            response=result.parsed.model_dump(mode="json"),
            input_tokens=u.input_tokens,
            cached_input_tokens=u.cached_tokens,
            output_tokens=u.output_tokens,
            cost_usd=cost_usd(req.model, u.input_tokens, u.cached_tokens, u.output_tokens),
            prompt_version=req.prompt_version,
        )

    def store(self, req: "StructuredRequest", result: CallResult) -> None:
        self.cache.put(
            req.key, result, kind=req.kind, provider=self.provider.name, model=req.model,
            prompt_version=req.prompt_version,
        )  # fmt: skip

    def structured(self, on_text: Callable[[str], None] | None = None, **kwargs) -> Cached:
        """Lookup-or-call in one step (single-threaded callers). `on_text` receives the
        output's JSON text as it streams in (not called on a cache hit)."""
        req = self.structured_request(**kwargs)
        if hit := self.cache.get_many([req.key]).get(req.key):
            return Cached(hit.response, hit, True, req.key)
        result = self.call(req, on_text)
        self.store(req, result)
        return Cached(result.response, result, False, req.key)

    def embed(self, *, model: str, dimensions: int, texts: list[str]) -> tuple[list, float, int]:
        """Vectors for `texts`, each cached on its own. Returns (vectors, cost of this
        run, number of texts that needed an API call)."""
        name = self.provider.name
        keys = [
            request_key(
                {"provider": name, "api": "embeddings", "model": model,
                 "dimensions": dimensions, "input": t}
            )
            for t in texts
        ]  # fmt: skip
        hits = self.cache.get_many(keys)
        missing = sorted({i for i, k in enumerate(keys) if k not in hits}, key=lambda i: i)
        # Identical texts share one call.
        first_of: dict[str, int] = {}
        todo = [i for i in missing if first_of.setdefault(keys[i], i) == i]
        spent = 0.0
        for start in range(0, len(todo), EMBED_BATCH):
            batch = todo[start : start + EMBED_BATCH]
            vectors, tokens = self.provider.embed(
                model=model, texts=[texts[i] for i in batch], dimensions=dimensions
            )
            batch_cost = cost_usd(model, tokens, 0, 0) or 0.0
            spent += batch_cost
            for i, vec in zip(batch, vectors, strict=True):
                share = tokens // max(1, len(batch))
                call = CallResult(
                    {"embedding": vec}, input_tokens=share,
                    cost_usd=batch_cost / max(1, len(batch)),
                )  # fmt: skip
                self.cache.put(
                    keys[i], call, kind="embedding", provider=name, model=model,
                    prompt_version=None,
                )  # fmt: skip
                hits[keys[i]] = call
        return [hits[k].response["embedding"] for k in keys], spent, len(todo)
