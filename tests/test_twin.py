"""v0.9 Opponent Twin: the choice model, its features, the book, building, playing and reviewing."""

import random

import chess
import pytest

from app import switches, twin
from app.games import parse_pgn
from tests.conftest import needs_engine
from tests.test_api import DEMO, client


@pytest.fixture(scope="module")
def m():
    import app.main as mod

    return mod


@pytest.fixture(autouse=True)
def _twin_on(m):
    yield
    m.store.kv_set("switch:twin", {"on": True})
    switches.clear_cache()


# ---- the model, without an engine ------------------------------------------------------------------------
def test_fit_recovers_known_weights():
    rng = random.Random(3)
    true = [0.0] * twin.NF
    ix = {n: k for k, n in enumerate(twin.FEATURES)}
    true[ix["loss_middlegame"]], true[ix["trade"]], true[ix["pawn_push"]] = 2.5, 1.5, -0.8
    data = []
    for _ in range(600):
        rows = [[0.0] * twin.NF for _ in range(8)]
        for f in rows:
            f[ix["loss_middlegame"]] = -10 * rng.random() * 0.2
            f[ix["trade"]] = float(rng.random() < 0.3)
            f[ix["pawn_push"]] = float(rng.random() < 0.4)
        p = twin.probabilities(true, rows)
        data.append((rows, twin._weighted(rng, list(range(8)), p)))
    beta, se = twin.fit(data, [0.0] * twin.NF, lam=0.5, with_se=True)
    assert abs(beta[ix["loss_middlegame"]] - 2.5) < 0.6 and abs(beta[ix["trade"]] - 1.5) < 0.4 and abs(beta[ix["pawn_push"]] + 0.8) < 0.4
    assert all(s > 0 for s in se)
    assert abs(beta[ix["check"]]) < 1e-6  # features never seen stay at the prior


def test_fit_is_stable_with_extreme_losses():
    """A few huge blunders (mates) must not throw the fit off (the line search guards against overshooting)."""
    ix = {n: k for k, n in enumerate(twin.FEATURES)}
    rows_ok = [[0.0] * twin.NF for _ in range(5)]
    rows_ok[1][ix["loss_middlegame"]] = -10.0
    data = [(rows_ok, 0)] * 50 + [(rows_ok, 1)] * 2
    beta = twin.fit(data, twin.prior(1500))
    assert 0 < beta[ix["loss_middlegame"]] < 20


def test_features_capture_human_habits():
    ix = {n: k for k, n in enumerate(twin.FEATURES)}
    b = chess.Board()
    for san in ("e4", "d5", "exd5"):
        b.push_san(san)
    opp_to = b.peek().to_square  # their pawn just captured on d5
    f = twin.features(b, b.parse_san("Qxd5"), 0.0, 0, False, None, opp_to)
    assert f[ix["capture"]] == 1 and f[ix["recapture"]] == 1 and f[ix["best"]] == 1 and f[ix["top3"]] == 1
    b = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3")
    f = twin.features(b, b.parse_san("Bc4"), 0.02, 4, False, None)
    assert f[ix["develop"]] == 1 and f[ix["top3"]] == 0 and f[ix["loss_opening"]] == pytest.approx(-0.2)
    b = chess.Board("r1bqk2r/pppp1ppp/2n2n2/2b1p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4")
    assert twin.features(b, b.parse_san("O-O"), 0.0, 0, False, None)[ix["castle"]] == 1
    assert twin.features(b, b.parse_san("Ke2"), 0.3, 9, True, None)[ix["king_walk"]] == 1
    assert twin.features(b, b.parse_san("Ke2"), 0.3, 9, True, None)[ix["loss_pressure"]] == pytest.approx(-3.0)


def test_book_trust_grows_with_evidence():
    assert twin.book_trust([]) == 0
    assert twin.book_trust([{"weight": 1.0}]) == pytest.approx(0.4)
    assert twin.book_trust([{"weight": 5.0}]) > 0.75
    assert twin.tc_key(None) == "all" == twin.tc_key(["bullet", "blitz", "rapid", "classical"])
    assert twin.tc_key(["rapid", "blitz", "nonsense"]) == "blitz,rapid"


# ---- the API ------------------------------------------------------------------------------------------
def _pro_with_player(m, email, handle):
    c = client(m, email)
    uid = m.store.user_by_email(email)["id"]
    m.store.set_plan(uid, "pro")
    p = m.library.get_or_create_player("pgn", handle, owner_id=uid)
    m.library.add_games(p["id"], parse_pgn(DEMO, "demo_player"), requested=40)
    m.library.save(uid, p["id"])
    return c, p["id"]


def test_twin_needs_a_library_player_and_pro(m):
    with client(m, "twin-free@test.com") as c:
        assert c.get("/api/twin/1").status_code == 402
    with _pro_with_player(m, "twin-a@test.com", "demo_player")[0] as c:
        assert c.get("/api/twin/999999").status_code == 404


@needs_engine
def test_build_play_and_review(m):
    c, pid = _pro_with_player(m, "twin-b@test.com", "demo_player")
    with c:
        st = c.get(f"/api/twin/{pid}").json()
        assert st["twin"] is None and st["games"] == 40 and st["builds_left"] == 10
        assert c.post(f"/api/twin/{pid}/move", json={"moves": []}).status_code == 404  # build first
        r = c.post(f"/api/twin/{pid}/build", json={}).json()
        assert r["job_id"]
        assert c.get(f"/api/jobs/{r['job_id']}").json()["state"] == "done"
        st = c.get(f"/api/twin/{pid}").json()
        t = st["twin"]
        assert t and t["games"] == 12 and t["decisions"] >= 30 and t["stale"] is False
        p = t["profile"]
        assert set(p["accuracy"]) == {"opening", "middlegame", "endgame"} and p["confidence"] in ("low", "medium", "high")
        assert p["faithfulness"]["decisions"] > 0 and p["faithfulness"]["twin_top3"] is not None
        assert p["openings"]["white"] and p["openings"]["white"][0]["pct"] > 0
        # Building again straight away doesn't spend a build
        again = c.post(f"/api/twin/{pid}/build", json={}).json()
        assert again["job_id"] is None and "up to date" in again["message"]
        # Play: the Twin answers with legal moves and says why
        moves = []
        for _ in range(6):
            r = c.post(f"/api/twin/{pid}/move", json={"moves": moves}).json()
            board = chess.Board()
            for u in moves:
                board.push_uci(u)
            assert chess.Move.from_uci(r["uci"]) in board.legal_moves and r["source"] in ("book", "model", "forced") and r["why"]
            moves.append(r["uci"])
            reply = next(iter(chess.Board(r["fen"]).legal_moves))
            moves.append(reply.uci())
        assert c.post(f"/api/twin/{pid}/move", json={"moves": ["e2e5"]}).status_code == 400
        # The very first move of their games comes from their book most of the time
        sources = [c.post(f"/api/twin/{pid}/move", json={"moves": []}).json()["source"] for _ in range(8)]
        assert sources.count("book") >= 4
        # Rehearsal: always their most common first move
        top = t["profile"]["openings"]["white"][0]["san"]
        assert {c.post(f"/api/twin/{pid}/move", json={"moves": [], "main_line": True}).json()["san"] for _ in range(4)} == {top}
        # Game over is reported, not answered
        over = c.post(f"/api/twin/{pid}/move", json={"moves": ["f2f3", "e7e5", "g2g4", "d8h4"]}).json()
        assert over["over"]["result"] == "0-1" and over["over"]["reason"] == "checkmate" and "uci" not in over
        # Review
        rev = c.post(f"/api/twin/{pid}/review", json={"moves": moves, "color": "black"}).json()
        assert rev["your_accuracy"] is not None and rev["twin_accuracy"] is not None and rev["moves"] == len(moves)
        assert isinstance(rev["mistakes"], list)


@needs_engine
def test_twin_switch_and_limits(m, monkeypatch):
    c, pid = _pro_with_player(m, "twin-c@test.com", "demo_player")
    with c:
        m.store.kv_set("switch:twin", {"on": False})
        switches.clear_cache()
        assert c.post(f"/api/twin/{pid}/build", json={}).status_code == 503
        m.store.kv_set("switch:twin", {"on": True})
        switches.clear_cache()
        monkeypatch.setitem(m.PLANS["pro"], "twin_builds_per_day", 0)
        assert c.post(f"/api/twin/{pid}/build", json={"time_classes": ["rapid"]}).status_code == 429
        # Too few games for a time control: the build job fails with a clear reason
        monkeypatch.setitem(m.PLANS["pro"], "twin_builds_per_day", 10)
        r = c.post(f"/api/twin/{pid}/build", json={"time_classes": ["bullet"]}).json()
        job = c.get(f"/api/jobs/{r['job_id']}").json()
        assert job["state"] == "error" and "at least 10 games" in job["error"]


def test_twin_is_private_for_uploaded_players(m):
    _, pid = _pro_with_player(m, "twin-d@test.com", "demo_player")
    with client(m, "twin-e@test.com") as other:
        m.store.set_plan(m.store.user_by_email("twin-e@test.com")["id"], "pro")
        assert other.get(f"/api/twin/{pid}").status_code == 404
        assert other.post(f"/api/twin/{pid}/move", json={"moves": []}).status_code == 404
