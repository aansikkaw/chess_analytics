"""List files in this folder that aren't part of the current release (leftovers from older patches).

  python scripts/stale_files.py           list them
  python scripts/stale_files.py --delete  delete them (asks first)

Your data is never touched: databases, outbox/, bundles/, .git and caches are skipped.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", "outbox", "bundles", "node_modules", ".venv", "venv", "bin"}
SKIP_SUFFIXES = (".db", ".db-wal", ".db-shm", ".zip", ".log")
RELEASE = set("""
.devcontainer/devcontainer.json
.dockerignore
.gitignore
DEPLOY.md
Dockerfile
README.md
REVIEW.md
app/__init__.py
app/analysis.py
app/answer.py
app/auth.py
app/backup.py
app/coach.py
app/config.py
app/digest.py
app/engine.py
app/games.py
app/grounding.py
app/jobs.py
app/knowledge/index.md
app/knowledge/log.md
app/knowledge/principles/blunder-check.md
app/knowledge/principles/clock-management.md
app/knowledge/principles/converting-advantages.md
app/knowledge/principles/defending-worse-positions.md
app/knowledge/principles/forcing-moves-first.md
app/knowledge/principles/index.md
app/knowledge/principles/king-activity.md
app/knowledge/principles/mating-patterns.md
app/knowledge/principles/middlegame-planning.md
app/knowledge/principles/opening-principles.md
app/knowledge/principles/repertoire-maintenance.md
app/knowledge/principles/rook-endgames.md
app/llm.py
app/mailer.py
app/main.py
app/observability.py
app/okf.py
app/plans.py
app/player_bundle.py
app/prep.py
app/profile.py
app/progress.py
app/repertoire.py
app/scheduler.py
app/scout.py
app/sources.py
app/static/app.css
app/static/fonts/LICENSE-Archivo.txt
app/static/fonts/LICENSE-Caveat.txt
app/static/fonts/LICENSE-NotoSansSymbols2.txt
app/static/fonts/archivo-latin-ext-standard-normal.woff2
app/static/fonts/archivo-latin-standard-normal.woff2
app/static/fonts/caveat-latin-600-normal.woff2
app/static/fonts/chess-pieces.woff2
app/static/icons/apple-touch-icon.png
app/static/icons/favicon-32.png
app/static/icons/favicon-64.png
app/static/icons/icon-192.png
app/static/icons/icon-512.png
app/static/icons/maskable-192.png
app/static/icons/maskable-512.png
app/static/index.html
app/static/js/board.js
app/static/js/core.js
app/static/js/dna.js
app/static/js/landing.js
app/static/js/main.js
app/static/js/plans.js
app/static/js/shell.js
app/static/js/status.js
app/static/js/support.js
app/static/js/views/coach.js
app/static/js/views/common.js
app/static/js/views/diagnose.js
app/static/js/views/prepare.js
app/static/js/views/students.js
app/static/js/views/train.js
app/static/manifest.webmanifest
app/static/pieces/chessnut/LICENSE-2.0.txt
app/static/pieces/chessnut/NOTICE.txt
app/static/pieces/chessnut/bB.svg
app/static/pieces/chessnut/bK.svg
app/static/pieces/chessnut/bN.svg
app/static/pieces/chessnut/bP.svg
app/static/pieces/chessnut/bQ.svg
app/static/pieces/chessnut/bR.svg
app/static/pieces/chessnut/wB.svg
app/static/pieces/chessnut/wK.svg
app/static/pieces/chessnut/wN.svg
app/static/pieces/chessnut/wP.svg
app/static/pieces/chessnut/wQ.svg
app/static/pieces/chessnut/wR.svg
app/static/status.html
app/static/sw.js
app/static/vendor/chess.js
app/static/vendor/chess.js.LICENSE
app/status.py
app/store.py
app/sync.py
app/worker.py
deploy/.env.example
deploy/Caddyfile
deploy/docker-compose.yml
deploy/setup-vps.sh
deploy/systemd/plateau-scheduler.service
deploy/systemd/plateau-web.service
deploy/systemd/plateau-worker.service
pyproject.toml
requirements-dev.txt
requirements-prod.txt
requirements.txt
sample_data/demo_games.pgn
scripts/admin.py
scripts/backup.py
scripts/coach_check.py
scripts/make_demo_pgn.py
scripts/setup_ollama.sh
scripts/stale_files.py
scripts/start_ollama.sh
tests/__init__.py
tests/conftest.py
tests/test_api.py
tests/test_coach.py
tests/test_coach_dashboard.py
tests/test_core.py
tests/test_email.py
tests/test_jobs.py
tests/test_okf.py
tests/test_ops.py
tests/test_prep.py
tests/test_pro.py
tests/test_sources.py
""".split())


def stale() -> list[Path]:
    out = []
    for p in sorted(ROOT.rglob("*")):
        rel = p.relative_to(ROOT)
        if not p.is_file() or SKIP_DIRS & set(rel.parts) or p.name.endswith(SKIP_SUFFIXES) or p.name.startswith(".env"):
            continue
        if rel.as_posix() not in RELEASE:
            out.append(rel)
    return out


def main(argv: list[str]) -> int:
    extra = stale()
    if not extra:
        print("Nothing left over: every file belongs to this release.")
        return 0
    print("Not part of this release (left over from older versions, or files you added yourself):")
    for p in extra:
        print(f"  {p}")
    if "--delete" in argv:
        if input("Delete these files? Type yes: ").strip().lower() == "yes":
            for p in extra:
                (ROOT / p).unlink()
            print(f"Deleted {len(extra)} files.")
        else:
            print("Nothing deleted.")
    else:
        print("\nCheck the list, then run with --delete to remove them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
