"""The "Think here" trainer: time per move from clocks, critical moments, the cost of rushing, the drill and its API."""

import math

import chess
import pytest

from app import think
from app.store import Store
from tests.conftest import needs_engine
from tests.test_api import client, demo_account

START = chess.STARTING_FEN
CRIT = [["e2e4", "e4", 55.0], ["d2d4", "d4", 30.0], ["g1f3", "Nf3", 29.0]]  # one move matters (25 points ahead)
EASY = [["e2e4", "e4", 52.0], ["d2d4", "d4", 51.0], ["g1f3", "Nf3", 50.0]]  # three moves about equal


def moment(ply, spent, gap, loss, critical, obvious=0, cands=CRIT, clock=600.0):
    return {"game_id": "g1", "ply": ply, "fen": START, "prev_fen": None, "prev_uci": None, "played": "d2d4", "played_san": "d4",
            "cands": cands, "best_win": cands[0][2], "gap": gap, "loss": loss, "spent": spent, "clock": clock, "base": 900,
            "critical": critical, "obvious": obvious, "phase": "middlegame", "time_class": "rapid", "played_at": "2026.09.01",
            "opponent": "rival", "move_number": ply // 2 + 1}


@pytest.fixture()
def seeded(tmp_path):
    store = Store(str(tmp_path / "think.db"))
    rows = [moment(10 + 2 * i, 20.0, 3.0, 1.0, 0, cands=EASY) for i in range(8)]  # your usual pace: about 20 s a move
    rows += [moment(40, 4.0, 25.0, 25.0, 1),  # critical, played in 4 s, and it cost 25%: rushed
             moment(42, 2.0, 40.0, 0.0, 1, obvious=1),  # a plain recapture: fast is right
             moment(44, 90.0, 25.0, 2.0, 1),  # critical, and you took your time
             moment(46, 120.0, 2.0, 0.0, 0, cands=EASY)]  # routine, but two minutes: over-thought
    think.save_scan(store, "u1:lichess:me", "g1", rows)
    return store


def test_time_spent_from_clocks_with_increment(demo_games):
    g = demo_games[0]  # demo000: 900+10, the demo player is White
    spent = think.time_spent(g)
    assert spent[0] == 3.6  # 900 - 906.4 + 10: the increment came back after the first move
    assert spent[2] == 37.8 and spent[4] == 28.3
    assert max(spent) < len(g.moves_san)  # the last move has no clock after it


def test_thresholds_follow_your_own_pace():
    assert think.fast_threshold(20) == 10.0 and think.fast_threshold(4) == 2.4 and think.fast_threshold(1) == 2.0
    assert think.slow_threshold(20) == 60.0 and think.slow_threshold(2) == 12.0
    assert think.elo_from_score(0) == 0
    assert math.isclose(think.elo_from_score(0.18), 400 * math.log10(0.68 / 0.32), rel_tol=1e-9)


def test_stats_put_a_number_on_rushing(seeded):
    s = think.stats(seeded, "u1:lichess:me")
    assert s["games"] == 1 and s["moves"] == 12
    assert s["critical"] == 2 and s["rushed"] == 1 and s["rushed_pct"] == 50  # the recapture doesn't count
    assert s["loss_rushed"] == 25.0 and s["loss_thought"] == 2.0
    # thinking typically saves: (2 + 8*5) / (1 + 5) = 7% -> 25 - 7 = 18% recoverable = 0.18 points in one game
    assert s["points_lost"] == 0.18 and s["rating_estimate"] == round(think.elo_from_score(0.18))
    assert s["overthought"] == 1 and s["overthought_s"] == 120
    assert s["fast_below"] == {"rapid": 10.0} and s["rushed_with_time"] == 1
    assert [b["label"] for b in s["buckets"]] == ["Routine", "Some care", "Critical", "Only move"]


def test_drill_mixes_rushed_critical_and_overthought_routine(seeded):
    items = think.drill(seeded, "u1:lichess:me")
    assert len(items) == 2
    assert "cands" not in items[0] and "critical" not in items[0]  # no answers before you answer
    crit = think.get(seeded, items[0]["id"])
    calm = think.get(seeded, items[1]["id"])
    assert crit["critical"] == 1 and calm["critical"] == 0


def test_call_then_move(seeded):
    items = think.drill(seeded, "u1:lichess:me")
    med = {"rapid": think.median_for(seeded, "u1:lichess:me", "rapid")}
    assert med["rapid"] == 20.0
    crit = think.get(seeded, items[0]["id"])
    r = think.call(seeded, crit, "play", med)
    assert r["right"] is False and r["critical"] and "fine_moves" not in r
    m = think.move(seeded, think.get(seeded, crit["id"]), "e2e4", practice=False)
    assert m["verdict"] == "correct" and m["box"] == 1  # wrong call: back to box 1 even though the move was found
    assert think.move(seeded, think.get(seeded, crit["id"]), "d4", practice=True)["verdict"] == "wrong"
    with pytest.raises(ValueError):
        think.move(seeded, think.get(seeded, crit["id"]), "e5", practice=True)
    calm = think.get(seeded, items[1]["id"])
    r = think.call(seeded, calm, "play", med)
    assert r["right"] and r["fine_moves"] == ["e4", "d4", "Nf3"] and r["box"] == 2 and "2m 00s" in r["message"]
    assert len(think.drill(seeded, "u1:lichess:me")) == 0  # both scheduled for later


def test_also_good_moves_count(seeded):
    rows = [moment(60, 3.0, 20.0, 15.0, 1, cands=[["e2e4", "e4", 60.0], ["d2d4", "d4", 58.0], ["g1f3", "Nf3", 30.0]])]
    think.save_scan(seeded, "u1:lichess:me", "g2", [r | {"game_id": "g2"} for r in rows])
    item = next(i for i in think.drill(seeded, "u1:lichess:me", 50) if i["game_id"] == "g2")
    assert think.move(seeded, think.get(seeded, item["id"]), "d2d4", practice=True)["verdict"] == "also_good"


@needs_engine
def test_scan_game_finds_moments(engine, demo_games):
    out = think.scan_game(demo_games[0], engine, depth=6)
    assert out and all(m["spent"] > 0 and m["ply"] % 2 == 0 for m in out)  # the demo player is White
    m = out[0]
    assert len(m["cands"]) >= 2 and m["gap"] == round(m["cands"][0][2] - m["cands"][1][2], 1)
    assert all(not x["obvious"] or x["critical"] for x in out)


@pytest.fixture(scope="module")
def app_mod():
    import app.main as m

    return m


@needs_engine
def test_think_api_flow(app_mod):
    c = client(app_mod, "think-flow@test.com")
    a = demo_account(c)
    st = c.get(f"/api/accounts/{a['id']}/think").json()
    assert st["scanned"] == 0 and st["clock_games"] == 3 and st["can_scan"] == 3
    r = c.post(f"/api/accounts/{a['id']}/think/scan").json()
    assert c.get(f"/api/jobs/{r['job_id']}").json()["state"] == "done"
    st = c.get(f"/api/accounts/{a['id']}/think").json()
    assert st["scanned"] == 3 and st["stats"]["moves"] > 0 and st["can_scan"] == 0
    assert c.post(f"/api/accounts/{a['id']}/think/scan").json()["job_id"] is None  # nothing left to scan
    # a known rushed moment for this account, then the drill endpoints
    uid = app_mod.store.user_by_email("think-flow@test.com")["id"]
    think.save_scan(app_mod.store, f"u{uid}:demo:demo_player", "synthetic", [moment(80, 1.0, 30.0, 30.0, 1) | {"game_id": "synthetic"}])
    items = c.get(f"/api/accounts/{a['id']}/think/drill?limit=50").json()
    item = next(i for i in items if i["game_id"] == "synthetic")
    r = c.post(f"/api/think/{item['id']}/call", json={"call": "think"}).json()
    assert r["right"] and r["critical"]
    r = c.post(f"/api/think/{item['id']}/move", json={"move": "e4"}).json()
    assert r["verdict"] == "correct" and r["box"] == 2 and r["best_san"] == "e4"
    assert c.post(f"/api/think/{item['id']}/call", json={"call": "maybe"}).status_code == 422
    other = client(app_mod, "think-other@test.com")
    assert other.post(f"/api/think/{item['id']}/call", json={"call": "think"}).status_code == 404
