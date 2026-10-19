"""Cache for every paid model call: LLM completions and embeddings.

A call is identified by a hash of its *request*: everything that determines the output
(provider, model, full prompt text, schema, parameters, input text, sha256 of any image).
Identical requests are never paid for twice. Any change to a request misses the cache,
but only for that request, so an edit that touches some pages re-runs only those pages.
An edit to text shared by every request (a system prompt) still re-runs all of them.

`prompt_version` is stored alongside for reporting; it is not part of the key.

Usage:
    fingerprint = {"provider": "openai", "model": m, "instructions": p, "input": text, ...}
    result, hit = cache.get_or_call(fingerprint, kind="transcript_clean",
                                    prompt_version="clean-v1", call=lambda: do_call())
"""

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import LlmCall


def image_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def request_key(fingerprint: dict[str, Any]) -> str:
    """sha256 of the canonical JSON of the request. Pass image *hashes*, not bytes."""
    canonical = json.dumps(
        fingerprint, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class CallResult:
    response: Any  # JSON-serialisable: parsed output, text, or {"embedding": [...]}
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0  # includes reasoning tokens
    cost_usd: float | None = None
    prompt_version: str | None = None  # filled in when read back from the cache


class LlmCache:
    def __init__(self, session_factory: Callable[[], Session]):
        self.session_factory = session_factory

    def get_many(self, keys: Iterable[str]) -> dict[str, CallResult]:
        keys = list(set(keys))
        if not keys:
            return {}
        with self.session_factory() as db:
            rows = db.scalars(select(LlmCall).where(LlmCall.request_hash.in_(keys))).all()
            return {row.request_hash: _result(row) for row in rows}

    def put(
        self,
        key: str,
        result: CallResult,
        *,
        kind: str,
        provider: str,
        model: str,
        prompt_version: str | None,
    ) -> None:
        with self.session_factory() as db:
            db.merge(
                LlmCall(
                    request_hash=key,
                    kind=kind,
                    provider=provider,
                    model=model,
                    prompt_version=prompt_version,
                    response_json=result.response,
                    input_tokens=result.input_tokens,
                    cached_input_tokens=result.cached_input_tokens,
                    output_tokens=result.output_tokens,
                    cost_usd=result.cost_usd,
                )
            )
            db.commit()

    def get_or_call(
        self,
        fingerprint: dict[str, Any],
        *,
        kind: str,
        prompt_version: str | None,
        call: Callable[[], CallResult],
    ) -> tuple[CallResult, bool]:
        """The cached result for this exact request, or make the call and store it.
        Returns (result, was_cached). Failed calls raise and store nothing."""
        key = request_key(fingerprint)
        if hit := self.get_many([key]).get(key):
            return hit, True
        result = call()
        self.put(
            key,
            result,
            kind=kind,
            provider=str(fingerprint.get("provider", "")),
            model=str(fingerprint.get("model", "")),
            prompt_version=prompt_version,
        )
        return result, False


def _result(row: LlmCall) -> CallResult:
    return CallResult(
        response=row.response_json,
        input_tokens=row.input_tokens,
        cached_input_tokens=row.cached_input_tokens,
        output_tokens=row.output_tokens,
        cost_usd=row.cost_usd,
        prompt_version=row.prompt_version,
    )
