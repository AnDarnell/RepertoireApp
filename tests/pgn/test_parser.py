"""Tests for converting PGN into game records."""

import io
import unittest

from repertoire_app.pgn.parser import PGNImportError, iter_game_records


PGN = """\
[Event "Rated Blitz game"]
[Site "https://lichess.org/abcdefgh"]
[Date "2025.01.02"]
[UTCDate "2025.01.02"]
[White "PlayerOne"]
[Black "PlayerTwo"]
[Result "1-0"]
[TimeControl "180+0"]

1. e4 e5 2. Nf3 Nc6 1-0
"""


class PGNParserTests(unittest.TestCase):
    def test_parses_metadata_and_mainline_moves(self) -> None:
        record = next(iter_game_records(io.StringIO(PGN), "PlayerOne"))

        self.assertEqual(record.game_id, "abcdefgh")
        self.assertEqual(record.username, "PlayerOne")
        self.assertEqual(record.result, "1-0")
        self.assertEqual(record.played_at, "2025.01.02")
        self.assertEqual(record.time_control, "180+0")
        self.assertEqual(record.variant, "Standard")
        self.assertEqual(record.moves_uci, ("e2e4", "e7e5", "g1f3", "b8c6"))

    def test_rejects_non_lichess_game_urls(self) -> None:
        pgn = PGN.replace("https://lichess.org/abcdefgh", "https://example.org/abcdefgh")

        with self.assertRaises(PGNImportError):
            next(iter_game_records(io.StringIO(pgn), "PlayerOne"))

    def test_empty_pgn_has_no_games(self) -> None:
        self.assertEqual(list(iter_game_records(io.StringIO(""), "PlayerOne")), [])


if __name__ == "__main__":
    unittest.main()
