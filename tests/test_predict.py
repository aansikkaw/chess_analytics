"""Twin-predicted puzzles: the Lichess bank, annotation, your Twin, the miss model, the deck and its API."""

import math

import chess
import pytest

from app import predict, puzzlebank
from app import twin as twin_mod
from app.store import Store
from tests.conftest import needs_engine
from tests.test_api import client, demo_account

# The example row from database.lichess.org: Black's Kd7 sets it up, White mates with Be6+ Kd8 Qf8#.
LICHESS_ROW = {"PuzzleId": "00sHx", "FEN": "q3k1nr/1pp1nQpp/3p4/1P2p3/4P3/B1PP1b2/B5PP/5K2 b k - 0 17", "Moves": "e8d7 a2e6 d7d8 f7f8",
               "Rating": "1760", "RatingDeviation": "80", "Popularity": "83", "NbPlays": "72", "Themes": "mate mateIn2 middlegame short",
               "GameUrl": "https://lichess.org/yyznGmXs/black#34", "OpeningTags": "Italian_Game Italian_Game_Classical_Variation"}


def test_lichess_row_means_opponent_move_first():
    row = puzzlebank.parse_row(LICHESS_ROW)
    assert row["setup_uci"] == "e8d7" and row["moves"] == "a2e6 d7d8 f7f8"
    assert row["sans"] == "Be6+ Kd8 Qf8#" and row["phase"] == "middlegame"
    assert chess.Board(row["fen"]).turn == chess.WHITE  # the solver is to move in the position shown
    assert puzzlebank.parse_row({**LICHESS_ROW, "Moves": "e8d7 a2e6 d7d8"}) is None  # must end on the solver's move
    assert puzzlebank.parse_row({**LICHESS_ROW, "Moves": "e8e1 a2e6"}) is None  # illegal
    assert puzzlebank.motif_themes(["mate", "mateIn2", "middlegame", "short", "fork"]) == ["mateIn2", "fork"]
    assert puzzlebank.theme_label("backRankMate") == "Back-rank mate" and puzzlebank.theme_label("newTheme") == "newTheme"


def test_import_is_balanced_and_filtered(tmp_path):
    store = Store(str(tmp_path / "bank.db"))
    rows = [{**LICHESS_ROW, "PuzzleId": f"p{i}", "Rating": str(1500 + (i % 3)), "Popularity": str(90 if i % 5 else 10)} for i in range(30)]
    added = puzzlebank.import_rows(store, rows, min_popularity=50, per_band=4)
    assert added == 4  # all in one 50-point band, capped at 4; unpopular ones skipped
    assert puzzlebank.import_rows(store, [{**LICHESS_ROW, "PuzzleId": "x", "Rating": "2100"}], per_band=4) == 1
    st = puzzlebank.stats(store)
    assert st["puzzles"] == 5 and st["annotated"] == 0 and not st["sample_only"]


def test_bundled_sample_loads_on_first_use(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    n = puzzlebank.ensure_bank(store)
    assert n >= 100 and puzzlebank.stats(store)["sample_only"]
    assert puzzlebank.ensure_bank(store) == n  # only once


def test_glicko_moves_the_rating_and_shrinks_uncertainty():
    up, rd1 = predict.glicko_update(1800, 300, 1800, 80, 1.0)
    down, rd2 = predict.glicko_update(1800, 300, 1800, 80, 0.0)
    assert up > 1800 > down and rd1 < 300 and math.isclose(up - 1800, 1800 - down, rel_tol=1e-9)
    small, _ = predict.glicko_update(1800, 80, 1800, 80, 1.0)
    assert small - 1800 < up - 1800  # a sure rating moves less
    assert predict.expected_score(1800, 1800) == 0.5


def test_theme_offsets_from_rating_dna_and_results():
    dna = {"base_rating": 1800, "skills": [{"key": "endgames", "rating": 1700, "low_confidence": False},
                                           {"key": "tactics", "rating": 1900, "low_confidence": False},
                                           {"key": "time", "rating": 1500, "low_confidence": True}]}
    skills = predict.dna_offsets(dna)
    assert skills == {"endgames": -100, "tactics": 100}
    assert predict.theme_offset(["endgame", "rookEndgame"], skills, {}) == pytest.approx(-60)
    assert predict.theme_offset(["fork", "endgame"], skills, {}) == pytest.approx(0)  # mean of +60 and -60
    assert predict.theme_offset(["fork"], skills, {"fork": [-50.0, 4]}) == pytest.approx(10)


@pytest.fixture(scope="module")
def annotated(engine):
    row = puzzlebank.parse_row(LICHESS_ROW)
    return row, puzzlebank.unpack_feats(puzzlebank.annotate_one(row, engine, depth=6))


@needs_engine
def test_annotation_holds_the_solution_among_the_candidates(annotated):
    row, feats = annotated
    assert feats["u"][feats["s"]] == "a2e6" and len(feats["u"]) == len(feats["rows"]) >= 3
    assert feats["s"] == feats["t"]  # Stockfish's top move is the solution


@needs_engine
def test_a_twin_that_avoids_checks_is_predicted_to_miss_a_checking_solution(annotated):
    row, feats = annotated
    typical = twin_mod.prior(1800)
    shy = list(typical)
    shy[twin_mod.FEATURES.index("check")] = -2.5
    you = {"rating": 1900.0, "rd": 100.0, "themes": {}}
    same = predict.score(feats, row["rating"], row["themes"].split(), you, typical, typical, {})
    blind = predict.score(feats, row["rating"], row["themes"].split(), you, shy, typical, {})
    assert same["blind"] == pytest.approx(0) and same["p_miss"] == pytest.approx(same["p_typical"])
    assert blind["blind"] > 0.3 and blind["p_miss"] > same["p_miss"] and blind["personal"] > 0
    why = predict.why_after(feats, blind, chess.Board(row["fen"]), shy, typical, row["themes"].split(), {}, {})
    assert any("Be6+" in w for w in why)
    if blind["pick"] != feats["s"]:
        assert any("checks aren't the first thing you look at" in w for w in why)


@needs_engine
def test_your_twin_from_your_own_games(tmp_path, engine, demo_games):
    import random

    store = Store(str(tmp_path / "self.db"))
    key = "u9:demo:demo_player"
    assert predict.record_of(demo_games[0]).player_color == chess.WHITE
    ids = predict.pick_own_unstudied(demo_games, set(), 4)
    n = predict.study_own(store, key, {g.game_id: g for g in demo_games}, ids, engine, 4, random.Random(1))
    assert n > 30 and predict.studied_ids(store, key, 4) == set(ids)
    with pytest.raises(ValueError):
        predict.fit_self(store, key, demo_games, 4, min_games=10)
    me = predict.fit_self(store, key, demo_games, 4, min_games=3)
    assert len(me["params"]) == twin_mod.NF and me["games"] == 4 and me["profile"]["rating"] == 1850
    # The typical player leaves you out: with no other Twins it's the rating prior
    assert predict.population_beta(store, 1850, exclude=key) == pytest.approx(twin_mod.prior(1850))
    assert predict.population_beta(store, 1850) != pytest.approx(twin_mod.prior(1850))


# ---- the API ------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def app_mod():
    import app.main as m

    return m


@needs_engine
def test_predicted_puzzles_flow(app_mod):
    c = client(app_mod, "predict-flow@test.com")
    a = demo_account(c)
    first = c.get(f"/api/accounts/{a['id']}/predicted").json()
    assert first["puzzles"] == [] and first["per_day"] == 8 and first["left_today"] == 8 and first["status"]["twin"] is None
    assert first["status"]["puzzle_rating"] == 1850 + predict.PUZZLE_RATING_OFFSET and first["status"]["bank"]["puzzles"] >= 100
    job = c.post(f"/api/accounts/{a['id']}/predicted/refresh", json={}).json()
    done = c.get(f"/api/jobs/{job['job_id']}").json()
    assert done["state"] == "done", done
    got = c.get(f"/api/accounts/{a['id']}/predicted").json()
    assert got["puzzles"] and got["left_today"] == 8 - done["predicted"] and got["status"]["band_ready"] >= 1
    p = got["puzzles"][0]
    assert p["kind"] == "predicted" and "solution" not in p and "line" not in p and "meta" not in p
    assert 35 <= round(100 * p["predicted"]["p_miss"]) <= 88 and p["predicted"]["why_before"] and p["prev_uci"]
    # They never mix into the deck of your own mistakes (or its due count)
    mine = c.get(f"/api/accounts/{a['id']}/puzzles?due=false&limit=100").json()
    assert all(x["kind"] != "predicted" for x in mine)
    # Answer one: the reasons appear and your puzzle rating moves
    board = chess.Board(p["fen"])
    secret = app_mod.store.get_puzzle(p["id"])["solution"]
    wrong = next(m.uci() for m in board.legal_moves if m.uci() != secret)
    r = c.post(f"/api/puzzles/{p['id']}/attempt", json={"move": wrong}).json()
    if r["verdict"] == "wrong":
        assert r["prediction"]["missed"] and r["prediction"]["rating"] < got["status"]["puzzle_rating"]
    assert r["why"] and r["predicted_miss"] == round(100 * p["predicted"]["p_miss"])
    again = c.post(f"/api/puzzles/{p['id']}/attempt", json={"move": secret, "practice": True}).json()
    assert "prediction" not in again  # a retry doesn't teach the model
    st = c.get(f"/api/accounts/{a['id']}/predicted").json()["status"]
    assert st["track"]["attempts"] == 1
    # Someone else can't answer your puzzles
    assert client(app_mod, "predict-other@test.com").post(f"/api/puzzles/{p['id']}/attempt", json={"move": wrong}).status_code == 404


@needs_engine
def test_predicted_daily_allowance_and_your_twin(app_mod):
    c = client(app_mod, "predict-twin@test.com")
    a = demo_account(c)
    uid = app_mod.store.user_by_email("predict-twin@test.com")["id"]
    app_mod.store.bump_usage(uid, "predicted", 8)
    r = c.post(f"/api/accounts/{a['id']}/predicted/refresh", json={})
    assert r.status_code == 429 and r.json()["detail"]["upgrade"]
    # Teaching your Twin is still allowed: it studies your 3 games (tests need 2) and fits
    job = c.post(f"/api/accounts/{a['id']}/predicted/refresh", json={"twin": True}).json()
    assert "studying 3 more" in job["message"]
    assert c.get(f"/api/jobs/{job['job_id']}").json()["state"] == "done"
    st = c.get(f"/api/accounts/{a['id']}/predicted").json()["status"]
    assert st["twin"]["games"] == 3 and st["studied"] == 3
    app_mod.switches.set_switch(app_mod.store, "predicted", False)
    try:
        assert c.post(f"/api/accounts/{a['id']}/predicted/refresh", json={"twin": True}).status_code == 503
    finally:
        app_mod.switches.set_switch(app_mod.store, "predicted", True)
    # Removing the account removes your Twin and its studies
    key = f"u{uid}:demo:demo_player"
    c.delete(f"/api/accounts/{a['id']}")
    with app_mod.store._conn() as conn:
        for table in ("self_twins", "self_twin_games", "puzzle_skill", "think_moments"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE username = ?", (key,)).fetchone()[0] == 0
