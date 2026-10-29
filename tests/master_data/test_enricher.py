"""Tests for thresholded and cached master-data enrichment."""

import tempfile
import unittest
from pathlib import Path

from repertoire_app.master_data.enricher import MasterDataEnricher
from repertoire_app.master_data.models import MasterPositionData, MasterStats
from repertoire_app.repertoire.builder import (
    PlayerColor,
    RepertoireNode,
    RepertoireTree,
)
from repertoire_app.storage.repository import SQLiteGameRepository


class FakeMastersClient:
    def __init__(self, responses: dict[str, MasterPositionData]) -> None:
        self.responses = responses
        self.requested_fens: list[str] = []

    def get_position_stats(self, fen: str) -> MasterPositionData:
        self.requested_fens.append(fen)
        return self.responses[fen]


def _node(
    fen: str,
    move_uci: str | None = None,
    *,
    games: int = 1,
) -> RepertoireNode:
    return RepertoireNode(fen=fen, move_uci=move_uci, games=games)


class MasterDataEnricherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.repository = SQLiteGameRepository(Path(self.directory.name) / "games.sqlite3")

    def tearDown(self) -> None:
        self.repository.close()
        self.directory.cleanup()

    def test_stops_each_branch_at_the_first_position_below_threshold_and_caches(self) -> None:
        root_fen = "start w KQkq - 0 1"
        rare_fen = "rare b - - 0 1"
        common_fen = "common b - - 0 1"
        below_fen = "below w - - 0 2"
        root = _node(root_fen, games=2)
        rare = _node(rare_fen, "e2e4")
        common = _node(common_fen, "d2d4")
        below = _node(below_fen, "g1f3")
        unqueried = _node("unqueried b - - 0 2", "b8c6")
        rare.children["b8c6"] = unqueried
        common.children["g1f3"] = below
        root.children["e2e4"] = rare
        root.children["d2d4"] = common
        tree = RepertoireTree("Alice", PlayerColor.WHITE, 2, root)
        client = FakeMastersClient(
            {
                root_fen: MasterPositionData(
                    MasterStats(10, 10, 10),
                    {
                        "e2e4": MasterStats(8, 3, 8),
                        "d2d4": MasterStats(10, 5, 5),
                    },
                ),
                common_fen: MasterPositionData(
                    MasterStats(10, 5, 5),
                    {"g1f3": MasterStats(5, 5, 9)},
                ),
            }
        )
        progress_events: list[tuple[int, str, MasterStats]] = []
        enricher = MasterDataEnricher(
            client,
            self.repository,
            theory_threshold=20,
            min_repertoire_games=1,
            on_progress=lambda index, source, stats: progress_events.append(
                (index, source, stats)
            ),
        )

        first = enricher.enrich(tree)
        self.assertEqual(
            [source for _, source, _ in progress_events],
            ["API", "parent response", "API", "parent response"],
        )
        self.assertEqual(
            [index for index, _, _ in progress_events],
            [1, 2, 3, 4],
        )
        database_path = Path(self.directory.name) / "games.sqlite3"
        self.repository.close()
        self.repository = SQLiteGameRepository(database_path)
        enricher = MasterDataEnricher(
            client,
            self.repository,
            theory_threshold=20,
            min_repertoire_games=1,
        )
        second = enricher.enrich(tree)

        self.assertEqual(len(client.requested_fens), 2)
        self.assertEqual(first.api_queries, 2)
        self.assertEqual(first.cache_hits, 0)
        self.assertEqual(first.theoretical_positions, 2)
        self.assertEqual(first.boundary_positions, 2)
        self.assertTrue(root.is_theoretical)
        self.assertFalse(rare.is_theoretical)
        self.assertTrue(common.is_theoretical)
        self.assertFalse(below.is_theoretical)
        self.assertIsNone(unqueried.master_stats)
        self.assertEqual(second.api_queries, 0)
        self.assertGreaterEqual(second.cache_hits, 1)
        self.assertEqual(self.repository.get_master_stats(root.position_key), MasterStats(10, 10, 10))

    def test_transposed_positions_are_queried_once_per_run(self) -> None:
        root_fen = "start w KQkq - 0 1"
        first_fen = "same b - - 0 2"
        second_fen = "same b - - 0 3"
        root = _node(root_fen, games=2)
        first = _node(first_fen, "e2e4")
        second = _node(second_fen, "d2d4")
        first.children["e7e5"] = _node("after-common-move w - - 0 2", "e7e5")
        second.children["e7e5"] = _node("after-common-move w - - 0 2", "e7e5")
        root.children["e2e4"] = first
        root.children["d2d4"] = second
        tree = RepertoireTree("Alice", PlayerColor.WHITE, 2, root)
        stats = MasterStats(10, 10, 10)
        client = FakeMastersClient(
            {
                root_fen: MasterPositionData(stats, {"e2e4": stats, "d2d4": stats}),
                first_fen: MasterPositionData(stats, {"e7e5": stats}),
            }
        )

        summary = MasterDataEnricher(
            client,
            self.repository,
            min_repertoire_games=1,
        ).enrich(tree)

        self.assertEqual(summary.api_queries, 2)
        self.assertEqual(client.requested_fens, [root_fen, first_fen])
        self.assertEqual(first.master_stats, stats)
        self.assertEqual(second.master_stats, stats)

    def test_rejects_non_positive_threshold(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive integer"):
            MasterDataEnricher(FakeMastersClient({}), self.repository, theory_threshold=0)

    def test_skips_branches_below_minimum_repertoire_frequency(self) -> None:
        root_fen = "start w KQkq - 0 1"
        common_fen = "common b - - 0 1"
        rare_fen = "rare b - - 0 1"
        next_fen = "next w - - 0 2"
        root = _node(root_fen, games=6)
        common = _node(common_fen, "e2e4", games=4)
        rare = _node(rare_fen, "d2d4", games=2)
        rare_child = _node("rare-child w - - 0 2", "d7d5", games=2)
        next_node = _node(next_fen, "e7e5", games=3)
        rare.children["d7d5"] = rare_child
        common.children["e7e5"] = next_node
        root.children["e2e4"] = common
        root.children["d2d4"] = rare
        tree = RepertoireTree("Alice", PlayerColor.WHITE, 6, root)
        client = FakeMastersClient(
            {
                root_fen: MasterPositionData(
                    MasterStats(10, 10, 10),
                    {
                        "e2e4": MasterStats(8, 8, 9),
                        "d2d4": MasterStats(1, 1, 1),
                    },
                ),
                common_fen: MasterPositionData(
                    MasterStats(8, 8, 9),
                    {"e7e5": MasterStats(4, 3, 3)},
                ),
            }
        )

        summary = MasterDataEnricher(
            client,
            self.repository,
            min_repertoire_games=3,
        ).enrich(tree)

        self.assertEqual(client.requested_fens, [root_fen, common_fen])
        self.assertEqual(summary.api_queries, 2)
        self.assertEqual(summary.frequency_skipped_branches, 1)
        self.assertEqual(rare.master_analysis_skipped, "repertoire_frequency")
        self.assertEqual(rare.master_stats, MasterStats(1, 1, 1))
        self.assertEqual(rare_child.master_analysis_skipped, "ancestor_repertoire_frequency")
        self.assertIsNone(rare_child.master_stats)
        self.assertIsNone(next_node.master_analysis_skipped)
        self.assertEqual(next_node.master_stats, MasterStats(4, 3, 3))

if __name__ == "__main__":
    unittest.main()
