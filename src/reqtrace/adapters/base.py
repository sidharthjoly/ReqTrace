"""Adapter contract.

Adding a vendor means writing one of these plus tests — nothing else changes.
Vendor isolation is a hard rule: one adapter raising must never break the run
for the others, so `run_board` converts any failure into a snapshot carrying
`error` and `complete=False`.
"""

from __future__ import annotations

from typing import Protocol

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from ..models import BoardSnapshot


class Adapter(Protocol):
    vendor: str

    def parse(self, payload, token: str) -> BoardSnapshot:
        """Turn a raw vendor payload into a snapshot. Kept separate from fetch
        so every adapter can be tested — and replayed — offline against a
        recorded response."""
        ...

    async def fetch(self, client: httpx.AsyncClient, token: str) -> BoardSnapshot: ...


class BoardIncomplete(Exception):
    """The vendor answered, but the board we got back is not the whole board."""


def _is_retryable(exc: BaseException) -> bool:
    """5xx and transport failures only. A 404 is a settled answer — the token is
    wrong — and retrying it with backoff just makes a run slow for no information."""
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500 or exc.response.status_code == 429
    return False


polite_retry = retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)


async def run_board(adapter: Adapter, client: httpx.AsyncClient, token: str) -> BoardSnapshot:
    """Fetch one board, never raising. A failed board is a failed board, not a
    failed run."""
    try:
        return await adapter.fetch(client, token)
    except Exception as exc:  # noqa: BLE001 - deliberate vendor isolation boundary
        return BoardSnapshot(
            ats_vendor=adapter.vendor, board_token=token,
            complete=False, error=f"{type(exc).__name__}: {exc}"[:300],
        )
