"""Game report card: opening names, move classes, the report, the annotated PGN, the coach's write-up and the API."""

import io
import json

import chess
import chess.pgn
import httpx
import pytest

from app import openings, review, review_ai
from app.llm import Backend
from tests.conftest import needs_engine
from tests.test_api import client, demo_account

FOOLS = ["f2f3", "e7e5", "g2g4", "d8h4"]  # 2.g4?? allows 2...Qh4#
MISSED = ["e2e4", "e7e5", "f1c4", "b8c6", "d1h5", "g8f6", "h5f3"]  # 3...Nf6?? allows Qxf7#, and 4.Qf3 misses it


# ---- openings ---------------------------------------------------------------------------------------------------
def test_openings_are_named_by_position():
    assert openings.identify(["e2e4", "e7e5", "g1f3", "b8c6", "f1b5"]) == {"eco": "C60", "name": "Ruy Lopez", "ply": 4, "left_book": None}
    # any move order reaching the same position gets the same name
    a = openings.identify(["g1f3", "d7d5", "d2d4"])
    b = openings.identify(["d2d4", "d7d5", "g1f3"])
    assert a["eco"] == b["eco"] == "D02" and a["name"] == b["name"]
    assert openings.identify([]) == {"eco": "", "name": "", "ply": None, "left_book": 0}


def test_theory_includes_positions_on_the_way_to_a_named_line():
    # 1.d4 Nf6 2.c4 e6 has no name of its own, but it's on the way to the Catalan: still theory
    assert openings.identify(["d2d4", "g8f6", "c2c4", "e7e6"])["left_book"] is None
    off = openings.identify(["e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6", "a2a3", "h7h5"])
    assert off["name"] == "Ruy Lopez: Morphy Defense" and off["left_book"] == 6  # 4.a3 left theory
    b = chess.Board()
    b.push_san("e4")
    assert openings.in_book(b)
    b.push_san("h5")
    b.push_san("Ke2")
    assert not openings.in_book(b)


# ---- sacrifices and move classes -----------------------------------------------------------------------------------
def test_sacrifices_need_a_legal_capture():
    b = chess.Board()
    for m in "e4 e5 Nf3 Nc6 Bc4 Bc5".split():
        b.push_san(m)
    assert review.is_sacrifice(b, b.parse_san("Bxf7+"))  # bishop for a pawn, and the king can take it
    assert not review.is_sacrifice(b, b.parse_san("O-O")) and not review.is_sacrifice(b, b.parse_san("d4"))
    mate = chess.Board("r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4")
    assert not review.is_sacrifice(mate, mate.parse_san("Qxf7#"))  # defended by the bishop: the king can't take


def _evals(*cps, best=None, legal=20):
    """Hand-made engine output: one cp per position, from the side to move's view."""
    return [{"cp": cp, "best": (best or {}).get(i), "second": None, "legal": legal} for i, cp in enumerate(cps)]


def test_classes_from_hand_made_evaluations():
    moves = ["g1f3", "g8f6", "b1c3", "b8c6", "a2a3"]  # 1.Nf3 Nf6 are named lines (book)
    # one score per position, side to move's view: start, after 1.Nf3, 1...Nf6, 2.Nc3, 2...Nc6 (White +6), 3.a3
    ev = _evals(30, -30, 30, -30, 600, -600, best={0: "g1f3", 1: "g8f6", 2: "b1c3", 3: "d7d5", 4: "a2a3"})
    rows = review.classify_moves(moves, ev)
    cls = [r["class"] for r in rows]
    assert cls[0] == "book" and cls[3] == "blunder"  # 2...Nc6 dropped from equal to -6: a blunder
    assert rows[3]["best_san"] == "d5" and rows[3]["loss"] >= 30
    assert cls[4] in ("best", "great")  # White kept the full advantage
    s = review.side_summary(rows, "black")
    assert s["counts"]["blunder"] == 1 and s["acpl"] > 100 and s["grade"] in ("D", "F")


def test_allowing_mate_is_a_blunder_even_when_lost_and_missing_mate_counts():
    moves = ["e2e4", "e7e5"]
    lost = _evals(-900, review.MATE_CP - 3, 0, best={0: "d2d4"})  # White was already lost, then walked into mate in 3
    assert review.classify_moves(moves[:1], lost[:2])[0]["class"] == "blunder"
    missed = _evals(review.MATE_CP - 2, -600, 0, best={0: "d2d4"})  # mate in 2 on the board; played a +6 move instead
    row = review.classify_moves(moves[:1], missed[:2])[0]
    assert row["class"] in ("mistake", "blunder") and "missed_mate" in row["tags"]


def test_forced_moves_and_clock_tags():
    ev = _evals(0, 0, legal=1)
    assert review.classify_moves(["e2e4"], ev)[0]["class"] == "forced"
    ev = _evals(0, 400, best={0: "d2d4"})
    row = review.classify_moves(["a2a3"], ev, clocks={0: 12.0}, base=600)[0]
    assert row["class"] == "blunder" and "time_trouble" in row["tags"]


def test_turning_point_and_verdict():
    rows = [{"ply": i, "move_number": i // 2 + 1, "color": "white" if i % 2 == 0 else "black", "san": s, "win_before": wb, "win_after": wa,
             "loss": max(0, wb - wa)} for i, (s, wb, wa) in enumerate([("e4", 50, 50), ("e5", 50, 48), ("Nf3", 52, 52), ("Qh4", 48, 10),
                                                                     ("Nxh4", 90, 92)])]
    tp = review.turning_point(rows)
    assert tp == {"ply": 3, "label": "2...Qh4", "color": "black"}
    sides = {"white": {"accuracy": 95}, "black": {"accuracy": 60}}
    assert "2...Qh4, a blunder by Black" in review.verdict(rows, sides, "1-0", tp, None)
    assert "by you" in review.verdict(rows, sides, "1-0", tp, "black")
    calm = [r | {"loss": 1, "win_after": 50, "win_before": 50} for r in rows]
    assert review.turning_point(calm) is None
    assert review.verdict(calm, sides, "1/2-1/2", None, None).startswith("A balanced game")


# ---- the full report, with Stockfish --------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def fools_report(engine):
    return review.build_report({"moves": FOOLS, "white": "Ann", "black": "Bob", "result": "0-1", "you": "white", "date": "2026.10.01"}, engine, 6)


@pytest.fixture(scope="module")
def missed_report(engine):
    return review.build_report({"moves": MISSED, "white": "Ann", "black": "Bob", "result": "*", "you": "white"}, engine, 6)


@needs_engine
def test_report_on_fools_mate(fools_report):
    r = fools_report
    assert r["opening"]["eco"] == "A00" and "Fool's Mate" in r["opening"]["name"]
    g4 = r["moves"][2]
    assert g4["san"] == "g4" and g4["class"] == "blunder" and "allowed_mate" in g4["tags"]
    assert r["moves"][3]["san"] == "Qh4#" and r["moves"][3]["class"] == "best"  # mate is the best move, not "book"
    assert len(r["series"]) == len(FOOLS) + 1 and r["series"][-1] < 5  # White's winning chances at the end
    assert r["sides"]["white"]["counts"]["blunder"] == 1 and r["sides"]["black"]["accuracy"] > r["sides"]["white"]["accuracy"]
    k = next(k for k in r["key_moments"] if k["label"] == "2.g4")  # (a shallow engine may also call 1.f3 an inaccuracy)
    assert k["reply"] == "Qh4#" and k["line"] and "forced mate" in k["explain"]
    assert r["verdict"] == "Decided by one big moment: 2.g4, a blunder by you."
    assert r["final_fen"] == chess.Board("rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3").fen()


@needs_engine
def test_missing_a_mate_in_one(missed_report):
    nf6, qf3 = missed_report["moves"][5], missed_report["moves"][6]
    assert nf6["class"] == "blunder" and "allowed_mate" in nf6["tags"]
    assert qf3["class"] in ("mistake", "blunder") and qf3["best_san"] == "Qxf7#" and "missed_mate" in qf3["tags"]
    assert [k["label"] for k in missed_report["key_moments"]] == ["3...Nf6", "4.Qf3"]


@needs_engine
def test_annotated_pgn_reads_back(fools_report, missed_report):
    game = chess.pgn.read_game(io.StringIO(review.annotated_pgn(fools_report)))
    h = game.headers
    assert h["ECO"] == "A00" and h["White"] == "Ann" and h["Result"] == "0-1" and h["Date"] == "2026.10.01"
    assert "WhiteAccuracy" in h and "Stockfish" in h["Annotator"]
    nodes = list(game.mainline())
    assert [n.move.uci() for n in nodes] == FOOLS
    assert chess.pgn.NAG_BLUNDER in nodes[2].nags and "was best" in nodes[2].comment and "Line:" in nodes[2].comment
    assert nodes[2].eval().pov(chess.WHITE).mate() == -1  # [%eval #-1]: Black mates in one
    assert nodes[3].eval() is None  # mate on the board: no evaluation
    assert nodes[0].eval().pov(chess.WHITE).score() is not None
    text = review.annotated_pgn(missed_report)
    assert "Qxf7# was best" in text and "$4" in text and '[Result "*"]' in text


@needs_engine
def test_report_json_round_trip_and_storage(tmp_path, fools_report):
    from app.store import Store

    store = Store(str(tmp_path / "r.db"))
    assert review.load(store, "k", "g1") is None
    ai = review_ai.builtin(fools_report) | {"source": "built-in"}
    review.save(store, "k", "g1", json.loads(review.to_json(fools_report)), ai)
    got = review.load(store, "k", "g1")
    assert got["report"]["verdict"] == fools_report["verdict"] and got["ai"]["source"] == "built-in" and got["depth"] == 6
    review.save_ai(store, "k", "g1", {"headline": "x", "source": "ai"})
    assert review.load(store, "k", "g1")["ai"]["source"] == "ai" and review.reviewed_ids(store, "k") == {"g1"}


# ---- the coach's write-up ---------------------------------------------------------------------------------------------
@needs_engine
def test_builtin_coach_writes_every_section(fools_report, missed_report):
    b = review_ai.builtin(fools_report)
    assert b["headline"] == fools_report["verdict"]
    assert b["summary"].startswith("You played White and lost.")
    assert "2.g4" in b["summary"] and "Fool's Mate" in b["opening"]
    assert b["lessons"][0]["title"] == "Check your king before every move" and b["lessons"][0]["ply"] == 2  # the costliest first
    assert b["training"][0]["feature"] == "predicted"
    review_ai.AIReview.model_validate(b)  # the same shape as the AI's
    m = review_ai.builtin(missed_report)
    assert any("mate" in x["detail"] for x in m["lessons"])


@needs_engine
def test_facts_hold_only_what_the_engine_found(fools_report):
    fx = review_ai.facts(fools_report)
    assert fx["you"] == "white" and fx["opening"]["eco"] == "A00"
    km = next(k for k in fx["key_moments"] if k["move"] == "2.g4")
    assert km["strongest_reply_after_it"] == "Qh4#" and km["reasons"] == ["allowed_mate"] and km["by"] == "white (you)"
    assert fx["players"]["white"]["counts"]["blunder"] == 1 and "endgame" not in fx["players"]["white"]["phases"]


def _model(replies):
    """A fake OpenAI-compatible model that answers with each reply in turn."""
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        calls.append(body)
        text = replies[min(len(calls) - 1, len(replies) - 1)]
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}], "usage": {"total_tokens": 900}})

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def _review(**over):
    base = {"headline": "One move decided it.", "summary": "You opened with 1.f3 and then 2.g4 lost at once to Qh4#.",
            "opening": "The Barnes Opening weakens your king.", "middlegame": "", "endgame": "",
            "strengths": ["You got the game started."],
            "lessons": [{"title": "Check your king", "detail": "After 2.g4 the diagonal to e1 was open. e4 was the move.", "ply": 2}],
            "training": [{"task": "Solve your predicted puzzles", "why": "Mates like this.", "feature": "predicted"},
                         {"task": "Something", "why": "", "feature": "nonsense"}]}
    return json.dumps(base | over)


BACKEND = Backend("openai", "test-model", "https://api.groq.com/openai/v1", "k")


@needs_engine
def test_ai_review_grounded_first_time(fools_report):
    http, calls = _model([_review()])
    out, used = review_ai.write(fools_report, [BACKEND], http=http)
    assert out["source"] == "ai" and out["model"] == "Groq · test-model" and out["removed"] == [] and used == 900
    assert len(calls) == 1 and calls[0]["response_format"] == {"type": "json_object"}
    assert "Facts (from Stockfish)" in calls[0]["messages"][1]["content"] and '"2.g4"' in calls[0]["messages"][1]["content"]
    assert out["training"][1]["feature"] is None  # unknown features are dropped, the task stays


@needs_engine
def test_ai_review_is_repaired_then_cleaned(fools_report):
    bad = _review(summary="You played 2.g4. Instead 2.Nh3 would have held. It was over after Qh4#.")
    http, calls = _model([bad, _review()])  # the repair works
    out, used = review_ai.write(fools_report, [BACKEND], http=http)
    assert len(calls) == 2 and "Nh3" in calls[1]["messages"][1]["content"] and out["removed"] == [] and used == 1800
    http, calls = _model([bad, bad])  # the repair doesn't: the sentence with the made-up move goes
    out, _ = review_ai.write(fools_report, [BACKEND], http=http)
    assert out["removed"] == ["Nh3"] and "Nh3" not in out["summary"] and "Qh4#" in out["summary"] and out["source"] == "ai"
    numbers = _review(summary="You were +3.5 after the opening. Then 2.g4 lost.")  # evaluations the facts don't have
    http, _ = _model([numbers, numbers])
    out, _ = review_ai.write(fools_report, [BACKEND], http=http)
    assert out["summary"] == "Then 2.g4 lost." and out["removed"] == ["+3.5"]


@needs_engine
def test_ai_review_falls_back_to_the_builtin_coach(fools_report):
    http, _ = _model(["not json at all"])
    out, used = review_ai.write(fools_report, [BACKEND], http=http)
    assert out["source"] == "built-in" and out["model"] is None and used == 900
    down = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom")))
    out, used = review_ai.write(fools_report, [BACKEND], http=down)
    assert out["source"] == "built-in" and used == 0
    assert review_ai.write(fools_report, [])[0]["source"] == "built-in"


# ---- the API ------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def app_mod():
    import app.main as m

    return m


def _first_game(app_mod, email, account):
    uid = app_mod.store.user_by_email(email)["id"]
    return app_mod.store.load_games(f"u{uid}:demo:demo_player")[0].game_id


@needs_engine
def test_review_api_flow(app_mod):
    email = "review-flow@test.com"
    c = client(app_mod, email)
    a = demo_account(c)
    gid = _first_game(app_mod, email, a)
    url = f"/api/accounts/{a['id']}/game/{gid}/review"
    st = c.get(url).json()
    assert st["state"] == "none" and st["reviews_left"] == 10 and st["can_ai"] is False
    assert c.get(url + ".pgn").status_code == 404
    r = c.post(url, json={"ai": True}).json()
    assert r["ai"] is False  # no model on this server: the built-in coach writes it
    assert c.get(f"/api/jobs/{r['job_id']}").json()["state"] == "done"
    st = c.get(url).json()
    assert st["state"] == "ready" and st["depth"] == 6 and st["reviews_left"] == 9
    rep = st["report"]
    assert rep["you"] == "white" and rep["white"] == "demo_player" and len(rep["moves"]) == rep["plies"] > 10
    assert {"accuracy", "grade", "acpl", "counts", "phases"} <= set(rep["sides"]["black"])
    assert st["ai"]["source"] == "built-in" and st["ai"]["lessons"]
    assert c.post(url, json={"ai": True}).json() == {"job_id": None, "message": "This game is already reviewed."}
    pgn = c.get(url + ".pgn")
    assert pgn.status_code == 200 and "attachment" in pgn.headers["content-disposition"] and "[%eval" in pgn.text
    assert chess.pgn.read_game(io.StringIO(pgn.text)) is not None
    # someone else's account, a missing game, and the switch
    other = client(app_mod, "review-other@test.com")
    assert other.get(url).status_code == 404 and other.post(url, json={}).status_code == 404
    assert c.get(f"/api/accounts/{a['id']}/game/nope/review").status_code == 404
    assert c.post(f"/api/accounts/{a['id']}/game/nope/review", json={}).status_code == 404
    app_mod.switches.set_switch(app_mod.store, "review", False)
    try:
        assert c.post(url, json={}).status_code == 503
    finally:
        app_mod.switches.set_switch(app_mod.store, "review", True)


@needs_engine
def test_review_api_daily_limit(app_mod, monkeypatch):
    email = "review-limit@test.com"
    c = client(app_mod, email)
    a = demo_account(c)
    uid = app_mod.store.user_by_email(email)["id"]
    games = app_mod.store.load_games(f"u{uid}:demo:demo_player")
    monkeypatch.setitem(app_mod.PLANS["free"], "reviews_per_day", 1)
    assert c.post(f"/api/accounts/{a['id']}/game/{games[0].game_id}/review", json={}).status_code == 200
    r = c.post(f"/api/accounts/{a['id']}/game/{games[1].game_id}/review", json={})
    assert r.status_code == 429 and r.json()["detail"]["upgrade"]


@needs_engine
def test_review_api_with_the_ai_coach(app_mod, monkeypatch):
    email = "review-ai@test.com"
    c = client(app_mod, email)
    a = demo_account(c)
    gid = _first_game(app_mod, email, a)
    url = f"/api/accounts/{a['id']}/game/{gid}/review"
    c.post(url, json={"ai": False})  # the engine review first, written by the built-in coach
    assert c.get(url).json()["ai"]["source"] == "built-in"
    monkeypatch.setattr(app_mod, "resolve_backends", lambda s: [BACKEND])
    monkeypatch.setattr("app.llm.resolve_backends", lambda s: [BACKEND])
    seen = []

    def fake(backend, system, prompt, http=None, **kw):
        seen.append(prompt)
        return json.dumps({"headline": "A tidy game.", "summary": "Good work.", "lessons": [], "training": []}), 1500

    monkeypatch.setattr(review_ai, "json_completion", fake)
    st = c.get(url).json()
    assert st["can_ai"] and st["ai_left"] == 3
    r = c.post(url, json={"ai": True}).json()
    assert r["ai"] is True and c.get(f"/api/jobs/{r['job_id']}").json()["state"] == "done"
    st = c.get(url).json()
    assert st["ai"]["source"] == "ai" and st["ai"]["headline"] == "A tidy game." and st["ai_left"] == 2 and st["reviews_left"] == 9
    assert len(seen) == 1 and "Facts (from Stockfish)" in seen[0]
    assert c.post(url, json={"ai": True}).json()["job_id"] is None  # already written up by the AI


@needs_engine
def test_a_scanned_game_can_be_reviewed(app_mod):
    from tests.test_scoresheet import RUY_UCI

    c = client(app_mod, "review-scan@test.com")
    out = c.post("/api/scan/save", json={"moves": RUY_UCI, "color": "white", "result": "1-0", "white": "Aanya", "black": "Rival"}).json()
    assert out["game_id"]
    url = f"/api/accounts/{out['account_id']}/game/{out['game_id']}/review"
    r = c.post(url, json={}).json()
    assert c.get(f"/api/jobs/{r['job_id']}").json()["state"] == "done"
    rep = c.get(url).json()["report"]
    assert rep["opening"]["name"].startswith("Ruy Lopez") and rep["white"] == "Aanya" and rep["event"] == "Over-the-board game"
    assert sum(m["class"] == "book" for m in rep["moves"][:8]) >= 6  # a shallow engine may dislike a theory move or two
