"""Pro analytics: repertoire leaks, recurring mistakes, progress, opponent scouting."""

from app.analysis import analyse_games
from app.games import parse_pgn
from app.progress import progress_report
from app.repertoire import fmt_line, recurring_mistakes, repertoire_report
from app.scout import scout_report
from tests.conftest import needs_engine


def _game(site: str, white: str, black: str, result: str, date: str, moves: str) -> str:
    return (f'[Site "https://lichess.org/{site}"]\n[White "{white}"]\n[Black "{black}"]\n[Result "{result}"]\n'
            f'[UTCDate "{date}"]\n[TimeControl "600+0"]\n[WhiteElo "1800"]\n[BlackElo "1800"]\n[Opening "Italian Game"]\n\n{moves} {result}\n\n')


# Alice (White) hangs her queen in the same position, the same way, three times.
BLUNDER = "1. e4 e5 2. Nf3 Nc6 3. Bc4 Nf6 4. Qe2 Bc5 5. Qd3 d6 6. Qe3 Bxe3 7. dxe3 O-O"
REPEATS = "".join(_game(f"rep{i:05d}", "alice", f"opp{i}", "0-1", f"2026.0{6 + i}.10", BLUNDER) for i in range(3))


def test_fmt_line():
    assert fmt_line(["e4", "e5", "Nf3"]) == "1.e4 e5 2.Nf3"


@needs_engine
def test_recurring_mistake_and_leak_detected(engine):
    games = analyse_games(parse_pgn(REPEATS, "alice"), engine)
    rec = recurring_mistakes(games)
    assert rec and rec[0]["times"] == 3, rec
    assert rec[0]["you_played"] in {"Qe3", "Qe2", "Qd3"} and len(rec[0]["opponents"]) == 3
    rep = repertoire_report(games)
    assert rep["white"] and rep["white"][0]["games"] == 3
    assert rep["leaks"], "three straight losses in one line must be a leak"
    leak = rep["leaks"][0]
    assert leak["score_pct"] == 0 and leak["line"].startswith("1.e4 e5")
    assert leak["costliest_move"] and leak["costliest_move"]["times_played"] == 3


@needs_engine
def test_progress_buckets_by_month(engine):
    pgn = REPEATS + "".join(_game(f"win{i:05d}", "alice", f"x{i}", "1-0", "2026.09.2" + str(i), BLUNDER) for i in range(3))
    games = analyse_games(parse_pgn(pgn, "alice"), engine)
    # One game in each of Jun/Jul/Aug (too few to bucket) and three in Sep.
    rep = progress_report(games)
    assert rep["granularity"] == "month"
    assert rep["buckets"][-1]["period"] == "2026-09" and rep["buckets"][-1]["score_pct"] == 100


def test_progress_needs_dates():
    assert progress_report([])["buckets"] == []


def _opp_games():
    """Bob's games: he loses with 1.e4 e5 2.Nf3 Nc6 as Black, wins with the Sicilian."""
    out = ""
    for i in range(4):
        out += _game(f"b{i:06d}", f"w{i}", "bob", "1-0", "2026.09.01", "1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 Nf6 5. O-O Be7")
    for i in range(3):
        out += _game(f"s{i:06d}", f"w{i}", "bob", "0-1", "2026.09.02", "1. e4 c5 2. Nf3 d6 3. d4 cxd4 4. Nxd4 Nf6 5. Nc3 a6")
    for i in range(3):
        out += _game(f"t{i:06d}", "bob", f"b{i}", "0-1", "2026.09.03", "1. d4 d5 2. c4 e6 3. Nc3 Nf6 4. Bg5 Be7 5. e3 O-O")
    return parse_pgn(out, "bob")


def test_scout_finds_weak_and_strong_lines():
    r = scout_report("bob", _opp_games(), engine=None)
    assert r["games"] == 10
    assert any(s["line"].startswith("1.e4 e5") and s["their_score_pct"] == 0 for s in r["steer_into"])
    assert any(a["line"].startswith("1.e4 c5") and a["their_score_pct"] == 100 for a in r["avoid"])
    assert r["as_white"][0]["line"].startswith("1.d4")
    assert any("Steer into" in t for t in r["insights"])


@needs_engine
def test_scout_prep_and_battleground(engine):
    me = analyse_games(parse_pgn("".join(_game(f"m{i:06d}", "alice", f"z{i}", "1-0", "2026.09.05",
                                               "1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 Nf6 5. O-O b5") for i in range(3)), "alice"), engine)
    r = scout_report("bob", _opp_games(), engine=engine, my_games=me)
    assert r["prep"] and all(p["eval_for_you"] for p in r["prep"])
    bg = r["battleground"]
    assert bg and bg[0]["you_play"] == "white" and bg[0]["line"].startswith("1.e4 e5") and bg[0]["your_score_pct"] == 100
