"""Review a selected repertoire line against Lichess Masters statistics."""

from typing import TypeAlias

import chess

from repertoire_app.master_data.client import LichessMastersClient, MasterDataAPIError
from repertoire_app.master_data.models import MasterPositionData, MasterStats
from repertoire_app.repertoire.builder import (
    PlayerColor,
    PlayerResults,
    RepertoireNode,
    RepertoireTree,
)
from repertoire_app.storage.repository import SQLiteGameRepository

_ReviewStep: TypeAlias = dict[str, object]
MIN_MASTER_GAMES = 100
REPERTOIRE_FREQUENCY = 0.75
MAX_REVIEW_STEPS = 20


class RepertoireReviewer:
    """Compare a player's line with master moves, only when requested."""

    def __init__(
        self,
        client: LichessMastersClient,
        repository: SQLiteGameRepository,
    ) -> None:
        self.client = client
        self.repository = repository

    def review(
        self,
        tree: RepertoireTree,
        path_uci: list[str],
        choices_uci: list[str] | None = None,
    ) -> dict[str, object]:
        """Review one selected position and follow the player's repertoire."""
        node = tree.root
        board = chess.Board(node.fen)
        line: list[str] = []
        line_uci: list[str] = []
        for move_uci in path_uci:
            child = node.children.get(move_uci)
            if child is None:
                raise ValueError("The selected line is not present in the player's games.")
            move = chess.Move.from_uci(move_uci)
            if not board.is_legal(move):
                raise ValueError("The selected line contains an illegal move.")
            line.append(_line_move(board, move))
            line_uci.append(move_uci)
            board.push(move)
            node = child

        selected_choices = choices_uci or []
        branch_index = 0
        steps: list[_ReviewStep] = []
        stopped_reason = "No further player moves are available in the imported games."
        for _ in range(MAX_REVIEW_STEPS):
            board = chess.Board(node.fen)
            opponent_plies = 0
            while board.turn != tree.color.chess_color:
                opponent_move = _most_played_child(node)
                if opponent_move is None:
                    return self._result(line, steps, stopped_reason)
                move = chess.Move.from_uci(opponent_move.move_uci or "")
                if not board.is_legal(move):
                    raise ValueError("The stored line contains an illegal opponent move.")
                line.append(_line_move(board, move))
                line_uci.append(opponent_move.move_uci or "")
                board.push(move)
                node = opponent_move
                opponent_plies += 1
                if opponent_plies > 1:
                    raise ValueError("Could not reach the player's turn in the stored line.")

            choices = [
                child for child in node.children.values()
                if child.played_by == "player"
            ]
            if not choices:
                break

            data = self._master_data(node)
            master_moves = _master_move_scores(data, board, tree.color)
            best_master_move = max(
                (move for move in master_moves if move["games"] >= MIN_MASTER_GAMES),
                key=lambda move: (move["score"], move["games"]),
                default=None,
            )
            highest_frequency = max(
                (move["games"] for move in master_moves),
                default=0,
            )
            master_most_played_moves = [
                move for move in master_moves
                if move["games"] == highest_frequency and highest_frequency > 0
            ]
            player_moves = [_player_move_summary(child, node, tree.color) for child in choices]
            repertoire_move = max(choices, key=lambda child: child.games)
            repertoire_frequency = repertoire_move.games / node.games if node.games else 0

            if repertoire_frequency < REPERTOIRE_FREQUENCY:
                best_player_move = max(
                    player_moves,
                    key=lambda move: (
                        move["score"] if isinstance(move["score"], (int, float)) else -1,
                        move["games"],
                    ),
                )
                branch_step: _ReviewStep = {
                    "line": " ".join(line),
                    "position_fen": node.fen,
                    "position_games": node.games,
                    "path_uci": list(line_uci),
                    "type": "branch",
                    "branch_index": branch_index,
                    "player_moves": player_moves,
                    "best_player_move": best_player_move,
                    "master_most_played_moves": master_most_played_moves,
                    "master_recommendation": best_master_move,
                    "minimum_master_games": MIN_MASTER_GAMES,
                }
                selected_move = None
                if branch_index < len(selected_choices):
                    selected_uci = selected_choices[branch_index]
                    selected_child = next(
                        (child for child in choices if child.move_uci == selected_uci),
                        None,
                    )
                    if selected_child is None:
                        raise ValueError("The selected review continuation is not in the player's games.")
                    selected_move = _player_move_summary(selected_child, node, tree.color)
                    branch_step["selected_move"] = selected_move
                steps.append(branch_step)
                branch_index += 1
                if selected_move is None:
                    stopped_reason = (
                        "Review stopped at this branch because no single move represents "
                        "the player's repertoire."
                    )
                    break
                selected_uci = selected_move["uci"]
                if not isinstance(selected_uci, str):
                    raise ValueError("The selected review continuation is invalid.")
                selected_child = node.children[selected_uci]
                move = chess.Move.from_uci(selected_uci)
                if not board.is_legal(move):
                    raise ValueError("The selected review continuation is illegal.")
                line.append(_line_move(board, move))
                line_uci.append(selected_uci)
                board.push(move)
                node = selected_child
                continue

            repertoire_summary = _player_move_summary(
                repertoire_move,
                node,
                tree.color,
            )
            selected_master_move = next(
                (
                    move for move in master_moves
                    if move["uci"] == repertoire_move.move_uci
                ),
                None,
            )
            steps.append(
                {
                    "line": " ".join(line),
                    "position_fen": node.fen,
                    "position_games": node.games,
                    "path_uci": list(line_uci),
                    "type": "repertoire",
                    "repertoire_move": repertoire_summary,
                    "master_move": selected_master_move,
                    "master_most_played_moves": master_most_played_moves,
                    "master_recommendation": best_master_move,
                    "minimum_master_games": MIN_MASTER_GAMES,
                }
            )
            move = chess.Move.from_uci(repertoire_move.move_uci or "")
            if not board.is_legal(move):
                raise ValueError("The stored repertoire contains an illegal move.")
            line.append(_line_move(board, move))
            line_uci.append(repertoire_move.move_uci or "")
            board.push(move)
            node = repertoire_move
        else:
            stopped_reason = f"Review stopped after {MAX_REVIEW_STEPS} player decisions."

        return self._result(line, steps, stopped_reason)

    def _master_data(self, node: RepertoireNode) -> MasterPositionData:
        cached = self.repository.get_master_data(node.position_key)
        if cached is not None and cached.move_stats is not None:
            return cached
        data = self.client.get_position_stats(node.fen)
        self.repository.save_master_data(node.position_key, data)
        return data

    @staticmethod
    def _result(
        line: list[str],
        steps: list[_ReviewStep],
        stopped_reason: str,
    ) -> dict[str, object]:
        return {
            "line": " ".join(line),
            "steps": steps,
            "stopped_reason": stopped_reason,
        }


def _most_played_child(node: RepertoireNode) -> RepertoireNode | None:
    if not node.children:
        return None
    return max(node.children.values(), key=lambda child: (child.games, child.move_san or ""))


def _player_move_summary(
    child: RepertoireNode,
    parent: RepertoireNode,
    color: PlayerColor,
) -> dict[str, object]:
    score = _score_percentage(child.results, color)
    return {
        "uci": child.move_uci,
        "san": child.move_san,
        "games": child.games,
        "frequency": child.games / parent.games if parent.games else 0,
        "score": score,
        "decided_games": child.results.decided_games,
    }


def _master_move_scores(
    data: MasterPositionData,
    board: chess.Board,
    color: PlayerColor,
) -> list[dict[str, object]]:
    if data.move_stats is None:
        return []
    moves = []
    for move_uci, stats in data.move_stats.items():
        if stats.total_games <= 0:
            raise MasterDataAPIError(
                f"Lichess Masters API returned no games for move {move_uci!r}"
            )
        try:
            move = chess.Move.from_uci(move_uci)
        except ValueError as error:
            raise MasterDataAPIError(
                f"Lichess Masters API returned an invalid UCI move {move_uci!r}"
            ) from error
        if not board.is_legal(move):
            raise MasterDataAPIError(
                f"Lichess Masters API returned an illegal move {move_uci!r}"
            )
        moves.append(
            {
                "uci": move_uci,
                "san": board.san(move),
                "games": stats.total_games,
                "score": _master_score_percentage(stats, color),
            }
        )
    return moves


def _score_percentage(results: PlayerResults, color: PlayerColor) -> float | None:
    decided_games = results.decided_games
    if not decided_games:
        return None
    points = (
        results.white_points
        if color is PlayerColor.WHITE
        else decided_games - results.white_points
    )
    return 100 * points / decided_games


def _master_score_percentage(stats: MasterStats, color: PlayerColor) -> float:
    points = (
        stats.white_wins + stats.draws / 2
        if color is PlayerColor.WHITE
        else stats.black_wins + stats.draws / 2
    )
    return 100 * points / stats.total_games


def _line_move(board: chess.Board, move: chess.Move) -> str:
    san = board.san(move)
    if board.turn == chess.WHITE:
        return f"{board.fullmove_number}. {san}"
    return san
