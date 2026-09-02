"""``trajweave`` command-line entry point.

Stage 0-3 surface only:

    trajweave init [PATH]        - opt a repository in
    trajweave projects           - list registered repositories
    trajweave import [--all]     - import historical Codex / Claude sessions
    trajweave sessions           - list discovered source sessions
    trajweave trajectories       - list stored trajectories
    trajweave show TW-000001     - inspect one trajectory
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from trajweave import __version__
from trajweave.config.paths import get_paths
from trajweave.ingest.importer import Importer
from trajweave.projects.registry import ProjectRegistry, RepoNotFoundError
from trajweave.storage.database import Database
from trajweave.storage.repository import Repository
from trajweave.utils.logging import configure_logging, get_logger

log = get_logger("cli")


# ----------------------------------------------------------------------
# parser
# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trajweave",
        description="Local-first coding-agent trajectory data substrate (Stage 0-3).",
    )
    parser.add_argument("--version", action="version", version=f"trajweave {__version__}")
    parser.add_argument(
        "--home",
        metavar="DIR",
        help="TrajWeave home directory (default: $TRAJWEAVE_HOME or ~/.trajweave)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose/debug logging")
    parser.add_argument("-q", "--quiet", action="store_true", help="only warnings and errors")

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_init = sub.add_parser("init", help="register the current repository with TrajWeave")
    p_init.add_argument("path", nargs="?", default=".", help="repository path (default: cwd)")
    p_init.add_argument("--name", help="override the project name")
    p_init.set_defaults(func=cmd_init)

    p_projects = sub.add_parser("projects", help="list registered projects")
    p_projects.add_argument("--json", action="store_true", help="machine-readable output")
    p_projects.set_defaults(func=cmd_projects)

    p_import = sub.add_parser("import", help="import historical agent sessions")
    p_import.add_argument("--all", action="store_true", help="import every known agent (default)")
    p_import.add_argument(
        "--agent", choices=["codex", "claude"], action="append", dest="agents",
        help="restrict to one agent (repeatable)",
    )
    p_import.add_argument("--project", metavar="PATH", help="only import sessions for this repo root")
    p_import.add_argument("--dry-run", action="store_true", help="discover + route but do not store")
    p_import.add_argument("--json", action="store_true", help="machine-readable summary")
    p_import.set_defaults(func=cmd_import)

    p_sessions = sub.add_parser("sessions", help="list discovered source sessions")
    p_sessions.add_argument("--agent", choices=["codex", "claude"])
    p_sessions.add_argument(
        "--status",
        choices=["imported", "ignored_unregistered", "failed", "skipped", "pending"],
    )
    p_sessions.add_argument("--json", action="store_true")
    p_sessions.set_defaults(func=cmd_sessions)

    p_traj = sub.add_parser("trajectories", help="list stored trajectories")
    p_traj.add_argument("--agent", choices=["codex", "claude"])
    p_traj.add_argument("--project", metavar="PATH", help="filter by repo root")
    p_traj.add_argument("--limit", type=int, default=50)
    p_traj.add_argument("--json", action="store_true")
    p_traj.set_defaults(func=cmd_trajectories)

    p_show = sub.add_parser("show", help="inspect a single trajectory")
    p_show.add_argument("trajectory_id")
    p_show.add_argument("--events", type=int, default=40, help="max events to print")
    p_show.add_argument("--json", action="store_true")
    p_show.set_defaults(func=cmd_show)

    return parser


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _open_db(args: argparse.Namespace) -> Database:
    paths = get_paths(args.home).ensure()
    return Database(paths.db_path)


def _print_json(obj) -> None:
    json.dump(obj, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def _resolve_project_id(repo: Repository, root: str | None) -> str | None:
    if not root:
        return None
    canonical = str(Path(root).expanduser().resolve())
    row = repo.get_project_by_root(canonical)
    return row["id"] if row else "__no_such_project__"


# ----------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------
def cmd_init(args: argparse.Namespace) -> int:
    with _open_db(args) as db:
        registry = ProjectRegistry(Repository(db))
        try:
            project = registry.init(args.path, name=args.name)
        except RepoNotFoundError as exc:
            log.error("%s", exc)
            return 2
    print(f"Initialized TrajWeave for project '{project.name}'")
    print(f"  project_id : {project.project_id}")
    print(f"  root       : {project.root}")
    print(f"  marker     : {Path(project.root) / '.trajweave' / 'project.json'}")
    print("\nThis repository's Codex/Claude sessions will now be imported by 'trajweave import'.")
    return 0


def cmd_projects(args: argparse.Namespace) -> int:
    with _open_db(args) as db:
        rows = [dict(r) for r in Repository(db).list_projects()]
    if args.json:
        _print_json(rows)
        return 0
    if not rows:
        print("No projects registered yet. Run 'trajweave init' inside a repository.")
        return 0
    width = max(len(r["name"]) for r in rows)
    print(f"{'NAME':<{width}}  {'STATUS':<8}  {'TRAJ':>5}  ROOT")
    for r in rows:
        print(
            f"{r['name']:<{width}}  {r['status']:<8}  {r['trajectory_count']:>5}  {r['root']}"
        )
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    agents = args.agents or (["codex", "claude"] if (args.all or not args.agents) else None)
    with _open_db(args) as db:
        importer = Importer(db)
        stats = importer.run(agents, project_filter=args.project, dry_run=args.dry_run)

    if args.json:
        _print_json(stats.as_dict())
        return 1 if stats.failed else 0

    for agent, count in stats.discovered.items():
        print(f"{agent.capitalize()} sessions discovered: {count}")
    print()
    if args.dry_run:
        print(f"Would import:            {stats.dry_run}")
    else:
        print(f"Imported:               {stats.imported}")
        print(f"Re-imported (changed):  {stats.reimported}")
    print(f"Already imported:       {stats.already_imported}")
    print(f"Ignored (unregistered): {stats.ignored_unregistered}")
    if stats.skipped_filtered:
        print(f"Skipped (filter):       {stats.skipped_filtered}")
    print(f"Failed:                 {stats.failed}")
    for agent, path, error in stats.failures:
        print(f"  - [{agent}] {path}\n      {error}")
    return 1 if stats.failed else 0


def cmd_sessions(args: argparse.Namespace) -> int:
    with _open_db(args) as db:
        rows = [dict(r) for r in Repository(db).list_source_sessions(args.agent, args.status)]
    if args.json:
        _print_json(rows)
        return 0
    if not rows:
        print("No sessions discovered yet. Run 'trajweave import --all'.")
        return 0
    print(f"{'AGENT':<7}  {'STATUS':<20}  {'SESSION ID':<38}  DETAIL")
    for r in rows:
        detail = r.get("detail") or ""
        print(f"{r['agent']:<7}  {r['status']:<20}  {str(r['source_session_id'])[:38]:<38}  {detail[:60]}")
    return 0


def cmd_trajectories(args: argparse.Namespace) -> int:
    with _open_db(args) as db:
        repo = Repository(db)
        project_id = _resolve_project_id(repo, args.project)
        if project_id == "__no_such_project__":
            log.error("no registered project with root %s", args.project)
            return 2
        rows = [
            dict(r)
            for r in repo.list_trajectories(
                project_id=project_id, agent=args.agent, limit=args.limit
            )
        ]
    if args.json:
        _print_json(rows)
        return 0
    if not rows:
        print("No trajectories stored yet.")
        return 0
    print(f"{'ID':<11}  {'AGENT':<7}  {'STATUS':<8}  {'EVENTS':>6}  {'PROJECT':<16}  TASK")
    for r in rows:
        task = (r.get("task") or "").replace("\n", " ")
        print(
            f"{r['id']:<11}  {r['agent']:<7}  {r['final_status']:<8}  "
            f"{r['event_count']:>6}  {(r.get('project_name') or '-'):<16}  {task[:70]}"
        )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    with _open_db(args) as db:
        repo = Repository(db)
        traj = repo.get_trajectory(args.trajectory_id)
        if traj is None:
            log.error("no trajectory %s", args.trajectory_id)
            return 2
        traj = dict(traj)
        events = [dict(e) for e in repo.get_trajectory_events(args.trajectory_id)]
        files = [dict(f) for f in repo.get_trajectory_files(args.trajectory_id)]

    if args.json:
        _print_json({"trajectory": traj, "events": events, "files": files})
        return 0

    print(f"{traj['id']}  ({traj['agent']})")
    print(f"  task          : {traj.get('task')}")
    print(f"  task_source   : {traj.get('task_source')}")
    print(f"  project       : {traj.get('project_name')} [{traj.get('repository_root')}]")
    print(f"  status        : {traj.get('final_status')}  ({traj.get('final_status_reason')})")
    print(f"  started/ended : {traj.get('started_at')}  ->  {traj.get('ended_at')}")
    print(f"  model         : {traj.get('model')}   cli: {traj.get('cli_version')}")
    print(f"  events        : {traj.get('event_count')}")
    if traj.get("parse_warnings"):
        try:
            warnings = json.loads(traj["parse_warnings"])
        except (TypeError, ValueError):
            warnings = [traj["parse_warnings"]]
        print(f"  parse_warnings: {len(warnings)} (e.g. {warnings[0] if warnings else ''})")

    changed = [f for f in files if f["was_created"] or f["was_modified"] or f["was_deleted"]]
    if changed:
        print("\n  files changed:")
        for f in changed:
            flags = "".join(
                c for c, on in
                (("r", f["was_read"]), ("c", f["was_created"]),
                 ("m", f["was_modified"]), ("d", f["was_deleted"]))
                if on
            )
            print(f"    [{flags}] {f['path']}")

    print("\n  events:")
    for e in events[: args.events]:
        bits = [f"{e['sequence']:>3}", f"{e['type']:<17}"]
        if e.get("path"):
            bits.append(e["path"])
        elif e.get("command"):
            bits.append(f"$ {e['command'][:80]}")
        elif e.get("summary"):
            bits.append(e["summary"][:90])
        if e.get("exit_code") is not None:
            bits.append(f"(exit {e['exit_code']})")
        if e.get("redacted"):
            bits.append("[redacted]")
        print("    " + "  ".join(bits))
    if len(events) > args.events:
        print(f"    ... {len(events) - args.events} more")
    return 0


# ----------------------------------------------------------------------
# entry
# ----------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(verbose=args.verbose, quiet=args.quiet)

    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:  # pragma: no cover
        log.error("interrupted")
        return 130
    except BrokenPipeError:  # pragma: no cover
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
