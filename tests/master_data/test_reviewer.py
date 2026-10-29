"""Tests for on-demand repertoire reviews."""

import chess
import unittest

from repertoire_app.master_data.models import MasterPositionData, MasterStats
from repertoire_app.master_data.reviewer import RepertoireReviewer
from repertoire_app.repertoire.builder import PlayerColor, RepertoireBuilder
from repertoire_app.pgn.models import GameRecord
from repertoire_app.storage.repository import SQLiteGameRepository


class FakeMastersClient:
    def __init__(self) -> None:
        self.calls = 0

    def get_position_stats(self, fen: str) -> MasterPositionData:
        self.calls += 1
        board = chess.Board(fen)
        if board.fullmove_number >= 3 and board.turn == chess.WHITE:
            move_stats = {
                "f1c4": MasterStats(white_wins=70, draws=20, black_wins=10),
                "d2d4": MasterStats(white_wins=60, draws=20, black_wins=20),
            }
        else:
            move_stats = {
                "g1f3": MasterStats(white_wins=60, draws=20, black_wins=20),
                "f1c4": MasterStats(white_wins=70, draws=20, black_wins=10),
            }
        return MasterPositionData(
            stats=MasterStats(white_wins=40, draws=20, black_wins=40),
            move_stats=move_stats,
        )


def _game(game_id: str, moves: tuple[str, ...], result: str) -> GameRecord:
    return GameRecord(
        game_id=game_id,
        username="Alice",
        white="Alice",
        black=f"Opponent-{game_id}",
        result=result,
        played_at=None,
        time_control=None,
        variant="Standard",
        initial_fen=chess.Board().fen(),
        moves_uci=moves,
        headers=(),
    )


def _review_games(repository: SQLiteGameRepository, split: bool) -> None:
    if split:
        games = (
            ("nf3-win-1", ("e2e4", "e7e5", "g1f3", "g8f6", "f1c4"), "1-0"),
            ("nf3-win-2", ("e2e4", "e7e5", "g1f3", "g8f6", "f1c4"), "1-0"),
            ("c4-loss-1", ("e2e4", "e7e5", "f1c4", "b8c6"), "0-1"),
            ("c4-loss-2", ("e2e4", "e7e5", "f1c4", "b8c6"), "0-1"),
        )
    else:
        games = (
            ("nf3-win-1", ("e2e4", "e7e5", "g1f3", "g8f6", "f1c4"), "1-0"),
            ("nf3-win-2", ("e2e4", "e7e5", "g1f3", "g8f6", "f1c4"), "1-0"),
            ("nf3-loss", ("e2e4", "e7e5", "g1f3", "g8f6", "f1c4"), "0-1"),
            ("c4-draw", ("e2e4", "e7e5", "f1c4", "b8c6"), "1/2-1/2"),
        )
    for game in games:
        repository.save_game(_game(*game))


class RepertoireReviewerTests(unittest.TestCase):
    def test_common_repertoire_move_is_compared_and_followed_from_selected_position(self) -> None:
        with SQLiteGameRepository(":memory:") as repository:
            _review_games(repository, split=False)
            tree = RepertoireBuilder(repository).build(
                "Alice",
                PlayerColor.WHITE,
                min_repertoire_games=1,
            )
            client = FakeMastersClient()
            reviewer = RepertoireReviewer(client, repository)

            result = reviewer.review(tree, ["e2e4", "e7e5"])
            repeated_result = reviewer.review(tree, ["e2e4", "e7e5"])

        self.assertEqual(result["steps"][0]["type"], "repertoire")
        step = result["steps"][0]
        self.assertEqual(step["repertoire_move"]["san"], "Nf3")
        self.assertEqual(step["repertoire_move"]["frequency"], 0.75)
        self.assertEqual(step["position_games"], 4)
        self.assertEqual(step["master_move"]["score"], 70)
        self.assertEqual(step["master_recommendation"]["san"], "Bc4")
        self.assertIn("position_fen", step)
        self.assertEqual(step["path_uci"], ["e2e4", "e7e5"])
        self.assertEqual(
            [move["san"] for move in step["master_most_played_moves"]],
            ["Nf3", "Bc4"],
        )
        self.assertIn("Nf3", result["line"])
        self.assertEqual(repeated_result["steps"], result["steps"])
        self.assertEqual(client.calls, 2)

    def test_split_repertoire_shows_best_player_and_eligible_master_moves(self) -> None:
        with SQLiteGameRepository(":memory:") as repository:
            _review_games(repository, split=True)
            tree = RepertoireBuilder(repository).build(
                "Alice",
                PlayerColor.WHITE,
                min_repertoire_games=1,
            )
            result = RepertoireReviewer(FakeMastersClient(), repository).review(
                tree,
                ["e2e4", "e7e5"],
            )

        step = result["steps"][0]
        self.assertEqual(step["type"], "branch")
        self.assertEqual(step["best_player_move"]["san"], "Nf3")
        self.assertEqual(step["master_recommendation"]["san"], "Bc4")
        self.assertEqual(len(step["player_moves"]), 2)

    def test_selected_branch_continues_review_and_returns_each_step_position(self) -> None:
        with SQLiteGameRepository(":memory:") as repository:
            _review_games(repository, split=True)
            tree = RepertoireBuilder(repository).build(
                "Alice",
                PlayerColor.WHITE,
                min_repertoire_games=1,
            )
            result = RepertoireReviewer(FakeMastersClient(), repository).review(
                tree,
                ["e2e4", "e7e5"],
                ["g1f3"],
            )

        self.assertEqual(result["steps"][0]["selected_move"]["san"], "Nf3")
        self.assertEqual(result["steps"][0]["path_uci"], ["e2e4", "e7e5"])
        self.assertEqual(result["steps"][1]["type"], "repertoire")
        self.assertIn("g1f3", result["steps"][1]["path_uci"])
