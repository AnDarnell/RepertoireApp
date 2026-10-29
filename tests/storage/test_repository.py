"""Tests for SQLite game persistence."""

import tempfile
import unittest
from pathlib import Path

from repertoire_app.master_data.models import MasterPositionData, MasterStats
from repertoire_app.pgn.models import GameRecord
from repertoire_app.storage.repository import SQLiteGameRepository


class SQLiteGameRepositoryTests(unittest.TestCase):
    def test_saves_and_retrieves_game_without_duplicates(self) -> None:
        game = GameRecord(
            game_id="abcdefgh",
            username="PlayerOne",
            white="PlayerOne",
            black="PlayerTwo",
            result="1-0",
            played_at="2025.01.02",
            time_control="180+0",
            variant="Standard",
            initial_fen="startpos-fen",
            moves_uci=("e2e4", "e7e5"),
            headers=(("Site", "https://lichess.org/abcdefgh"),),
        )

        with tempfile.TemporaryDirectory() as directory:
            with SQLiteGameRepository(Path(directory) / "games.sqlite3") as repository:
                self.assertTrue(repository.save_game(game))
                self.assertFalse(repository.save_game(game))
                self.assertEqual(repository.get_game("abcdefgh"), game)
                self.assertEqual(repository.count_games(), 1)
                self.assertEqual(repository.list_games("PlayerOne"), [game])

    def test_persists_position_and_next_move_master_stats(self) -> None:
        data = MasterPositionData(
            stats=MasterStats(10, 5, 5),
            move_stats={"e2e4": MasterStats(5, 3, 2)},
        )

        with SQLiteGameRepository(":memory:") as repository:
            repository.save_master_data("position-key", data)
            self.assertEqual(repository.get_master_data("position-key"), data)
            self.assertEqual(repository.get_master_stats("position-key"), data.stats)

    def test_import_keeps_three_most_recent_players_and_preserves_master_cache(self) -> None:
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

        with SQLiteGameRepository(":memory:") as repository:
            for name in ("Oldest", "Middle", "Newest"):
                repository.save_game(game(f"{name}-game", name))
            with repository._connection:
                repository._connection.executemany(
                    "UPDATE imported_players SET last_imported_at = ? WHERE username = ?",
                    (
                        ("2024-01-01T00:00:00+00:00", "Oldest"),
                        ("2024-01-02T00:00:00+00:00", "Middle"),
                        ("2024-01-03T00:00:00+00:00", "Newest"),
                    ),
                )
            repository.save_master_stats("shared-position", MasterStats(1, 2, 3))

            imported, already_present, evicted = repository.save_games_for_player(
                [game("Fourth-game", "Fourth")],
                "Fourth",
            )

            self.assertEqual((imported, already_present, evicted), (1, 0, "Oldest"))
            self.assertEqual(
                {record.username for record in repository.list_games()},
                {"Middle", "Newest", "Fourth"},
            )
            self.assertEqual(repository.get_master_stats("shared-position"), MasterStats(1, 2, 3))


if __name__ == "__main__":
    unittest.main()
