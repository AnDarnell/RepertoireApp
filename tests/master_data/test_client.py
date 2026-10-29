"""Tests for the Lichess Masters API client."""

import io
import json
import unittest
from email.message import Message
from urllib.error import HTTPError
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from repertoire_app.master_data.client import LichessMastersClient, MasterDataAPIError
from repertoire_app.master_data.models import MasterPositionData


class LichessMastersClientTests(unittest.TestCase):
    def test_parses_position_result_counts(self) -> None:
        payload = {
            "white": 12,
            "draws": 8,
            "black": 5,
            "moves": [
                {"uci": "e2e4", "white": 7, "draws": 4, "black": 2},
            ],
        }
        response = io.BytesIO(json.dumps(payload).encode("utf-8"))
        with patch("repertoire_app.master_data.client.urlopen", return_value=response) as urlopen:
            stats = LichessMastersClient(token="test-token").get_position_stats(
                "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
            )

        self.assertIsInstance(stats, MasterPositionData)
        self.assertEqual(stats.stats.white_wins, 12)
        self.assertEqual(stats.stats.draws, 8)
        self.assertEqual(stats.stats.black_wins, 5)
        self.assertEqual(stats.stats.total_games, 25)
        self.assertEqual(stats.move_stats["e2e4"].total_games, 13)
        request = urlopen.call_args.args[0]
        self.assertEqual(urlparse(request.full_url).path, "/masters")
        self.assertEqual(urlparse(request.full_url).hostname, "explorer.lichess.org")
        query = parse_qs(urlparse(request.full_url).query)
        self.assertIn("fen", query)
        self.assertEqual(query["moves"], ["100"])
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")

    def test_rejects_invalid_result_counts(self) -> None:
        response = io.BytesIO(b'{"white": 2, "draws": -1, "black": 3, "moves": []}')
        with patch("repertoire_app.master_data.client.urlopen", return_value=response):
            with self.assertRaisesRegex(MasterDataAPIError, "draws"):
                LichessMastersClient(token="test-token").get_position_stats("position")

    def test_rejects_non_object_response(self) -> None:
        response = io.BytesIO(b"[]")
        with patch("repertoire_app.master_data.client.urlopen", return_value=response):
            with self.assertRaisesRegex(MasterDataAPIError, "invalid response"):
                LichessMastersClient(token="test-token").get_position_stats("position")

    def test_rejects_empty_fen(self) -> None:
        with self.assertRaisesRegex(ValueError, "fen must not be empty"):
            LichessMastersClient().get_position_stats(" ")

    def test_requires_oauth_token_before_requesting_stats(self) -> None:
        with self.assertRaisesRegex(MasterDataAPIError, "LICHESS_API_TOKEN"):
            LichessMastersClient().get_position_stats("position")

    def test_retries_rate_limit_after_retry_after_delay(self) -> None:
        headers = Message()
        headers["Retry-After"] = "3"
        rate_limit = HTTPError(
            "https://explorer.lichess.org/masters",
            429,
            "Too Many Requests",
            headers,
            io.BytesIO(b""),
        )
        response = io.BytesIO(b'{"white": 4, "draws": 2, "black": 1, "moves": []}')
        client = LichessMastersClient(token="test-token", max_retries=1)
        with (
            patch(
                "repertoire_app.master_data.client.urlopen",
                side_effect=[rate_limit, response],
            ) as urlopen,
            patch("repertoire_app.master_data.client.time.sleep") as sleep,
        ):
            with patch.object(client, "on_rate_limit") as on_rate_limit:
                stats = client.get_position_stats("position")

        self.assertEqual(stats.stats.total_games, 7)
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(3.0)
        on_rate_limit.assert_called_once_with(3.0, 1, 1)

    def test_reports_rate_limit_after_retry_limit(self) -> None:
        headers = Message()
        headers["Retry-After"] = "0"
        rate_limit = HTTPError(
            "https://explorer.lichess.org/masters",
            429,
            "Too Many Requests",
            headers,
            io.BytesIO(b""),
        )
        client = LichessMastersClient(token="test-token", max_retries=1)
        with (
            patch(
                "repertoire_app.master_data.client.urlopen",
                side_effect=[rate_limit, rate_limit],
            ) as urlopen,
            patch("repertoire_app.master_data.client.time.sleep"),
        ):
            with self.assertRaisesRegex(MasterDataAPIError, "rate limit persisted"):
                client.get_position_stats("position")

        self.assertEqual(urlopen.call_count, 2)


if __name__ == "__main__":
    unittest.main()
