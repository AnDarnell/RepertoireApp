"""Look up ECO codes and opening names from the bundled Lichess dataset."""

import csv
from functools import lru_cache
from importlib.resources import files
from io import StringIO

import chess.pgn


@lru_cache(maxsize=1)
def _openings_by_position() -> dict[str, tuple[str, str, int]]:
    openings: dict[str, tuple[str, str, int]] = {}
    package = files("repertoire_app")
    for filename in ("a.tsv", "b.tsv", "c.tsv", "d.tsv", "e.tsv"):
        with package.joinpath("openings", filename).open(
            "r", encoding="utf-8", newline=""
        ) as source:
            for entry in csv.DictReader(source, delimiter="\t"):
                game = chess.pgn.read_game(StringIO(entry["pgn"]))
                if game is None or game.errors:
                    raise ValueError(f"Invalid opening line in {filename}: {entry['pgn']!r}")

                board = game.end().board()
                position_key = " ".join(board.fen().split()[:4])
                value = (entry["eco"], entry["name"], len(list(game.mainline_moves())))
                previous = openings.get(position_key)
                if previous is None or value[2] > previous[2] or (
                    value[2] == previous[2] and value[:2] < previous[:2]
                ):
                    openings[position_key] = value
    return openings


def opening_for_fen(fen: str) -> tuple[str, str] | None:
    """Return the most specific known opening for a position."""
    position_key = " ".join(fen.split()[:4])
    opening = _openings_by_position().get(position_key)
    if opening is None:
        return None
    return opening[0], opening[1]
