"""Build a player's move tree from imported games."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

import chess

from repertoire_app.pgn.models import GameRecord
from repertoire_app.master_data.models import MasterStats
from repertoire_app.openings import opening_for_fen
from repertoire_app.storage.repository import SQLiteGameRepository


class PlayerColor(str, Enum):
    """The color whose repertoire is being built."""

    WHITE = "white"
    BLACK = "black"

    @property
    def chess_color(self) -> chess.Color:
        return self is PlayerColor.WHITE


class RepertoireBuildError(ValueError):
    """Raised when imported games cannot form a standard-chess repertoire."""


@dataclass
class PlayerResults:
    """Game outcomes from White's perspective."""

    white_wins: int = 0
    draws: int = 0
    black_wins: int = 0
    undecided: int = 0

    @property
    def decided_games(self) -> int:
        return self.white_wins + self.draws + self.black_wins

    @property
    def white_points(self) -> float:
        return self.white_wins + self.draws / 2

    def record(self, result: str) -> None:
        if result == "1/2-1/2":
            self.draws += 1
        elif result == "1-0":
            self.white_wins += 1
        elif result == "0-1":
            self.black_wins += 1
        else:
            self.undecided += 1

    def to_dict(self) -> dict[str, int | float]:
        return {
            "white_wins": self.white_wins,
            "draws": self.draws,
            "black_wins": self.black_wins,
            "undecided": self.undecided,
            "decided_games": self.decided_games,
            "white_points": self.white_points,
        }


@dataclass
class RepertoireNode:
    """A position in a move-prefix tree and the games that reached it."""

    fen: str
    games: int = 0
    move_uci: str | None = None
    move_san: str | None = None
    played_by: Literal["player", "opponent"] | None = None
    children: dict[str, "RepertoireNode"] = field(default_factory=dict)
    results: PlayerResults = field(default_factory=PlayerResults)
    master_stats: MasterStats | None = None
    is_theoretical: bool | None = None
    master_analysis_skipped: str | None = None
    last_played_year: int | None = None
    opponent_rating_total: int = 0
    rated_opponents: int = 0

    @property
    def position_key(self) -> str:
        """Return the FEN position fields, excluding move counters."""
        return " ".join(self.fen.split()[:4])

    @property
    def average_opponent_rating(self) -> float | None:
        if not self.rated_opponents:
            return None
        return self.opponent_rating_total / self.rated_opponents

    def to_dict(self, parent_games: int | None = None) -> dict[str, object]:
        """Return a stable JSON-ready representation of this position."""
        frequency = 1.0 if parent_games is None else self.games / parent_games
        opening = opening_for_fen(self.fen)
        return {
            "fen": self.fen,
            "position_key": self.position_key,
            "games": self.games,
            "last_played_year": self.last_played_year,
            "average_opponent_rating": self.average_opponent_rating,
            "eco_code": opening[0] if opening is not None else None,
            "opening_name": opening[1] if opening is not None else None,
            "frequency": frequency,
            "move_uci": self.move_uci,
            "move_san": self.move_san,
            "played_by": self.played_by,
            "results": self.results.to_dict(),
            "master_statistics": (
                self.master_stats.to_dict() if self.master_stats is not None else None
            ),
            "is_theoretical": self.is_theoretical,
            "master_analysis_skipped": self.master_analysis_skipped,
            "children": [
                child.to_dict(self.games)
                for child in sorted(
                    self.children.values(),
                    key=lambda node: (-node.games, node.move_san or "", node.move_uci or ""),
                )
            ],
        }

    def iter_nodes(self):
        """Yield this node and all descendant positions."""
        yield self
        for child in self.children.values():
            yield from child.iter_nodes()


@dataclass(frozen=True)
class RepertoireTree:
    """A player's repertoire tree for one color."""

    username: str
    color: PlayerColor
    games: int
    root: RepertoireNode
    pruned_branches: int = 0
    unsupported_games: int = 0

    @property
    def unique_positions(self) -> int:
        """Count distinct board positions, including transpositions once."""
        return len({node.position_key for node in self.root.iter_nodes()})

    def to_dict(self) -> dict[str, object]:
        """Return the complete tree in a JSON-ready representation."""
        return {
            "username": self.username,
            "color": self.color.value,
            "games": self.games,
            "unique_positions": self.unique_positions,
            "pruned_branches": self.pruned_branches,
            "unsupported_games": self.unsupported_games,
            "root": self.root.to_dict(),
        }


class RepertoireBuilder:
    """Build repertoires from games in the local repository."""

    def __init__(self, repository: SQLiteGameRepository) -> None:
        self.repository = repository

    def build(
        self,
        username: str,
        color: PlayerColor,
        min_repertoire_games: int = 1,
    ) -> RepertoireTree:
        """Build and optionally trim a move tree by game frequency."""
        normalized_username = username.strip()
        if not normalized_username:
            raise ValueError("username must not be empty")
        if (
            isinstance(min_repertoire_games, bool)
            or not isinstance(min_repertoire_games, int)
            or min_repertoire_games <= 0
        ):
            raise ValueError("min_repertoire_games must be a positive integer")

        games = self.repository.list_games(normalized_username)
        if not games:
            raise RepertoireBuildError(
                f"No imported games found for Lichess user {normalized_username!r}"
            )

        root = RepertoireNode(fen=chess.Board().fen())
        included_games = 0
        unsupported_games = 0
        for game in games:
            self._validate_game(game, normalized_username)
            if self._player_color(game, normalized_username) is not color.chess_color:
                continue
            if game.variant.casefold() != "standard":
                unsupported_games += 1
                continue
            if game.initial_fen != root.fen:
                raise RepertoireBuildError(
                    f"Game {game.game_id} does not start from the standard chess position"
                )

            self._add_game(root, game, color)
            included_games += 1

        pruned_branches = self._prune(root, min_repertoire_games)
        return RepertoireTree(
            username=normalized_username,
            color=color,
            games=included_games,
            root=root,
            pruned_branches=pruned_branches,
            unsupported_games=unsupported_games,
        )

    @staticmethod
    def _validate_game(game: GameRecord, username: str) -> None:
        participants = (game.white.casefold(), game.black.casefold())
        if participants[0] == participants[1] or username.casefold() not in participants:
            raise RepertoireBuildError(
                f"Stored game {game.game_id} does not identify {username!r} as exactly one player"
            )

    @staticmethod
    def _player_color(game: GameRecord, username: str) -> chess.Color:
        return game.white.casefold() == username.casefold()

    @staticmethod
    def _add_game(root: RepertoireNode, game: GameRecord, color: PlayerColor) -> None:
        board = chess.Board(game.initial_fen)
        node = root
        node.games += 1
        node.results.record(game.result)
        year = _played_year(game.played_at)
        opponent_rating = _opponent_rating(game, color)
        if year is not None:
            node.last_played_year = max(node.last_played_year or year, year)
        if opponent_rating is not None:
            node.opponent_rating_total += opponent_rating
            node.rated_opponents += 1

        for move_uci in game.moves_uci:
            try:
                move = chess.Move.from_uci(move_uci)
                if not board.is_legal(move):
                    raise RepertoireBuildError(
                        f"Stored game {game.game_id} contains illegal move {move_uci!r}"
                    )
                move_san = board.san(move)
                board.push(move)
            except ValueError as error:
                raise RepertoireBuildError(
                    f"Could not replay move {move_uci!r} in stored game {game.game_id}: {error}"
                ) from error

            child = node.children.get(move_uci)
            if child is None:
                child = RepertoireNode(
                    fen=board.fen(),
                    move_uci=move_uci,
                    move_san=move_san,
                    played_by="player" if board.turn != color.chess_color else "opponent",
                )
                node.children[move_uci] = child
            elif child.fen != board.fen():
                raise RepertoireBuildError(
                    f"Move {move_uci!r} reached inconsistent positions in the repertoire tree"
                )

            child.games += 1
            child.results.record(game.result)
            if year is not None:
                child.last_played_year = max(child.last_played_year or year, year)
            if opponent_rating is not None:
                child.opponent_rating_total += opponent_rating
                child.rated_opponents += 1
            node = child

    @classmethod
    def _prune(cls, node: RepertoireNode, min_games: int) -> int:
        """Remove branches not reached by enough games and count removed roots."""
        pruned_branches = 0
        for move_uci, child in list(node.children.items()):
            if child.games < min_games:
                del node.children[move_uci]
                pruned_branches += 1
            else:
                pruned_branches += cls._prune(child, min_games)
        return pruned_branches


def _played_year(played_at: str | None) -> int | None:
    if played_at is None:
        return None
    year_text = played_at[:4]
    if len(year_text) != 4 or not year_text.isdecimal():
        return None
    year = int(year_text)
    return year if year > 0 else None


def _opponent_rating(game: GameRecord, color: PlayerColor) -> int | None:
    headers = dict(game.headers)
    header = "BlackElo" if color is PlayerColor.WHITE else "WhiteElo"
    rating_text = headers.get(header)
    if rating_text is None or not rating_text.isdecimal():
        return None
    rating = int(rating_text)
    return rating if rating > 0 else None
