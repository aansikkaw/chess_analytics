"""Scoresheet scanner: notation in many languages, OCR-style misreads, decoding with chess rules, the vision call, the API."""

import json

import chess
import httpx
import pytest

from app import scoresheet as ss
from tests.conftest import needs_engine
from tests.test_api import client

RUY = "1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 Nf6 5. O-O Be7 6. Re1 b5 7. Bb3 d6 8. c3 O-O 9. h3 Nb8 10. d4 Nbd7"
RUY_UCI = ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6", "b5a4", "g8f6", "e1g1", "f8e7", "f1e1", "b7b5", "a4b3", "d7d6", "c2c3",
           "e8g8", "h2h3", "c6b8", "d2d4", "b8d7"]


def movetext(tokens, **kw):
    return ss.decode(tokens, **kw).to_dict()["movetext"]


# ---- normalising ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("raw,lang,want", [
    ("Sf3", "de", "Nf3"), ("Lb5+", "de", "Bb5"), ("Te1", "de", "Re1"), ("Dd1", "de", "Qd1"), ("e8=D", "de", "e8Q"),
    ("Cf3", "fr", "Nf3"), ("Fb5", "fr", "Bb5"), ("Rg1", "fr", "Kg1"), ("Ab5", "es", "Bb5"), ("Pf3", "nl", "Nf3"),
    ("Фd1", "ru", "Qd1"), ("Крe1", "ru", "Ke1"), ("Кf3", "ru", "Nf3"), ("♘f3", "en", "Nf3"),
    ("0-0", "en", "O-O"), ("0-0-0", "en", "O-O-O"), ("o-o", "en", "O-O"), ("O-O+", "en", "O-O"),
    ("e:d5", "en", "exd5"), ("exd5!?", "en", "exd5"), ("Nxe5#", "en", "Nxe5"), ("12. Nf3", "en", "Nf3"), ("12...Nf6", "en", "Nf6"),
    ("e8(Q)", "en", "e8Q"), ("e8/Q", "en", "e8Q"), ("Ng1-f3", "en", "Ng1-f3"), ("exd6e.p.", "en", "exd6"),
])
def test_normalise_notation(raw, lang, want):
    assert ss.norm(raw, lang) == want


def test_tokens_from_text_drops_numbers_results_and_comments():
    toks = ss.tokens_from_text("[Event \"x\"]\n1.e4 e5 2. Nf3 {a comment} Nc6 (2...d6 3.d4) 3.Bb5 a6 1-0")
    assert [t[0] for t in toks] == ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6"]
    assert [t[0] for t in ss.tokens_from_text("1. d4 Nf6 2. c4 e6 ½-½")] == ["d4", "Nf6", "c4", "e6"]


# ---- decoding --------------------------------------------------------------------------------------------
def test_exact_english_game_is_all_sure():
    d = ss.decode(ss.tokens_from_text(RUY)).to_dict()
    assert [m["uci"] for m in d["moves"]] == RUY_UCI
    assert d["counts"] == {"sure": 20, "check": 0, "guess": 0}
    assert d["language"] == "en" and len(d["fens"]) == 21


@pytest.mark.parametrize("text,lang", [
    ("1. e4 e5 2. Sf3 Sc6 3. Lb5 a6 4. La4 Sf6 5. 0-0 Le7 6. Te1 b5 7. Lb3 d6 8. c3 0-0 9. h3 Sb8 10. d4 Sbd7", "de"),
    ("1. e4 e5 2. Cf3 Cc6 3. Fb5 a6 4. Fa4 Cf6 5. 0-0 Fe7 6. Te1 b5 7. Fb3 d6 8. c3 0-0 9. h3 Cb8 10. d4 Cbd7", "fr"),
    ("1. e4 e5 2. Cf3 Cc6 3. Ab5 a6 4. Aa4 Cf6 5. 0-0 Ae7 6. Te1 b5 7. Ab3 d6 8. c3 0-0 9. h3 Cb8 10. d4 Cbd7", "es"),
    ("1. e4 e5 2. Pf3 Pc6 3. Lb5 a6 4. La4 Pf6 5. 0-0 Le7 6. Te1 b5 7. Lb3 d6 8. c3 0-0 9. h3 Pb8 10. d4 Pbd7", "nl"),
    ("1. e4 e5 2. Кf3 Кc6 3. Сb5 a6 4. Сa4 Кf6 5. 0-0 Сe7 6. Лe1 b5 7. Сb3 d6 8. c3 0-0 9. h3 Кb8 10. d4 Кbd7", "ru"),
])
def test_other_languages_are_detected_and_decoded(text, lang):
    d = ss.decode(ss.tokens_from_text(text)).to_dict()
    assert d["language"] == lang
    assert [m["uci"] for m in d["moves"]] == RUY_UCI


def test_handwriting_misreads_are_fixed_by_legality():
    # long algebraic, b->6 (B65), 0-0, 1->l (Rel), missing capture marks (Ne5, Re5, Qe1)
    toks = [["e2-e4"], ["e7-e5"], ["Ng1-f3"], ["Nc6"], ["B65"], ["Nf6"], ["0-0"], ["Nxe4"], ["Rel"], ["Nd6"], ["Ne5"], ["Be7"],
            ["Bf1"], ["Ne5"], ["Re5"], ["0-0"], ["d4"], ["Bf6"], ["Rel"], ["Re8"], ["Bf4"], ["Re1"], ["Qe1"]]
    d = ss.decode(toks).to_dict()
    assert [m["san"] for m in d["moves"]] == ["e4", "e5", "Nf3", "Nc6", "Bb5", "Nf6", "O-O", "Nxe4", "Re1", "Nd6", "Nxe5", "Be7", "Bf1",
                                              "Nxe5", "Rxe5", "O-O", "d4", "Bf6", "Re1", "Re8", "Bf4", "Rxe1", "Qxe1"]
    assert d["moves"][4]["read"] == "B65" and d["moves"][4]["confidence"] in ("sure", "check")
    assert not d["skipped"] and d["counts"]["guess"] == 0


def test_omitted_disambiguation_and_missing_capture_mark():
    toks = ss.tokens_from_text("1. d4 d5 2. c4 e6 3. Nc3 Nf6 4. Bg5 Be7 5. e3 O-O 6. Nf3 Nd7 7. cd5 ed5")
    sans = [m["san"] for m in ss.decode(toks).to_dict()["moves"]]
    assert sans[11] == "Nbd7" and sans[12:14] == ["cxd5", "exd5"]


def test_later_moves_settle_an_unclear_one():
    # "Bh5" or "Bb5"? Only Bb5 lets White play Bxc6 next.
    toks = [["e4"], ["e5"], ["Nf3"], ["Nc6"], ["Bh5", "Bb5"], ["a6"], ["Bxc6"], ["dxc6"]]
    d = ss.decode(toks).to_dict()
    assert d["moves"][4]["san"] == "Bb5" and d["moves"][6]["san"] == "Bxc6"


def test_locked_moves_win_over_the_reading():
    toks = ss.tokens_from_text("1. e4 e5 2. Nf3 Nc6")
    d = ss.decode(toks, locked={2: "b1c3"}).to_dict()
    assert d["moves"][2]["san"] == "Nc3" and d["moves"][2]["locked"] and d["moves"][2]["confidence"] == "sure"


def test_a_fix_after_a_skipped_scribble_keeps_moves_aligned():
    toks = [["e4"], ["e5"], ["%%%"], ["Nf3"], ["Nc6"], ["Bb5"]]
    first = ss.decode(toks).to_dict()
    assert first["skipped"] == [2] and [m["token"] for m in first["moves"]] == [0, 1, 3, 4, 5]
    # You correct Black's second move (read from entry 4) to d6, with the moves before it kept as they were
    locked = {m["token"]: m["uci"] for m in first["moves"][:3]} | {4: "d7d6"}
    d = ss.decode(toks, locked=locked).to_dict()
    assert [m["san"] for m in d["moves"]] == ["e4", "e5", "Nf3", "d6", "Bb5+"] and d["skipped"] == [2]


def test_blank_cells_and_noise():
    d = ss.decode([["e4"], [""], ["Nf3"], ["Nc6"]]).to_dict()  # a blank: some legal move is guessed, the rest continues
    assert len(d["moves"]) == 4 and d["moves"][1]["confidence"] == "guess" and d["moves"][2]["san"] == "Nf3"
    d = ss.decode([["e4"], ["e5"], ["%%%"], ["Nf3"], ["Nc6"]]).to_dict()  # a stray scribble is skipped
    assert [m["san"] for m in d["moves"]] == ["e4", "e5", "Nf3", "Nc6"] and d["skipped"] == [2]


def test_moves_after_mate_are_left_unused():
    d = ss.decode(ss.tokens_from_text("1. f3 e5 2. g4 Qh4# 3. Nc3 Nc6")).to_dict()
    assert d["movetext"].endswith("Qh4#") and d["unused"] == 2


# ---- the vision model's reply ---------------------------------------------------------------------------------
def test_sheet_parsing_is_forgiving_but_strict_about_shape():
    reply = "Here you go:\n```json\n" + json.dumps({
        "white": "Sikka, Aanya", "black": "Opponent", "result": "½-½", "date": "2026-09-25",
        "moves": [{"n": "2.", "white": "Nf3", "black": ["Nc6", "Nc8"]}, {"n": 1, "white": ["e4"], "black": "e5"},
                  {"n": 3, "white": ["Bb5"], "black": []}]}) + "\n```"
    sheet = ss.parse_sheet(reply)
    assert sheet.tokens() == [["e4"], ["e5"], ["Nf3"], ["Nc6", "Nc8"], ["Bb5"]]
    assert sheet.headers()["result"] == "1/2-1/2" and sheet.headers()["white"] == "Sikka, Aanya"
    with pytest.raises(ss.VisionError):
        ss.parse_sheet("I can't read this image.")


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def test_transcribe_openai_compatible_sends_the_image_and_retries_without_json_mode():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        calls.append(body)
        if "response_format" in body:
            return httpx.Response(400, json={"error": {"message": "response_format is not supported with images"}})
        content = body["messages"][0]["content"]
        assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"moves": [{"n": 1, "white": ["e4"], "black": ["e5"]}]})}}],
                                         "usage": {"total_tokens": 2300}})

    vb = ss.VisionBackend("openai", "qwen/qwen3.8-27b", "https://api.groq.com/openai/v1", "k")
    sheet, used = ss.transcribe(JPEG, vb, http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert sheet.tokens() == [["e4"], ["e5"]] and used == 2300 and len(calls) == 2


def test_transcribe_anthropic_and_errors():
    def ok(req: httpx.Request) -> httpx.Response:
        assert req.headers["x-api-key"] == "sk" and req.url.host == "api.anthropic.com"
        body = json.loads(req.content)
        assert body["messages"][0]["content"][0]["source"]["media_type"] == "image/jpeg"
        return httpx.Response(200, json={"content": [{"type": "text", "text": '{"moves": [{"n": 1, "white": ["d4"]}]}'}],
                                         "usage": {"input_tokens": 1800, "output_tokens": 40}})

    vb = ss.VisionBackend("anthropic", "claude-sonnet-5-5", api_key="sk")
    sheet, used = ss.transcribe(JPEG, vb, http=httpx.Client(transport=httpx.MockTransport(ok)))
    assert sheet.tokens() == [["d4"]] and used == 1840
    denied = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401, json={})))
    with pytest.raises(ss.VisionError, match="key was rejected"):
        ss.transcribe(JPEG, vb, http=denied)
    with pytest.raises(ss.VisionError, match="JPEG"):
        ss.transcribe(b"GIF89a....", vb, http=denied)


def test_vision_backend_choice():
    from dataclasses import replace

    from app.config import load_settings

    s = load_settings()
    assert ss.vision_backend(replace(s, anthropic_api_key=None, llm_base_url=None, vision_base_url=None)) is None
    groq = ss.vision_backend(replace(s, anthropic_api_key=None, llm_base_url="https://api.groq.com/openai/v1", llm_api_key="g"))
    assert groq.kind == "openai" and groq.model == ss.GROQ_VISION_DEFAULT
    claude = ss.vision_backend(replace(s, anthropic_api_key="sk", coach_model="claude-x"))
    assert claude.kind == "anthropic" and claude.model == "claude-x"
    assert ss.vision_backend(replace(s, anthropic_api_key="sk", vision_provider="off")) is None


# ---- the PGN ---------------------------------------------------------------------------------------------------
def test_build_pgn_cleans_headers_and_checks_legality():
    pgn = ss.build_pgn(RUY_UCI, 'Sikka, "Aanya"', "Rival [x]", "1-0", date="25/09/2026", event="Club ch")
    game = chess.pgn.read_game(__import__("io").StringIO(pgn))
    assert game.headers["White"] == "Sikka, Aanya" and game.headers["Black"] == "Rival x" and game.headers["Date"] == "2026.09.25"
    assert [m.uci() for m in game.mainline_moves()] == RUY_UCI
    with pytest.raises(ValueError):
        ss.build_pgn(["e2e4", "e2e4"], "a", "b", "1-0")


# ---- the API -------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def app_mod():
    import app.main as m

    return m


def test_the_camera_is_allowed_for_this_site_only(app_mod):
    from fastapi.testclient import TestClient

    policy = TestClient(app_mod.app).get("/api/health").headers["permissions-policy"]
    assert "camera=(self)" in policy and "microphone=()" in policy


def test_scan_info_and_photo_without_a_vision_model(app_mod):
    c = client(app_mod, "scan-info@test.com")
    info = c.get("/api/scan").json()
    assert info["photo"] is False and info["per_day"] == 3 and "de" in info["languages"]
    r = c.post("/api/scan/photo", files={"file": ("s.jpg", JPEG, "image/jpeg")})
    assert r.status_code == 400 and "Type the moves" in r.json()["detail"]


def test_scan_photo_with_a_mocked_reader(app_mod, monkeypatch):
    c = client(app_mod, "scan-photo@test.com")
    vb = ss.VisionBackend("openai", "m", "https://example.test/v1", "k")
    monkeypatch.setattr(app_mod.scoresheet, "vision_backends", lambda s: [vb])
    sheet = ss.Sheet.model_validate({"white": "Me", "black": "You", "result": "1-0",
                                     "moves": [{"n": i + 1, "white": [w], "black": [b]} for i, (w, b) in
                                               enumerate(zip(RUY_UCI[::2], RUY_UCI[1::2], strict=True))]})
    monkeypatch.setattr(app_mod.scoresheet, "transcribe", lambda data, backend: (sheet, 1234))
    r = c.post("/api/scan/photo", files={"file": ("s.jpg", JPEG, "image/jpeg")})
    assert r.status_code == 200, r.text
    out = r.json()
    assert [m["uci"] for m in out["decoded"]["moves"]] == RUY_UCI and out["sheet"]["result"] == "1-0"
    assert c.post("/api/scan/photo", files={"file": ("s.gif", b"GIF89a", "image/gif")}).status_code == 400
    for _ in range(2):
        c.post("/api/scan/photo", files={"file": ("s.jpg", JPEG, "image/jpeg")})
    r = c.post("/api/scan/photo", files={"file": ("s.jpg", JPEG, "image/jpeg")})  # free plan: 3 photos a day
    assert r.status_code == 429 and r.json()["detail"]["upgrade"]
    app_mod.switches.set_switch(app_mod.store, "scanner", False)
    try:
        assert c.post("/api/scan/photo", files={"file": ("s.jpg", JPEG, "image/jpeg")}).status_code == 503
    finally:
        app_mod.switches.set_switch(app_mod.store, "scanner", True)


def test_scan_decode_endpoint(app_mod):
    c = client(app_mod, "scan-decode@test.com")
    r = c.post("/api/scan/decode", json={"text": "1. e4 e5 2. Sf3 Sc6"})
    assert r.status_code == 200 and r.json()["decoded"]["language"] == "de"
    r = c.post("/api/scan/decode", json={"tokens": r.json()["tokens"], "locked": {"2": "b1c3", "x": "zz"}})
    assert r.json()["decoded"]["moves"][2]["san"] == "Nc3"
    assert c.post("/api/scan/decode", json={"text": "   "}).status_code == 400


@needs_engine
def test_scan_save_creates_an_over_the_board_account_and_analyses_the_game(app_mod):
    c = client(app_mod, "scan-save@test.com")
    body = {"moves": RUY_UCI, "color": "white", "result": "1-0", "white": "Aanya Sikka", "black": "Club Rival", "date": "2026-09-25",
            "event": "Club championship"}
    assert c.post("/api/scan/save", json={**body, "moves": RUY_UCI[:6]}).status_code == 400  # too short to analyse
    r = c.post("/api/scan/save", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert c.get(f"/api/jobs/{out['job_id']}").json()["state"] == "done"
    acct = next(a for a in c.get("/api/me").json()["accounts"] if a["id"] == out["account_id"])
    assert acct["platform"] == "pgn" and acct["handle"] == "Aanya Sikka" and acct["games"] == 1
    assert '[Annotator "Plateau Breaker scoresheet scanner"]' in out["pgn"]
    # The library needs Pro; a second game goes to the same account
    assert c.post("/api/scan/save", json={**body, "target": "library"}).status_code == 402
    r = c.post("/api/scan/save", json={**body, "account_id": out["account_id"], "result": "0-1"})
    assert r.status_code == 200 and r.json()["account_id"] == out["account_id"]


@needs_engine
def test_scan_save_to_the_opponent_library_with_pro(app_mod):
    c = client(app_mod, "admin@test.com")  # admins get Pro
    body = {"moves": RUY_UCI, "color": "black", "result": "1/2-1/2", "white": "Local Rival", "black": "Me Myself", "target": "library"}
    r = c.post("/api/scan/save", json=body)
    assert r.status_code == 200, r.text
    pid = r.json()["library_player"]
    lib = c.get("/api/library").json()["players"]
    assert any(p["id"] == pid and p["handle"] == "Local Rival" for p in lib)
    assert c.post("/api/scan/save", json={**body, "white": ""}).status_code == 400  # the library needs a name


# ---- several readers, a second look, a human-move prior ------------------------------------------------------------
def _sheet(*cells):
    """cells: readings per ply, White and Black alternating."""
    rows = []
    for i in range(0, len(cells), 2):
        rows.append({"n": i // 2 + 1, "white": cells[i], "black": cells[i + 1] if i + 1 < len(cells) else []})
    return ss.Sheet.model_validate({"moves": rows})


def test_merging_readers_weights_agreement():
    a = _sheet(["e4"], ["e5"], ["Nf3"], ["Nc6", "Nc8"])
    b = _sheet(["e4"], ["e5"], ["Nf3"], ["Nc6"], ["Bb5"])
    tokens, priors, slots, votes = ss.merge_sheets([a, b])
    assert tokens == [["e4"], ["e5"], ["Nf3"], ["Nc6", "Nc8"], ["Bb5"]]
    assert slots[3] == (2, "black") and slots[4] == (3, "white")
    assert priors[3][0] == 0.0 and priors[3][1] == pytest.approx(-1.386, abs=0.01)  # 0.5 against 2.0
    assert votes == [2, 2, 2, 2, 1]
    c = _sheet(["e4"], ["e5"], ["Nf3"], ["Nf6"])  # a reader that disagrees: a tie, and only one vote for the winner
    tokens, priors, _, votes = ss.merge_sheets([a, c])
    assert set(tokens[3][:2]) == {"Nc6", "Nf6"} and priors[3][:2] == [0.0, 0.0] and votes[3] == 1
    h = ss.merge_headers([ss.Sheet(white="Ann", result="1-0"), ss.Sheet(white="Anne", black="Bob", event="Club")])
    assert h["white"] == "Ann" and h["black"] == "Bob" and h["result"] == "1-0" and h["event"] == "Club"


class FakeEngine:
    """Prefers Nc6 or Nf6 after 1.e4 e5 2.Nf3, as told."""

    def __init__(self, best):
        self.best, self.calls = best, 0

    def candidates(self, board, n, limit):
        self.calls += 1
        other = "g8f6" if self.best == "b8c6" else "b8c6"
        return [(self.best, 30), (other, -100), ("d7d6", -150)]


def test_the_human_move_prior_breaks_ties_between_readers():
    toks, pri = [["e4"], ["e5"], ["Nf3"], ["Nf6", "Nc6"]], [[0.0], [0.0], [0.0], [0.0, 0.0]]
    for best, san in (("b8c6", "Nc6"), ("g8f6", "Nf6")):
        eng = FakeEngine(best)
        fn = ss.engine_prior(eng)
        d = ss.decode(toks, priors=pri, prior_fn=fn, prior_on={3})
        assert d.plies[3].san == san
        assert eng.calls <= ss.PRIOR_STATES  # only the unclear entry asks, for the best few readings so far
    h, floor = ss.engine_prior(FakeEngine("b8c6"))(chess.Board("rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2"))
    assert h["b8c6"] == 0.0 and h["g8f6"] < 0 and floor < min(h.values())

    class Broken:
        def candidates(self, *a):
            raise RuntimeError("engine died")

    assert ss.engine_prior(Broken())(chess.Board()) == ({}, 0.0)  # no prior, no crash


def _verifier(answers, seen):
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        text = body["messages"][0]["content"][0]["text"]
        seen.append(text)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"answers": answers})}}], "usage": {"total_tokens": 500}})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_the_second_look_confirms_unclear_moves():
    toks = [["e4"], ["e5"], ["Nf3"], ["N6"]]
    pri = [[0.0]] * 4
    slots = [(1, "white"), (1, "black"), (2, "white"), (2, "black")]
    first = ss.decode(toks, priors=pri)
    assert first.plies[3].confidence != "sure"
    seen = []
    vb = ss.VisionBackend("openai", "m", "https://example.test/v1", "k")
    t2, p2, confirmed, used = ss.verify([JPEG], vb, toks, pri, slots, first, http=_verifier([{"id": 1, "choice": "Nf6"}], seen))
    q = json.loads(seen[0].split("Questions:\n", 1)[1])
    assert q[0]["move"] == "move 2, black" and q[0]["we_read"] == "N6" and len(q) == 1 and "Nf6" in q[0]["options"]
    assert t2[3][0] == "g8f6" and p2[3][0] == ss.VERIFY_PRIOR and confirmed == {3} and used == 500
    final = ss.decode(t2, priors=p2, checked=confirmed)
    assert final.plies[3].san == "Nf6" and final.plies[3].confidence == "sure" and final.plies[3].checked
    assert final.plies[3].raw == "N6"  # what's on the sheet, not the coordinates of the answer
    # an answer outside the options still counts if it's legal there; "unclear" and nonsense don't
    t3, _, c3, _ = ss.verify([JPEG], vb, toks, pri, slots, first, http=_verifier([{"id": 1, "choice": "Na6"}, {"id": 7, "choice": "e4"}], []))
    assert t3[3][0] == "b8a6" and c3 == {3}
    for junk in ([{"id": 1, "choice": "unclear"}], [{"id": 1, "choice": "Qh5"}], "nope"):
        assert ss.verify([JPEG], vb, toks, pri, slots, first, http=_verifier(junk, []))[2] == set()
    sure = ss.decode([["e4"], ["e5"]])
    assert ss.verify([JPEG], vb, [["e4"], ["e5"]], [[0.0]] * 2, slots[:2], sure, http=_verifier([], seen)) == ([["e4"], ["e5"]], [[0.0]] * 2, set(), 0)


def test_read_photo_runs_the_whole_pipeline(monkeypatch):
    a = ss.VisionBackend("openai", "reader-a", "https://api.groq.com/openai/v1", "k")
    b = ss.VisionBackend("openai", "reader-b", "https://generativelanguage.googleapis.com/v1beta/openai", "g")
    sheets = {"reader-a": _sheet(["e4"], ["e5"], ["Nf3"], ["N6"], ["Bb5"]), "reader-b": _sheet(["e4"], ["e5"], ["Nf3"], ["N6"])}
    got = []

    def fake_transcribe(images, backend):
        got.append((len(images), backend.model))
        return sheets[backend.model], 1000

    monkeypatch.setattr(ss, "transcribe", fake_transcribe)
    out = ss.read_photo([JPEG, JPEG, JPEG], [a, b], engine=FakeEngine("g8f6"), http=_verifier([{"id": 1, "choice": "Nf6"}], []))
    assert got == [(3, "reader-a"), (3, "reader-b")]  # both readers saw the sheet and its two zoomed halves
    assert out["readers"] == ["Groq · reader-a", "Gemini · reader-b"] and out["used"] == 2500 and out["checked"] == 1
    assert [p.san for p in out["decoded"].plies] == ["e4", "e5", "Nf3", "Nf6", "Bb5"] and out["votes"][3] == 2
    assert out["decoded"].plies[3].confidence == "sure" and out["slots"][4] == [3, "white"]

    def failing(images, backend):
        if backend.model == "reader-a":
            raise ss.VisionError("rate limit")
        return sheets["reader-b"], 10

    monkeypatch.setattr(ss, "transcribe", failing)  # one reader down: the other one's reading stands
    down = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429, json={})))
    out = ss.read_photo([JPEG], [a, b], http=down)  # the second look fails too: the first reading stands
    assert out["readers"] == ["Gemini · reader-b"] and len(out["decoded"].plies) == 4 and out["checked"] == 0
    monkeypatch.setattr(ss, "transcribe", lambda images, backend: (_ for _ in ()).throw(ss.VisionError("down")))
    with pytest.raises(ss.VisionError, match="down"):
        ss.read_photo([JPEG], [a, b])


@needs_engine
def test_engine_prior_with_stockfish(engine):
    fn = ss.engine_prior(engine, depth=6)
    board = chess.Board()
    h, floor = fn(board)
    assert max(h.values()) == 0.0 and len(h) >= 4 and floor < min(h.values())
    assert fn(board) is fn(board)  # cached
