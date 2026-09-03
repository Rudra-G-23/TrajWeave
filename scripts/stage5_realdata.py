#!/usr/bin/env python3
"""Stage 5 real-data validation harness (NOT part of the package).

Mirrors the Stage 4 method: a throwaway ``TRAJWEAVE_HOME``, with the repo roots
referenced by real Codex/Claude sessions registered *directly in the DB* - no
``.trajweave/project.json`` marker is ever written into a real repository, and no
transcript is copied or modified.

    python scripts/stage5_realdata.py [--home DIR] [--keep]

Prints: import stats, extraction stats, and an inspection of the top-20 /
random-10 / lowest-10 candidate experiences with their evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trajweave.adapters import ADAPTERS  # noqa: E402
from trajweave.experience import ExperienceExtractor  # noqa: E402
from trajweave.ingest.importer import Importer  # noqa: E402
from trajweave.projects.git import find_repo_root  # noqa: E402
from trajweave.projects.registry import project_id_for_root  # noqa: E402
from trajweave.storage.database import Database  # noqa: E402
from trajweave.storage.repository import Repository  # noqa: E402


def register_real_repos(repo: Repository) -> int:
    roots: dict[str, str] = {}
    for name, cls in ADAPTERS.items():
        adapter = cls(None)
        if not adapter.root.is_dir():
            continue
        for sess in adapter.discover():
            if not sess.cwd:
                continue
            root = find_repo_root(sess.cwd)
            if root is None:
                continue
            root = str(root.resolve())
            roots.setdefault(root, Path(root).name)
    for root, nm in sorted(roots.items()):
        repo.upsert_project(
            project_id=project_id_for_root(root), name=nm, root=root, git_remote=None
        )
    return len(roots)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default=None)
    ap.add_argument("--keep", action="store_true", help="do not delete the throwaway home")
    ap.add_argument("--seed", type=int, default=13)
    args = ap.parse_args()

    home = Path(args.home) if args.home else Path(tempfile.mkdtemp(prefix="tw-stage5-"))
    home.mkdir(parents=True, exist_ok=True)
    os.environ["TRAJWEAVE_HOME"] = str(home)
    print(f"throwaway home: {home}")

    db = Database(home / "trajweave.db")
    repo = Repository(db)

    n_repos = register_real_repos(repo)
    print(f"registered {n_repos} real repo roots (DB only, no markers written)")

    stats = Importer(db).run(list(ADAPTERS.keys()))
    print("\n== import ==")
    print(json.dumps(stats.as_dict(), indent=2, default=str))
    print("counts:", repo.counts())

    print("\n== extract ==")
    result = ExperienceExtractor(repo).run(rebuild=True)
    print(json.dumps(result.as_dict(), indent=2, default=str))
    print("experience counts:", repo.experience_counts())

    exps = [dict(e) for e in repo.list_experiences()]
    cands = [e for e in exps if e["status"] == "candidate"]
    nme = [e for e in exps if e["status"] == "needs_more_evidence"]

    def dump(label: str, rows: list[dict]) -> None:
        print(f"\n===== {label} ({len(rows)}) =====")
        for e in rows:
            print(f"\n{e['id']}  conf={e['confidence']:.2f}  {e['pattern_type']}  "
                  f"[{e['status']}]  proj={e['project_count']}")
            print(f"  title : {e['title']}")
            print(f"  lesson: {e['reusable_lesson']}")
            print(f"  occ={e['occurrence_count']} support={e['support_count']} "
                  f"contra={e['contradiction_count']} amb={e['ambiguous_count']}")
            for ev in repo.get_experience_evidence(e["id"]):
                ev = dict(ev)
                task = (ev.get("task") or "").replace("\n", " ")[:70]
                print(f"    {ev['trajectory_id']}  {ev['relationship']:<13} "
                      f"seq {ev['start_sequence']}-{ev['end_sequence']}  {task}")

    by_conf = sorted(cands, key=lambda e: e["confidence"], reverse=True)
    dump("TOP 20 CANDIDATES", by_conf[:20])
    rng = random.Random(args.seed)
    dump("RANDOM 10 CANDIDATES", rng.sample(cands, min(10, len(cands))))
    dump("LOWEST 10 CANDIDATES", by_conf[-10:])
    dump("NEEDS MORE EVIDENCE (sample 10)", nme[:10])

    print("\n== group_key distribution ==")
    dist: dict[str, int] = {}
    for r in repo.load_occurrences():
        dist[r["group_key"]] = dist.get(r["group_key"], 0) + 1
    for k, v in sorted(dist.items(), key=lambda kv: -kv[1]):
        print(f"  {v:4d}  {k}")

    db.close()
    if not args.keep:
        shutil.rmtree(home, ignore_errors=True)
        print(f"\nremoved {home}")
    else:
        print(f"\nkept {home}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
