"""Client for the Lichess Masters opening explorer."""

import json
import time
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from repertoire_app.master_data.models import MasterPositionData, MasterStats


class MasterDataAPIError(RuntimeError):
    """Raised when the Lichess Masters API request or response is invalid."""


class LichessMastersClient:
    """Retrieve master-game result counts for chess positions."""

    def __init__(
        self,
        token: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 5,
        on_rate_limit: Callable[[float, int, int], None] | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if token is not None and not token.strip():
            raise ValueError("token must not be empty")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")
        self.token = token.strip() if token is not None else None
        self.timeout = timeout
        self.max_retries = max_retries
        self.on_rate_limit = on_rate_limit

    def get_position_stats(self, fen: str) -> MasterPositionData:
        """Return a position's results and the available next-move results."""
        if not fen.strip():
            raise ValueError("fen must not be empty")
        if self.token is None:
            raise MasterDataAPIError(
                "Lichess Masters requires OAuth2 authentication; "
                "set the LICHESS_API_TOKEN environment variable"
            )

        request = Request(
            f"https://explorer.lichess.org/masters?{urlencode({'fen': fen, 'moves': 100})}",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.token}",
                "User-Agent": "RepertoireApp/0.1",
            },
        )
        payload: object | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                break
            except HTTPError as error:
                if error.code == 429 and attempt < self.max_retries:
                    delay = self._retry_delay(error.headers.get("Retry-After"), attempt)
                    error.close()
                    if self.on_rate_limit is not None:
                        self.on_rate_limit(delay, attempt + 1, self.max_retries)
                    time.sleep(delay)
                    continue
                if error.code == 429:
                    raise MasterDataAPIError(
                        f"Lichess Masters rate limit persisted after "
                        f"{self.max_retries} retries for position {fen!r}; "
                        "wait before retrying. Successfully cached positions are preserved."
                    ) from error
                raise MasterDataAPIError(
                    f"Lichess Masters API returned HTTP {error.code} for position {fen!r}"
                ) from error
            except (URLError, TimeoutError, HTTPException) as error:
                raise MasterDataAPIError(
                    f"Could not retrieve master statistics for position {fen!r}: {error}"
                ) from error
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise MasterDataAPIError(
                    f"Lichess Masters API returned invalid JSON for position {fen!r}"
                ) from error

        if payload is None:
            raise MasterDataAPIError(f"Lichess Masters API returned no data for position {fen!r}")
        return self._parse_stats(payload, fen)

    @staticmethod
    def _retry_delay(retry_after: str | None, attempt: int) -> float:
        if retry_after is not None:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    pass
        return float(min(2**attempt, 30))

    @staticmethod
    def _parse_stats(payload: object, fen: str) -> MasterPositionData:
        if not isinstance(payload, dict):
            raise MasterDataAPIError(f"Lichess Masters API returned an invalid response for {fen!r}")

        position_stats = LichessMastersClient._parse_game_counts(payload, fen)
        raw_moves = payload.get("moves")
        if not isinstance(raw_moves, list):
            raise MasterDataAPIError(
                f"Lichess Masters API returned an invalid moves list for {fen!r}"
            )

        move_stats: dict[str, MasterStats] = {}
        for move in raw_moves:
            if not isinstance(move, dict):
                raise MasterDataAPIError(
                    f"Lichess Masters API returned an invalid move entry for {fen!r}"
                )
            uci = move.get("uci")
            if not isinstance(uci, str) or not uci:
                raise MasterDataAPIError(
                    f"Lichess Masters API returned a move without UCI notation for {fen!r}"
                )
            if uci in move_stats:
                raise MasterDataAPIError(
                    f"Lichess Masters API returned duplicate move {uci!r} for {fen!r}"
                )
            move_stats[uci] = LichessMastersClient._parse_game_counts(move, fen)

        return MasterPositionData(stats=position_stats, move_stats=move_stats)

    @staticmethod
    def _parse_game_counts(payload: dict[str, object], fen: str) -> MasterStats:
        values: list[int] = []
        for field in ("white", "draws", "black"):
            value = payload.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise MasterDataAPIError(
                    f"Lichess Masters API returned an invalid {field!r} count for {fen!r}"
                )
            values.append(value)

        return MasterStats(
            white_wins=values[0],
            draws=values[1],
            black_wins=values[2],
        )
