"""Serve a local interactive repertoire explorer."""

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
import webbrowser
from collections.abc import Sequence
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from repertoire_app.lichess.client import (
    SUPPORTED_PERF_TYPES,
    LichessAPIError,
    LichessClient,
)
from repertoire_app.lichess.importer import GameImporter
from repertoire_app.master_data.client import LichessMastersClient, MasterDataAPIError
from repertoire_app.master_data.reviewer import RepertoireReviewer
from repertoire_app.pgn.parser import PGNImportError
from repertoire_app.repertoire.builder import (
    PlayerColor,
    RepertoireBuildError,
    RepertoireBuilder,
)
from repertoire_app.storage.repository import SQLiteGameRepository


def build_repertoire_payload(
    database: str,
    username: str,
    color: str,
    min_repertoire_games: int = 2,
) -> dict[str, object]:
    """Build the locally filtered tree and return its JSON representation."""
    with SQLiteGameRepository(database) as repository:
        tree = RepertoireBuilder(repository).build(
            username,
            PlayerColor(color),
            min_repertoire_games=min_repertoire_games,
        )
        return tree.to_dict()


def build_review_payload(
    database: str,
    username: str,
    color: str,
    path_uci: list[str],
    choices_uci: list[str],
) -> dict[str, object]:
    """Review one selected line using cached and on-demand Masters data."""
    token = os.environ.get("LICHESS_API_TOKEN")
    if not token:
        raise MasterDataAPIError(
            "Set the LICHESS_API_TOKEN environment variable to request a review."
        )
    with SQLiteGameRepository(database) as repository:
        tree = RepertoireBuilder(repository).build(
            username,
            PlayerColor(color),
            min_repertoire_games=1,
        )
        return RepertoireReviewer(
            LichessMastersClient(token=token),
            repository,
        ).review(tree, path_uci, choices_uci)


def create_handler(database: str) -> type[BaseHTTPRequestHandler]:
    """Create an HTTP handler bound to the selected local database."""
    index_html = files("repertoire_app.web").joinpath("static", "index.html").read_bytes()

    class RepertoireRequestHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            request = urlparse(self.path)
            if request.path == "/":
                self._send_response(
                    HTTPStatus.OK,
                    index_html,
                    "text/html; charset=utf-8",
                )
                return

            if request.path != "/api/repertoire":
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                return

            query = parse_qs(request.query)
            username = query.get("username", [""])[0].strip()
            color = query.get("color", ["white"])[0]
            min_games_text = query.get("min_games", ["2"])[0]
            if not username:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "Enter a Lichess username."},
                )
                return
            if color not in {PlayerColor.WHITE.value, PlayerColor.BLACK.value}:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "Color must be white or black."},
                )
                return
            try:
                min_games = int(min_games_text)
                if min_games <= 0:
                    raise ValueError
                payload = build_repertoire_payload(
                    database,
                    username,
                    color,
                    min_repertoire_games=min_games,
                )
            except RepertoireBuildError as error:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": str(error)})
                return
            except ValueError as error:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": f"Invalid request: {error}"},
                )
                return
            except sqlite3.Error as error:
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": f"Could not read the repertoire database: {error}"},
                )
                return

            self._send_json(HTTPStatus.OK, payload)

        def do_POST(self) -> None:
            request_path = urlparse(self.path).path
            if request_path == "/api/review":
                self._handle_review()
                return
            if request_path != "/api/import":
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                return

            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid content length."})
                return
            if content_length <= 0 or content_length > 4096:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "Request body must be between 1 and 4096 bytes."},
                )
                return

            try:
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"Invalid JSON: {error}"})
                return
            if not isinstance(payload, dict):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "Request body must be a JSON object."},
                )
                return

            username = payload.get("username")
            max_games = payload.get("max_games", 100)
            perf_types = payload.get("perf_types", list(SUPPORTED_PERF_TYPES))
            if not isinstance(username, str) or not username.strip():
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "Enter a Lichess username."},
                )
                return
            if (
                isinstance(max_games, bool)
                or not isinstance(max_games, int)
                or not 1 <= max_games <= 1000
            ):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "max_games must be an integer from 1 to 1000."},
                )
                return
            if (
                not isinstance(perf_types, list)
                or not perf_types
                or any(not isinstance(perf_type, str) for perf_type in perf_types)
                or len(set(perf_types)) != len(perf_types)
                or any(
                    perf_type not in SUPPORTED_PERF_TYPES
                    for perf_type in perf_types
                )
            ):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "error": (
                            "perf_types must include at least one of: "
                            f"{', '.join(SUPPORTED_PERF_TYPES)}."
                        )
                    },
                )
                return

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

            def send_event(event: dict[str, object]) -> None:
                body = json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n"
                self.wfile.write(body)
                self.wfile.flush()

            try:
                with SQLiteGameRepository(database) as repository:
                    summary = GameImporter(LichessClient(), repository).import_user(
                        username.strip(),
                        max_games=max_games,
                        perf_types=perf_types,
                        progress_callback=lambda processed: send_event(
                            {"type": "progress", "processed": processed}
                        ),
                    )
            except (LichessAPIError, PGNImportError) as error:
                send_event({"type": "error", "error": f"Import failed: {error}"})
                return
            except sqlite3.Error as error:
                send_event(
                    {"type": "error", "error": f"Could not write imported games: {error}"}
                )
                return
            except ValueError as error:
                send_event({"type": "error", "error": str(error)})
                return

            send_event(
                {
                    "type": "complete",
                    "username": username.strip(),
                    "processed": summary.processed,
                    "imported": summary.imported,
                    "already_present": summary.already_present,
                    "evicted_username": summary.evicted_username,
                }
            )

        def _handle_review(self) -> None:
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid content length."})
                return
            if content_length <= 0 or content_length > 8192:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "Request body must be between 1 and 8192 bytes."},
                )
                return
            try:
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"Invalid JSON: {error}"})
                return
            if not isinstance(payload, dict):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "Request body must be a JSON object."},
                )
                return

            username = payload.get("username")
            color = payload.get("color")
            path_uci = payload.get("path_uci")
            choices_uci = payload.get("choices_uci", [])
            if not isinstance(username, str) or not username.strip():
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "Enter a Lichess username."},
                )
                return
            if color not in {PlayerColor.WHITE.value, PlayerColor.BLACK.value}:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "Color must be white or black."},
                )
                return
            if (
                not isinstance(path_uci, list)
                or not path_uci
                or len(path_uci) > 100
                or any(not isinstance(move, str) or len(move) > 10 for move in path_uci)
                or not isinstance(choices_uci, list)
                or len(choices_uci) > 20
                or any(not isinstance(move, str) or len(move) > 10 for move in choices_uci)
            ):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "error": (
                            "path_uci must contain between 1 and 100 UCI moves, "
                            "and choices_uci may contain at most 20 UCI moves."
                        )
                    },
                )
                return

            try:
                result = build_review_payload(
                    database,
                    username.strip(),
                    color,
                    path_uci,
                    choices_uci,
                )
            except (RepertoireBuildError, ValueError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            except MasterDataAPIError as error:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
                return
            except sqlite3.Error as error:
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": f"Could not read the review database: {error}"},
                )
                return

            self._send_json(HTTPStatus.OK, result)

        def log_message(self, format: str, *args: object) -> None:
            print(f"web: {format % args}")

        def _send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send_response(status, body, "application/json; charset=utf-8")

        def _send_response(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return RepertoireRequestHandler


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Explore an imported repertoire in your browser."
    )
    parser.add_argument(
        "--database",
        default="repertoire.sqlite3",
        help="SQLite database path (default: repertoire.sqlite3)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        choices=("127.0.0.1", "localhost"),
        help="local interface to bind (default: 127.0.0.1)",
    )
    parser.add_argument("--port", type=_port_number, default=8765)
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="do not open the browser automatically",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="restart when application source files change (development only)",
    )
    arguments = parser.parse_args(argv)
    server = ThreadingHTTPServer(
        (arguments.host, arguments.port),
        create_handler(arguments.database),
    )
    url = f"http://{arguments.host}:{server.server_port}/"
    print(f"Repertoire explorer running at {url}. Press Ctrl+C to stop.")
    if arguments.reload:
        snapshot = _source_snapshot()
        threading.Thread(
            target=_restart_on_source_change,
            args=(snapshot,),
            daemon=True,
        ).start()
    if not arguments.no_browser and "REPERTOIRE_WEB_RELOADED" not in os.environ:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping repertoire explorer.")
    finally:
        server.server_close()
    return 0


def _source_snapshot() -> tuple[tuple[str, int, int], ...]:
    package_root = Path(__file__).resolve().parents[1]
    snapshot = []
    for path in sorted(package_root.rglob("*")):
        if path.suffix not in {".py", ".html", ".tsv"}:
            continue
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        snapshot.append((str(path), stat.st_mtime_ns, stat.st_size))
    return tuple(snapshot)


def _restart_on_source_change(snapshot: tuple[tuple[str, int, int], ...]) -> None:
    while True:
        time.sleep(0.5)
        current_snapshot = _source_snapshot()
        if current_snapshot == snapshot:
            continue

        print("Application files changed; restarting repertoire-web.")
        environment = os.environ.copy()
        environment["REPERTOIRE_WEB_RELOADED"] = "1"
        arguments = [
            sys.executable,
            "-m",
            "repertoire_app.web.server",
            *sys.argv[1:],
        ]
        try:
            os.execve(sys.executable, arguments, environment)
        except OSError as error:
            print(f"Could not restart repertoire-web: {error}", file=sys.stderr, flush=True)
            snapshot = current_snapshot


def _port_number(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if not 0 < port < 65536:
        raise argparse.ArgumentTypeError("must be between 1 and 65535")
    return port


if __name__ == "__main__":
    raise SystemExit(main())
