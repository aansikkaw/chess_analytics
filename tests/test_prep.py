"""ChessBase support: file decoding, name matching, repertoire parsing and Prep Check."""

import io
import zipfile

import chess
import pytest

from app.analysis import analyse_games
from app.games import GameImportError, decode_upload, name_matches, parse_pgn, player_names
from app.prep import check_repertoire, detect_color, parse_repertoire
from tests.conftest import needs_engine

# A ChessBase-style White repertoire: comments, NAGs, nested variations, a transposition.
WHITE_REP = """[Event "Repertoire 1.e4"]
[White "Repertoire"]
[Black "?"]
[Result "*"]
[Annotator "Sikka, Aanya"]

1. e4 {Main weapon} e5 $1 (1... c6 2. d4 d5 3. e5 Bf5 4. Nf3) (1... c5 2. Nf3 d6 (2... Nc6 3. d4 cxd4 4. Nxd4)
3. d4 cxd4 4. Nxd4 Nf6 5. Nc3) 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 Nf6 5. O-O *

[Event "Repertoire 1.e4 ch2"]
[White "Repertoire"]
[Black "?"]
[Result "*"]

1. e4 g6 2. d4 Bg7 3. Nc3 d6 4. f4 *
"""

BLACK_REP = """[Event "Black vs 1.e4"]
[White "?"]
[Black "Repertoire"]
[Result "*"]

1. e4 c5 2. Nf3 (2. Nc3 Nc6 3. f4 g6) (2. c3 Nf6 3. e5 Nd5) 2... d6 3. d4 cxd4 4. Nxd4 Nf6 5. Nc3 a6 *
"""


def _game(site, white, black, result, moves):
    return (f'[Site "https://lichess.org/{site}"]\n[White "{white}"]\n[Black "{black}"]\n[Result "{result}"]\n'
            f'[UTCDate "2026.09.10"]\n[TimeControl "5400+30"]\n\n{moves} {result}\n\n')


# Aanya (as "Sikka, Aanya", ChessBase style) plays the Ruy Lopez prep correctly, deviates in the Caro-Kann twice,
# and meets an uncovered reply (1...e5 2.Nf3 d6) once.
GAMES = (
    _game("g0000001", "Sikka, Aanya", "Opp A", "1-0", "1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 Nf6 5. O-O Be7 6. Re1 b5")
    + _game("g0000002", "Sikka, Aanya", "Opp B", "0-1", "1. e4 c6 2. Nc3 d5 3. Nf3 Bg4 4. h3 Bxf3 5. Qxf3 e6 6. d4 Nf6")
    + _game("g0000003", "Sikka, Aanya", "Opp C", "1/2-1/2", "1. e4 c6 2. Nc3 d5 3. Nf3 dxe4 4. Nxe4 Nf6 5. Nxf6+ exf6 6. d4 Bd6")
    + _game("g0000004", "Sikka, Aanya", "Opp D", "1-0", "1. e4 e5 2. Nf3 d6 3. d4 exd4 4. Nxd4 Nf6 5. Nc3 Be7 6. Be2 O-O")
    + _game("g0000005", "Sikka, Aanya", "Opp E", "1-0", "1. d4 d5 2. c4 e6 3. Nc3 Nf6 4. Bg5 Be7 5. e3 O-O 6. Nf3 h6")
)


# ---- uploads --------------------------------------------------------------------------------
def test_decode_cp1252_and_zip():
    raw = '[White "Nepomniachtchi, Ján"]\n[Black "x"]\n[Result "*"]\n\n1. e4 *\n'.encode("cp1252")
    assert "Ján" in decode_upload(raw, "games.pgn")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.pgn", raw)
        z.writestr("b.PGN", "1. d4 *")
        z.writestr("readme.txt", "ignore me")
    text = decode_upload(buf.getvalue(), "export.zip")
    assert "Ján" in text and "1. d4" in text and "ignore" not in text


def test_chessbase_native_files_get_export_instructions():
    with pytest.raises(GameImportError, match="File > New > Database"):
        decode_upload(b"\x00\x01", "MyGames.cbh")


@pytest.mark.parametrize("header,wanted,ok", [
    ("Sikka, Aanya", "Aanya Sikka", True), ("Sikka,Aanya", "aanya sikka", True), ("Šikka, Aanya", "Sikka Aanya", True),
    ("AanLetsGo", "aanletsgo", True), ("Aanya Sharma", "Aanya", False), ("Sikka, Aanya (IND)", "Aanya Sikka", True),
])
def test_name_matching(header, wanted, ok):
    assert name_matches(header, wanted) is ok


def test_parse_with_chessbase_names_and_name_listing():
    recs = parse_pgn(GAMES, "Aanya Sikka")
    assert len(recs) == 5 and all(r.player_color == chess.WHITE for r in recs)
    assert player_names(GAMES)[0] == ("Sikka, Aanya", 5)
    with pytest.raises(GameImportError, match="Names in the file: Sikka, Aanya"):
        parse_pgn(GAMES, "Someone Else")


# ---- repertoire parsing ------------------------------------------------------------------------------
def test_parse_repertoire_tree():
    t = parse_repertoire(WHITE_REP, chess.WHITE)
    assert t.chapters == 2 and len(t.leaves) == 5
    start = chess.Board().epd()
    assert t.my_moves[start] == ["e4"]
    after_e4 = chess.Board()
    after_e4.push_san("e4")
    assert set(t.their_moves[after_e4.epd()]) == {"e5", "c6", "c5", "g6"}


def test_detect_color():
    assert detect_color(WHITE_REP) == chess.WHITE
    assert detect_color(BLACK_REP) == chess.BLACK


def test_empty_repertoire_is_an_error():
    with pytest.raises(GameImportError, match="No prepared moves"):
        parse_repertoire('[Event "x"]\n[Result "*"]\n\n*\n', chess.WHITE)


# ---- Prep Check ------------------------------------------------------------------------------------
@needs_engine
def test_prep_check_finds_deviations_gaps_and_drills(engine):
    games = analyse_games(parse_pgn(GAMES, "Aanya Sikka"), engine)
    r = check_repertoire(parse_repertoire(WHITE_REP, chess.WHITE), games, engine, "e4 file")
    s = r["summary"]
    assert s["games"] == 4 and s["other_openings"] == 1  # the 1.d4 game isn't this repertoire
    dev = r["deviations"][0]
    assert dev["line"] == "1.e4 c6" and dev["prep_moves"] == ["d4"] and dev["played"] == "Nc3" and dev["times"] == 2
    gap = r["gaps"][0]
    assert gap["opponent_move"] == "d6" and gap["line"].startswith("1.e4 e5 2.Nf3 d6") and gap.get("engine_reply")
    assert s["followed_pct"] == 50  # 2 of 4 games: deviated in both Caro-Kann games
    d0 = r["drills"][0]
    assert d0["you_played"] == "Nc3" and d0["accept"] == ["d2d4"]  # drills start with what you got wrong


@needs_engine
def test_prep_holes_detected(engine):
    bad = '[Event "x"]\n[Result "*"]\n\n1. e4 e5 2. Qh5 Nc6 3. Qxe5+ Nxe5 *\n'  # a line that drops the queen
    games = analyse_games(parse_pgn(GAMES, "Aanya Sikka"), engine)
    r = check_repertoire(parse_repertoire(bad, chess.WHITE), games, engine, "bad")
    assert r["holes"] and r["holes"][0]["cp"] < -300
