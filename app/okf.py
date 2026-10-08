"""Open Knowledge Format (OKF v0.2) bundles: write, read, navigate, search.

OKF (Google Cloud, https://github.com/GoogleCloudPlatform/knowledge-catalog) is a
directory of markdown files with YAML frontmatter, one concept per file:

    bundle/
      index.md          directory listing (progressive disclosure); root may carry okf_version
      log.md            dated change history, newest first
      skills/index.md
      skills/endgames.md    ---\\n type: Skill Assessment ... \\n---\\n # body

Concept ID = file path inside the bundle without ".md" (e.g. "/skills/endgames").
Links are plain markdown links, preferably bundle-absolute: [Endgames](/skills/endgames.md).

This module is the knowledge layer for the coach: the analysis pipeline *writes* a
bundle per player, and the coach *reads* it the way the spec intends: open the root
index, scope to a topic, follow links. A keyword search over frontmatter and body is
offered as well (the spec leaves consumer-side search open, and it helps for long tails).
"""

from __future__ import annotations

import datetime as dt
import math
import re
import shutil
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

OKF_VERSION = "0.2"
RESERVED = {"index.md", "log.md"}
LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+\.md)(#[^)]*)?\)")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


class OKFError(ValueError):
    pass


# ---- YAML: keep timestamps as the strings authors wrote (spec-reference behaviour) ----
class _Loader(yaml.SafeLoader):
    pass


_Loader.yaml_implicit_resolvers = {
    ch: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


class _Dumper(yaml.SafeDumper):
    pass


def _flow_small(dumper, data):
    """Short dicts like generated: {by, at} read best inline, as in the spec examples."""
    flow = len(data) <= 3 and all(isinstance(v, (str, int, float, bool)) for v in data.values())
    return dumper.represent_mapping("tag:yaml.org,2002:map", data, flow_style=flow)


_Dumper.add_representer(dict, _flow_small)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slug(text: str, max_len: int = 60) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-")[:max_len] or "x"


# ---- documents -------------------------------------------------------------------------
@dataclass
class Concept:
    id: str  # "/skills/endgames"
    frontmatter: dict[str, Any]
    body: str

    @property
    def type(self) -> str:
        return str(self.frontmatter.get("type", ""))

    @property
    def title(self) -> str:
        return str(self.frontmatter.get("title") or self.id.rsplit("/", 1)[-1].replace("-", " ").title())

    @property
    def description(self) -> str:
        return str(self.frontmatter.get("description", ""))

    @property
    def tags(self) -> list[str]:
        t = self.frontmatter.get("tags") or []
        return [str(x) for x in t] if isinstance(t, list) else [str(t)]

    @property
    def status(self) -> str:
        return str(self.frontmatter.get("status", "stable"))

    @property
    def trust(self) -> str:
        """§5.3: unverified / machine-confirmed / human-reviewed."""
        v = self.frontmatter.get("verified")
        if not v:
            return "unverified"
        entries = [v] if isinstance(v, dict) else list(v)
        if any(str(e.get("by", "")).startswith("human:") for e in entries if isinstance(e, dict)):
            return "human-reviewed"
        return "machine-confirmed"

    @property
    def stale(self) -> bool:
        s = self.frontmatter.get("stale_after")
        if not s:
            return False
        try:
            return dt.datetime.now(dt.timezone.utc) >= dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        except ValueError:
            return False

    def links(self) -> list[tuple[str, str]]:
        """(label, resolved concept id) for every markdown link to a .md file."""
        out = []
        for label, target, _ in LINK_RE.findall(self.body):
            if target.startswith(("http://", "https://")):
                continue
            out.append((label, resolve_link(self.id, target)))
        return out

    def summary(self) -> dict:
        return {"id": self.id, "type": self.type, "title": self.title, "description": self.description,
                "tags": self.tags, "trust": self.trust, "status": self.status, "stale": self.stale}

    def render(self) -> str:
        return render_doc(self.frontmatter, self.body)


def resolve_link(from_id: str, target: str) -> str:
    """Bundle-absolute (/a/b.md) or relative (../a/b.md) link -> concept id (/a/b)."""
    target = target.split("#", 1)[0]
    if target.startswith("/"):
        path = target
    else:
        base = from_id.rsplit("/", 1)[0] or "/"
        parts = [p for p in base.split("/") if p]
        for seg in target.split("/"):
            if seg in ("", "."):
                continue
            if seg == "..":
                if parts:
                    parts.pop()
            else:
                parts.append(seg)
        path = "/" + "/".join(parts)
    path = re.sub(r"/+", "/", path)
    if path.endswith("/index.md"):
        return path[: -len("index.md")]  # directory link
    return path[:-3] if path.endswith(".md") else path


def render_doc(frontmatter: dict, body: str) -> str:
    if not frontmatter.get("type"):
        raise OKFError("OKF §4.1: every concept needs a non-empty `type`.")
    fm = yaml.dump(frontmatter, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=1000).strip()
    return f"---\n{fm}\n---\n\n{body.strip()}\n"


def parse_doc(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        raise OKFError("missing frontmatter")
    parts = text.split("\n---", 1)
    if len(parts) != 2:
        raise OKFError("unterminated frontmatter")
    fm = yaml.load(parts[0][3:], Loader=_Loader) or {}
    if not isinstance(fm, dict):
        raise OKFError("frontmatter is not a mapping")
    body = parts[1].lstrip("-").lstrip("\n")
    return fm, body


# ---- writing ----------------------------------------------------------------------------
class BundleWriter:
    """Collects concepts in memory, then writes a spec-conformant directory atomically."""

    def __init__(self, title: str):
        self.title = title
        self.concepts: dict[str, Concept] = {}
        self.dir_titles: dict[str, tuple[str, str]] = {}  # "/skills" -> (title, description)

    def add(self, concept_id: str, frontmatter: dict, body: str) -> str:
        cid = "/" + concept_id.strip("/")
        if cid.rsplit("/", 1)[-1] + ".md" in RESERVED:
            raise OKFError(f"{cid}: index.md and log.md are reserved names (§3.1)")
        if not frontmatter.get("type"):
            raise OKFError(f"{cid}: missing type")
        self.concepts[cid] = Concept(cid, frontmatter, body)
        return cid

    def describe_dir(self, path: str, title: str, description: str) -> None:
        self.dir_titles["/" + path.strip("/")] = (title, description)

    def _index_for(self, dir_id: str) -> str:
        """§8: sections of `* [Title](url) - description`."""
        depth = 0 if dir_id == "/" else dir_id.strip("/").count("/") + 1
        children_dirs = sorted({c.id.split("/")[depth + 1] for c in self.concepts.values()
                                if c.id.startswith(dir_id.rstrip("/") + "/") and c.id.count("/") > depth + 1})
        docs = sorted((c for c in self.concepts.values()
                       if c.id.startswith(dir_id.rstrip("/") + "/") and c.id.count("/") == depth + 1),
                      key=lambda c: (c.frontmatter.get("order", 999), c.title))
        lines = []
        if docs:
            by_type: dict[str, list[Concept]] = {}
            for c in docs:
                by_type.setdefault(c.type, []).append(c)
            for t, cs in by_type.items():
                lines.append(f"# {t}\n")
                lines += [f"* [{c.title}]({c.id.rsplit('/', 1)[-1]}.md) - {c.description}" for c in cs]
                lines.append("")
        if children_dirs:
            lines.append("# Subdirectories\n")
            for d in children_dirs:
                full = (dir_id.rstrip("/") + "/" + d)
                title, desc = self.dir_titles.get(full, (d.replace("-", " ").title(), ""))
                lines.append(f"* [{title}]({d}/index.md) - {desc}".rstrip(" -"))
            lines.append("")
        return "\n".join(lines).strip() + "\n"

    def write(self, out_dir: Path, log_entry: str | None = None) -> Path:
        """Write to a temp dir, then swap in, so readers never see a half-written bundle."""
        out_dir = Path(out_dir)
        tmp = out_dir.with_name(out_dir.name + ".tmp")
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        for c in self.concepts.values():
            p = tmp / (c.id.lstrip("/") + ".md")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(c.render(), encoding="utf-8")
        dirs = {"/"} | {c.id.rsplit("/", 1)[0] or "/" for c in self.concepts.values()}
        for d in list(dirs):  # include intermediate dirs
            parts = d.strip("/").split("/")
            for i in range(1, len(parts)):
                dirs.add("/" + "/".join(parts[:i]))
        for d in dirs:
            target = tmp / d.lstrip("/") / "index.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            body = self._index_for(d)
            if d == "/":
                body = f"---\nokf_version: \"{OKF_VERSION}\"\n---\n\n# {self.title}\n\n" + body
            target.write_text(body, encoding="utf-8")
        # §9 log: carry history forward, newest first.
        old_log = (out_dir / "log.md").read_text(encoding="utf-8") if (out_dir / "log.md").exists() else ""
        old_entries = old_log.split("\n", 1)[1].strip() if old_log.startswith("# ") else ""
        today = dt.date.today().isoformat()
        entry = log_entry or "**Update**: Regenerated bundle."
        if old_entries.startswith(f"## {today}"):
            head, _, rest = old_entries.partition("\n")
            new_log = f"# Bundle Update Log\n\n{head}\n* {entry}\n{rest}".rstrip() + "\n"
        else:
            new_log = f"# Bundle Update Log\n\n## {today}\n* {entry}\n\n{old_entries}".rstrip() + "\n"
        (tmp / "log.md").write_text(new_log[:200_000], encoding="utf-8")
        old = out_dir.with_name(out_dir.name + ".old")
        if old.exists():
            shutil.rmtree(old)
        if out_dir.exists():
            out_dir.rename(old)
        tmp.rename(out_dir)
        if old.exists():
            shutil.rmtree(old)
        return out_dir


# ---- reading -----------------------------------------------------------------------------
class Bundle:
    """A loaded bundle (or several mounted under prefixes), with navigation and search."""

    K1, B = 1.4, 0.75

    def __init__(self):
        self.concepts: dict[str, Concept] = {}
        self.indexes: dict[str, str] = {}  # dir id -> index.md text
        self.logs: dict[str, str] = {}
        self.errors: list[str] = []
        self.roots: set[str] = set()  # mount points; each is a bundle root

    @classmethod
    def load(cls, root: Path, mount: str = "/") -> Bundle:
        return cls().mount(root, mount)

    def mount(self, root: Path, mount: str = "/") -> Bundle:
        root = Path(root)
        prefix = "/" + mount.strip("/")
        prefix = "" if prefix == "/" else prefix
        if not root.exists():
            return self
        self.roots.add(prefix or "/")
        for p in sorted(root.rglob("*.md")):
            rel = p.relative_to(root).as_posix()
            text = p.read_text(encoding="utf-8", errors="replace")
            if p.name == "index.md":
                d = prefix + "/" + rel[: -len("index.md")].rstrip("/")
                self.indexes[d.rstrip("/") or "/"] = text
                continue
            if p.name == "log.md":
                self.logs[(prefix + "/" + rel[: -len("log.md")]).rstrip("/") or "/"] = text
                continue
            cid = prefix + "/" + rel[:-3]
            try:
                fm, body = parse_doc(text)
                if not fm.get("type"):
                    raise OKFError("missing type")
                self.concepts[cid] = Concept(cid, fm, body)
            except (OKFError, yaml.YAMLError) as exc:
                self.errors.append(f"{cid}: {exc}")
        self._reindex()
        return self

    def _reindex(self) -> None:
        self._toks = {cid: _tokens(" ".join([c.title, c.description, " ".join(c.tags), c.type, c.body]))
                      for cid, c in self.concepts.items()}
        self._df = Counter(t for toks in self._toks.values() for t in set(toks))
        self._avgdl = (sum(len(t) for t in self._toks.values()) / len(self._toks)) if self._toks else 1.0

    # -- navigation
    def get(self, concept_id: str) -> Concept | None:
        cid = "/" + concept_id.strip().strip("/").removesuffix(".md")
        return self.concepts.get(cid)

    def index(self, path: str = "/") -> str | None:
        d = ("/" + path.strip().strip("/").removesuffix("index.md").strip("/")).rstrip("/") or "/"
        return self.indexes.get(d)

    def exists(self, concept_id: str) -> bool:
        cid = "/" + concept_id.strip("/")
        return cid in self.concepts or (cid.rstrip("/") or "/") in self.indexes

    def backlinks(self, concept_id: str) -> list[str]:
        cid = "/" + concept_id.strip("/")
        return [c.id for c in self.concepts.values() if any(t == cid for _, t in c.links())]

    # -- query
    def find(self, type: str | None = None, **fields) -> list[Concept]:
        """Exact-match filter on frontmatter. A list-valued field matches if it contains the value."""
        out = []
        for c in self.concepts.values():
            if type and c.type != type:
                continue
            ok = True
            for k, v in fields.items():
                fv = c.frontmatter.get(k)
                ok = v in fv if isinstance(fv, list) else fv == v
                if not ok:
                    break
            if ok:
                out.append(c)
        return out

    def search(self, query: str, type: str | None = None, tag: str | None = None, prefix: str | None = None,
               limit: int = 8) -> list[Concept]:
        q = expand_query(query)
        n = len(self.concepts) or 1
        scored = []
        for cid, c in self.concepts.items():
            if type and c.type != type:
                continue
            if tag and tag not in c.tags:
                continue
            if prefix and not cid.startswith("/" + prefix.strip("/")):
                continue
            toks = self._toks[cid]
            tf = Counter(toks)
            s = 0.0
            for t in set(q):
                if t in tf:
                    idf = math.log(1 + (n - self._df[t] + 0.5) / (self._df[t] + 0.5))
                    f = tf[t]
                    s += idf * f * (self.K1 + 1) / (f + self.K1 * (1 - self.B + self.B * len(toks) / self._avgdl))
            if q and s == 0:
                continue
            weight = float(c.frontmatter.get("winning_chances_lost", 0) or 0)
            scored.append((s + 0.01 * weight + (0.2 if c.status == "stable" else 0), c))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in scored[:limit]]

    # -- conformance (§11)
    def validate(self) -> list[str]:
        problems = list(self.errors)
        for cid, c in self.concepts.items():
            if not c.type:
                problems.append(f"{cid}: missing type")
        for d, text in self.indexes.items():
            if text.startswith("---") and d not in self.roots:
                problems.append(f"{d}/index.md: only the root index may carry frontmatter (§8)")
        for d, text in self.logs.items():
            for h in re.findall(r"^## (.+)$", text, re.M):
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", h.strip()):
                    problems.append(f"{d}/log.md: date heading '{h}' is not YYYY-MM-DD (§9)")
        return problems


# ---- search helpers ---------------------------------------------------------------------
SYNONYMS = {
    "time_trouble": ["time", "clock", "flag", "flagged", "seconds", "pressure", "zeitnot", "hurry"],
    "hanging_piece": ["hang", "hung", "hanging", "loose", "blunder", "blundered", "dropped", "en prise"],
    "missed_tactic": ["tactic", "tactics", "tactical", "combination", "missed", "fork", "pin", "skewer", "shot"],
    "conversion": ["convert", "converting", "winning", "won", "better", "advantage", "throw", "threw", "blew"],
    "missed_mate": ["mate", "checkmate", "mating"],
    "endgame": ["endgame", "endgames", "ending", "endings"],
    "opening": ["opening", "openings", "theory", "prep", "repertoire"],
    "middlegame": ["middlegame", "middle", "plans", "planning", "strategy", "strategic"],
    "defending": ["defend", "defending", "defense", "defence", "worse", "save"],
    "repertoire": ["prep", "preparation", "repertoire", "chessbase", "book", "theory"],
}
_STOP = set("a an the i my me of in on at to for is was do did why how what when where which with and or your you it this "
            "that are be been have has had game games move moves show tell about can could should".split())


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9_]+", text.lower()) if t not in _STOP]


def expand_query(text: str) -> list[str]:
    toks = _tokens(text)
    low = text.lower()
    for canon, words in SYNONYMS.items():
        if any(w in low for w in words):
            toks.append(canon)
    return toks
