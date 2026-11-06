"""Opt-in input fitting for a self-hosted Cohere-compatible reranker.

Use that server's llama.cpp-compatible tokenizer, not Hindsight's fact tokenizer.
Only ranking copies are shortened; queries and stored source text stay intact.
"""

import asyncio
import math
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, StrictInt, StrictStr

from .aiohttp_session import LoopLocalSession, per_phase_timeout, raise_for_status


class RerankInputBudgetError(ValueError):
    """An input cannot be safely fitted to the configured server budget."""


class _TokenizedText(BaseModel):
    tokens: list[StrictInt]


class _DetokenizedText(BaseModel):
    content: StrictStr


class ServerTokenizerBudget:
    """Fit query/document pairs with the same tokenizer used by the serving model."""

    def __init__(
        self,
        *,
        rerank_url: str | None,
        tokenizer_url: str,
        api_key: str,
        max_input_tokens: int,
        token_overhead: int = 128,
        timeout: float = 30.0,
    ) -> None:
        if max_input_tokens <= 0 or token_overhead < 0 or token_overhead >= max_input_tokens:
            raise RerankInputBudgetError("Reranker input budget must exceed its nonnegative template reserve")
        if not math.isfinite(timeout) or timeout <= 0:
            raise RerankInputBudgetError("Tokenizer timeout must be finite and positive")
        rerank = urlsplit(rerank_url or "")
        tokenizer = urlsplit(tokenizer_url)
        # Reuse the existing credential only at the operator's same-origin endpoint.
        # Disallow userinfo, query strings and redirects instead of forwarding secrets.
        if (
            rerank.scheme not in {"http", "https"}
            or tokenizer.scheme not in {"http", "https"}
            or not rerank.hostname
            or not tokenizer.hostname
            or rerank.username is not None
            or tokenizer.username is not None
            or tokenizer.query
            or tokenizer.fragment
            or (rerank.scheme, rerank.hostname, rerank.port) != (tokenizer.scheme, tokenizer.hostname, tokenizer.port)
            or not tokenizer.path.endswith("/tokenize")
        ):
            raise RerankInputBudgetError("Tokenizer must be a same-origin /tokenize endpoint of a custom reranker")
        self.tokenizer_url = tokenizer_url
        self.detokenizer_url = urlunsplit(
            tokenizer._replace(path=tokenizer.path.removesuffix("tokenize") + "detokenize")
        )
        self.max_input_tokens = max_input_tokens
        self.token_overhead = token_overhead
        self.timeout = min(timeout, 30.0)
        self._session = LoopLocalSession(
            timeout=per_phase_timeout(self.timeout),
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def _tokenize(self, text: str) -> list[int]:
        async with self._session.get().post(
            self.tokenizer_url, json={"content": text, "add_special": False}, allow_redirects=False
        ) as response:
            if 300 <= response.status < 400:
                raise RerankInputBudgetError("Tokenizer redirects are not supported")
            await raise_for_status(response)
            tokens = _TokenizedText.model_validate(await response.json(content_type=None)).tokens
            if any(token < 0 for token in tokens) or (text and not tokens):
                raise RerankInputBudgetError("Tokenizer returned invalid or empty token IDs")
            return tokens

    async def _detokenize(self, tokens: list[int]) -> str:
        async with self._session.get().post(
            self.detokenizer_url, json={"tokens": tokens}, allow_redirects=False
        ) as response:
            if 300 <= response.status < 400:
                raise RerankInputBudgetError("Tokenizer redirects are not supported")
            await raise_for_status(response)
            return _DetokenizedText.model_validate(await response.json(content_type=None)).content

    async def fit(self, query: str, documents: list[str]) -> list[str]:
        """Keep one ranking pool, fitting each copy under query + template + document."""
        # One wall budget for all tokenization/detokenization, not N request timeouts.
        async with asyncio.timeout(self.timeout):
            query_tokens = await self._tokenize(query)
            available = self.max_input_tokens - self.token_overhead - len(query_tokens)
            if available <= 0:
                raise RerankInputBudgetError("Reranker query leaves no document budget; query was not truncated")
            semaphore = asyncio.Semaphore(8)  # Per-call, on this loop; no process-global asyncio primitive.

            async def fit_document(document: str) -> str:
                async with semaphore:
                    tokens = await self._tokenize(document)
                    if document and not tokens:
                        raise RerankInputBudgetError("Tokenizer returned no tokens for a nonempty document")
                    if len(tokens) <= available:
                        return document
                    # A token prefix can end halfway through a UTF-8 character. Back
                    # off a bounded suffix rather than introducing replacement text.
                    for tail in range(min(8, available)):
                        fitted = await self._detokenize(tokens[: available - tail])
                        if fitted and document.startswith(fitted) and len(await self._tokenize(fitted)) <= available:
                            return fitted
                    raise RerankInputBudgetError("Tokenizer could not produce a verified nonempty document prefix")

            tasks = [asyncio.create_task(fit_document(document)) for document in documents]
            try:
                return await asyncio.gather(*tasks)
            finally:
                # gather does not cancel siblings when one fails. No orphan tokenizer
                # calls may continue after this fit failed or its caller was cancelled.
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
