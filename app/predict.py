"""Puzzles from mistakes you haven't made yet.

Other sites give you puzzles at your rating. This picks the ones *you* are likely to get wrong, before you
ever meet them in a game, and says why.

The model
---------
For a puzzle rated R_p and a player whose puzzle rating is R:

    logit P(miss) = (R_p - R) * ln10/400            what a typical player at your level would do (Elo)
                  - offset(themes) * ln10/400        your skills: Rating DNA, then your own puzzle results
                  + blind_spot                       your Twin vs a typical player's Twin, on this position

* **Your Twin** is the Opponent Twin's choice model, fitted to *your* games: from Stockfish's candidate moves
  it learns what you tend to play (captures or quiet moves, checks, trades, retreats, how much you slip in
  each phase). Every bank puzzle is annotated once with the same candidates (see app/puzzlebank.py), so
  your Twin can "play" thousands of puzzles in a second.
* **blind_spot** compares your Twin's *instincts* with a typical player's. In a game the Twin also weighs how
  much each move loses, but a puzzle hides exactly that (it's what the puzzle rating measures). So here the
  Twin uses only its style weights (captures, checks, trades, retreats, king hunts, recaptures...) to share
  out its attention among Stockfish's candidates: blind_spot = log A_typical(solution) - log A_you(solution),
  where the typical Twin is the average of every Twin on this server near your rating, shrunk toward the
  rating prior. Positive means "your instincts look elsewhere here".
* **Your puzzle rating** starts from your game rating and moves with every attempt (Glicko-1). Theme offsets
  start from Rating DNA (e.g. weaker endgames) and learn from your results on each theme.

We serve puzzles inside your range (you'd miss 35-88% of them) where the *personal* part is largest, mixed
across motifs. After each answer we check the prediction (expected vs actual misses), so the page can say
honestly how well it's predicting you.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import random
import threading
import time
from collections import defaultdict

import chess
import chess.engine

from . import puzzlebank
from . import twin as twin_mod
from .analysis import GameAnalysis, classify, move_accuracy
from .games import GameRecord, parse_time_control

LN10_400 = math.log(10) / 400
Q = LN10_400
RD_MIN, RD_MAX, RD_START = 60.0, 350.0, 300.0
PUZZLE_RATING_OFFSET = 150  # puzzle ratings run higher than game ratings for the same player; we learn the rest
DNA_SHRINK = 0.6  # a skill 100 below your average lowers matching puzzles' expected result by 60 points
BLIND_WEIGHT = 0.7
BLIND_CLAMP = 1.5
# The Twin's instincts: everything except how much a move loses and whether it's the engine's choice
INSTINCT_SKIP = set(twin_mod.LOSS) | {twin_mod.FEATURES.index("best"), twin_mod.FEATURES.index("top3")}
BAND_BELOW, BAND_ABOVE = 250, 400  # which bank puzzles are candidates for you
MISS_MIN, MISS_MAX = 0.35, 0.88  # learnable: not trivial, not hopeless
THEME_K = 30.0
THEME_CAP = 250.0
SELF_MIN_GAMES = 10
SELF_MAX_DECISIONS = 12_000
STUDY_CHUNK = 10
ANNOTATE_CHUNK = 40

# Which Rating DNA skill a Lichess theme draws on
TACTIC_THEMES = {"fork", "pin", "skewer", "discoveredAttack", "doubleCheck", "hangingPiece", "sacrifice", "deflection", "attraction",
                 "clearance", "interference", "intermezzo", "xRayAttack", "trappedPiece", "capturingDefender", "mate", "mateIn1",
                 "mateIn2", "mateIn3", "mateIn4", "mateIn5", "backRankMate", "smotheredMate", "anastasiaMate", "arabianMate",
                 "bodenMate", "doubleBishopMate", "dovetailMate", "hookMate", "attackingF2F7", "kingsideAttack", "queensideAttack",
                 "exposedKing", "promotion", "advancedPawn", "underPromotion", "enPassant"}
THEME_SKILL: dict[str, str] = {t: "tactics" for t in TACTIC_THEMES}
THEME_SKILL.update({"opening": "openings", "middlegame": "middlegame", "endgame": "endgames", "defensiveMove": "defending",
                    "equality": "defending", "quietMove": "middlegame"})
THEME_SKILL.update({t: "endgames" for t in puzzlebank.ENDGAME_THEMES})
SKILL_WORD = {"openings": "Openings", "middlegame": "Middlegame play", "endgames": "Endgames", "tactics": "Tactics",
              "defending": "Defending", "converting": "Converting", "time": "Time management"}

# What a feature difference between your Twin's pick and the solution says about you: (feature, +1 if the pick has more of it)
FEATURE_WHY = {
    ("capture", 1): "you like to grab material", ("capture", -1): "you tend to keep the tension instead of capturing",
    ("check", 1): "you reach for checks first", ("check", -1): "checks aren't the first thing you look at",
    ("trade", 1): "you like to trade pieces", ("trade", -1): "you tend to avoid trades",
    ("recapture", 1): "you take back almost automatically", ("escape", 1): "you move attacked pieces to safety first",
    ("escape", -1): "you leave pieces hanging to follow your own idea", ("pawn_push", 1): "you like pawn moves",
    ("pawn_push", -1): "pawn moves are easy for you to overlook", ("forward", 1): "you prefer moving pieces forward",
    ("forward", -1): "backward moves are easy for you to overlook", ("king_hunt", 1): "you head for the enemy king",
    ("king_hunt", -1): "moves toward the enemy king aren't your first thought", ("same_piece", 1): "you keep moving the same piece",
    ("develop", 1): "you develop pieces first", ("castle", 1): "you castle as soon as you can",
    ("queen_early", 1): "you bring the queen out early", ("king_walk", 1): "you're happy to walk the king",
}
STYLE_TEXT = twin_mod.STYLE_TEXT


# ==== your Twin, from your own analysed games ==========================================================
def record_of(g: GameAnalysis) -> GameRecord | None:
    """An analysed game back as a GameRecord (moves in UCI, clock before each of your moves) for the Twin's study."""
    board, ucis = chess.Board(), []
    for san in g.moves_san:
        try:
            mv = board.parse_san(san)
        except ValueError:
            return None
        ucis.append(mv.uci())
        board.push(mv)
    if len(ucis) < 10:
        return None
    clocks: list[float | None] = [None] * len(ucis)
    for m in g.moves:  # the clock before your move = what you had left after your previous move
        if m.clock_before is not None and m.ply >= 2 and m.ply - 2 < len(clocks):
            clocks[m.ply - 2] = m.clock_before
    base, _ = parse_time_control(g.time_control)
    return GameRecord(game_id=g.game_id, white="", black="", player_color=chess.WHITE if g.player_color == "white" else chess.BLACK,
                      player_rating=g.player_rating, result=g.result, time_control=g.time_control, initial_seconds=base,
                      opening=g.opening, played_at=g.played_at, moves=ucis, clocks=clocks, time_class=g.time_class)


def studied_ids(store, key: str, depth: int) -> set[str]:
    with store._conn() as c:
        return {r[0] for r in c.execute("SELECT game_id FROM self_twin_games WHERE username = ? AND version = ? AND depth = ?",
                                        (key, twin_mod.VERSION, depth))}


def pick_own_unstudied(games: list[GameAnalysis], done: set[str], budget: int) -> list[str]:
    todo = [g.game_id for g in sorted(games, key=lambda g: (g.played_at, g.game_id), reverse=True) if g.game_id not in done]
    return todo[:max(0, budget)]


def study_own(store, key: str, games: dict[str, GameAnalysis], ids: list[str], engine, depth: int, rng: random.Random,
              on_game=None) -> int:
    """Study these games of yours (each once, ever): your decisions among Stockfish's candidates."""
    limit = chess.engine.Limit(depth=depth, time=0.5)
    n = 0
    for i, gid in enumerate(ids, 1):
        g = games.get(gid)
        rec = record_of(g) if g else None
        decisions = twin_mod.collect_decisions([rec], engine, limit, rng) if rec else []
        with store._conn() as c:
            c.execute("INSERT OR REPLACE INTO self_twin_games (username, game_id, version, depth, n, data, studied_at) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?)", (key, gid, twin_mod.VERSION, depth, len(decisions), twin_mod.pack(decisions), time.time()))
        n += len(decisions)
        if on_game:
            on_game(i)
    return n


def _recency(played_at: str, newest: dt.date) -> float:
    try:
        d = dt.date(*map(int, played_at[:10].split(".")))
    except (ValueError, TypeError):
        return 0.5
    return 0.5 ** (max(0, (newest - d).days) / twin_mod.MODEL_HALF_LIFE_DAYS)


def fit_self(store, key: str, games: list[GameAnalysis], depth: int, min_games: int = SELF_MIN_GAMES) -> dict:
    """Fit your Twin on every game of yours studied so far; faithfulness on held-out games; save it."""
    by_id = {g.game_id: g for g in games}
    with store._conn() as c:
        rows = c.execute("SELECT game_id, data FROM self_twin_games WHERE username = ? AND version = ? AND depth = ?",
                         (key, twin_mod.VERSION, depth)).fetchall()
    studied = []
    for r in rows:
        g = by_id.get(r["game_id"])
        decs = twin_mod.unpack(r["data"])
        if g and decs:
            studied.append((g, decs))
    studied.sort(key=lambda x: (x[0].played_at, x[0].game_id), reverse=True)
    used, total = [], 0
    for g, decs in studied:
        used.append((g, decs))
        total += len(decs)
        if total >= SELF_MAX_DECISIONS:
            break
    if len(used) < min_games:
        raise ValueError(f"Your Twin needs at least {min_games} studied games; there are {len(used)}.")
    if total < 30:
        raise ValueError("Not enough of your own moves to learn from yet.")
    try:
        newest = dt.date(*map(int, used[0][0].played_at[:10].split(".")))
    except (ValueError, TypeError):
        newest = dt.date.today()
    ratings = sorted(g.player_rating for g, _ in used if g.player_rating)
    rating = ratings[len(ratings) // 2] if ratings else None
    beta0 = twin_mod.prior(rating)
    weights = [_recency(g.played_at, newest) for g, _ in used]
    test_idx = {i for i in range(len(used)) if i % 5 == 4} if len(used) >= 10 else set()
    train = [(d["rows"], d["chosen"], weights[i]) for i, (_, decs) in enumerate(used) if i not in test_idx for d in decs]
    test = [d for i in sorted(test_idx) for d in used[i][1]]
    faith = twin_mod.evaluate(twin_mod.fit(train, beta0), test) if test else None
    beta, se = twin_mod.fit([(d["rows"], d["chosen"], weights[i]) for i, (_, decs) in enumerate(used) for d in decs], beta0, with_se=True)
    decisions = [d for _, decs in used for d in decs]
    prof = self_profile(decisions, beta, beta0, se)
    prof.update(rating=rating, faithfulness=faith, games=len(used), studied=len(studied), decisions=len(decisions))
    with store._conn() as c:
        c.execute("INSERT INTO self_twins (username, version, built_at, games, decisions, params, profile) VALUES (?, ?, ?, ?, ?, ?, ?) "
                  "ON CONFLICT (username) DO UPDATE SET version = excluded.version, built_at = excluded.built_at, games = excluded.games, "
                  "decisions = excluded.decisions, params = excluded.params, profile = excluded.profile",
                  (key, twin_mod.VERSION, time.time(), len(used), len(decisions), json.dumps(beta), json.dumps(prof)))
    _POP_CACHE.clear()
    return load_self(store, key)


def self_profile(decisions: list[dict], beta: list[float], beta0: list[float], se: list[float]) -> dict:
    by_phase: dict[str, list[float]] = defaultdict(list)
    blunders = 0
    for d in decisions:
        by_phase[d["phase"]].append(move_accuracy(100 * d["best_es"], 100 * d["played_es"]))
        blunders += classify(100 * d["loss"]) == "blunder"
    style = []
    for k, name in enumerate(twin_mod.FEATURES):
        if k in twin_mod.LOSS or name == "best":
            continue
        delta = beta[k] - beta0[k]
        if abs(delta) >= 0.5 and abs(delta) >= 3 * se[k]:
            text = STYLE_TEXT.get((name, 1 if delta > 0 else -1))
            if text:
                style.append({"feature": name, "text": text, "strength": round(min(1.0, abs(delta) / 1.5), 2)})
    style.sort(key=lambda s: -s["strength"])
    n = len(decisions)
    return {"accuracy": {p: round(sum(v) / len(v), 1) if v else None for p, v in ((p, by_phase.get(p, [])) for p in puzzlebank.PHASES)},
            "blunders_per_100": round(100 * blunders / n, 1) if n else None, "style": style[:5],
            "confidence": "high" if n >= 600 else "medium" if n >= 250 else "low"}


def load_self(store, key: str) -> dict | None:
    with store._conn() as c:
        r = c.execute("SELECT * FROM self_twins WHERE username = ?", (key,)).fetchone()
    if not r or r["version"] != twin_mod.VERSION:
        return None
    t = dict(r)
    t["params"] = json.loads(t["params"])
    t["profile"] = json.loads(t["profile"])
    return t


# ==== a typical player's Twin at a given rating (empirical Bayes over every Twin on this server) ===========
_POP_CACHE: dict[tuple, tuple[float, list[float]]] = {}
_POP_LOCK = threading.Lock()
POP_PSEUDO = 3.0  # the rating prior counts as three Twins
POP_WIDTH = 250.0


def population_beta(store, rating: float, exclude: str | None = None) -> list[float]:
    """The average fitted Twin of players near this rating (yours left out), shrunk toward the rating prior."""
    ck = (round(rating / 25) * 25, exclude)
    now = time.time()
    with _POP_LOCK:
        hit = _POP_CACHE.get(ck)
        if hit and now - hit[0] < 600:
            return hit[1]
    pts: list[tuple[float, list[float]]] = []
    with store._conn() as c:
        rows = list(c.execute("SELECT params, profile FROM twins WHERE version = ?", (twin_mod.VERSION,)))
        rows += list(c.execute("SELECT params, profile FROM self_twins WHERE version = ? AND username IS NOT ?", (twin_mod.VERSION, exclude)))
    for r in rows:
        try:
            r_rating = json.loads(r["profile"]).get("rating")
            beta = json.loads(r["params"])
        except (ValueError, TypeError):
            continue
        if r_rating and len(beta) == twin_mod.NF:
            pts.append((float(r_rating), beta))
    base = twin_mod.prior(rating)
    acc = [POP_PSEUDO * b for b in base]
    wsum = POP_PSEUDO
    for r_rating, beta in pts:
        w = math.exp(-0.5 * ((r_rating - rating) / POP_WIDTH) ** 2)
        if w < 1e-3:
            continue
        wsum += w
        for k in range(twin_mod.NF):
            acc[k] += w * beta[k]
    out = [a / wsum for a in acc]
    with _POP_LOCK:
        if len(_POP_CACHE) > 256:
            _POP_CACHE.clear()
        _POP_CACHE[ck] = (now, out)
    return out


# ==== your puzzle rating and theme skills (learned from every answer) =======================================
def _g(rd: float) -> float:
    return 1 / math.sqrt(1 + 3 * Q * Q * rd * rd / math.pi ** 2)


def expected_score(r: float, rj: float, rdj: float = 0.0) -> float:
    return 1 / (1 + 10 ** (-_g(rdj) * (r - rj) / 400))


def glicko_update(r: float, rd: float, rj: float, rdj: float, s: float) -> tuple[float, float]:
    """One Glicko-1 rating period with a single game (s = 1 solved, 0 missed)."""
    gj = _g(rdj)
    e = expected_score(r, rj, rdj)
    d2 = 1 / (Q * Q * gj * gj * max(e * (1 - e), 1e-6))
    denom = 1 / (rd * rd) + 1 / d2
    return r + Q / denom * gj * (s - e), max(RD_MIN, math.sqrt(1 / denom))


def dna_offsets(dna: dict | None) -> dict[str, float]:
    if not dna:
        return {}
    base = dna.get("base_rating") or 1500
    return {s["key"]: s["rating"] - base for s in dna.get("skills", []) if not s.get("low_confidence")}


def skill_row(store, key: str, dna: dict | None) -> dict:
    """Your puzzle rating, its uncertainty, learned theme offsets and the prediction track record."""
    with store._conn() as c:
        r = c.execute("SELECT * FROM puzzle_skill WHERE username = ?", (key,)).fetchone()
    if r:
        d = dict(r)
        d["themes"] = json.loads(d["themes"] or "{}")
        days = max(0.0, (time.time() - (d["updated_at"] or time.time())) / 86_400)
        d["rd"] = min(RD_MAX, math.sqrt(d["rd"] ** 2 + 6.0 ** 2 * days))  # unsure again after a break
        return d
    base = (dna or {}).get("base_rating") or 1500
    return {"username": key, "rating": float(base + PUZZLE_RATING_OFFSET), "rd": RD_START, "themes": {}, "attempts": 0,
            "expected": 0.0, "missed": 0, "brier": 0.0, "updated_at": None}


def save_skill(store, row: dict) -> None:
    with store._conn() as c:
        c.execute("INSERT INTO puzzle_skill (username, rating, rd, themes, attempts, expected, missed, brier, updated_at) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (username) DO UPDATE SET rating = excluded.rating, rd = excluded.rd, "
                  "themes = excluded.themes, attempts = excluded.attempts, expected = excluded.expected, missed = excluded.missed, "
                  "brier = excluded.brier, updated_at = excluded.updated_at",
                  (row["username"], row["rating"], row["rd"], json.dumps(row["themes"]), row["attempts"], row["expected"],
                   row["missed"], row["brier"], time.time()))


def theme_offset(themes: list[str], skills: dict[str, float], learned: dict) -> float:
    """Your rating offset on a puzzle with these themes: Rating DNA skills, plus what your own answers taught us."""
    parts = []
    used = set()
    for t in themes:
        sk = THEME_SKILL.get(t)
        if sk and sk in skills and sk not in used:
            used.add(sk)
            parts.append(DNA_SHRINK * skills[sk])
    off = sum(parts) / len(parts) if parts else 0.0
    learned_parts = [learned[t][0] for t in themes if t in learned and t not in puzzlebank.META_THEMES]
    if learned_parts:
        off += sum(learned_parts) / len(learned_parts)
    return off


def record_result(store, key: str, dna: dict | None, meta: dict, themes: list[str], solved: bool) -> dict:
    """After a first attempt at a predicted puzzle: update your puzzle rating and theme skills, and score the prediction."""
    row = skill_row(store, key, dna)
    p_miss = float(meta.get("p_miss") or 0.5)
    rp, rdp = float(meta.get("rating") or row["rating"]), float(meta.get("rd") or 80)
    off = theme_offset(themes, dna_offsets(dna), row["themes"])
    e = expected_score(row["rating"] + off, rp, rdp)
    row["rating"], row["rd"] = glicko_update(row["rating"], row["rd"], rp, rdp, 1.0 if solved else 0.0)
    for t in set(themes) - puzzlebank.META_THEMES:
        cur, n = row["themes"].get(t, [0.0, 0])
        cur += THEME_K / (1 + n / 15) * ((1.0 if solved else 0.0) - e)
        row["themes"][t] = [round(max(-THEME_CAP, min(THEME_CAP, cur)), 1), n + 1]
    row["attempts"] += 1
    row["expected"] = round(row["expected"] + p_miss, 4)
    row["missed"] += 0 if solved else 1
    row["brier"] = round(row.get("brier", 0.0) + (p_miss - (0.0 if solved else 1.0)) ** 2, 4)
    save_skill(store, row)
    return {"rating": round(row["rating"]), "rd": round(row["rd"]), "predicted_miss": round(100 * p_miss), "missed": not solved}


# ==== scoring a puzzle for you ===================================================================================
def attention(beta: list[float], rows) -> list[float]:
    """How a Twin shares its attention among the candidate moves from its style alone (no engine knowledge)."""
    return twin_mod.probabilities([0.0 if k in INSTINCT_SKIP else beta[k] for k in range(twin_mod.NF)], rows)


def score(feats: dict, rating_p: float, themes: list[str], you: dict, beta_you: list[float] | None, beta_typ: list[float],
          skills: dict[str, float]) -> dict:
    """P(you miss it), split into what a typical player at your level would do and the part that's personal."""
    rows, s = feats["rows"], feats["s"]
    base_logit = (rating_p - you["rating"]) * LN10_400
    off = theme_offset(themes, skills, you["themes"])
    blind, pu, pt, pick = 0.0, None, None, None
    if beta_you:
        pu = attention(beta_you, rows)
        pt = attention(beta_typ, rows)
        blind = max(-BLIND_CLAMP, min(BLIND_CLAMP, math.log(max(pt[s], 1e-9)) - math.log(max(pu[s], 1e-9))))
        pick = max(range(len(pu)), key=lambda k: (round(pu[k], 9), -k))  # ties: the engine's higher-ranked move
    personal = -off * LN10_400 + BLIND_WEIGHT * blind
    p_miss = 1 / (1 + math.exp(-(base_logit + personal)))
    p_typ = 1 / (1 + math.exp(-base_logit))
    return {"p_miss": p_miss, "p_typical": p_typ, "personal": personal, "blind": blind, "offset": off,
            "p_you": pu[s] if pu else None, "p_twin_typical": pt[s] if pt else None, "pick": pick, "pick_p": pu[pick] if pu else None}


def why_after(feats: dict, sc: dict, board: chess.Board, beta_you: list[float] | None, beta_typ: list[float], themes: list[str],
              skills: dict[str, float], learned: dict) -> list[str]:
    """Why we predicted a miss, in words (shown after the attempt: it names the solution)."""
    out = []
    u, s = feats["u"], feats["s"]
    sol_san = board.san(chess.Move.from_uci(u[s]))
    if beta_you and sc["pick"] is not None:
        pick = sc["pick"]
        share = f"{round(100 * sc['p_you'])}%"
        typ = f"{round(100 * sc['p_twin_typical'])}%"
        if pick != s:
            pick_san = board.san(chess.Move.from_uci(u[pick]))
            out.append(f"Your Twin's instinct is to look at {pick_san} first. It gives the winning move, {sol_san}, {share} of its "
                       f"attention; a typical player at your level gives it {typ}.")
            reason = _feature_reason(feats, pick, s, beta_you, beta_typ) if sc["blind"] > 0.05 else None
            if reason:
                out.append(f"Why: {reason}.")
            elif sc["blind"] <= 0.05:
                out.append("So this one isn't a personal blind spot: it's simply hard at your level.")
        else:
            out.append(f"Your Twin's instinct does point at {sol_san} ({share} of its attention, typical {typ}): "
                       "the hard part is seeing it through.")
    weak = []
    for t in themes:
        sk = THEME_SKILL.get(t)
        if sk and skills.get(sk, 0) <= -40 and SKILL_WORD.get(sk) not in weak:
            weak.append(SKILL_WORD[sk])
    if weak:
        out.append(f"{' and '.join(weak[:2])} {'is' if len(weak[:2]) == 1 else 'are'} below your average in your Rating DNA.")
    missed = [(t, v) for t, v in learned.items() if t in themes and v[1] >= 3 and v[0] <= -40]
    if missed:
        t = min(missed, key=lambda x: x[1][0])[0]
        out.append(f"{puzzlebank.theme_label(t)} puzzles have been harder for you than your rating suggests.")
    motifs = [puzzlebank.theme_label(t) for t in puzzlebank.motif_themes(themes)][:3]
    if motifs:
        out.append(f"Theme{'s' if len(motifs) > 1 else ''}: {', '.join(motifs)}.")
    if not beta_you:
        out.append("Build your Twin (it studies your own games) to see which of your habits make moves like this one easy to miss.")
    return out


def _feature_reason(feats: dict, pick: int, sol: int, beta_you: list[float], beta_typ: list[float]) -> str | None:
    """The style habit that pulls your attention from the solution to your Twin's pick more than a typical player's."""
    fp = dict(feats["rows"][pick])
    fs = dict(feats["rows"][sol])
    best, best_val = None, 0.15
    for k, name in enumerate(twin_mod.FEATURES):
        if k in INSTINCT_SKIP:
            continue
        diff = fp.get(k, 0.0) - fs.get(k, 0.0)
        if not diff:
            continue
        contrib = (beta_you[k] - beta_typ[k]) * diff
        if contrib > best_val:
            best, best_val = (name, diff), contrib
    if not best:
        return None
    name, diff = best
    return FEATURE_WHY.get((name, 1 if diff > 0 else -1))


# ==== building your deck ================================================================================================
def in_deck(store, key: str) -> set[str]:
    with store._conn() as c:
        return {r[0][3:] for r in c.execute("SELECT game_id FROM puzzles WHERE username = ? AND kind = 'predicted'", (key,))}


def band(you: dict) -> tuple[int, int]:
    r = int(you["rating"])
    return r - BAND_BELOW, r + BAND_ABOVE


def choose(store, key: str, dna: dict | None, n: int, rng: random.Random | None = None) -> list[dict]:
    """Score every annotated bank puzzle in your range and pick the n most personal likely misses, mixed across motifs."""
    if n <= 0:
        return []
    you = skill_row(store, key, dna)
    skills = dna_offsets(dna)
    me = load_self(store, key)
    beta_you = me["params"] if me else None
    beta_typ = population_beta(store, (me["profile"].get("rating") if me else None) or you["rating"] - PUZZLE_RATING_OFFSET, exclude=key)
    lo, hi = band(you)
    pool = puzzlebank.annotated_in_band(store, lo, hi, in_deck(store, key))
    scored = []
    for row in pool:
        feats = puzzlebank.unpack_feats(row["feats"])
        themes = row["themes"].split()
        sc = score(feats, row["rating"], themes, you, beta_you, beta_typ, skills)
        if MISS_MIN <= sc["p_miss"] <= MISS_MAX:
            scored.append((row, feats, themes, sc))
    rng = rng or random.Random()
    rng.shuffle(scored)  # ties: no fixed order
    scored.sort(key=lambda x: (-(x[3]["personal"]), -x[3]["p_miss"]))
    picked, motif_count = [], defaultdict(int)
    for row, feats, themes, sc in scored:
        motifs = puzzlebank.motif_themes(themes) or [row["phase"]]
        if motif_count[motifs[0]] >= max(2, n // 4) and len(scored) > 3 * n:
            continue
        motif_count[motifs[0]] += 1
        picked.append(_deck_entry(row, feats, themes, sc, beta_you, beta_typ, skills, you, me is not None))
        if len(picked) >= n:
            break
    return picked


def _deck_entry(row, feats, themes, sc, beta_you, beta_typ, skills, you, has_twin) -> dict:
    board = chess.Board(row["fen"])
    sans = row["sans"].split()
    before = [f"Predicted miss: {round(100 * sc['p_miss'])}% (a typical player at your level: {round(100 * sc['p_typical'])}%)."]
    phase_skill = THEME_SKILL.get(row["phase"])
    if phase_skill and skills.get(phase_skill, 0) <= -40:
        before.append(f"{SKILL_WORD[phase_skill]} are a weak spot in your Rating DNA." if phase_skill == "endgames"
                      else f"{SKILL_WORD[phase_skill]} is a weak spot in your Rating DNA.")
    if has_twin and sc["blind"] >= 0.3:
        before.append("Your Twin, trained on your games, tends to look elsewhere in positions like this.")
    meta = {"bank_id": row["id"], "rating": row["rating"], "rd": row["rd"], "p_miss": round(sc["p_miss"], 4),
            "p_typical": round(sc["p_typical"], 4), "blind": round(sc["blind"], 3), "model": "twin" if has_twin else "basic",
            "prev_fen": row["setup_fen"], "prev_uci": row["setup_uci"], "game_url": row["game_url"], "source": row["source"],
            "popularity": row["popularity"], "plays": row["plays"], "why_before": before,
            "why_after": why_after(feats, sc, board, beta_you, beta_typ, themes, skills, you["themes"])}
    if sc["pick"] is not None:
        meta["twin_pick"] = feats["u"][sc["pick"]]
        meta["twin_pick_pct"] = round(100 * sc["pick_p"])
    return {"game_id": f"lp-{row['id']}", "fen": row["fen"], "solution": row["moves"].split()[0], "solution_san": sans[0],
            "line": sans, "themes": themes, "phase": row["phase"], "win_loss": round(100 * sc["p_miss"], 1), "meta": meta}


def add_to_deck(store, key: str, entries: list[dict]) -> int:
    now = time.time()
    rows = [(key, e["game_id"], 0, e["fen"], e["solution"], e["solution_san"], json.dumps(e["line"]), "", json.dumps(e["themes"]),
             e["phase"], e["win_loss"], now, "[]", "predicted", json.dumps(e["meta"])) for e in entries]
    with store._conn() as c:
        before = c.total_changes
        c.executemany("INSERT OR IGNORE INTO puzzles (username, game_id, ply, fen, solution, solution_san, line, played_san, themes, phase, "
                      "win_loss, due_at, accept, kind, meta) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        return c.total_changes - before


def status(store, key: str, dna: dict | None, games: int, depth: int) -> dict:
    you = skill_row(store, key, dna)
    me = load_self(store, key)
    lo, hi = band(you)
    ready, total = puzzlebank.band_counts(store, lo, hi)
    with store._conn() as c:
        deck = c.execute("SELECT COUNT(*) AS n, SUM(due_at <= ?) AS due, SUM(attempts > 0) AS tried FROM puzzles "
                         "WHERE username = ? AND kind = 'predicted'", (time.time(), key)).fetchone()
        studied = c.execute("SELECT COUNT(*) FROM self_twin_games WHERE username = ? AND version = ? AND depth = ?",
                            (key, twin_mod.VERSION, depth)).fetchone()[0]
    learned = sorted(((t, v) for t, v in you["themes"].items() if v[1] >= 3), key=lambda x: x[1][0])
    track = None
    if you["attempts"]:
        track = {"attempts": you["attempts"], "expected_misses": round(you["expected"], 1), "missed": you["missed"],
                 "brier": round(you.get("brier", 0.0) / you["attempts"], 3)}
    return {
        "puzzle_rating": round(you["rating"]), "rating_sure": you["rd"] < 120, "rd": round(you["rd"]),
        "twin": None if not me else {"games": me["games"], "decisions": me["decisions"], "built_at": me["built_at"],
                                     "faithfulness": me["profile"].get("faithfulness"), "style": me["profile"].get("style", []),
                                     "accuracy": me["profile"].get("accuracy"), "confidence": me["profile"].get("confidence")},
        "studied": studied, "games": games, "band": [lo, hi], "band_ready": ready, "band_total": total,
        "bank": puzzlebank.stats(store), "deck": deck["n"] or 0, "due": deck["due"] or 0, "tried": deck["tried"] or 0,
        "weak_themes": [{"theme": t, "label": puzzlebank.theme_label(t), "offset": round(v[0]), "n": v[1]} for t, v in learned[:4] if v[0] < 0],
        "strong_themes": [{"theme": t, "label": puzzlebank.theme_label(t), "offset": round(v[0]), "n": v[1]} for t, v in learned[::-1][:3] if v[0] > 0],
        "track": track,
    }
