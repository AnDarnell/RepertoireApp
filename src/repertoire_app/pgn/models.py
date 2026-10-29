"""Data models for parsed chess games."""

from dataclasses import dataclass


@dataclass(frozen=True)
class GameRecord:
    """A parsed game ready for persistence."""

    game_id: str
    username: str
    white: str
    black: str
    result: str
    played_at: str | None
    time_control: str | None
    variant: str
    initial_fen: str
    moves_uci: tuple[str, ...]
    headers: tuple[tuple[str, str], ...]
