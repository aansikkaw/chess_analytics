"""Build the sample report card shown on the landing page (app/static/sample-report.json).

It comes from one of the synthetic demo games (sample_data/demo_games.pgn), so no real player appears on the
landing page. Run it again after changing the report format:

    python scripts/make_sample_report.py [--depth 14]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import review, review_ai  # noqa: E402
from app.analysis import analyse_game  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.engine import Engine  # noqa: E402
from app.games import parse_pgn  # noqa: E402

HANDLE = "demo_player"
MOVE_KEYS = ("ply", "move_number", "color", "san", "uci", "best", "best_san", "class", "loss", "win_before", "win_after", "eval_white", "fen")
MOMENT_KEYS = ("ply", "color", "san", "best_san", "class", "loss", "fen", "uci", "best", "label", "line", "explain")


def pick(games, engine):
    """The decisive game, 25-50 moves long, with the demo player's single costliest mistake (analysed)."""
    best = None
    for g in games:
        if g.result not in ("1-0", "0-1") or not 25 <= len(g.moves) // 2 <= 50:
            continue
        a = analyse_game(g, engine)
        worst = max((m.win_loss for m in a.moves if m.classification in ("mistake", "blunder")), default=0)
        if worst and (best is None or worst > best[0]):
            best = (worst, a)
    return best[1] if best else analyse_game(games[0], engine)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build the landing page's sample report card.")
    ap.add_argument("--depth", type=int, default=14)
    args = ap.parse_args(argv)
    settings = load_settings()
    games = parse_pgn((ROOT / "sample_data" / "demo_games.pgn").read_text(), HANDLE)
    engine = Engine(settings.stockfish_path, depth=args.depth)
    try:
        g = pick(games, engine)
        rep = review.build_report(review.game_input(HANDLE, g), engine, args.depth)
    finally:
        engine.close()
    ai = review_ai.builtin(rep)
    out = {
        "white": rep["white"], "black": rep["black"], "result": rep["result"], "you": rep["you"], "depth": rep["depth"],
        "opening": rep.get("opening"), "verdict": rep["verdict"], "series": [round(v, 1) for v in rep["series"]],
        "sides": {c: {k: rep["sides"][c][k] for k in ("accuracy", "grade", "acpl", "moves")} for c in ("white", "black")},
        "moves": [{k: m[k] for k in MOVE_KEYS} for m in rep["moves"]],
        "key_moments": [{k: km.get(k) for k in MOMENT_KEYS} for km in rep["key_moments"]],
        "final_fen": rep.get("final_fen"), "summary": ai.get("summary"),
    }
    path = ROOT / "app" / "static" / "sample-report.json"
    path.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"Wrote {path.relative_to(ROOT)}: {rep['white']} vs {rep['black']} {rep['result']}, {len(rep['moves'])} plies, "
          f"{len(rep['key_moments'])} key moments, {path.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
