"""Check that the Opponent Twin learns what it should: a parameter-recovery test with known "personas".

Each persona is a player whose decision weights we set by hand (a trader, an attacker, a player who is weak in
endgames). The persona plays games against Stockfish; a Twin is then built from those games alone, exactly
as for a real opponent. A working method recovers the persona's character (the right style weights move in
the right direction) and predicts the persona's moves better than Stockfish does.

  python scripts/twin_validate.py                 all personas, 30 games each (about 5 minutes)
  python scripts/twin_validate.py trader 20       one persona, 20 games

This validates the fitting machinery. Real players are messier than personas; the Twin's faithfulness
score on each real opponent (shown in the app) is the honest measure there.
"""

from __future__ import annotations

import math
import random
import sys
import tempfile
import time
from pathlib import Path

import chess
import chess.engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import twin  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.engine import Engine  # noqa: E402
from app.games import GameRecord  # noqa: E402
from app.library import Library  # noqa: E402
from app.store import Store  # noqa: E402


def persona(name: str) -> tuple[list[float], dict[str, str]]:
    b = twin.prior(1600)
    ix = {n: k for k, n in enumerate(twin.FEATURES)}
    expect: dict[str, str] = {}
    if name == "trader":
        b[ix["trade"]] += 2.0
        b[ix["capture"]] += 1.0
        expect = {"trade": "+", "capture": "+"}
    elif name == "attacker":
        b[ix["check"]] += 1.5
        b[ix["king_hunt"]] += 2.5
        expect = {"check": "+", "king_hunt": "+"}
    elif name == "endgame_weak":
        b[ix["loss_endgame"]] = 0.4
        b[ix["loss_middlegame"]] = 2.6
        expect = {"endgame_vs_middlegame": "-"}
    else:
        raise SystemExit(f"Unknown persona {name}")
    return b, expect


def play(store, beta: list[float], engine: Engine, persona_white: bool, rng: random.Random, depth: int) -> GameRecord:
    board = chess.Board()
    me = {"player_id": -1, "params": beta}
    prev_to = opp_to = None
    while not board.is_game_over(claim_draw=True) and len(board.move_stack) < 120:
        if board.turn == (chess.WHITE if persona_white else chess.BLACK):
            pick = twin.choose(store, me, board, engine, prev_to=prev_to, opp_to=opp_to, rng=rng, depth=depth)
            mv = chess.Move.from_uci(pick["uci"])
            prev_to = mv.to_square
        else:  # opponent: a decent engine that varies its moves
            cands = engine.candidates(board, 3, chess.engine.Limit(depth=6))
            w = [math.exp(cp / 60) for _, cp in cands]
            mv = chess.Move.from_uci(twin._weighted(rng, [u for u, _ in cands], w))
            opp_to = mv.to_square
        board.push(mv)
    result = board.result(claim_draw=True)
    if result == "*":
        result = "1/2-1/2"
    return GameRecord(game_id=f"g{rng.random():.12f}", white="persona" if persona_white else "engine",
                      black="engine" if persona_white else "persona", player_color=persona_white, player_rating=1600,
                      result=result, time_control="600+5", initial_seconds=600, opening="", played_at="2026.10.01",
                      moves=[m.uci() for m in board.move_stack], time_class="rapid")


def run(name: str, games: int) -> dict:
    s = load_settings()
    tmp = tempfile.mkdtemp(prefix="twin-validate-")
    store = Store(str(Path(tmp) / "v.db"))
    lib = Library(store)
    rng = random.Random(42)
    beta, expect = persona(name)
    t0 = time.time()
    with Engine(s.stockfish_path) as engine:
        recs = [play(store, beta, engine, i % 2 == 0, rng, s.twin_depth) for i in range(games)]
        p = lib.get_or_create_player("pgn", f"{name}-persona", owner_id=1)
        lib.add_games(p["id"], recs, requested=games)
        t1 = time.time()
        tw = twin.build(store, lib, p["id"], None, engine, max_games=games, depth=s.twin_depth)
    fitted = tw["params"]
    ix = {n: k for k, n in enumerate(twin.FEATURES)}
    prior = twin.prior(1600)
    checks = {}
    for feat, sign in expect.items():
        if feat == "endgame_vs_middlegame":
            ok = fitted[ix["loss_endgame"]] < fitted[ix["loss_middlegame"]]
            checks[feat] = (ok, round(fitted[ix["loss_endgame"]], 2), round(fitted[ix["loss_middlegame"]], 2))
        else:
            ok = (fitted[ix[feat]] - prior[ix[feat]]) > 0.3 if sign == "+" else (fitted[ix[feat]] - prior[ix[feat]]) < -0.3
            checks[feat] = (ok, round(fitted[ix[feat]], 2), round(beta[ix[feat]], 2))
    f = tw["profile"]["faithfulness"]
    return {"persona": name, "games": games, "decisions": tw["decisions"], "play_s": round(t1 - t0), "build_s": round(time.time() - t1),
            "recovered": checks, "twin_top1": f["twin_top1"], "engine_top1": f["engine_top1"], "twin_top3": f["twin_top3"],
            "engine_top3": f["engine_top3"], "style": [x["text"] for x in tw["profile"]["style"]]}


if __name__ == "__main__":
    names = [sys.argv[1]] if len(sys.argv) > 1 else ["trader", "attacker", "endgame_weak"]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    for nm in names:
        r = run(nm, n)
        print(f"\n{r['persona']}: {r['games']} games, {r['decisions']} decisions (play {r['play_s']}s, build {r['build_s']}s)")
        for feat, (ok, got, want) in r["recovered"].items():
            if feat == "endgame_vs_middlegame":
                print(f"  {'OK ' if ok else 'BAD'} endgame accuracy weight {got} below middlegame {want} (set 0.4 vs 2.6)")
            else:
                print(f"  {'OK ' if ok else 'BAD'} {feat}: fitted {got} (set {want})")
        print(f"  move match on held-out games: Twin {r['twin_top1']}% (top-3 {r['twin_top3']}%) vs Stockfish {r['engine_top1']}% "
              f"(top-3 {r['engine_top3']}%)")
        print(f"  style the Twin reports: {', '.join(r['style']) or '-'}")
