"""Write a player's analysis as an OKF v0.2 bundle.

    /player.md                     Player Profile     (entry point: ratings, findings, links)
    /skills/<skill>.md             Skill Assessment   (rating, accuracy, costliest moments)
    /patterns/<pattern>.md         Mistake Pattern    (how often, where, which principle)
    /moments/<game>-<ply>.md       Critical Moment    (position, move, engine best + line, evals)
    /games/<game>.md               Game               (result, opening, accuracy, its moments)
    /openings/<colour>/<line>.md   Opening Line       (Pro)
    /recurring.md                  Recurring Mistakes (Pro)
    /progress.md                   Progress Report    (Pro)
    /prep/<colour>.md              Repertoire Check   (Pro, from uploaded ChessBase repertoires)

Engine-derived concepts are written by `plateau_breaker/<version>` and verified by
`process:stockfish` (OKF trust tier: machine-confirmed). Nothing in a player bundle
claims human review. Chess principles live in a separate bundle mounted at /knowledge.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from pathlib import Path

from .analysis import GameAnalysis, MoveAnalysis
from .okf import BundleWriter, now_iso, slug
from .profile import SKILLS, contested_moves, insights, rating_dna
from .progress import progress_report
from .repertoire import repertoire_report

PRODUCER = "plateau_breaker/0.4"
STALE_DAYS = 30
PRO_PREFIXES = ("/openings", "/recurring", "/progress", "/prep")

PATTERN_INFO = {
    "hanging_piece": ("Hanging pieces", "Moves that left a piece where the opponent's best reply captures it.", "blunder-check"),
    "missed_tactic": ("Missed tactics", "Positions where a forcing move (check, capture, promotion) was best and you played something else.",
                      "forcing-moves-first"),
    "time_trouble": ("Time-trouble mistakes", "Mistakes made with less than 10% of the starting clock (at least 30 seconds).",
                     "clock-management"),
    "conversion": ("Failed conversions", "Mistakes made while already clearly better (+2 or more).", "converting-advantages"),
    "missed_mate": ("Missed mates", "A forced mate was available and the move played let it go.", "mating-patterns"),
}
SKILL_PRINCIPLES = {
    "openings": ["opening-principles"], "middlegame": ["middlegame-planning"], "endgames": ["rook-endgames", "king-activity"],
    "tactics": ["forcing-moves-first", "blunder-check"], "time": ["clock-management"], "converting": ["converting-advantages"],
    "defending": ["defending-worse-positions"],
}
PRINCIPLE_TITLES = {
    "blunder-check": "Blunder check", "forcing-moves-first": "Forcing moves first", "clock-management": "Clock management",
    "converting-advantages": "Converting a winning position", "mating-patterns": "Mating patterns",
    "opening-principles": "Opening principles", "middlegame-planning": "Middlegame planning",
    "rook-endgames": "Rook endgame essentials", "king-activity": "King activity in the endgame",
    "defending-worse-positions": "Defending worse positions", "repertoire-maintenance": "Keeping a repertoire healthy",
}


def principle_link(key: str) -> str:
    return f"[{PRINCIPLE_TITLES[key]}](/knowledge/principles/{key}.md)"


def moment_id(g: GameAnalysis, m: MoveAnalysis) -> str:
    return f"/moments/{slug(g.game_id, 30)}-{m.ply}"


def game_id(g: GameAnalysis) -> str:
    return f"/games/{slug(g.game_id, 30)}"


def _pawns(cp: int) -> str:
    if abs(cp) >= 9000:
        return "mate" if cp > 0 else "getting mated"
    return f"{cp / 100:+.1f}"


def _move_label(m: MoveAnalysis) -> str:
    return f"{m.move_number}{'.' if m.color == 'white' else '...'}{m.played_san}"


def _result_word(g: GameAnalysis) -> str:
    return "won" if g.score == 1 else "lost" if g.score == 0 else "drew"


def _trust(at: str) -> dict:
    stale = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=STALE_DAYS)).replace(microsecond=0)
    return {
        "status": "stable",
        "generated": {"by": PRODUCER, "at": at},
        "verified": {"by": "process:stockfish", "at": at},
        "stale_after": stale.isoformat().replace("+00:00", "Z"),
    }


def _mlink(g: GameAnalysis, m: MoveAnalysis) -> str:
    return f"[{_move_label(m)} vs {g.opponent}]({moment_id(g, m)}.md)"


def build_player_bundle(out_dir: Path, games: list[GameAnalysis], handle: str, platform: str,
                        prep_reports: list[dict] | None = None, log_entry: str | None = None) -> Path:
    at = now_iso()
    trust = _trust(at)
    w = BundleWriter(f"{handle} ({platform}): chess knowledge bundle")
    w.describe_dir("skills", "Skills", "Rating DNA: one assessment per skill area.")
    w.describe_dir("patterns", "Mistake patterns", "Recurring kinds of mistakes and the principle behind each.")
    w.describe_dir("moments", "Critical moments", "Every mistake and blunder, with the position and the engine's preferred move.")
    w.describe_dir("games", "Games", "Each analysed game with its result, opening and critical moments.")
    w.describe_dir("openings", "Openings", "Opening lines by colour with scores and leaks (Pro).")
    w.describe_dir("prep", "Prepared repertoire", "Comparison of uploaded ChessBase repertoires with games played (Pro).")

    dna = rating_dna(games)
    finds = insights(games, dna)
    critical = [(g, m) for g in games for m in g.moves if m.classification in ("mistake", "blunder")]
    critical.sort(key=lambda gm: gm[1].win_loss, reverse=True)
    contested = set(map(id, contested_moves(games)))

    # ---- moments
    for g, m in critical:
        tags = sorted({m.phase, m.classification, *m.tags, g.time_class} - {""})
        fm = {
            "type": "Critical Moment",
            "title": f"{_move_label(m)} vs {g.opponent} ({g.played_at})",
            "description": f"{_pawns(m.cp_before)} to {_pawns(m.cp_after)}; engine preferred {m.best_san or 'n/a'}"
                           + (f"; {', '.join(t.replace('_', ' ') for t in m.tags)}" if m.tags else ""),
            "tags": tags,
            "game": game_id(g),
            "opponent": g.opponent, "date": g.played_at, "opening": g.opening, "time_class": g.time_class,
            "color": m.color, "move": _move_label(m), "played": m.played_san, "best": m.best_san, "played_uci": m.played, "best_uci": m.best,
            "line": m.best_line[:6], "eval_before": _pawns(m.cp_before), "eval_after": _pawns(m.cp_after),
            "winning_chances_lost": m.win_loss, "classification": m.classification, "phase": m.phase,
            "patterns": list(m.tags), "clock_seconds": m.clock_before, "fen": m.fen_before,
            **trust,
        }
        if g.url:
            fm["resource"] = f"{g.url}#{m.ply}" if "lichess.org" in g.url else g.url
        rel = [f"[{PATTERN_INFO[t][0]}](/patterns/{t}.md)" for t in m.tags if t in PATTERN_INFO]
        body = (
            f"# Position\n\n`{m.fen_before}`\n\n"
            f"| | |\n|---|---|\n| You played | **{m.played_san}** |\n| Engine preferred | **{m.best_san or 'n/a'}** |\n"
            f"| Engine line | {' '.join(m.best_line[:6]) or 'n/a'} |\n| Evaluation | {_pawns(m.cp_before)} to {_pawns(m.cp_after)} |\n"
            f"| Winning chances lost | {m.win_loss:.0f} points |\n| Phase | {m.phase} |\n"
            + (f"| Clock | {int(m.clock_before)} s |\n" if m.clock_before is not None else "")
            + f"\n# Related\n\n* Game: [{g.opponent}, {g.played_at}]({game_id(g)}.md)\n"
            + "".join(f"* Pattern: {r}\n" for r in rel)
        )
        w.add(moment_id(g, m), fm, body)

    # ---- games
    for g in games:
        ms = [m for m in g.moves if m.classification in ("mistake", "blunder")]
        moves_txt = " ".join(f"{i // 2 + 1}.{s}" if i % 2 == 0 else s for i, s in enumerate(g.moves_san))
        fm = {
            "type": "Game", "title": f"vs {g.opponent}, {g.played_at} ({_result_word(g)})",
            "description": f"{g.player_color.title()}, {g.opening}, {g.time_class or g.time_control}, accuracy {g.accuracy}%.",
            "tags": sorted({g.time_class, g.player_color, _result_word(g)} - {""}),
            "opponent": g.opponent, "date": g.played_at, "color": g.player_color, "result": g.result,
            "score": g.score, "opening": g.opening, "time_class": g.time_class, "accuracy": g.accuracy, **trust,
        }
        if g.url:
            fm["resource"] = g.url
        body = "# Critical moments\n\n" + ("".join(f"* {_mlink(g, m)} - {m.classification}, {m.win_loss:.0f} points\n" for m in ms) or "None.\n")
        body += f"\n# Moves\n\n{moves_txt}\n" if moves_txt else ""
        w.add(game_id(g), fm, body)

    # ---- patterns
    tag_counts = Counter(t for _, m in critical for t in m.tags)
    for tag, (title, desc, principle) in PATTERN_INFO.items():
        items = [(g, m) for g, m in critical if tag in m.tags]
        if not items:
            continue
        phases = Counter(m.phase for _, m in items)
        fm = {"type": "Mistake Pattern", "title": title, "description": desc, "tags": [tag],
              "count": len(items), "total_winning_chances_lost": round(sum(m.win_loss for _, m in items), 1), **trust}
        body = (f"# Summary\n\n{len(items)} mistakes or blunders in {len(games)} games, mostly in the "
                f"{phases.most_common(1)[0][0]} ({', '.join(f'{k}: {v}' for k, v in phases.most_common())}).\n\n"
                f"# Principle\n\n{principle_link(principle)}\n\n# Costliest examples\n\n"
                + "".join(f"* {_mlink(g, m)} - {m.win_loss:.0f} points\n" for g, m in items[:8]))
        w.add(f"/patterns/{tag}", fm, body)

    # ---- skills
    for s in dna["skills"]:
        sel = SKILLS[s["key"]][1]
        examples = [(g, m) for g, m in critical if sel(m) and id(m) in contested][:5]
        fm = {"type": "Skill Assessment", "title": s["label"],
              "description": f"About {s['rating']} vs overall {dna['base_rating']}; {s['accuracy']}% accuracy over {s['moves']} moves"
                             + (" (low confidence)." if s["low_confidence"] else "."),
              "tags": [s["key"]] + (["low_confidence"] if s["low_confidence"] else []),
              "rating": s["rating"], "accuracy": s["accuracy"], "moves": s["moves"], "share_of_moves": s["share_of_moves"],
              "share_of_loss": s["share_of_loss"], "low_confidence": s["low_confidence"], "order": s["rating"], **trust}
        body = (f"# Assessment\n\n| Rating | Accuracy | Moves | Share of moves | Share of winning chances lost |\n|---|---|---|---|---|\n"
                f"| {s['rating']} | {s['accuracy']}% | {s['moves']} | {s['share_of_moves']}% | {s['share_of_loss']}% |\n\n"
                "The rating shifts the overall rating by 12 points per accuracy point above or below the player's average, "
                "using only positions still in play (within 6 pawns).\n\n"
                f"# Principles\n\n" + "".join(f"* {principle_link(p)}\n" for p in SKILL_PRINCIPLES.get(s["key"], []))
                + "\n# Costliest moments\n\n" + ("".join(f"* {_mlink(g, m)} - {m.win_loss:.0f} points\n" for g, m in examples) or "None.\n"))
        w.add(f"/skills/{s['key']}", fm, body)

    # ---- Pro: openings, recurring, progress
    rep = repertoire_report(games)
    leak_lines = {(lk["color"], lk["line"]) for lk in rep["leaks"]}
    for color in ("white", "black"):
        for row in rep[color]:
            cid = f"/openings/{color}/{slug(row['line'], 50)}"
            is_leak = (color, row["line"]) in leak_lines
            fm = {"type": "Opening Line", "title": f"{'White' if color == 'white' else 'Black'}: {row['line']}",
                  "description": f"{row['games']} games, {row['score_pct']}% score" + (" (leak)." if is_leak else "."),
                  "tags": [color, "opening"] + (["leak"] if is_leak else []), "color": color, "line": row["line"],
                  "games": row["games"], "score_pct": row["score_pct"], "opening_accuracy": row["opening_accuracy"],
                  "avg_eval_after_opening": row["avg_eval_after_opening"], "leak": is_leak, **trust}
            body = f"# Line\n\n{row['line']} ({row['opening']})\n\n# Principle\n\n{principle_link('opening-principles')}\n"
            leak = next((lk for lk in rep["leaks"] if lk["color"] == color and lk["line"] == row["line"]), None)
            if leak and leak.get("costliest_move"):
                c = leak["costliest_move"]
                g = next((x for x in games if x.game_id == c["game_id"]), None)
                mv = next((m for m in g.moves if m.ply == c["ply"]), None) if g else None
                link = _mlink(g, mv) if (g and mv and mv.classification in ("mistake", "blunder")) else c["move"]
                body += (f"\n# Costliest opening move\n\n{link}, played {c['times_played']} time(s); engine preferred "
                         f"{c['engine_best']} ({' '.join(c['engine_line'])}).\n")
            w.add(cid, fm, body)

    rec = rep["recurring_mistakes"]
    rec_body = "# Same position, same wrong move\n\n"
    for r in rec:
        g = next((x for x in games if x.game_id == r["game_id"]), None)
        mv = next((m for m in g.moves if m.ply == r["ply"]), None) if g else None
        where = _mlink(g, mv) if (g and mv and mv.classification in ("mistake", "blunder")) else f"`{r['fen']}`"
        rec_body += (f"* **{r['times']}x** {r['you_played']} instead of {r['engine_best']} ({r['phase']}), "
                     f"{r['total_winning_chances_lost']} points lost in total, vs {', '.join(r['opponents'])}. First seen: {where}\n")
    if not rec:
        rec_body += "None yet.\n"
    w.add("/recurring", {"type": "Recurring Mistakes", "title": "Recurring mistakes",
                         "description": f"{len(rec)} positions where the same wrong move was played more than once.",
                         "tags": ["recurring", "habits"], "count": len(rec), **trust}, rec_body)

    prog = progress_report(games)
    pbody = "# Trend\n\n"
    t = prog.get("trend")
    if t:
        pbody += (f"From {t['from']} to {t['to']}: accuracy {t['accuracy_change']:+} points, blunders per 100 moves "
                  f"{t['blunder_rate_change']:+}, most improved: {t['most_improved']}, needs attention: {t['most_declined']}.\n\n")
    else:
        pbody += (prog.get("note") or "Not enough data yet.") + "\n\n"
    if prog["buckets"]:
        pbody += "| Period | Games | Score | Accuracy | Blunders/100 | Rating |\n|---|---|---|---|---|---|\n"
        pbody += "".join(f"| {b['period']} | {b['games']} | {b['score_pct']}% | {b['accuracy']}% | {b['blunders_per_100']} | {b['rating'] or '-'} |\n"
                         for b in prog["buckets"])
    w.add("/progress", {"type": "Progress Report", "title": "Progress over time",
                        "description": "Accuracy, blunder rate and rating by " + (prog.get("granularity") or "period") + ".",
                        "tags": ["progress"], **trust}, pbody)

    for pr in prep_reports or []:
        add_prep_concept(w, pr, games, trust)

    # ---- entry point
    weakest = sorted((s for s in dna["skills"] if not s["low_confidence"]), key=lambda s: s["rating"])[:2]
    body = (f"# Overview\n\n| Rating | Accuracy | Games | Moves analysed |\n|---|---|---|---|\n"
            f"| {dna['base_rating']}{' (estimated)' if dna['rating_is_estimated'] else ''} | {dna['overall_accuracy']}% | "
            f"{dna['games']} | {dna['moves']} |\n\n# Skills\n\n"
            + "".join(f"* [{s['label']}](/skills/{s['key']}.md) - {s['rating']}{' (low confidence)' if s['low_confidence'] else ''}\n"
                      for s in sorted(dna["skills"], key=lambda s: s["rating"]))
            + "\n# Key findings\n\n" + ("".join(f"* {i['stat']} {i['text']}\n" for i in finds) or "None yet.\n")
            + "\n# Mistake patterns\n\n" + ("".join(f"* [{PATTERN_INFO[t][0]}](/patterns/{t}.md) - {n}\n" for t, n in tag_counts.most_common()
                                                    if t in PATTERN_INFO) or "None yet.\n")
            + "\n# Where to look next\n\n"
            + "".join(f"* Weakest skill: [{s['label']}](/skills/{s['key']}.md)\n" for s in weakest)
            + "* [All critical moments](/moments/index.md)\n* [Opening lines](/openings/index.md) (Pro)\n"
              "* [Recurring mistakes](/recurring.md) (Pro)\n* [Progress](/progress.md) (Pro)\n"
            + ("* [Prepared repertoire check](/prep/index.md) (Pro)\n" if prep_reports else ""))
    w.add("/player", {"type": "Player Profile", "title": f"{handle} on {platform}",
                      "description": f"Rating DNA, findings and entry points for {handle}'s {len(games)} analysed games.",
                      "tags": ["profile"], "rating": dna["base_rating"], "accuracy": dna["overall_accuracy"],
                      "games": dna["games"], "order": 0, **trust}, body)
    return w.write(out_dir, log_entry or f"**Update**: Regenerated from {len(games)} analysed games ({len(critical)} critical moments).")


def add_prep_concept(w: BundleWriter, pr: dict, games: list[GameAnalysis], trust: dict) -> None:
    """One Repertoire Check concept per uploaded repertoire (see prep.py)."""
    color = pr["color"]
    s = pr["summary"]
    body = (f"# Summary\n\n| Games checked | Stayed in prep | Avg. moves in prep | Deviations | Gaps | Prep holes |\n|---|---|---|---|---|---|\n"
            f"| {s['games']} | {s['followed_pct']}% | {s['avg_moves_in_prep']} | {len(pr['deviations'])} | {len(pr['gaps'])} | {len(pr['holes'])} |\n\n"
            f"Repertoire file: {pr['name']} ({s['positions']} prepared positions).\n\n"
            f"# Where you left your own preparation\n\n")
    for d in pr["deviations"][:10]:
        body += (f"* After {d['line'] or 'the start'}: prep says **{', '.join(d['prep_moves'])}**, you played **{d['played']}** "
                 f"({d['times']}x, you scored {d['score_pct']}%)\n")
    body += "\n# Opponent moves your prep doesn't cover\n\n"
    for g_ in pr["gaps"][:10]:
        sug = f"; engine suggests **{g_['engine_reply']}** ({g_['engine_eval']})" if g_.get("engine_reply") else ""
        body += f"* After {g_['line']}: opponents played **{g_['opponent_move']}** {g_['times']}x{sug}\n"
    body += "\n# Prep lines that end badly\n\n"
    for h in pr["holes"][:10]:
        body += f"* {h['line']}: {h['eval_for_you']} for you\n"
    body += f"\n# Principle\n\n{principle_link('repertoire-maintenance')}\n"
    w.add(f"/prep/{color}-{slug(pr['name'], 30)}", {
        "type": "Repertoire Check", "title": f"{'White' if color == 'white' else 'Black'} repertoire: {pr['name']}",
        "description": f"{s['followed_pct']}% of games stayed in prep; {len(pr['deviations'])} deviations, {len(pr['gaps'])} gaps, {len(pr['holes'])} holes.",
        "tags": ["repertoire", color, "chessbase"], "color": color, **trust,
        "sources": [{"id": "chessbase-upload", "resource": f"upload:{pr['name']}", "title": "Uploaded ChessBase repertoire (PGN)"}],
    }, body)
