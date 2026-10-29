"""Tests for building repertoire trees from imported games."""

import tempfile
import unittest
from pathlib import Path

import chess

from repertoire_app.pgn.models import GameRecord
from repertoire_app.repertoire.builder import (
    PlayerColor,
    RepertoireBuildError,
    RepertoireBuilder,
)
from repertoire_app.storage.repository import SQLiteGameRepository


def _game(
    game_id: str,
    white: str,
    black: str,
    moves: tuple[str, ...],
    *,
    variant: str = "Standard",
    initial_fen: str | None = None,
    result: str = "*",
    played_at: str | None = None,
    headers: tuple[tuple[str, str], ...] = (),
) -> GameRecord:
    return GameRecord(
        game_id=game_id,
        username="Alice",
        white=white,
        black=black,
        result=result,
        played_at=played_at,
        time_control=None,
        variant=variant,
        initial_fen=initial_fen or chess.Board().fen(),
        moves_uci=moves,
        headers=headers,
    )


class RepertoireBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.repository = SQLiteGameRepository(Path(self.directory.name) / "games.sqlite3")
        for game in (
            _game(
                "white-e5", "Alice", "Bob", ("e2e4", "e7e5", "g1f3", "b8c6"),
                result="1-0",
            ),
            _game(
                "white-c5", "Alice", "Carol", ("e2e4", "c7c5"), result="0-1",
            ),
            _game(
                "black-d5", "Dan", "Alice", ("d2d4", "d7d5", "c2c4", "e7e6"),
                result="1-0",
            ),
        ):
            self.repository.save_game(game)
        self.builder = RepertoireBuilder(self.repository)

    def tearDown(self) -> None:
        self.repository.close()
        self.directory.cleanup()

    def test_white_tree_counts_player_and_opponent_branches(self) -> None:
        tree = self.builder.build("alice", PlayerColor.WHITE)

        self.assertEqual(tree.games, 2)
        self.assertEqual(tree.root.games, 2)
        self.assertEqual(tree.unique_positions, 6)
        e4 = tree.root.children["e2e4"]
        self.assertEqual((e4.move_san, e4.played_by, e4.games), ("e4", "player", 2))
        self.assertAlmostEqual(e4.to_dict(2)["frequency"], 1.0)
        self.assertEqual(e4.children["e7e5"].played_by, "opponent")
        self.assertEqual(e4.children["e7e5"].games, 1)
        self.assertEqual(e4.children["c7c5"].games, 1)
        self.assertEqual(e4.children["e7e5"].children["g1f3"].played_by, "player")
        self.assertEqual(e4.results.to_dict(), {
            "white_wins": 1,
            "draws": 0,
            "black_wins": 1,
            "undecided": 0,
            "decided_games": 2,
            "white_points": 1.0,
        })
        self.assertEqual(e4.children["e7e5"].results.white_wins, 1)

    def test_draw_counts_as_half_a_point(self) -> None:
        with SQLiteGameRepository(":memory:") as repository:
            repository.save_game(
                _game("draw", "Alice", "Bob", ("e2e4",), result="1/2-1/2")
            )
            tree = RepertoireBuilder(repository).build("Alice", PlayerColor.WHITE)

        self.assertEqual(tree.root.children["e2e4"].results.white_points, 0.5)

    def test_records_latest_played_year_for_each_move(self) -> None:
        with SQLiteGameRepository(":memory:") as repository:
            repository.save_game(
                _game("older", "Alice", "Bob", ("e2e4", "e7e5"), played_at="2023.05.10")
            )
            repository.save_game(
                _game("newer", "Alice", "Carol", ("e2e4", "c7c5"), played_at="2025.02.14")
            )
            repository.save_game(
                _game("unknown-date", "Alice", "Dan", ("d2d4",), played_at=None)
            )
            tree = RepertoireBuilder(repository).build("Alice", PlayerColor.WHITE)

        e4 = tree.root.children["e2e4"]
        self.assertEqual(e4.last_played_year, 2025)
        self.assertEqual(e4.children["e7e5"].last_played_year, 2023)
        self.assertEqual(tree.root.children["d2d4"].last_played_year, None)
        self.assertEqual(e4.to_dict(2)["last_played_year"], 2025)

    def test_averages_available_opponent_ratings_at_each_position(self) -> None:
        with SQLiteGameRepository(":memory:") as repository:
            repository.save_game(
                _game(
                    "white-elo-1", "Alice", "Bob", ("e2e4",), result="1-0",
                    headers=(("BlackElo", "1800"),),
                )
            )
            repository.save_game(
                _game(
                    "white-elo-2", "Alice", "Carol", ("e2e4",), result="0-1",
                    headers=(("BlackElo", "2000"),),
                )
            )
            repository.save_game(
                _game(
                    "white-no-elo", "Alice", "Dan", ("e2e4",), result="1-0",
                    headers=(("BlackElo", "?"),),
                )
            )
            tree = RepertoireBuilder(repository).build("Alice", PlayerColor.WHITE)

        e4 = tree.root.children["e2e4"]
        self.assertEqual(e4.average_opponent_rating, 1900)
        self.assertEqual(e4.to_dict(3)["average_opponent_rating"], 1900)

    def test_uses_white_rating_as_opponent_rating_for_black_repertoire(self) -> None:
        with SQLiteGameRepository(":memory:") as repository:
            repository.save_game(
                _game(
                    "black-rating", "Bob", "Alice", ("e2e4", "e7e5"),
                    headers=(("WhiteElo", "2100"), ("BlackElo", "1700")),
                )
            )
            tree = RepertoireBuilder(repository).build("Alice", PlayerColor.BLACK)

        self.assertEqual(tree.root.children["e2e4"].average_opponent_rating, 2100)
        self.assertEqual(tree.root.children["e2e4"].children["e7e5"].average_opponent_rating, 2100)

    def test_black_tree_starts_with_opponent_move_and_counts_black_reply(self) -> None:
        tree = self.builder.build("Alice", PlayerColor.BLACK)

        self.assertEqual(tree.games, 1)
        d4 = tree.root.children["d2d4"]
        self.assertEqual((d4.move_san, d4.played_by), ("d4", "opponent"))
        d5 = d4.children["d7d5"]
        self.assertEqual((d5.move_san, d5.played_by), ("d5", "player"))
        self.assertEqual(d4.results.white_wins, 1)
        self.assertEqual(d4.results.white_points, 1.0)

    def test_json_representation_orders_branches_by_frequency(self) -> None:
        tree = self.builder.build("Alice", PlayerColor.WHITE)
        data = tree.to_dict()

        self.assertEqual(data["color"], "white")
        self.assertEqual(data["games"], 2)
        self.assertEqual(data["root"]["children"][0]["move_san"], "e4")
        self.assertEqual(data["root"]["children"][0]["frequency"], 1.0)

    def test_json_includes_eco_for_known_opening_position(self) -> None:
        with SQLiteGameRepository(":memory:") as repository:
            repository.save_game(
                _game(
                    "ruy-lopez",
                    "Alice",
                    "Bob",
                    ("e2e4", "e7e5", "g1f3", "b8c6", "f1b5"),
                )
            )
            tree = RepertoireBuilder(repository).build("Alice", PlayerColor.WHITE)

        node = tree.root
        for move in ("e2e4", "e7e5", "g1f3", "b8c6", "f1b5"):
            node = node.children[move]
        self.assertEqual(node.to_dict()["eco_code"], "C60")
        self.assertEqual(node.to_dict()["opening_name"], "Ruy Lopez")

    def test_trims_tree_to_positions_reached_by_minimum_number_of_games(self) -> None:
        tree = self.builder.build(
            "Alice",
            PlayerColor.WHITE,
            min_repertoire_games=2,
        )

        self.assertEqual(tree.games, 2)
        self.assertEqual(tree.unique_positions, 2)
        self.assertEqual(tree.pruned_branches, 2)
        self.assertEqual(list(tree.root.children), ["e2e4"])
        self.assertEqual(tree.root.children["e2e4"].children, {})
        self.assertEqual(tree.to_dict()["pruned_branches"], 2)

    def test_empty_color_returns_an_empty_starting_position_tree(self) -> None:
        with SQLiteGameRepository(":memory:") as repository:
            repository.save_game(_game("only-white", "Alice", "Bob", ("e2e4",)))
            tree = RepertoireBuilder(repository).build("Alice", PlayerColor.BLACK)

        self.assertEqual(tree.root.fen, chess.Board().fen())
        self.assertEqual(tree.games, 0)
        self.assertEqual(tree.root.games, 0)
        self.assertEqual(tree.root.children, {})

    def test_excludes_games_from_unsupported_variants(self) -> None:
        with SQLiteGameRepository(":memory:") as repository:
            repository.save_game(_game("crazyhouse", "Alice", "Bob", (), variant="Crazyhouse"))
            repository.save_game(
                _game("black-crazyhouse", "Bob", "Alice", (), variant="Crazyhouse")
            )
            tree = RepertoireBuilder(repository).build("Alice", PlayerColor.WHITE)

        self.assertEqual(tree.games, 0)
        self.assertEqual(tree.unsupported_games, 1)
        self.assertEqual(tree.root.children, {})

    def test_rejects_a_stored_illegal_move(self) -> None:
        with SQLiteGameRepository(":memory:") as repository:
            repository.save_game(_game("illegal", "Alice", "Bob", ("e2e5",)))
            with self.assertRaisesRegex(RepertoireBuildError, "illegal move"):
                RepertoireBuilder(repository).build("Alice", PlayerColor.WHITE)

    def test_errors_when_no_games_exist(self) -> None:
        with SQLiteGameRepository(":memory:") as repository:
            with self.assertRaisesRegex(RepertoireBuildError, "No imported games"):
                RepertoireBuilder(repository).build("Alice", PlayerColor.WHITE)

    def test_transpositions_share_position_count_but_keep_separate_branches(self) -> None:
        with SQLiteGameRepository(":memory:") as repository:
            repository.save_game(
                _game("knight-first", "Alice", "Bob", ("g1f3", "d7d5", "g2g3"))
            )
            repository.save_game(
                _game("pawn-first", "Alice", "Carol", ("g2g3", "d7d5", "g1f3"))
            )
            tree = RepertoireBuilder(repository).build("Alice", PlayerColor.WHITE)

        self.assertEqual(tree.games, 2)
        self.assertEqual(tree.unique_positions, 6)
        final_positions = [
            node
            for node in tree.root.iter_nodes()
            if node.position_key == "rnbqkbnr/ppp1pppp/8/3p4/8/5NP1/PPPPPP1P/RNBQKB1R b KQkq -"
        ]
        self.assertEqual(len(final_positions), 2)
        self.assertNotEqual(final_positions[0].fen, final_positions[1].fen)
        self.assertEqual(final_positions[0].position_key, final_positions[1].position_key)


if __name__ == "__main__":
    unittest.main()
