import chess
import pytest

from app.analysis import (
    _is_tactical,
    analyse_game,
    classify,
    game_phase,
    move_accuracy,
    win_percent,
)
from app.engine import Engine
from app.games import GameImportError, parse_pgn, validate_username
from app.profile import contested_moves, rating_dna, weekly_plan
from app.store import Store
from tests.conftest import SETTINGS, needs_engine

# Black (alice) walks into Scholar's mate.
WALKS_INTO_MATE = """[White "bob"]
[Black "alice"]
[Result "1-0"]
[WhiteElo "1500"]
[BlackElo "1480"]
[TimeControl "600+0"]

1. Nf3 { [%clk 0:10:00] } Nf6 { [%clk 0:10:00] } 2. Ng1 { [%clk 0:09:58] } Ng8 { [%clk 0:09:55] }
3. e4 { [%clk 0:09:50] } e5 { [%clk 0:09:50] } 4. Qh5 { [%clk 0:09:45] } Nc6 { [%clk 0:00:25] }
5. Bc4 { [%clk 0:09:40] } Nf6 { [%clk 0:00:20] } 6. Qxf7# 1-0
"""

# White (alice) misses mate in one and hangs the queen instead.
MISSES_MATE = """[White "alice"]
[Black "carol"]
[Result "0-1"]
[TimeControl "600+0"]

1. Nf3 Nf6 2. Ng1 Ng8 3. e4 e5 4. Qh5 Nc6 5. Bc4 Nf6 6. d3 Nxh5 0-1
"""


# ---- pure maths ---------------------------------------------------------------
def test_win_percent_is_centred_and_monotonic():
    assert win_percent(0) == pytest.approx(50)
    assert win_percent(300) > win_percent(100) > 50 > win_percent(-100)
    assert win_percent(200) + win_percent(-200) == pytest.approx(100)
    assert win_percent(50_000) == win_percent(1000)  # clamped


def test_move_accuracy_bounds():
    assert move_accuracy(60, 60) == pytest.approx(100, abs=0.01)
    assert move_accuracy(60, 70) == pytest.approx(100, abs=0.01)  # improving never scores >100
    assert move_accuracy(90, 10) < 5
    assert 0 <= move_accuracy(100, 0) <= 100


@pytest.mark.parametrize("loss,label", [(0, "good"), (9.9, "good"), (10, "inaccuracy"), (20, "mistake"), (30, "blunder")])
def test_classify_thresholds(loss, label):
    assert classify(loss) == label


def test_game_phase():
    assert game_phase(chess.Board()) == "opening"
    assert game_phase(chess.Board("8/8/4k3/8/8/4K3/4R3/8 w - - 0 50")) == "endgame"
    middlegame = chess.Board("r1bq1rk1/pp2bppp/2n1pn2/3p4/3P4/2NBPN2/PP3PPP/R2QK2R w KQ - 0 14")
    assert game_phase(middlegame) == "middlegame"


def test_plain_recapture_is_not_a_tactic():
    b = chess.Board()
    for san in ["e4", "d5", "exd5"]:
        b.push_san(san)
    # Qxd5 just takes back on the square White captured on.
    assert not _is_tactical(b, b.parse_san("Qxd5").uci())


# ---- PGN import ---------------------------------------------------------------
def test_parse_pgn_finds_player_side_and_clocks():
    [g] = parse_pgn(WALKS_INTO_MATE, "ALICE")  # case-insensitive
    assert g.player_color == chess.BLACK
    assert g.player_rating == 1480
    assert g.player_score == 0.0
    assert g.initial_seconds == 600
    assert g.clocks[9] == 20  # Black's 5th move
    assert len(g.moves) == 11


def test_parse_pgn_errors():
    with pytest.raises(GameImportError):
        parse_pgn("", "alice")
    with pytest.raises(GameImportError):
        parse_pgn(WALKS_INTO_MATE, "nobody")


def test_parse_pgn_skips_short_and_unfinished_games():
    short = '[White "alice"]\n[Black "x"]\n[Result "1-0"]\n\n1. e4 e5 1-0\n'
    unfinished = WALKS_INTO_MATE.replace('[Result "1-0"]', '[Result "*"]').replace("1-0\n", "*\n")
    with pytest.raises(GameImportError):
        parse_pgn(short + "\n" + unfinished, "alice")


@pytest.mark.parametrize("name,ok", [("magnus", True), ("a", False), ("bad name", False), ("x" * 31, False), ("ok_name-2", True)])
def test_validate_username(name, ok):
    if ok:
        assert validate_username(name) == name
    else:
        with pytest.raises(GameImportError):
            validate_username(name)


# ---- engine-backed analysis ------------------------------------------------------
@pytest.fixture(scope="module")
def engine():
    if not SETTINGS.stockfish_path:
        pytest.skip("Stockfish not installed")
    with Engine(SETTINGS.stockfish_path, depth=8, time_limit=0.05) as e:
        yield e


@needs_engine
def test_blunder_into_mate_is_flagged(engine):
    [g] = parse_pgn(WALKS_INTO_MATE, "alice")
    ga = analyse_game(g, engine)
    blunder = next(m for m in ga.moves if m.played_san == "Nf6" and m.move_number == 5)
    assert blunder.classification == "blunder"
    assert blunder.clock_before == 25  # [%clk] is time left *after* a move, so use the previous one
    assert blunder.time_pressure
    assert "time_trouble" in blunder.tags
    assert blunder.best and blunder.best != blunder.played


@needs_engine
def test_missed_mate_and_hung_queen(engine):
    [g] = parse_pgn(MISSES_MATE, "alice")
    ga = analyse_game(g, engine)
    m = next(m for m in ga.moves if m.played_san == "d3")
    assert m.classification == "blunder"
    assert m.best_san == "Qxf7#"
    assert {"missed_mate", "hanging_piece"} <= set(m.tags)


@needs_engine
def test_store_roundtrip_and_spaced_repetition(engine, tmp_path):
    [g] = parse_pgn(MISSES_MATE, "alice")
    ga = analyse_game(g, engine)
    st = Store(str(tmp_path / "t.db"))
    st.save_games("Alice", [ga])
    assert st.known_game_ids("alice") == {ga.game_id}
    loaded = st.load_games("alice")[0]
    assert loaded.moves[0].fen_before == ga.moves[0].fen_before

    added = st.add_puzzles_from("alice", [ga])
    assert added >= 1
    assert st.add_puzzles_from("alice", [ga]) == 0  # idempotent
    pz = st.puzzles("alice")[0]
    r = st.record_attempt(pz["id"], correct=True)
    assert r["box"] == 2 and r["next_review_in_days"] == pytest.approx(3, abs=0.01)
    assert pz["id"] not in {p["id"] for p in st.puzzles("alice")}  # no longer due
    r = st.record_attempt(pz["id"], correct=False)
    assert r["box"] == 1
    assert st.get_puzzle(pz["id"])["attempts"] == 2


@needs_engine
def test_rating_dna_and_plan(engine):
    games = [analyse_game(g, engine) for g in parse_pgn(WALKS_INTO_MATE + "\n" + MISSES_MATE, "alice")]
    dna = rating_dna(games)
    assert dna["base_rating"] == 1480 and not dna["rating_is_estimated"]
    assert dna["moves"] == len(contested_moves(games))
    for s in dna["skills"]:
        assert abs(s["rating"] - 1480) <= 350
    assert rating_dna(games, override=2000)["base_rating"] == 2000
    plan = weekly_plan(dna, due_puzzles=3, minutes_per_day=30)
    assert len(plan["days"]) == 7
    assert all(d["minutes"] <= 30 or len(d["items"]) == 1 for d in plan["days"])

