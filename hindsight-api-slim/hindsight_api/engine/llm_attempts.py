"""Bounded, payload-free provider attempts attached to one logical LLM trace.

The wrapper owns a fresh sink per call. Providers opt in without adding trace
rows, changing token accounting, or sharing mutable state between concurrent
calls. Unknown upstream work/usage is not inferred from a successful retry.
"""

from contextvars import ContextVar, Token
from dataclasses import dataclass, field, replace
from typing import Literal


@dataclass
class ProviderAttempt:
    attempt: int
    outcome: Literal["queued", "response", "http_error", "transport_error", "parse_error", "error", "cancelled"] = (
        "queued"
    )
    queue_wait_ms: int = 0
    request_ms: int | None = None
    elapsed_ms: int = 0
    backoff_ms: int = 0
    planned_backoff_ms: int = 0
    http_status: int | None = None
    error_type: str | None = None
    request_id: str | None = None


@dataclass(frozen=True)
class ProviderAttemptSummary:
    total: int
    omitted: int
    records: list[ProviderAttempt]


@dataclass
class ProviderAttemptLog:
    records: list[ProviderAttempt] = field(default_factory=list)
    total: int = 0

    def start(self, attempt: int) -> ProviderAttempt:
        self.total += 1
        record = ProviderAttempt(attempt=attempt)
        # Even a caller-configured huge retry count must not make tracing unbounded.
        if len(self.records) < 32:
            self.records.append(record)
        return record

    def summary(self) -> ProviderAttemptSummary:
        return ProviderAttemptSummary(
            total=self.total,
            omitted=self.total - len(self.records),
            records=[replace(record) for record in self.records],
        )


_attempt_log: ContextVar[ProviderAttemptLog | None] = ContextVar("hindsight_provider_attempts", default=None)


def bind_provider_attempts() -> Token:
    return _attempt_log.set(ProviderAttemptLog())


def reset_provider_attempts(token: Token) -> None:
    _attempt_log.reset(token)


def current_provider_attempts() -> ProviderAttemptLog | None:
    return _attempt_log.get()
