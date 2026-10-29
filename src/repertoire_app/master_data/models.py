"""Models returned by the Lichess Masters database."""

from dataclasses import dataclass


@dataclass(frozen=True)
class MasterStats:
    """Master-game results from a position, from White's perspective."""

    white_wins: int
    draws: int
    black_wins: int

    @property
    def total_games(self) -> int:
        """Return the number of master games reaching the position."""
        return self.white_wins + self.draws + self.black_wins

    def to_dict(self) -> dict[str, int]:
        """Return result counts in a JSON-ready shape."""
        return {
            "white_wins": self.white_wins,
            "draws": self.draws,
            "black_wins": self.black_wins,
            "total_games": self.total_games,
        }


@dataclass(frozen=True)
class MasterPositionData:
    """Master-game results for a position and its returned next moves."""

    stats: MasterStats
    move_stats: dict[str, MasterStats] | None
