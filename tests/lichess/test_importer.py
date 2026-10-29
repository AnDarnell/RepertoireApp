"""Tests for coordinating Lichess game imports."""

import io
import tempfile
import unittest
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO
from unittest.mock import patch
from urllib.error import HTTPError

from repertoire_app.lichess.client import LichessAPIError, LichessClient
from repertoire_app.lichess.importer import GameImporter
from repertoire_app.pgn.models import GameRecord
from repertoire_app.pgn.parser import PGNImportError
from repertoire_app.storage.repository import SQLiteGameRepository


PGN = """\
[Event "Game one"]
[Site "https://lichess.org/abcdefgh"]
[White "PlayerOne"]
[Black "PlayerTwo"]
[Result "1-0"]

1. e4 e5 1-0

[Event "Game two"]
[Site "https://lichess.org/ijklmnop"]
[White "PlayerOne"]
[Black "PlayerThree"]
[Result "0-1"]

1. d4 d5 0-1
"""


class FakeLichessClient:
    @contextmanager
    def stream_user_games(
        self,
        username: str,
        *,
        max_games: int,
        since: int | None = None,
        until: int | None = None,
        perf_types: tuple[str, ...] | list[str] | None = None,
    ) -> Generator[TextIO, None, None]:
        self.last_request = (username, max_games, since, until, perf_types)
        yield io.StringIO(PGN)


class LichessImporterTests(unittest.TestCase):
    def test_imports_once_and_counts_repeated_games_as_existing(self) -> None:
        client = FakeLichessClient()
        with tempfile.TemporaryDirectory() as directory:
            with SQLiteGameRepository(Path(directory) / "games.sqlite3") as repository:
                importer = GameImporter(client, repository)
                progress = []

                first = importer.import_user(
                    "PlayerOne",
                    max_games=25,
                    since=10,
                    progress_callback=progress.append,
                )
                second = importer.import_user("PlayerOne", max_games=25, since=10)

                self.assertEqual((first.processed, first.imported, first.already_present), (2, 2, 0))
                self.assertEqual((second.processed, second.imported, second.already_present), (2, 0, 2))
                self.assertEqual(progress, [1, 2])
                self.assertEqual(repository.count_games("PlayerOne"), 2)
                self.assertEqual(client.last_request, ("PlayerOne", 25, 10, None, None))

    def test_import_summary_reports_evicted_player(self) -> None:
        def game(game_id: str, username: str) -> GameRecord:
            return GameRecord(
                game_id=game_id,
                username=username,
                white=username,
                black="Opponent",
                result="1-0",
                played_at=None,
                time_control=None,
                variant="Standard",
                initial_fen="startpos-fen",
                moves_uci=("e2e4",),
                headers=(),
            )

        with tempfile.TemporaryDirectory() as directory:
            with SQLiteGameRepository(Path(directory) / "games.sqlite3") as repository:
                for username in ("Oldest", "Middle", "Newest"):
                    repository.save_game(game(f"{username}-game", username))
                with repository._connection:
                    repository._connection.execute(
                        "UPDATE imported_players SET last_imported_at = ? WHERE username = ?",
                        ("2024-01-01T00:00:00+00:00", "Oldest"),
                    )
                    repository._connection.execute(
                        "UPDATE imported_players SET last_imported_at = ? WHERE username = ?",
                        ("2024-01-02T00:00:00+00:00", "Middle"),
                    )
                    repository._connection.execute(
                        "UPDATE imported_players SET last_imported_at = ? WHERE username = ?",
                        ("2024-01-03T00:00:00+00:00", "Newest"),
                    )

                summary = GameImporter(FakeLichessClient(), repository).import_user("PlayerOne")

                self.assertEqual(summary.evicted_username, "Oldest")

    def test_filtered_import_preserves_other_games_already_in_the_cache(self) -> None:
        preserved_game = GameRecord(
            game_id="old-bullet-game",
            username="PlayerOne",
            white="PlayerOne",
            black="Opponent",
            result="1-0",
            played_at=None,
            time_control="60+0",
            variant="Standard",
            initial_fen="startpos-fen",
            moves_uci=("e2e4",),
            headers=(),
        )
        client = FakeLichessClient()
        with tempfile.TemporaryDirectory() as directory:
            with SQLiteGameRepository(Path(directory) / "games.sqlite3") as repository:
                repository.save_game(preserved_game)

                GameImporter(client, repository).import_user(
                    "PlayerOne",
                    perf_types=("blitz", "rapid"),
                )

                saved_games = repository.list_games("PlayerOne")

        self.assertEqual(client.last_request[4], ("blitz", "rapid"))
        self.assertEqual(
            {game.game_id for game in saved_games},
            {"old-bullet-game", "abcdefgh", "ijklmnop"},
        )

    def test_invalid_pgn_is_reported_without_claiming_success(self) -> None:
        class InvalidPGNClient(FakeLichessClient):
            @contextmanager
            def stream_user_games(
                self,
                username: str,
                *,
                max_games: int,
                since: int | None = None,
                until: int | None = None,
                perf_types: tuple[str, ...] | list[str] | None = None,
            ) -> Generator[TextIO, None, None]:
                self.last_request = (username, max_games, since, until, perf_types)
                yield io.StringIO('[Event "Missing site"]\n\n1. e4 *\n')

        with tempfile.TemporaryDirectory() as directory:
            with SQLiteGameRepository(Path(directory) / "games.sqlite3") as repository:
                with self.assertRaises(PGNImportError):
                    GameImporter(InvalidPGNClient(), repository).import_user("PlayerOne")
                self.assertEqual(repository.count_games(), 0)

    def test_client_builds_bounded_export_request(self) -> None:
        response = io.BytesIO(PGN.encode("utf-8"))
        with patch("repertoire_app.lichess.client.urlopen", return_value=response) as urlopen:
            client = LichessClient()
            with client.stream_user_games(" Player One ", max_games=25, since=10, until=20) as stream:
                self.assertIn("abcdefgh", stream.read())

        request = urlopen.call_args.args[0]
        self.assertIn("/api/games/user/Player%20One?", request.full_url)
        self.assertIn("max=25", request.full_url)
        self.assertIn("since=10", request.full_url)
        self.assertIn("until=20", request.full_url)
        self.assertNotIn("perfType%5B%5D", request.full_url)
        self.assertEqual(request.get_header("Accept"), "application/x-chess-pgn")

    def test_client_sends_selected_performance_types_to_lichess(self) -> None:
        response = io.BytesIO(PGN.encode("utf-8"))
        with patch("repertoire_app.lichess.client.urlopen", return_value=response) as urlopen:
            with LichessClient().stream_user_games(
                "PlayerOne",
                perf_types=("blitz", "rapid"),
            ) as stream:
                stream.read()

        request = urlopen.call_args.args[0]
        self.assertIn("perfType%5B%5D=blitz", request.full_url)
        self.assertIn("perfType%5B%5D=rapid", request.full_url)

    def test_client_rejects_empty_or_unknown_performance_type_filters(self) -> None:
        with self.assertRaisesRegex(ValueError, "perf_types"):
            with LichessClient().stream_user_games("PlayerOne", perf_types=()):
                self.fail("An empty filter should not open a request")
        with self.assertRaisesRegex(ValueError, "perf_types"):
            with LichessClient().stream_user_games("PlayerOne", perf_types=("classical",)):
                self.fail("Unsupported filters should not open a request")

    def test_client_surfaces_http_errors(self) -> None:
        error = HTTPError("https://lichess.org", 404, "Not found", {}, None)
        with patch("repertoire_app.lichess.client.urlopen", side_effect=error):
            with self.assertRaises(LichessAPIError):
                with LichessClient().stream_user_games("missing-user"):
                    self.fail("the API request should fail")


if __name__ == "__main__":
    unittest.main()
