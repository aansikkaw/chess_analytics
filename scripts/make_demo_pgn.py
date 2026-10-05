"""Generate synthetic demo games for `demo_player` so the app works offline.

The demo player is Stockfish limited to ~1800 Elo, with two built-in weaknesses
so the Rating DNA has something real to find:
  * in endgames it plays at ~1400 strength;
  * when its clock drops below 60 seconds it plays at the engine's floor (1320).

Usage:  python scripts/make_demo_pgn.py [n_games] [out_path]
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import chess
import chess.engine
import chess.pgn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.analysis import game_phase  # noqa: E402
from app.config import load_settings  # noqa: E402

PLAYER = "demo_player"
OPPONENTS = ["rook_lifter", "knightowl88", "caro_kanntrol", "zugzwang_zoe", "pawnstorm_ravi", "endgame_eli"]
INITIAL, INCREMENT = 900, 10  # 15+10 rapid
MAX_PLIES = 150


def play_game(engine: chess.engine.SimpleEngine, rng: random.Random, idx: int) -> chess.pgn.Game:
    player_white = idx % 2 == 0
    opp_elo = rng.choice([1700, 1750, 1800, 1850, 1900])
    board = chess.Board()
    game = chess.pgn.Game()
    node = game
    clocks = {chess.WHITE: float(INITIAL), chess.BLACK: float(INITIAL)}

    # A few varied openings so opening stats aren't all identical.
    opening, sans = rng.choice([
        ("Caro-Kann Defense", ["e4", "c6"]), ("Indian Defense", ["d4", "Nf6"]),
        ("King's Knight Opening", ["e4", "e5", "Nf3"]), ("English Opening", ["c4"]),
        ("Sicilian Defense", ["e4", "c5"]), ("Queen's Gambit", ["d4", "d5", "c4"]),
    ])
    for san in sans:
        move = board.parse_san(san)
        board.push(move)
        node = node.add_variation(move)
        clocks[not board.turn] -= rng.uniform(2, 6)
        clocks[not board.turn] += INCREMENT
        node.set_clock(clocks[not board.turn])

    while not board.is_game_over() and board.ply() < MAX_PLIES:
        is_player = (board.turn == chess.WHITE) == player_white
        if is_player:
            if clocks[board.turn] < 60:
                elo = 1320
            elif game_phase(board) == "endgame":
                elo = 1400
            else:
                elo = 1800
            spent = rng.uniform(5, 45) if board.ply() < 60 else rng.uniform(10, 70)
        else:
            elo = opp_elo
            spent = rng.uniform(4, 25)
        engine.configure({"UCI_LimitStrength": True, "UCI_Elo": elo})
        move = engine.play(board, chess.engine.Limit(time=0.02)).move
        clocks[board.turn] = max(1.0, clocks[board.turn] - min(spent, clocks[board.turn] - 1)) + INCREMENT
        node = node.add_variation(move)
        node.set_clock(round(clocks[board.turn], 1))
        board.push(move)

    if board.is_game_over():
        result = board.result()
    else:  # adjudicate long games with a full-strength look
        engine.configure({"UCI_LimitStrength": False})
        cp = engine.analyse(board, chess.engine.Limit(depth=12))["score"].white().score(mate_score=10000)
        result = "1-0" if cp > 250 else "0-1" if cp < -250 else "1/2-1/2"

    white, black = (PLAYER, OPPONENTS[idx % len(OPPONENTS)]) if player_white else (OPPONENTS[idx % len(OPPONENTS)], PLAYER)
    game.headers.update({
        "Event": "Rated rapid game", "Site": f"https://example.org/demo{idx:03d}",
        "Date": f"2026.09.{1 + idx % 28:02d}", "UTCDate": f"2026.09.{1 + idx % 28:02d}",
        "White": white, "Black": black, "Result": result,
        "WhiteElo": str(1850 if player_white else opp_elo), "BlackElo": str(opp_elo if player_white else 1850),
        "TimeControl": f"{INITIAL}+{INCREMENT}", "Opening": opening,
    })
    return game


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).resolve().parent.parent / "sample_data" / "demo_games.pgn"
    path = load_settings().stockfish_path
    if not path:
        sys.exit("Stockfish not found; set STOCKFISH_PATH.")
    rng = random.Random(7)
    engine = chess.engine.SimpleEngine.popen_uci(path)
    try:
        games = [play_game(engine, rng, i) for i in range(n)]
    finally:
        engine.quit()
    out.write_text("\n\n".join(str(g) for g in games) + "\n")
    print(f"Wrote {n} games to {out}")


if __name__ == "__main__":
    main()
