"""HTTP client for Lichess game exports."""

import io
from contextlib import contextmanager
from collections.abc import Generator, Sequence
from http.client import HTTPException
from typing import TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


class LichessAPIError(RuntimeError):
    """Raised when a Lichess API request fails."""


SUPPORTED_PERF_TYPES = ("bullet", "blitz", "rapid")


class LichessClient:
    """Stream a user's games from the public Lichess API."""

    def __init__(self, timeout: float = 30.0) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        self.timeout = timeout

    @contextmanager
    def stream_user_games(
        self,
        username: str,
        *,
        max_games: int = 100,
        since: int | None = None,
        until: int | None = None,
        perf_types: Sequence[str] | None = None,
    ) -> Generator[TextIO, None, None]:
        """Open a bounded PGN export stream for a Lichess user."""
        normalized_username = username.strip()
        if not normalized_username:
            raise ValueError("username must not be empty")
        if isinstance(max_games, bool) or not isinstance(max_games, int) or max_games <= 0:
            raise ValueError("max_games must be a positive integer")
        if since is not None and (isinstance(since, bool) or not isinstance(since, int) or since < 0):
            raise ValueError("since must be a non-negative Unix timestamp in milliseconds")
        if until is not None and (isinstance(until, bool) or not isinstance(until, int) or until < 0):
            raise ValueError("until must be a non-negative Unix timestamp in milliseconds")
        if perf_types is not None and (
            not perf_types
            or any(not isinstance(perf_type, str) for perf_type in perf_types)
            or len(set(perf_types)) != len(perf_types)
            or any(perf_type not in SUPPORTED_PERF_TYPES for perf_type in perf_types)
        ):
            raise ValueError(
                f"perf_types must contain unique values from {', '.join(SUPPORTED_PERF_TYPES)}"
            )

        query = [("max", str(max_games))]
        if since is not None:
            query.append(("since", str(since)))
        if until is not None:
            query.append(("until", str(until)))
        if perf_types is not None and set(perf_types) != set(SUPPORTED_PERF_TYPES):
            query.extend(("perfType[]", perf_type) for perf_type in perf_types)
        url = (
            f"https://lichess.org/api/games/user/{quote(normalized_username, safe='')}"
            f"?{urlencode(query)}"
        )
        request = Request(
            url,
            headers={
                "Accept": "application/x-chess-pgn",
                "User-Agent": "RepertoireApp/0.1",
            },
        )

        try:
            with urlopen(request, timeout=self.timeout) as response:
                with io.TextIOWrapper(response, encoding="utf-8") as stream:
                    yield stream
        except HTTPError as error:
            raise LichessAPIError(
                f"Lichess API returned HTTP {error.code} for user {normalized_username!r}"
            ) from error
        except (URLError, TimeoutError, HTTPException) as error:
            raise LichessAPIError(
                f"Could not retrieve games for Lichess user {normalized_username!r}: {error}"
            ) from error
