"""Enrich repertoire-tree positions with cached master-game statistics."""

from dataclasses import dataclass
from collections.abc import Callable

from repertoire_app.master_data.client import LichessMastersClient
from repertoire_app.master_data.models import MasterPositionData, MasterStats
from repertoire_app.repertoire.builder import RepertoireNode, RepertoireTree
from repertoire_app.storage.repository import SQLiteGameRepository


@dataclass(frozen=True)
class EnrichmentSummary:
    """Counts for one master-data enrichment run."""

    api_queries: int
    cache_hits: int
    theoretical_positions: int
    boundary_positions: int
    frequency_skipped_branches: int


class MasterDataEnricher:
    """Look up positions only until each repertoire branch leaves theory."""

    def __init__(
        self,
        client: LichessMastersClient,
        repository: SQLiteGameRepository,
        theory_threshold: int = 20,
        min_repertoire_games: int = 2,
        on_progress: Callable[[int, str, MasterStats], None] | None = None,
    ) -> None:
        if isinstance(theory_threshold, bool) or not isinstance(theory_threshold, int):
            raise ValueError("theory_threshold must be a positive integer")
        if theory_threshold <= 0:
            raise ValueError("theory_threshold must be a positive integer")
        if isinstance(min_repertoire_games, bool) or not isinstance(min_repertoire_games, int):
            raise ValueError("min_repertoire_games must be a positive integer")
        if min_repertoire_games <= 0:
            raise ValueError("min_repertoire_games must be a positive integer")
        self.client = client
        self.repository = repository
        self.theory_threshold = theory_threshold
        self.min_repertoire_games = min_repertoire_games
        self.on_progress = on_progress

    def enrich(self, tree: RepertoireTree) -> EnrichmentSummary:
        """Annotate the tree and stop querying descendants below the threshold."""
        data_by_position: dict[str, MasterPositionData] = {}
        api_queries = 0
        cache_hits = 0
        theoretical_positions: set[str] = set()
        boundary_positions: set[str] = set()
        checked_positions = 0
        frequency_skipped_branches = 0

        def mark_frequency_skipped(node: RepertoireNode, reason: str) -> None:
            node.master_analysis_skipped = reason
            for child in node.children.values():
                mark_frequency_skipped(child, "ancestor_repertoire_frequency")

        def visit(
            node: RepertoireNode,
            inherited_stats: MasterStats | None = None,
        ) -> None:
            nonlocal api_queries, cache_hits, checked_positions, frequency_skipped_branches

            position_key = node.position_key
            if node.games < self.min_repertoire_games:
                cached_data = self.repository.get_master_data(position_key)
                stats = inherited_stats or (
                    cached_data.stats if cached_data is not None else None
                )
                if inherited_stats is None and cached_data is not None:
                    cache_hits += 1
                node.master_stats = stats
                node.is_theoretical = (
                    stats.total_games >= self.theory_threshold if stats is not None else None
                )
                mark_frequency_skipped(node, "repertoire_frequency")
                frequency_skipped_branches += 1
                return

            data = data_by_position.get(position_key)
            if data is None:
                cached_data = self.repository.get_master_data(position_key)
                stats = inherited_stats or (
                    cached_data.stats if cached_data is not None else None
                )
                needs_move_data = (
                    bool(node.children)
                    and stats is not None
                    and stats.total_games >= self.theory_threshold
                    and (cached_data is None or cached_data.move_stats is None)
                )
                if stats is None or needs_move_data:
                    data = self.client.get_position_stats(node.fen)
                    self.repository.save_master_data(position_key, data)
                    api_queries += 1
                    source = "API"
                elif cached_data is not None:
                    data = cached_data
                    cache_hits += 1
                    source = "database cache"
                else:
                    data = MasterPositionData(stats=stats, move_stats=None)
                    source = "parent response"
                data_by_position[position_key] = data
                checked_positions += 1
                if self.on_progress is not None:
                    self.on_progress(checked_positions, source, data.stats)

            node.master_stats = data.stats
            node.is_theoretical = data.stats.total_games >= self.theory_threshold
            node.master_analysis_skipped = None
            if node.is_theoretical:
                theoretical_positions.add(position_key)
                for child in sorted(
                    node.children.values(),
                    key=lambda candidate: (
                        -candidate.games,
                        candidate.move_san or "",
                    ),
                ):
                    child_stats = (
                        data.move_stats.get(child.move_uci)
                        if data.move_stats is not None and child.move_uci is not None
                        else None
                    )
                    visit(child, child_stats)
            else:
                boundary_positions.add(position_key)

        visit(tree.root)
        return EnrichmentSummary(
            api_queries=api_queries,
            cache_hits=cache_hits,
            theoretical_positions=len(theoretical_positions),
            boundary_positions=len(boundary_positions),
            frequency_skipped_branches=frequency_skipped_branches,
        )
