"""Convert PGN streams into records used by the application."""

from collections.abc import Iterator
from typing import TextIO
from urllib.parse import urlparse

import chess.pgn

from repertoire_app.pgn.models import GameRecord


class PGNImportError(ValueError):
    """Raised when a PGN game cannot be represented safely."""


def _game_id(site: str) -> str:
    parsed_site = urlparse(site)
    if parsed_site.hostname not in {"lichess.org", "www.lichess.org"}:
        raise PGNImportError(f"Expected a Lichess game URL, got {site!r}")

    game_id = parsed_site.path.rstrip("/").rsplit("/", maxsplit=1)[-1]
    if not game_id:
        raise PGNImportError(f"Could not find a game ID in the Lichess URL {site!r}")
    return game_id


def _optional_header(value: str | None) -> str | None:
    if not value or value.startswith("?"):
        return None
    return value


def iter_game_records(source: TextIO, username: str) -> Iterator[GameRecord]:
    """Read games one at a time, retaining their main-line moves as UCI."""
    while (game := chess.pgn.read_game(source)) is not None:
        if game.errors:
            details = "; ".join(str(error) for error in game.errors)
            raise PGNImportError(f"Invalid PGN for game {game.headers.get('Site', 'unknown')}: {details}")

        headers = game.headers
        game_id = _game_id(headers.get("Site", ""))
        try:
            board = game.board()
            initial_fen = board.fen()
            moves_uci: list[str] = []
            for move in game.mainline_moves():
                moves_uci.append(board.uci(move))
                board.push(move)
        except ValueError as error:
            raise PGNImportError(f"Could not read moves for Lichess game {game_id}: {error}") from error

        result = headers.get("Result", "*")
        if result not in {"1-0", "0-1", "1/2-1/2", "*"}:
            raise PGNImportError(f"Invalid result {result!r} in Lichess game {game_id}")

        played_at = _optional_header(headers.get("UTCDate")) or _optional_header(
            headers.get("Date")
        )
        yield GameRecord(
            game_id=game_id,
            username=username,
            white=headers.get("White", ""),
            black=headers.get("Black", ""),
            result=result,
            played_at=played_at,
            time_control=_optional_header(headers.get("TimeControl")),
            variant=headers.get("Variant", "Standard"),
            initial_fen=initial_fen,
            moves_uci=tuple(moves_uci),
            headers=tuple(headers.items()),
        )
