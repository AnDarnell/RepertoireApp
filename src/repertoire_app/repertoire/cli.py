"""Command-line entry point for inspecting a repertoire tree."""

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Sequence

import chess

from repertoire_app.master_data.client import LichessMastersClient, MasterDataAPIError
from repertoire_app.master_data.enricher import MasterDataEnricher
from repertoire_app.master_data.models import MasterStats
from repertoire_app.repertoire.builder import (
    PlayerColor,
    RepertoireBuildError,
    RepertoireBuilder,
    RepertoireNode,
    RepertoireTree,
)
from repertoire_app.storage.repository import SQLiteGameRepository


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a repertoire tree from imported games."
    )
    parser.add_argument("username", help="Lichess username used during import")
    parser.add_argument("--color", choices=("white", "black"), required=True)
    parser.add_argument(
        "--database",
        default="repertoire.sqlite3",
        help="SQLite database path (default: repertoire.sqlite3)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the repertoire tree as JSON",
    )
    parser.add_argument(
        "--masters",
        action="store_true",
        help="enrich positions with cached Lichess Masters statistics",
    )
    parser.add_argument(
        "--theory-threshold",
        type=_positive_integer,
        default=20,
        help="stop a branch below this many master games (default: 20)",
    )
    parser.add_argument(
        "--min-repertoire-games",
        type=_positive_integer,
        default=2,
        help="trim branches appearing in fewer imported games (default: 2)",
    )
    parser.add_argument(
        "--show-all-branches",
        action="store_true",
        help="keep all branches in the tree (Masters lookups still use the cutoff)",
    )
    parser.add_argument(
        "--opening-plies",
        type=_positive_integer,
        default=4,
        help="group the summary by this many initial half-moves (default: 4)",
    )
    parser.add_argument(
        "--tree",
        action="store_true",
        help="print the detailed move tree instead of the opening summary",
    )
    arguments = parser.parse_args(argv)
    color = PlayerColor(arguments.color)
    enrichment = None

    try:
        with SQLiteGameRepository(arguments.database) as repository:
            tree = RepertoireBuilder(repository).build(
                arguments.username,
                color,
                min_repertoire_games=(
                    1 if arguments.show_all_branches else arguments.min_repertoire_games
                ),
            )
            if arguments.masters:
                print(
                    "Starting Lichess Masters lookups "
                    f"(theory threshold {arguments.theory_threshold}; "
                    f"minimum repertoire frequency "
                    f"{arguments.min_repertoire_games} games)...",
                    file=sys.stderr,
                    flush=True,
                )
                enrichment = MasterDataEnricher(
                    LichessMastersClient(
                        token=os.environ.get("LICHESS_API_TOKEN"),
                        on_rate_limit=_report_rate_limit,
                    ),
                    repository,
                    theory_threshold=arguments.theory_threshold,
                    min_repertoire_games=arguments.min_repertoire_games,
                    on_progress=_report_enrichment_progress,
                ).enrich(tree)
    except KeyboardInterrupt:
        parser.exit(
            130,
            "Cancelled. Successfully cached master statistics are preserved; "
            "rerun the command to continue.\n",
        )
    except (
        RepertoireBuildError,
        MasterDataAPIError,
        ValueError,
        OSError,
        sqlite3.Error,
    ) as error:
        parser.exit(1, f"Could not build repertoire: {error}\n")

    if arguments.json:
        print(json.dumps(tree.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(f"Repertoire for {tree.username} as {tree.color.value}")
        print(f"Games: {tree.games}")
        if tree.unsupported_games:
            print(
                f"Unsupported-variant games excluded for this color: "
                f"{tree.unsupported_games}"
            )
        print(f"Unique positions: {tree.unique_positions}")
        print(
            f"Branches pruned from local repertoire: {tree.pruned_branches}"
        )
        if enrichment is not None:
            print(f"Master API queries: {enrichment.api_queries}")
            print(f"Cached positions reused: {enrichment.cache_hits}")
            print(f"Theoretical positions: {enrichment.theoretical_positions}")
            print(f"Positions below {arguments.theory_threshold}: {enrichment.boundary_positions}")
            print(
                f"Branches skipped (under {arguments.min_repertoire_games} "
                f"imported games): {enrichment.frequency_skipped_branches}"
            )
        if arguments.tree:
            _print_repertoire_lines(
                tree.root,
                parent_games=tree.games,
                masters_requested=arguments.masters,
            )
        else:
            _print_opening_summary(
                tree,
                opening_plies=arguments.opening_plies,
                masters_requested=arguments.masters,
            )
    return 0


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _report_rate_limit(delay: float, retry: int, max_retries: int) -> None:
    print(
        f"Lichess rate limit (HTTP 429): waiting {delay:g} seconds "
        f"before retry {retry}/{max_retries}.",
        file=sys.stderr,
        flush=True,
    )


def _report_enrichment_progress(index: int, source: str, stats: MasterStats) -> None:
    print(
        f"Masters: checked {index} unique positions ({source}; "
        f"{stats.total_games} games at this position).",
        file=sys.stderr,
        flush=True,
    )


def _print_opening_summary(
    tree: RepertoireTree,
    opening_plies: int,
    masters_requested: bool,
) -> None:
    groups = _collect_opening_groups(tree.root, opening_plies)
    print(f"Opening repertoire (first {opening_plies} half-moves):")
    if not groups:
        print("  No qualifying opening lines.")
        return

    represented_games = sum(node.games for _, node in groups)
    for label, node in groups:
        percentage = node.games / tree.games if tree.games else 0
        line = f"  {label} - {node.games} games ({percentage:.1%})"
        if masters_requested and node.master_stats is not None:
            stats = node.master_stats
            total = stats.total_games
            if total:
                results = (
                    f"{stats.white_wins / total:.1%}/"
                    f"{stats.draws / total:.1%}/"
                    f"{stats.black_wins / total:.1%}"
                )
            else:
                results = "0.0%/0.0%/0.0%"
            line += f" | Masters: {total} games (W/D/B {results})"
        elif masters_requested:
            line += " | Masters: not queried"
        print(line)

    unrepresented_games = tree.games - represented_games
    if unrepresented_games:
        percentage = unrepresented_games / tree.games if tree.games else 0
        print(
            f"  Other (branches below the repertoire cutoff): "
            f"{unrepresented_games} games ({percentage:.1%})"
        )


def _collect_opening_groups(
    root: RepertoireNode,
    opening_plies: int,
) -> list[tuple[str, RepertoireNode]]:
    groups: list[tuple[str, RepertoireNode]] = []

    def visit(
        node: RepertoireNode,
        board: chess.Board,
        moves: tuple[str, ...],
        depth: int,
    ) -> None:
        if depth == opening_plies or not node.children:
            if moves:
                groups.append((" ".join(moves), node))
            return

        children = sorted(
            node.children.values(),
            key=lambda child: (-child.games, child.move_san or ""),
        )
        for child in children:
            if child.move_uci is None:
                continue
            move = chess.Move.from_uci(child.move_uci)
            move_number = board.fullmove_number
            move_san = board.san(move)
            move_label = (
                f"{move_number}. {move_san}"
                if board.turn == chess.WHITE
                else f"{move_number}... {move_san}"
            )
            next_board = board.copy()
            next_board.push(move)
            visit(
                child,
                next_board,
                (*moves, move_label),
                depth + 1,
            )

    visit(root, chess.Board(), (), 0)
    return sorted(groups, key=lambda group: (-group[1].games, group[0]))


def _print_repertoire_lines(
    node: RepertoireNode,
    parent_games: int,
    masters_requested: bool,
    depth: int = 0,
) -> None:
    if depth == 0:
        print("Repertoire lines:")
    children = sorted(
        node.children.values(),
        key=lambda child: (-child.games, child.move_san or ""),
    )
    if not children and depth == 0:
        print("  No games with this color.")
    for child in children:
        fen_fields = node.fen.split()
        move_number = fen_fields[5]
        move_prefix = f"{move_number}." if fen_fields[1] == "w" else f"{move_number}..."
        proportion = child.games / parent_games if parent_games else 0
        line = (
            f"{'  ' * depth}{move_prefix} {child.move_san} - "
            f"{child.games}/{parent_games} games ({proportion:.1%} of previous position)"
        )
        if child.master_stats is not None:
            stats = child.master_stats
            total = stats.total_games
            percentages = (
                f"{stats.white_wins / total:.1%}/"
                f"{stats.draws / total:.1%}/"
                f"{stats.black_wins / total:.1%}"
                if total
                else "0.0%/0.0%/0.0%"
            )
            line += (
                f" | Masters: {total} games "
                f"(White/Draw/Black {percentages})"
            )
            if child.is_theoretical is False:
                line += " [below theory threshold]"
        elif masters_requested:
            skip_reason = child.master_analysis_skipped
            if skip_reason == "repertoire_frequency":
                line += " | Masters: skipped (branch under cutoff)"
            elif skip_reason == "ancestor_repertoire_frequency":
                line += " | Masters: skipped (ancestor under branch cutoff)"
            else:
                line += " | Masters: not queried"
        print(line)
        _print_repertoire_lines(
            child,
            parent_games=child.games,
            masters_requested=masters_requested,
            depth=depth + 1,
        )


if __name__ == "__main__":
    raise SystemExit(main())
