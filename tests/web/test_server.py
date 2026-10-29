"""Tests for repertoire web routes and data loading."""

import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from repertoire_app.lichess.client import LichessAPIError
from repertoire_app.lichess.importer import ImportSummary
from repertoire_app.master_data.models import MasterPositionData, MasterStats
from repertoire_app.pgn.models import GameRecord
from repertoire_app.storage.repository import SQLiteGameRepository
from repertoire_app.web.server import (
    _source_snapshot,
    build_repertoire_payload,
    create_handler,
)


def _game(
    game_id: str,
    variant: str = "Standard",
    result: str = "*",
    moves: tuple[str, ...] = ("e2e4", "e7e5"),
) -> GameRecord:
    return GameRecord(
        game_id=game_id,
        username="Alice",
        white="Alice",
        black=f"Opponent-{game_id}",
        result=result,
        played_at=None,
        time_control=None,
        variant=variant,
        initial_fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        moves_uci=moves,
        headers=(),
    )


class RepertoireWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = str(Path(self.directory.name) / "games.sqlite3")
        with SQLiteGameRepository(self.database) as repository:
            for game_id, result in zip(
                ("one", "two", "three"),
                ("1-0", "1/2-1/2", "0-1"),
            ):
                repository.save_game(_game(game_id, result=result))
            repository.save_game(_game("variant", "Crazyhouse"))

        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            create_handler(self.database),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def test_api_returns_frequency_trimmed_tree(self) -> None:
        with urlopen(
            f"{self.base_url}/api/repertoire?username=Alice&color=white&min_games=3"
        ) as response:
            payload = json.loads(response.read())

        self.assertEqual(payload["games"], 3)
        self.assertEqual(payload["unsupported_games"], 1)
        self.assertEqual(payload["root"]["children"][0]["move_san"], "e4")
        self.assertEqual(payload["root"]["children"][0]["games"], 3)
        self.assertEqual(
            payload["root"]["children"][0]["results"],
            {
                "white_wins": 1,
                "draws": 1,
                "black_wins": 1,
                "undecided": 0,
                "decided_games": 3,
                "white_points": 1.5,
            },
        )
        self.assertEqual(payload["root"]["children"][0]["children"][0]["move_san"], "e5")

    def test_api_reports_missing_user_games(self) -> None:
        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.base_url}/api/repertoire?username=Missing&color=white")

        self.assertEqual(error.exception.code, 404)
        payload = json.loads(error.exception.read())
        self.assertIn("No imported games", payload["error"])

    def test_root_serves_the_explorer_page(self) -> None:
        with urlopen(f"{self.base_url}/") as response:
            page = response.read().decode("utf-8")

        self.assertEqual(response.headers.get_content_type(), "text/html")
        self.assertIn("Repertoarutforskare", page)
        self.assertIn("Drag från ställningen", page)
        self.assertIn('id="import-button"', page)
        self.assertIn('id="max-games"', page)
        self.assertIn('max="1000"', page)
        self.assertIn('id="max-games" type="number" min="1" max="1000" value="1000"', page)
        self.assertIn('aria-label="Spelformer att importera"', page)
        self.assertIn('name="perf-type" value="bullet" checked', page)
        self.assertIn('name="perf-type" value="blitz" checked', page)
        self.assertIn('name="perf-type" value="rapid" checked', page)
        self.assertIn("perf_types: perfTypes", page)
        self.assertIn('id="import-status"', page)
        self.assertIn("Välj ett av dina vanligaste förstadrag.", page)
        self.assertIn('<option value="2" selected>2 partier</option>', page)
        self.assertIn('<option value="3">3 partier</option>', page)
        self.assertIn("position-results", page)
        self.assertIn("function formatPositionMove(node)", page)
        self.assertIn("`${moveNumber}... ${node.move_san}`", page)
        self.assertIn("`${moveNumber}. ${node.move_san}`", page)
        self.assertIn("? formatPositionMove(node)", page)
        self.assertIn("function indexPositions(root)", page)
        self.assertIn("positionIndex.get(positionKey(currentNode))", page)
        self.assertIn("Samma ställning via ${line.join", page)
        self.assertIn("currentNode = occurrence.node", page)
        self.assertIn('id="position-opening"', page)
        self.assertIn('class="board-heading"', page)
        self.assertLess(page.index('id="position-title"'), page.index('id="position-opening"'))
        self.assertLess(page.index('id="flip-board-button"'), page.index('id="position-opening"'))
        self.assertIn('id="board-player-label"', page)
        self.assertIn('${repertoire.username} som ${color === "white" ? "vit" : "svart"}', page)
        self.assertIn("outlier-opening", page)
        self.assertIn("poängprocenten visas alltid ur vits perspektiv", page.lower())
        self.assertIn("results.white_points", page)
        self.assertIn("Vits poängprocent:", page)
        self.assertIn('id="flip-board-button"', page)
        self.assertIn('class="flip-button" type="button" id="flip-board-button" aria-pressed="false">Flip</button>', page)
        self.assertIn(".board-heading", page)
        self.assertIn('"ECO:"', page)
        self.assertIn("45–55 % är neutralt", page)
        self.assertIn("score-neutral", page)
        self.assertIn("score-positive", page)
        self.assertIn("score-negative", page)
        self.assertIn("square.style.gridRow", page)
        self.assertIn("move-last-played", page)
        self.assertIn("Senast spelad", page)
        self.assertIn("move-label", page)
        self.assertIn("--move-frequency", page)
        self.assertIn("rgba(112, 174, 123, .28)", page)
        self.assertIn('arrows.classList.add("board-arrows")', page)
        self.assertIn("renderBoardArrows(node.move_uci, hoveredMoveUci, reviewNextMoveUci)", page)
        self.assertIn("step.repertoire_move.uci", page)
        self.assertIn("reviewMovePreview(`${move.san} (${move.games})`, move.uci)", page)
        self.assertIn('preview.addEventListener("mouseenter"', page)
        self.assertIn('preview.addEventListener("mouseleave"', page)
        self.assertIn('button.addEventListener("mouseenter"', page)
        self.assertIn('button.addEventListener("mouseleave"', page)
        self.assertIn('button.addEventListener("focus"', page)
        self.assertIn('button.addEventListener("blur"', page)
        self.assertIn("child.average_opponent_rating", page)
        self.assertIn("Motståndarrating i snitt", page)
        self.assertIn("available.slice(0, 5)", page)
        self.assertIn("Visa alla ${available.length} förstadrag", page)
        self.assertIn("Visa färre förstadrag", page)
        self.assertIn('toggle.setAttribute("aria-expanded", String(firstMovesExpanded))', page)
        self.assertIn("positions.slice(0, 5)", page)
        self.assertIn("Visa alla ${positions.length} ställningar", page)
        self.assertIn("Visa färre ställningar", page)
        self.assertIn('toggle.setAttribute("aria-expanded", String(expanded))', page)
        self.assertIn("Ställningar att granska", page)
        self.assertIn("Ställningar med starka resultat", page)
        self.assertIn("review-list", page)
        self.assertIn("strength-list", page)
        self.assertIn("decidedGames >= 10", page)
        self.assertIn("playerScore < lowerThreshold", page)
        self.assertIn("isReviewPosition && !hasReviewAncestor", page)
        self.assertIn("hasReviewAncestor || isReviewPosition", page)
        self.assertIn("playerScore > upperThreshold", page)
        self.assertIn("moveDepth < 3", page)
        self.assertIn("Begär granskning", page)
        self.assertIn("${ownMove.games}/${step.position_games} partier", page)
        self.assertIn("ownMove.frequency).toFixed(1)} %)", page)
        self.assertIn("renderBranchMasterSummary(step)", page)
        self.assertIn('summary.append(document.createTextNode("Masters: "))', page)
        self.assertIn('"vanligast och bäst poäng "', page)
        self.assertIn('"bäst poäng "', page)
        self.assertIn('https://lichess.org/analysis/standard/${positionFen.replace(/ /g, "_")}', page)
        self.assertIn('link.className = "review-analysis-link"', page)
        self.assertIn('link.textContent = "Analyse"', page)
        self.assertIn("color: #8fc5ff", page)
        self.assertIn("text-decoration: underline", page)
        self.assertIn("analysisLine.append(lichessAnalysisLink(step.position_fen))", page)
        self.assertIn('link.rel = "noopener noreferrer"', page)
        self.assertIn('target = "_blank"', page)
        self.assertIn("reviewMovePreview(`${move.san} (${move.games})`, move.uci)", page)
        self.assertIn('id="review-next"', page)
        self.assertIn('id="review-previous"', page)
        self.assertIn(".primary:disabled:hover", page)
        self.assertIn("Välj en fortsättning ovan för att gå vidare.", page)
        self.assertNotIn("review-step-count", page)
        self.assertIn("advancePastSelectedBranch", page)
        self.assertIn("selectedBranchIndex + 1", page)
        self.assertIn("reviewSession.advancePastSelectedBranch = true", page)
        self.assertIn(".board-panel.review-active", page)
        self.assertIn(".review-active .board-content", page)
        self.assertIn("align-self: start", page)
        self.assertIn("scoreClassForPercentage(score)", page)
        self.assertIn("baseline - 5", page)
        self.assertIn("baseline + 5", page)
        self.assertIn('id="review-flow"', page)
        self.assertIn("boardPanel.classList.add(\"review-active\")", page)
        self.assertIn("node.children.find((candidate) => candidate.move_uci === moveUci)", page)
        self.assertIn("review-threshold", page)
        self.assertIn("baseline - 10", page)
        self.assertIn("100 partier", page)
        self.assertIn("formatMove(path[index].fen, move.move_san, false)", page)
        self.assertNotIn("vinster ·", page)
        self.assertNotIn('id="opening-list"', page)
        self.assertIn("const pieceGlyphs", page)

    def test_payload_validates_frequency_cutoff(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive integer"):
            build_repertoire_payload(self.database, "Alice", "white", min_repertoire_games=0)

    def test_reload_watches_application_web_and_opening_data(self) -> None:
        watched_files = {Path(path).name for path, _, _ in _source_snapshot()}

        self.assertIn("server.py", watched_files)
        self.assertIn("index.html", watched_files)
        self.assertIn("a.tsv", watched_files)

    def test_api_defaults_to_two_games_per_branch(self) -> None:
        with SQLiteGameRepository(self.database) as repository:
            repository.save_game(_game("d4-one", result="1-0", moves=("d2d4",)))
            repository.save_game(_game("d4-two", result="0-1", moves=("d2d4",)))
            repository.save_game(_game("c4-one", moves=("c2c4",)))

        with urlopen(f"{self.base_url}/api/repertoire?username=Alice&color=white") as response:
            payload = json.loads(response.read())

        self.assertEqual(
            [child["move_san"] for child in payload["root"]["children"]],
            ["e4", "d4"],
        )

    def test_import_api_imports_games_and_returns_summary(self) -> None:
        body = json.dumps(
            {
                "username": "NewPlayer",
                "max_games": 1000,
                "perf_types": ["blitz", "rapid"],
            }
        ).encode("utf-8")
        request = Request(
            f"{self.base_url}/api/import",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with patch(
            "repertoire_app.web.server.GameImporter.import_user",
            side_effect=lambda username, *, max_games, perf_types, progress_callback: (
                progress_callback(1),
                ImportSummary(processed=5, imported=3, already_present=2),
            )[1],
        ) as import_user:
            with urlopen(request) as response:
                events = [
                    json.loads(line)
                    for line in response.read().decode("utf-8").splitlines()
                ]

        self.assertEqual(events[0], {"type": "progress", "processed": 1})
        payload = events[-1]
        self.assertEqual(payload["type"], "complete")
        self.assertEqual(payload["username"], "NewPlayer")
        self.assertEqual(payload["processed"], 5)
        self.assertEqual(payload["imported"], 3)
        self.assertEqual(payload["already_present"], 2)
        self.assertEqual(import_user.call_args.args[0], "NewPlayer")
        self.assertEqual(import_user.call_args.kwargs["max_games"], 1000)
        self.assertEqual(import_user.call_args.kwargs["perf_types"], ["blitz", "rapid"])

    def test_import_api_rejects_invalid_game_limit(self) -> None:
        body = json.dumps({"username": "NewPlayer", "max_games": 1001}).encode("utf-8")
        request = Request(
            f"{self.base_url}/api/import",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request)

        self.assertEqual(error.exception.code, 400)
        self.assertIn("1 to 1000", json.loads(error.exception.read())["error"])

    def test_import_api_rejects_empty_or_unknown_performance_filters(self) -> None:
        for perf_types in ([], ["classical"]):
            body = json.dumps(
                {"username": "NewPlayer", "perf_types": perf_types}
            ).encode("utf-8")
            request = Request(
                f"{self.base_url}/api/import",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.subTest(perf_types=perf_types), self.assertRaises(HTTPError) as error:
                urlopen(request)
            self.assertEqual(error.exception.code, 400)
            self.assertIn("perf_types", json.loads(error.exception.read())["error"])

    def test_import_api_reports_lichess_errors(self) -> None:
        body = json.dumps({"username": "NewPlayer"}).encode("utf-8")
        request = Request(
            f"{self.base_url}/api/import",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with patch(
            "repertoire_app.web.server.GameImporter.import_user",
            side_effect=LichessAPIError("Lichess is unavailable"),
        ):
            with urlopen(request) as response:
                event = json.loads(response.read().decode("utf-8"))

        self.assertEqual(event["type"], "error")
        self.assertIn("Lichess is unavailable", event["error"])

    def test_review_api_uses_selected_line_and_returns_master_suggestions(self) -> None:
        with SQLiteGameRepository(self.database) as repository:
            repository.save_game(_game("review-line", moves=("e2e4", "e7e5", "g1f3")))

        body = json.dumps(
            {
                "username": "Alice",
                "color": "white",
                "path_uci": ["e2e4", "e7e5"],
            }
        ).encode("utf-8")
        request = Request(
            f"{self.base_url}/api/review",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        master_data = MasterPositionData(
            stats=MasterStats(white_wins=40, draws=20, black_wins=40),
            move_stats={
                "g1f3": MasterStats(white_wins=70, draws=20, black_wins=10),
            },
        )
        with (
            patch.dict("os.environ", {"LICHESS_API_TOKEN": "test-token"}),
            patch(
                "repertoire_app.web.server.LichessMastersClient.get_position_stats",
                return_value=master_data,
            ) as get_position_stats,
            urlopen(request) as response,
        ):
            payload = json.loads(response.read())

        self.assertEqual(payload["steps"][0]["type"], "branch")
        self.assertEqual(payload["steps"][0]["master_recommendation"]["san"], "Nf3")
        get_position_stats.assert_called_once()

    def test_review_api_reports_missing_lichess_token(self) -> None:
        body = json.dumps(
            {
                "username": "Alice",
                "color": "white",
                "path_uci": ["e2e4", "e7e5"],
            }
        ).encode("utf-8")
        request = Request(
            f"{self.base_url}/api/review",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(HTTPError) as error:
                urlopen(request)

        self.assertEqual(error.exception.code, 502)
        self.assertIn("LICHESS_API_TOKEN", json.loads(error.exception.read())["error"])


if __name__ == "__main__":
    unittest.main()
