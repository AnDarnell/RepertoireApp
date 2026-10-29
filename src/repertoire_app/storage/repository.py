"""SQLite persistence for imported games."""

import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from repertoire_app.master_data.models import MasterPositionData, MasterStats
from repertoire_app.pgn.models import GameRecord

MAX_SAVED_PLAYERS = 3


class SQLiteGameRepository:
    """Persist imported games and prevent duplicate Lichess game IDs."""

    def __init__(self, database_path: str | Path) -> None:
        self._connection = sqlite3.connect(database_path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS games (
                game_id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                white TEXT NOT NULL,
                black TEXT NOT NULL,
                result TEXT NOT NULL,
                played_at TEXT,
                time_control TEXT,
                variant TEXT NOT NULL,
                initial_fen TEXT NOT NULL,
                moves_uci_json TEXT NOT NULL,
                headers_json TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS master_position_cache (
                position_key TEXT PRIMARY KEY,
                white_wins INTEGER NOT NULL,
                draws INTEGER NOT NULL,
                black_wins INTEGER NOT NULL,
                moves_json TEXT,
                fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS imported_players (
                username TEXT PRIMARY KEY COLLATE NOCASE,
                last_imported_at TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            INSERT OR IGNORE INTO imported_players (username, last_imported_at)
            SELECT username, CURRENT_TIMESTAMP
            FROM games
            GROUP BY username COLLATE NOCASE
            """
        )
        cache_columns = {
            row["name"]
            for row in self._connection.execute(
                "PRAGMA table_info(master_position_cache)"
            ).fetchall()
        }
        if "moves_json" not in cache_columns:
            self._connection.execute(
                "ALTER TABLE master_position_cache ADD COLUMN moves_json TEXT"
            )
        self._connection.commit()

    def save_game(self, game: GameRecord) -> bool:
        """Insert a game; return False when its Lichess ID is already stored."""
        imported, _, _ = self.save_games_for_player([game], game.username)
        return imported == 1

    def save_games_for_player(
        self,
        games: Sequence[GameRecord],
        username: str,
    ) -> tuple[int, int, str | None]:
        """Save an import atomically and retain only the three most recent players."""
        normalized_username = username.strip()
        if not normalized_username:
            raise ValueError("username must not be empty")
        if any(game.username.casefold() != normalized_username.casefold() for game in games):
            raise ValueError("all imported games must belong to the importing username")

        imported = 0
        already_present = 0
        evicted_username: str | None = None
        imported_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        with self._connection:
            for game in games:
                imported += self._insert_game(game)
            already_present = len(games) - imported

            existing_player = self._connection.execute(
                "SELECT 1 FROM imported_players WHERE username = ? COLLATE NOCASE",
                (normalized_username,),
            ).fetchone()
            if games or existing_player is not None:
                self._connection.execute(
                    """
                    INSERT INTO imported_players (username, last_imported_at)
                    VALUES (?, ?)
                    ON CONFLICT(username) DO UPDATE SET
                        last_imported_at = excluded.last_imported_at
                    """,
                    (normalized_username, imported_at),
                )

                player_count = self._connection.execute(
                    "SELECT COUNT(*) FROM imported_players"
                ).fetchone()[0]
                if player_count > MAX_SAVED_PLAYERS:
                    oldest = self._connection.execute(
                        """
                        SELECT username
                        FROM imported_players
                        WHERE username != ? COLLATE NOCASE
                        ORDER BY last_imported_at, username COLLATE NOCASE
                        LIMIT 1
                        """,
                        (normalized_username,),
                    ).fetchone()
                    if oldest is None:
                        raise sqlite3.DatabaseError("Could not select an imported player to evict")
                    evicted_username = oldest["username"]
                    self._connection.execute(
                        "DELETE FROM games WHERE username = ? COLLATE NOCASE",
                        (evicted_username,),
                    )
                    self._connection.execute(
                        "DELETE FROM imported_players WHERE username = ? COLLATE NOCASE",
                        (evicted_username,),
                    )

        return imported, already_present, evicted_username

    def _insert_game(self, game: GameRecord) -> int:
        """Insert a game within the caller's transaction."""
        cursor = self._connection.execute(
            """
            INSERT INTO games (
                game_id, username, white, black, result, played_at,
                time_control, variant, initial_fen, moves_uci_json, headers_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(game_id) DO NOTHING
            """,
            (
                game.game_id,
                game.username,
                game.white,
                game.black,
                game.result,
                game.played_at,
                game.time_control,
                game.variant,
                game.initial_fen,
                json.dumps(game.moves_uci),
                json.dumps(dict(game.headers), ensure_ascii=False),
            ),
        )
        return cursor.rowcount

    def get_game(self, game_id: str) -> GameRecord | None:
        """Retrieve a game by its Lichess ID."""
        row = self._connection.execute(
            "SELECT * FROM games WHERE game_id = ?",
            (game_id,),
        ).fetchone()
        return self._record_from_row(row) if row is not None else None

    def list_games(self, username: str | None = None) -> list[GameRecord]:
        """List stored games, optionally scoped to the importing username."""
        if username is None:
            rows = self._connection.execute(
                "SELECT * FROM games ORDER BY played_at, game_id"
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM games WHERE username = ? COLLATE NOCASE ORDER BY played_at, game_id",
                (username,),
            ).fetchall()
        return [self._record_from_row(row) for row in rows]

    def count_games(self, username: str | None = None) -> int:
        """Count all stored games, optionally scoped to the importing username."""
        if username is None:
            row = self._connection.execute("SELECT COUNT(*) FROM games").fetchone()
        else:
            row = self._connection.execute(
                "SELECT COUNT(*) FROM games WHERE username = ? COLLATE NOCASE",
                (username,),
            ).fetchone()
        return int(row[0])

    def get_master_stats(self, position_key: str) -> MasterStats | None:
        """Retrieve cached master-game results for a normalized position."""
        cached = self.get_master_data(position_key)
        return cached.stats if cached is not None else None

    def get_master_data(self, position_key: str) -> MasterPositionData | None:
        """Retrieve cached position and next-move results."""
        row = self._connection.execute(
            """
            SELECT white_wins, draws, black_wins, moves_json
            FROM master_position_cache
            WHERE position_key = ?
            """,
            (position_key,),
        ).fetchone()
        if row is None:
            return None
        move_stats_json = row["moves_json"]
        move_stats = (
            {
                uci: MasterStats(
                    white_wins=counts["white_wins"],
                    draws=counts["draws"],
                    black_wins=counts["black_wins"],
                )
                for uci, counts in json.loads(move_stats_json).items()
            }
            if move_stats_json is not None
            else None
        )
        return MasterPositionData(
            stats=MasterStats(
                white_wins=row["white_wins"],
                draws=row["draws"],
                black_wins=row["black_wins"],
            ),
            move_stats=move_stats,
        )

    def save_master_stats(self, position_key: str, stats: MasterStats) -> None:
        """Save or refresh cached master-game results for a position."""
        self.save_master_data(position_key, MasterPositionData(stats, None))

    def save_master_data(self, position_key: str, data: MasterPositionData) -> None:
        """Save position and next-move results in the persistent cache."""
        move_stats_json = (
            json.dumps(
                {
                    uci: {
                        "white_wins": stats.white_wins,
                        "draws": stats.draws,
                        "black_wins": stats.black_wins,
                    }
                    for uci, stats in data.move_stats.items()
                }
            )
            if data.move_stats is not None
            else None
        )
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO master_position_cache (
                    position_key, white_wins, draws, black_wins, moves_json, fetched_at
                )
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(position_key) DO UPDATE SET
                    white_wins = excluded.white_wins,
                    draws = excluded.draws,
                    black_wins = excluded.black_wins,
                    moves_json = excluded.moves_json,
                    fetched_at = CURRENT_TIMESTAMP
                """,
                (
                    position_key,
                    data.stats.white_wins,
                    data.stats.draws,
                    data.stats.black_wins,
                    move_stats_json,
                ),
            )

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._connection.close()

    def __enter__(self) -> "SQLiteGameRepository":
        return self

    def __exit__(self, _exc_type: object, _exc_value: object, _traceback: object) -> None:
        try:
            self._connection.__exit__(_exc_type, _exc_value, _traceback)
        finally:
            self.close()

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> GameRecord:
        headers = json.loads(row["headers_json"])
        return GameRecord(
            game_id=row["game_id"],
            username=row["username"],
            white=row["white"],
            black=row["black"],
            result=row["result"],
            played_at=row["played_at"],
            time_control=row["time_control"],
            variant=row["variant"],
            initial_fen=row["initial_fen"],
            moves_uci=tuple(json.loads(row["moves_uci_json"])),
            headers=tuple(headers.items()),
        )
