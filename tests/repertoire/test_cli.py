"""Tests for repertoire CLI interruption handling."""

import io
import unittest
from contextlib import redirect_stderr
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

import chess

from repertoire_app.master_data.enricher import EnrichmentSummary
from repertoire_app.master_data.models import MasterStats
from repertoire_app.repertoire.builder import PlayerColor, RepertoireNode, RepertoireTree
from repertoire_app.repertoire.cli import main


class RepertoireCLITests(unittest.TestCase):
    def test_default_output_summarizes_opening_prefixes_and_master_results(self) -> None:
        board = chess.Board()
        root = RepertoireNode(fen=board.fen(), games=5)

        board.push_san("e4")
        e4 = RepertoireNode(
            fen=board.fen(),
            games=4,
            move_uci="e2e4",
            move_san="e4",
            master_stats=MasterStats(white_wins=30, draws=50, black_wins=20),
        )
        root.children["e2e4"] = e4

        board.push_san("c5")
        c5 = RepertoireNode(
            fen=board.fen(), games=3, move_uci="c7c5", move_san="c5"
        )
        e4.children["c7c5"] = c5

        tree = RepertoireTree(
            username="Alice",
            color=PlayerColor.WHITE,
            games=5,
            root=root,
            pruned_branches=1,
        )

        with (
            patch(
                "repertoire_app.repertoire.cli.SQLiteGameRepository",
                return_value=MagicMock(),
            ),
            patch(
                "repertoire_app.repertoire.cli.RepertoireBuilder.build",
                return_value=tree,
            ) as build,
            patch(
                "repertoire_app.repertoire.cli.MasterDataEnricher.enrich",
                return_value=EnrichmentSummary(
                    api_queries=0,
                    cache_hits=0,
                    theoretical_positions=1,
                    boundary_positions=0,
                    frequency_skipped_branches=0,
                ),
            ),
            redirect_stdout(io.StringIO()) as stdout,
        ):
            self.assertEqual(
                main(["Alice", "--color", "white", "--masters", "--opening-plies", "1"]),
                0,
            )

        output = stdout.getvalue()
        self.assertIn("Opening repertoire (first 1 half-moves):", output)
        self.assertIn("1. e4 - 4 games (80.0%)", output)
        self.assertIn("Masters: 100 games (W/D/B 30.0%/50.0%/20.0%)", output)
        self.assertNotIn("1... c5", output)
        self.assertIn("Branches pruned from local repertoire: 1", output)
        self.assertEqual(build.call_args.kwargs["min_repertoire_games"], 2)

    def test_tree_option_prints_detailed_move_branches(self) -> None:
        board = chess.Board()
        root = RepertoireNode(fen=board.fen(), games=3)
        board.push_san("e4")
        e4 = RepertoireNode(
            fen=board.fen(), games=3, move_uci="e2e4", move_san="e4"
        )
        root.children["e2e4"] = e4
        board.push_san("c5")
        e4.children["c7c5"] = RepertoireNode(
            fen=board.fen(), games=3, move_uci="c7c5", move_san="c5"
        )
        tree = RepertoireTree("Alice", PlayerColor.WHITE, 3, root)

        with (
            patch(
                "repertoire_app.repertoire.cli.SQLiteGameRepository",
                return_value=MagicMock(),
            ),
            patch(
                "repertoire_app.repertoire.cli.RepertoireBuilder.build",
                return_value=tree,
            ),
            redirect_stdout(io.StringIO()) as stdout,
        ):
            self.assertEqual(
                main(["Alice", "--color", "white", "--tree"]),
                0,
            )

        output = stdout.getvalue()
        self.assertIn("Repertoire lines:", output)
        self.assertIn("1. e4 - 3/3 games (100.0% of previous position)", output)
        self.assertIn("1... c5 - 3/3 games (100.0% of previous position)", output)

    def test_show_all_branches_does_not_change_master_analysis_cutoff(self) -> None:
        board = chess.Board()
        root = RepertoireNode(fen=board.fen(), games=1)
        board.push_san("d4")
        root.children["d2d4"] = RepertoireNode(
            fen=board.fen(), games=1, move_uci="d2d4", move_san="d4"
        )
        tree = RepertoireTree(
            username="Alice",
            color=PlayerColor.WHITE,
            games=1,
            root=root,
        )

        with (
            patch(
                "repertoire_app.repertoire.cli.SQLiteGameRepository",
                return_value=MagicMock(),
            ),
            patch(
                "repertoire_app.repertoire.cli.RepertoireBuilder.build",
                return_value=tree,
            ) as build,
            patch(
                "repertoire_app.repertoire.cli.MasterDataEnricher",
                return_value=MagicMock(
                    enrich=MagicMock(
                        return_value=EnrichmentSummary(
                            api_queries=0,
                            cache_hits=0,
                            theoretical_positions=0,
                            boundary_positions=0,
                            frequency_skipped_branches=1,
                        )
                    )
                ),
            ) as enricher_class,
            redirect_stdout(io.StringIO()) as stdout,
        ):
            self.assertEqual(
                main(["Alice", "--color", "white", "--masters", "--show-all-branches"]),
                0,
            )

        self.assertIn("1. d4 - 1 games (100.0%)", stdout.getvalue())
        self.assertEqual(build.call_args.kwargs["min_repertoire_games"], 1)
        self.assertEqual(enricher_class.call_args.kwargs["min_repertoire_games"], 2)

    def test_ctrl_c_exits_cleanly_and_mentions_cached_data(self) -> None:
        repository = MagicMock()
        tree = RepertoireTree(
            username="Alice",
            color=PlayerColor.WHITE,
            games=0,
            root=RepertoireNode(fen="start"),
        )

        with (
            patch(
                "repertoire_app.repertoire.cli.SQLiteGameRepository",
                return_value=repository,
            ),
            patch(
                "repertoire_app.repertoire.cli.RepertoireBuilder.build",
                return_value=tree,
            ),
            patch(
                "repertoire_app.repertoire.cli.MasterDataEnricher.enrich",
                side_effect=KeyboardInterrupt,
            ),
            redirect_stderr(io.StringIO()) as stderr,
        ):
            with self.assertRaises(SystemExit) as exit_error:
                main(["Alice", "--color", "white", "--masters"])

        self.assertEqual(exit_error.exception.code, 130)
        self.assertIn("Successfully cached master statistics are preserved", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
