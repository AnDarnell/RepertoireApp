"""Command-line entry point for importing Lichess games."""

import argparse
import sqlite3
from collections.abc import Sequence

from repertoire_app.lichess.client import LichessAPIError, LichessClient
from repertoire_app.lichess.importer import GameImporter
from repertoire_app.pgn.parser import PGNImportError
from repertoire_app.storage.repository import SQLiteGameRepository


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _non_negative_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import games from a Lichess username.")
    parser.add_argument("username", help="Lichess username")
    parser.add_argument(
        "--database",
        default="repertoire.sqlite3",
        help="SQLite database path (default: repertoire.sqlite3)",
    )
    parser.add_argument("--max-games", type=_positive_integer, default=100)
    parser.add_argument(
        "--since",
        type=_non_negative_integer,
        help="only include games since this Unix timestamp in milliseconds",
    )
    parser.add_argument(
        "--until",
        type=_non_negative_integer,
        help="only include games until this Unix timestamp in milliseconds",
    )
    arguments = parser.parse_args(argv)
    if not arguments.username.strip():
        parser.error("username must not be empty")

    try:
        with SQLiteGameRepository(arguments.database) as repository:
            summary = GameImporter(LichessClient(), repository).import_user(
                arguments.username,
                max_games=arguments.max_games,
                since=arguments.since,
                until=arguments.until,
            )
    except (LichessAPIError, PGNImportError, sqlite3.Error) as error:
        parser.exit(1, f"Import failed: {error}\n")

    print(
        f"Processed {summary.processed} games: imported {summary.imported}, "
        f"already present {summary.already_present}."
    )
    if summary.evicted_username is not None:
        print(
            f"Removed stored games for least recently imported player "
            f"{summary.evicted_username!r} to keep the cache at three players."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
