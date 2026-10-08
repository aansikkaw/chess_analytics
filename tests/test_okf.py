"""OKF v0.2 bundles: writer/reader conformance, navigation, trust, and the bundles this app produces."""

import pytest

from app.okf import Bundle, BundleWriter, OKFError, parse_doc, render_doc, resolve_link
from app.sync import KNOWLEDGE_DIR
from tests.conftest import needs_engine


# ---- spec mechanics ----------------------------------------------------------------------
def test_render_requires_type():
    with pytest.raises(OKFError):
        render_doc({"title": "x"}, "body")


def test_timestamps_round_trip_as_written():
    text = render_doc({"type": "Metric", "generated": {"by": "human:a", "at": "2026-06-30T14:00:00Z"}}, "# Body")
    fm, body = parse_doc(text)
    assert fm["generated"]["at"] == "2026-06-30T14:00:00Z" and body.startswith("# Body")
    assert "generated: {by: 'human:a', at: '2026-06-30T14:00:00Z'}" in text or "generated: {by: human:a" in text


@pytest.mark.parametrize("frm,target,expected", [
    ("/skills/endgames", "/moments/a-1.md", "/moments/a-1"),
    ("/skills/endgames", "../patterns/time_trouble.md", "/patterns/time_trouble"),
    ("/knowledge/principles/a", "b.md", "/knowledge/principles/b"),
    ("/player", "moments/index.md", "/moments/"),
])
def test_resolve_link(frm, target, expected):
    assert resolve_link(frm, target) == expected


def test_writer_reserved_names_and_index_log(tmp_path):
    w = BundleWriter("Test")
    with pytest.raises(OKFError):
        w.add("/a/index", {"type": "X"}, "")
    w.add("/a/one", {"type": "Thing", "title": "One", "description": "first"}, "See [two](/a/two.md).")
    w.add("/a/two", {"type": "Thing", "title": "Two", "description": "second", "verified": {"by": "human:me", "at": "2026-01-01T00:00:00Z"}}, "")
    w.add("/top", {"type": "Overview", "verified": [{"by": "process:x", "at": "2026-01-01T00:00:00Z"}]}, "[One](/a/one.md)")
    out = w.write(tmp_path / "b", "**Creation**: test")
    root = (out / "index.md").read_text()
    assert root.startswith('---\nokf_version: "0.2"\n---') and "[A](a/index.md)" in root and "[Top](top.md)" in root
    sub = (out / "a" / "index.md").read_text()
    assert not sub.startswith("---") and "* [One](one.md) - first" in sub
    assert "## " in (out / "log.md").read_text()
    b = Bundle.load(out)
    assert b.validate() == []
    assert b.get("/a/two").trust == "human-reviewed" and b.get("/top").trust == "machine-confirmed" and b.get("/a/one").trust == "unverified"
    assert b.backlinks("/a/one") == ["/top"]
    # Rewriting keeps the log history, newest first.
    w.write(out, "**Update**: second")
    log = (out / "log.md").read_text()
    assert log.index("second") < log.index("test")


def test_reader_tolerates_bad_docs(tmp_path):
    (tmp_path / "ok.md").write_text("---\ntype: X\n---\nbody\n")
    (tmp_path / "bad.md").write_text("no frontmatter here")
    (tmp_path / "notype.md").write_text("---\ntitle: y\n---\n")
    b = Bundle.load(tmp_path)
    assert list(b.concepts) == ["/ok"] and len(b.errors) == 2


def test_stale_after():
    from app.okf import Concept
    assert Concept("/a", {"type": "X", "stale_after": "2000-01-01T00:00:00Z"}, "").stale
    assert not Concept("/a", {"type": "X", "stale_after": "2999-01-01T00:00:00Z"}, "").stale


# ---- the shipped principles bundle ---------------------------------------------------------
def test_knowledge_bundle_is_conformant_and_unreviewed():
    b = Bundle.load(KNOWLEDGE_DIR, "/knowledge")
    assert b.validate() == [] and len(b.concepts) >= 10
    assert all(c.type == "Principle" and c.trust == "unverified" for c in b.concepts.values())
    assert all(b.exists(t) for c in b.concepts.values() for _, t in c.links())


# ---- the player bundle this app writes ----------------------------------------------------------
@needs_engine
def test_player_bundle_structure(demo_bundle, demo_games):
    b = demo_bundle
    assert b.validate() == []
    types = {c.type for c in b.concepts.values()}
    assert {"Player Profile", "Skill Assessment", "Critical Moment", "Game", "Progress Report", "Recurring Mistakes", "Principle"} <= types
    assert len(b.find(type="Game")) == len(demo_games)
    # Every link in every concept resolves (player -> knowledge links included).
    broken = [(c.id, t) for c in b.concepts.values() for _, t in c.links() if not b.exists(t)]
    assert broken == []
    # Engine-derived concepts are machine-confirmed; principles stay unverified.
    assert {c.trust for c in b.concepts.values() if not c.id.startswith("/knowledge")} == {"machine-confirmed"}


@needs_engine
def test_moment_frontmatter_is_queryable(demo_bundle):
    m = demo_bundle.find(type="Critical Moment")[0]
    f = m.frontmatter
    for key in ("fen", "played", "best", "line", "eval_before", "eval_after", "winning_chances_lost", "phase", "game"):
        assert key in f
    assert demo_bundle.exists(f["game"])
    assert demo_bundle.find(type="Critical Moment", phase=f["phase"])


@needs_engine
def test_index_progressive_disclosure(demo_bundle):
    root = demo_bundle.index("/")
    assert "Subdirectories" in root and "moments/index.md" in root and "player.md" in root
    assert demo_bundle.index("/knowledge") and demo_bundle.index("/skills")
