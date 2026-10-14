"""Load the Lichess puzzle database (CC0) into the puzzle bank used by "Predicted for you".

  python scripts/import_puzzles.py --download                 download Lichess's file (~300 MB) and keep 200,000 puzzles
  python scripts/import_puzzles.py --file lichess_db_puzzle.csv.zst
  python scripts/import_puzzles.py --download --limit 400000 --min-plays 1000
  python scripts/import_puzzles.py --annotate 3000 --around 1800   (optional) prepare puzzles for predictions ahead of time
  python scripts/import_puzzles.py --stats

What's kept: well-tested puzzles only (played 500+ times, popularity 80+, rating deviation 90 or less), the same
number from every 50-point rating band from 600 to 2900, so every level is covered. Run it again any time: puzzles
already in the bank are skipped. Reading .zst needs `pip install zstandard` (or the zstd tool).

Predictions need each puzzle "annotated" once (Stockfish's candidate moves with the Twin's features). The app
does that on demand around each player's level; --annotate does it ahead of time, e.g. overnight on the server.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import puzzlebank  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.store import Store  # noqa: E402

BANDS = (2900 - 600) // puzzlebank.BAND + 1


def download(dest: Path) -> Path:
    import httpx

    print(f"Downloading {puzzlebank.LICHESS_URL} …", flush=True)
    with httpx.stream("GET", puzzlebank.LICHESS_URL, follow_redirects=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        got, last = 0, 0.0
        with dest.open("wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
                got += len(chunk)
                if time.time() - last > 2:
                    last = time.time()
                    pct = f" ({100 * got / total:.0f}%)" if total else ""
                    print(f"  {got / 1e6:,.0f} MB{pct}", flush=True)
    print(f"Downloaded {got / 1e6:,.0f} MB.", flush=True)
    return dest


def annotate(store: Store, n: int, around: int, depth: int) -> None:
    from app.engine import Engine

    s = load_settings()
    lo, hi = around - 400, around + 500
    ids = puzzlebank.pick_unannotated(store, lo, hi, n, seed=around)
    if not ids:
        print(f"Every puzzle rated {lo}-{hi} is already prepared.")
        return
    print(f"Preparing {len(ids)} puzzles rated {lo}-{hi} at depth {depth} …", flush=True)
    start = time.time()
    with Engine(s.stockfish_path, depth=depth) as engine:
        def tick(i):
            if i % 100 == 0 or i == len(ids):
                rate = i / max(0.1, time.time() - start)
                print(f"  {i:,} of {len(ids):,} ({rate:.1f} a second)", flush=True)
        done = puzzlebank.annotate(store, engine, ids, depth, on_one=tick)
    print(f"Prepared {done:,} puzzles.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--download", action="store_true", help="download the latest Lichess puzzle database")
    src.add_argument("--file", help="a lichess_db_puzzle.csv or .csv.zst you already have")
    ap.add_argument("--limit", type=int, default=200_000, help="how many puzzles to keep in total (default 200,000)")
    ap.add_argument("--min-plays", type=int, default=500)
    ap.add_argument("--min-popularity", type=int, default=80)
    ap.add_argument("--max-rd", type=int, default=90)
    ap.add_argument("--keep", action="store_true", help="keep the downloaded file")
    ap.add_argument("--annotate", type=int, default=0, metavar="N", help="prepare N puzzles for predictions (uses Stockfish)")
    ap.add_argument("--around", type=int, default=1800, help="…rated around this (default 1800)")
    ap.add_argument("--depth", type=int, default=None, help="engine depth for --annotate (default BANK_DEPTH, 10)")
    ap.add_argument("--stats", action="store_true", help="show what's in the bank")
    args = ap.parse_args()
    s = load_settings()
    store = Store(s.db_path)

    if args.download or args.file:
        tmp = None
        if args.download:
            tmp = Path(tempfile.gettempdir()) / "lichess_db_puzzle.csv.zst"
            path = download(tmp)
        else:
            path = Path(args.file)
        per_band = max(1, args.limit // BANDS)
        print(f"Importing up to {args.limit:,} puzzles ({per_band:,} per 50-point band) …", flush=True)
        added = puzzlebank.import_rows(
            store, puzzlebank.read_csv(path), min_popularity=args.min_popularity, min_plays=args.min_plays, max_rd=args.max_rd,
            rating_min=600, rating_max=2949, per_band=per_band, limit=args.limit,
            progress=lambda seen, n: print(f"  read {seen:,} rows, added {n:,}", flush=True) if seen % 200_000 < 2000 else None)
        print(f"Added {added:,} puzzles.")
        if tmp and not args.keep:
            os.remove(tmp)
    if args.annotate:
        annotate(store, args.annotate, args.around, args.depth or s.bank_depth)
    if args.stats or not (args.download or args.file or args.annotate):
        st = puzzlebank.stats(store)
        print(f"Bank: {st['puzzles']:,} puzzles rated {st['rating_min']}-{st['rating_max']}, {st['annotated']:,} prepared for predictions."
              + (" (Only the built-in sample: run with --download for Lichess's database.)" if st["sample_only"] else ""))


if __name__ == "__main__":
    main()
