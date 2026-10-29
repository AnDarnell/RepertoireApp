"""Coordinate downloading, parsing, and storing Lichess games."""

from dataclasses import dataclass
from collections.abc import Callable, Sequence

from repertoire_app.lichess.client import LichessClient
from repertoire_app.pgn.parser import iter_game_records
from repertoire_app.storage.repository import SQLiteGameRepository


@dataclass(frozen=True)
class ImportSummary:
    """Counts for a completed Lichess import."""

    processed: int
    imported: int
    already_present: int
    evicted_username: str | None = None


class GameImporter:
    """Import a user's Lichess games into the local repository."""

    def __init__(self, client: LichessClient, repository: SQLiteGameRepository) -> None:
        self.client = client
        self.repository = repository

    def import_user(
        self,
        username: str,
        *,
        max_games: int = 100,
        since: int | None = None,
        until: int | None = None,
        perf_types: Sequence[str] | None = None,
        progress_callback: Callable[[int], None] | None = None,
    ) -> ImportSummary:
        """Stream, parse, and persist games, safely allowing repeated imports."""
        with self.client.stream_user_games(
            username,
            max_games=max_games,
            since=since,
            until=until,
            perf_types=perf_types,
        ) as pgn_stream:
            records = []
            for record in iter_game_records(pgn_stream, username.strip()):
                records.append(record)
                if progress_callback is not None:
                    progress_callback(len(records))

        imported, already_present, evicted_username = (
            self.repository.save_games_for_player(records, username)
        )

        return ImportSummary(
            processed=len(records),
            imported=imported,
            already_present=already_present,
            evicted_username=evicted_username,
        )
