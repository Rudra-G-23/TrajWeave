"""``trajweave`` command-line entry point.

Current command surface:

    trajweave init [PATH]        - opt a repository in
    trajweave projects           - list registered repositories
    trajweave import [--all]     - import historical Codex / Claude sessions
    trajweave sessions           - list discovered source sessions
    trajweave trajectories       - list stored trajectories
    trajweave show TW-000001     - inspect one trajectory
    trajweave review list        - review Stage 6 proposals
    trajweave apply <id>         - explicitly apply an accepted preview
"""

from __future__ import annotations

import argparse
import json
import shlex
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
        description="Local-first coding-agent trajectory learning substrate.",
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

    p_exp = sub.add_parser("experiences", help="extract / inspect candidate experiences (Stage 5)")
    exp_sub = p_exp.add_subparsers(dest="exp_command", metavar="<subcommand>")

    e_extract = exp_sub.add_parser("extract", help="detect pattern occurrences and (re)build experiences")
    e_extract.add_argument("--project", metavar="PATH", help="only analyze trajectories for this repo root")
    e_extract.add_argument("--rebuild", action="store_true", help="reprocess every trajectory from scratch")
    e_extract.add_argument("--min-occurrences", type=int, default=None, help="candidate threshold (default 3)")
    e_extract.add_argument("--max-event-gap", type=int, default=None, help="failure->resolution window (default 20)")
    e_extract.add_argument(
        "--min-correction-tokens", type=int, default=None,
        help="bare human-correction token cap (default 3)",
    )
    e_extract.add_argument("--json", action="store_true")
    e_extract.set_defaults(func=cmd_experiences_extract)

    e_list = exp_sub.add_parser("list", help="list candidate experiences")
    e_list.add_argument(
        "--status", choices=["candidate", "needs_more_evidence", "rejected", "archived"]
    )
    e_list.add_argument("--json", action="store_true")
    e_list.set_defaults(func=cmd_experiences_list)

    e_show = exp_sub.add_parser("show", help="inspect one experience and its evidence")
    e_show.add_argument("experience_id")
    e_show.add_argument("--json", action="store_true")
    e_show.set_defaults(func=cmd_experiences_show)

    e_review = exp_sub.add_parser("review", help="record a false-positive / validity annotation")
    e_review.add_argument("experience_id")
    e_review.add_argument(
        "--status", required=True,
        choices=["valid", "false_positive", "needs_more_evidence", "unreviewed"],
    )
    e_review.add_argument("--note", default=None)
    e_review.set_defaults(func=cmd_experiences_review)

    p_exp.set_defaults(func=lambda _a: (p_exp.print_help() or 0))

    p_placements = sub.add_parser(
        "placements",
        help="generate / inspect read-only Stage 6 placement proposals",
    )
    placements_sub = p_placements.add_subparsers(dest="placements_command", metavar="<subcommand>")

    pl_generate = placements_sub.add_parser(
        "generate", help="derive deterministic placement alternatives for eligible experiences"
    )
    pl_generate.add_argument("--json", action="store_true", help="machine-readable summary")
    pl_generate.set_defaults(func=cmd_placements_generate)

    pl_list = placements_sub.add_parser("list", help="list current placement recommendations")
    pl_list.add_argument(
        "--type",
        dest="placement_type",
        choices=["ignore", "global_rule", "project_rule", "scoped_rule", "skill"],
        help="only recommendation sets whose recommended placement has this type",
    )
    pl_list.add_argument(
        "--recommended",
        choices=["ignore", "global_rule", "project_rule", "scoped_rule", "skill"],
        help="alias for --type, retained for explicit recommendation filtering",
    )
    pl_list.add_argument("--json", action="store_true", help="machine-readable output")
    pl_list.set_defaults(func=cmd_placements_list)

    pl_show = placements_sub.add_parser("show", help="show alternatives and evidence for one experience")
    pl_show.add_argument("experience_id")
    pl_show.add_argument("--json", action="store_true", help="machine-readable output")
    pl_show.set_defaults(func=cmd_placements_show)

    p_placements.set_defaults(func=lambda _a: (p_placements.print_help() or 0))

    p_review = sub.add_parser("review", help="review Stage 6 proposals without applying them")
    review_sub = p_review.add_subparsers(dest="review_command", metavar="<subcommand>")
    rv_list = review_sub.add_parser("list", help="list reviewable proposals and review state")
    rv_list.add_argument("--status", choices=["unreviewed", "accepted", "rejected", "deferred", "test_first", "applied", "stale"])
    rv_list.add_argument("--json", action="store_true")
    rv_list.set_defaults(func=cmd_review_list)
    rv_show = review_sub.add_parser("show", help="show review, alternatives, evidence, and history")
    rv_show.add_argument("review_id")
    rv_show.add_argument("--json", action="store_true")
    rv_show.set_defaults(func=cmd_review_show)
    rv_accept = review_sub.add_parser("accept", help="approve an exact proposal for later Apply")
    rv_accept.add_argument("review_id")
    rv_accept.add_argument("--agent", choices=["codex", "claude"])
    rv_accept.add_argument("--target")
    rv_accept.set_defaults(func=cmd_review_accept)
    for name, handler, help_text in (
        ("reject", cmd_review_reject, "reject a proposal"),
        ("defer", cmd_review_defer, "defer a proposal and keep collecting evidence"),
        ("test-first", cmd_review_test_first, "handoff a reviewed proposal to Stage 8"),
    ):
        command = review_sub.add_parser(name, help=help_text)
        command.add_argument("review_id")
        command.set_defaults(func=handler)
    rv_edit = review_sub.add_parser("edit", help="store an edited proposal variant")
    rv_edit.add_argument("review_id")
    content = rv_edit.add_mutually_exclusive_group(required=True)
    content.add_argument("--content")
    content.add_argument("--file")
    rv_edit.set_defaults(func=cmd_review_edit)
    rv_choose = review_sub.add_parser("choose", help="choose another Stage 6 placement alternative")
    rv_choose.add_argument("review_id")
    rv_choose.add_argument("--placement", required=True,
                           choices=["ignore", "global_rule", "project_rule", "scoped_rule", "skill"])
    rv_choose.set_defaults(func=cmd_review_choose)
    p_review.set_defaults(func=lambda _a: (p_review.print_help() or 0))

    p_apply = sub.add_parser("apply", help="preview or explicitly apply an accepted review")
    p_apply.add_argument("review_id")
    p_apply.add_argument("--dry-run", action="store_true", help="show the exact diff without writing")
    p_apply.set_defaults(func=cmd_apply)

    p_eval = sub.add_parser(
        "eval", help="run and inspect Stage 8 baseline/candidate evaluations of a reviewed policy"
    )
    eval_sub = p_eval.add_subparsers(dest="eval_command", metavar="<subcommand>")

    ev_list = eval_sub.add_parser("list", help="list frozen evaluations")
    ev_list.add_argument("--json", action="store_true")
    ev_list.set_defaults(func=cmd_eval_list)

    ev_show = eval_sub.add_parser("show", help="show one evaluation, its runs, and comparisons")
    ev_show.add_argument("evaluation_id")
    ev_show.add_argument("--json", action="store_true")
    ev_show.set_defaults(func=cmd_eval_show)

    ev_run = eval_sub.add_parser(
        "run",
        help="create a frozen evaluation from a reviewed proposal, or add repetitions to one",
    )
    ev_run.add_argument(
        "ref", help="a reviewed review/proposal id (creates a new evaluation) "
                    "or an existing evaluation id (adds repetitions to it)"
    )
    ev_run.add_argument("--repo", metavar="PATH", help="repository to snapshot (required when creating)")
    ev_run.add_argument("--commit", help="commit to snapshot (default: HEAD of --repo)")
    ev_run.add_argument("--task", help="inline task description")
    ev_run.add_argument("--task-file", metavar="PATH", help="read the task specification from a file")
    ev_run.add_argument(
        "--verify", action="append", metavar="CMD",
        help="a verifier shell command (repeatable; the first is the primary task check)",
    )
    ev_run.add_argument(
        "--agent-cmd", metavar="CMD",
        help="shell command that performs the task inside the isolated workspace "
             "(omit to skip the agent step and only verify the isolated snapshot); "
             "never invoked automatically - this must be passed explicitly",
    )
    ev_run.add_argument("--target-agent", choices=["codex", "claude"], help="default: the review's target agent")
    ev_run.add_argument("--target", help="explicit relative override for the candidate policy's target path")
    ev_run.add_argument("--agent-name", help="harness/agent name, for provenance only")
    ev_run.add_argument("--agent-version")
    ev_run.add_argument("--model")
    ev_run.add_argument("--model-version")
    ev_run.add_argument("--reasoning", help="reasoning/effort configuration, for provenance only")
    ev_run.add_argument("--timeout", type=int, default=120, help="per-command timeout in seconds")
    ev_run.add_argument("--repetitions", type=int, default=1, help="paired trials to run now")
    ev_run.add_argument(
        "--order", choices=["baseline_first", "candidate_first", "alternating"], default="baseline_first",
    )
    ev_run.add_argument("--seed", help="recorded verbatim; TrajWeave does not control agent-side determinism")
    ev_run.add_argument("--json", action="store_true")
    ev_run.set_defaults(func=cmd_eval_run)

    ev_compare = eval_sub.add_parser("compare", help="show paired baseline/candidate comparisons")
    ev_compare.add_argument("evaluation_id")
    ev_compare.add_argument("--json", action="store_true")
    ev_compare.set_defaults(func=cmd_eval_compare)

    p_eval.set_defaults(func=lambda _a: (p_eval.print_help() or 0))

    p_ui = sub.add_parser("ui", help="launch the local trajectory and review explorer")
    p_ui.add_argument(
        "--port", type=int, default=None,
        help="port to bind on 127.0.0.1 (default: 8765, auto-advances if taken)",
    )
    p_ui.add_argument("--no-browser", action="store_true", help="do not open a browser")
    p_ui.set_defaults(func=cmd_ui)

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


def _experience_config(args: argparse.Namespace):
    from trajweave.experience import ExperienceConfig

    base = ExperienceConfig()
    return ExperienceConfig(
        min_occurrences=args.min_occurrences if getattr(args, "min_occurrences", None) else base.min_occurrences,
        max_event_gap=args.max_event_gap if getattr(args, "max_event_gap", None) else base.max_event_gap,
        meaningful_correction_min_tokens=(
            args.min_correction_tokens
            if getattr(args, "min_correction_tokens", None)
            else base.meaningful_correction_min_tokens
        ),
    ).validated()


def cmd_experiences_extract(args: argparse.Namespace) -> int:
    from trajweave.experience import ExperienceExtractor

    cfg = _experience_config(args)
    with _open_db(args) as db:
        repo = Repository(db)
        project_id = _resolve_project_id(repo, args.project)
        if project_id == "__no_such_project__":
            log.error("no registered project with root %s", args.project)
            return 2
        result = ExperienceExtractor(repo, cfg).run(project_id=project_id, rebuild=args.rebuild)

    if args.json:
        _print_json(result.as_dict())
        return 0

    print(f"Trajectories considered: {result.trajectories_considered}")
    print(f"New trajectories analyzed: {result.trajectories_analyzed}")
    print()
    print(f"Pattern occurrences found: {result.occurrences_found}")
    print(f"Clusters formed: {result.clusters_formed}")
    print()
    print(f"Experience candidates created: {result.candidates_created}")
    print(f"Need more evidence: {result.needs_more_evidence}")
    print()
    print(f"LLM summaries: {'enabled' if result.llm_used else 'no'}"
          + (f" ({result.llm_tokens} tokens)" if result.llm_tokens else ""))
    print(f"Runtime: {result.runtime_seconds:.2f}s")
    if result.top_candidates:
        print("\nHighest-confidence candidates:\n")
        for c in result.top_candidates:
            print(f"  {c['confidence']:.2f}  {c['title']}  "
                  f"(x{c['occurrences']}, +{c['support']}/-{c['contradictions']}, "
                  f"{c['projects']} proj)")
    return 0


def cmd_experiences_list(args: argparse.Namespace) -> int:
    with _open_db(args) as db:
        rows = [dict(r) for r in Repository(db).list_experiences(status=args.status)]
    if args.json:
        _print_json(rows)
        return 0
    if not rows:
        print("No experiences yet. Run 'trajweave experiences extract'.")
        return 0
    print(f"{'ID':<8}  {'CONF':>4}  {'OCC':>4}  {'SUP':>4}  {'CON':>4}  {'PROJ':>4}  "
          f"{'STATUS':<19}  {'REVIEW':<14}  TITLE")
    for r in rows:
        print(
            f"{r['id']:<8}  {r['confidence']:>4.2f}  {r['occurrence_count']:>4}  "
            f"{r['support_count']:>4}  {r['contradiction_count']:>4}  {r['project_count']:>4}  "
            f"{r['status']:<19}  {r['review_status']:<14}  {(r['title'] or '')[:60]}"
        )
    return 0


def cmd_experiences_show(args: argparse.Namespace) -> int:
    with _open_db(args) as db:
        repo = Repository(db)
        exp = repo.get_experience(args.experience_id)
        if exp is None:
            log.error("no experience %s", args.experience_id)
            return 2
        exp = dict(exp)
        evidence = [dict(e) for e in repo.get_experience_evidence(args.experience_id)]

    if args.json:
        _print_json({"experience": exp, "evidence": evidence})
        return 0

    try:
        conf = json.loads(exp.get("confidence_json") or "{}")
    except (TypeError, ValueError):
        conf = {}

    print(f"{exp['id']}  {exp['title']}")
    print(f"  status        : {exp['status']}   review: {exp['review_status']}")
    print(f"  pattern_type  : {exp['pattern_type']}")
    print(f"  confidence    : {exp['confidence']:.2f}")
    print(f"  occurrences   : {exp['occurrence_count']}  "
          f"(support {exp['support_count']}, contradiction {exp['contradiction_count']}, "
          f"ambiguous {exp['ambiguous_count']})")
    print(f"  projects      : {exp['project_count']}")
    print(f"  seen          : {exp['first_seen_at']}  ->  {exp['last_seen_at']}")
    comp = conf.get("components", {})
    if comp:
        print(f"  confidence parts: support_ratio={comp.get('support_ratio')}, "
              f"recurrence={comp.get('recurrence')}, cross_project={comp.get('cross_project')}, "
              f"recency={comp.get('recency')}")
    print(f"\n  summary\n    {exp['summary']}")
    print(f"\n  candidate reusable lesson\n    {exp['reusable_lesson']}")
    if exp.get("review_note"):
        print(f"\n  review note: {exp['review_note']}")
    print("\n  evidence:")
    for e in evidence:
        task = (e.get("task") or "").replace("\n", " ")[:52]
        print(f"    {e['trajectory_id']:<12}  {e['relationship']:<13}  "
              f"seq {e['start_sequence']}-{e['end_sequence']}  {task}")
    return 0


def cmd_experiences_review(args: argparse.Namespace) -> int:
    with _open_db(args) as db:
        ok = Repository(db).set_experience_review(
            args.experience_id, review_status=args.status, note=args.note
        )
    if not ok:
        log.error("no experience %s", args.experience_id)
        return 2
    print(f"{args.experience_id}: review_status = {args.status}")
    return 0


def cmd_placements_generate(args: argparse.Namespace) -> int:
    """Generate canonical proposals only - never apply them to a repository."""

    from trajweave.placement import PlacementGenerator

    with _open_db(args) as db:
        result = PlacementGenerator(Repository(db)).run()
    payload = result.as_dict()
    if args.json:
        _print_json(payload)
        return 0

    print(f"Eligible experiences: {payload['eligible_experiences']}")
    print(f"Placement proposal sets: {payload['proposal_sets']}")
    print(f"Regenerated: {payload.get('regenerated', 0)}")
    print(f"Unchanged: {payload.get('unchanged', 0)}")
    print(f"Runtime: {payload['runtime_seconds']:.2f}s")
    return 0


def cmd_placements_list(args: argparse.Namespace) -> int:
    placement_type = args.recommended or args.placement_type
    with _open_db(args) as db:
        rows = [
            dict(row)
            for row in Repository(db).list_placement_proposal_sets(
                placement_type=placement_type,
                recommended=placement_type,
            )
        ]
    if args.json:
        _print_json(rows)
        return 0
    if not rows:
        print("No placement proposal sets. Run 'trajweave placements generate'.")
        return 0
    print(f"{'EXPERIENCE':<12}  {'RECOMMENDED':<14}  {'SCORE':>5}  {'SCOPE':<30}  TITLE")
    for row in rows:
        placement_kind = row["recommended_type"]
        score = row["recommended_score"]
        scope = _placement_scope({
            "scope_type": row.get("recommended_scope_type"),
            "scope_value": row.get("recommended_scope_value"),
        })
        print(
            f"{row['experience_id']:<12}  {placement_kind:<14}  "
            f"{float(score):>5.2f}  {scope:<30}  {(row.get('experience_title') or '')[:55]}"
        )
    return 0


def cmd_placements_show(args: argparse.Namespace) -> int:
    with _open_db(args) as db:
        payload = _read_placement_set(Repository(db), args.experience_id)
    if payload is None:
        log.error("no placement proposal set for experience %s", args.experience_id)
        return 2
    payload = _placement_payload(payload)
    if args.json:
        _print_json(payload)
        return 0

    proposal_set = payload.get("proposal_set") or {}
    proposals = payload.get("proposals") or []
    evidence = payload.get("evidence") or []
    print(f"{args.experience_id} placement alternatives")
    if proposal_set.get("generator_version"):
        print(f"  generator: {proposal_set['generator_version']}")
    if proposal_set.get("created_at"):
        print(f"  generated: {proposal_set['created_at']}")
    print()
    for proposal in proposals:
        scope = _placement_scope(proposal)
        print(
            f"  {proposal.get('rank', '-')}. {proposal['placement_type']:<14} "
            f"{float(proposal['score']):.2f}  scope: {scope}"
        )
        if proposal.get("proposed_content"):
            print(f"     {proposal['proposed_content']}")
        for diagnostic in proposal.get("diagnostics") or []:
            sign = (
                diagnostic.get("sign", diagnostic.get("polarity", ""))
                if isinstance(diagnostic, dict) else ""
            )
            message = diagnostic.get("message", "") if isinstance(diagnostic, dict) else str(diagnostic)
            print(f"     {sign} {message}".rstrip())
    if evidence:
        print("\n  evidence:")
        for item in evidence:
            print(
                f"    {item.get('trajectory_id', '-'):<12}  "
                f"{item.get('relationship', '-'):<13}  "
                f"seq {item.get('start_sequence', '-')}-{item.get('end_sequence', '-')}"
            )
    return 0


def _review_service(args: argparse.Namespace):
    from trajweave.review.service import ReviewService

    paths = get_paths(args.home).ensure()
    db = _open_db(args)
    return db, ReviewService(Repository(db), paths)


def _review_payload(service, value: str) -> dict:
    return service.history(value)


def cmd_review_list(args: argparse.Namespace) -> int:
    db, service = _review_service(args)
    try:
        rows = service.list(status=args.status)
    finally:
        db.close()
    if args.json:
        _print_json(rows)
        return 0
    if not rows:
        print("No reviewable proposals. Run 'trajweave placements generate'.")
        return 0
    print(f"{'REVIEW':<22}  {'STATUS':<12}  {'RECOMMENDED':<14}  {'SCORE':>5}  TITLE")
    for row in rows:
        print(f"{row['review_id']:<22}  {row.get('review_status', 'unreviewed'):<12}  "
              f"{row.get('recommended_type', '-'):<14}  {float(row.get('recommended_score') or 0):>5.2f}  "
              f"{(row.get('experience_title') or '')[:60]}")
    return 0


def _print_review(payload: dict) -> None:
    review = payload.get("review") or {}
    proposal = payload.get("proposal") or {}
    exp = payload.get("experience") or {}
    print(f"{review.get('id') or 'unreviewed'}  {exp.get('id')}  {exp.get('title') or '(untitled)'}")
    print(f"  status       : {review.get('computed_status') or review.get('status') or 'unreviewed'}")
    print(f"  proposal     : {proposal.get('id')}  {proposal.get('placement_type')}  rank {proposal.get('rank')}")
    print(f"  scope        : {_placement_scope(proposal)}")
    print(f"  content      : {proposal.get('effective_content') or '-'}")
    print(f"  alternatives : {len(payload.get('alternatives') or [])}")
    print(f"  evidence     : {len(payload.get('evidence') or [])}")
    if review:
        print(f"  target       : {review.get('target_path') or '(not selected)'}")
        print(f"  history      : {len((payload.get('history') or {}).get('actions') or [])} actions")


def cmd_review_show(args: argparse.Namespace) -> int:
    db, service = _review_service(args)
    try:
        payload = _review_payload(service, args.review_id)
    except Exception as exc:
        log.error(str(exc))
        return 2
    finally:
        db.close()
    if args.json:
        _print_json(payload)
    else:
        _print_review(payload)
        print("\n  alternatives:")
        for item in payload.get("alternatives") or []:
            print(f"    {item.get('rank')}. {item.get('placement_type')} {float(item.get('score') or 0):.2f} "
                  f"{_placement_scope(item)}  {item.get('proposed_content') or '-'}")
        for action in (payload.get("history") or {}).get("actions") or []:
            print(f"    action {action['created_at']}: {action['action']} -> {action['to_status']}")
    return 0


def _run_review_action(args: argparse.Namespace, action: str) -> int:
    db, service = _review_service(args)
    try:
        if action == "accept":
            rid = service.accept(args.review_id, agent=args.agent, target=args.target)
        elif action == "reject":
            rid = service.reject(args.review_id)
        elif action == "defer":
            rid = service.defer(args.review_id)
        elif action == "test_first":
            rid = service.test_first(args.review_id)
        elif action == "choose":
            rid = service.choose(args.review_id, args.placement)
        else:
            if args.content is not None:
                text = args.content
            else:
                text = Path(args.file).read_text("utf-8")
            rid = service.edit(args.review_id, text)
    except Exception as exc:
        log.error(str(exc))
        return 2
    finally:
        db.close()
    print(f"{rid}: {action} recorded; Apply remains explicit")
    return 0


def cmd_review_accept(args: argparse.Namespace) -> int:
    return _run_review_action(args, "accept")


def cmd_review_reject(args: argparse.Namespace) -> int:
    return _run_review_action(args, "reject")


def cmd_review_defer(args: argparse.Namespace) -> int:
    return _run_review_action(args, "defer")


def cmd_review_test_first(args: argparse.Namespace) -> int:
    return _run_review_action(args, "test_first")


def cmd_review_edit(args: argparse.Namespace) -> int:
    return _run_review_action(args, "edit")


def cmd_review_choose(args: argparse.Namespace) -> int:
    return _run_review_action(args, "choose")


def cmd_apply(args: argparse.Namespace) -> int:
    db, service = _review_service(args)
    try:
        result = service.apply(args.review_id, dry_run=args.dry_run)
    except Exception as exc:
        log.error(str(exc))
        return 2
    finally:
        db.close()
    if args.dry_run:
        print(f"Dry-run: {result['target_path']}")
        print(f"Target hash: {result.get('target_hash') or '(missing)'}")
        print(result.get("unified_diff") or "(no changes)")
        print("Writes: no")
    else:
        print(f"{result['outcome']}: {result['target_path']}")
    return 0


def _eval_service(args: argparse.Namespace):
    from trajweave.evaluation import EvaluationService

    paths = get_paths(args.home).ensure()
    db = _open_db(args)
    return db, EvaluationService(Repository(db), paths)


def _parse_verifiers(args: argparse.Namespace) -> list:
    from trajweave.evaluation.models import VerifierCheck

    checks = []
    for i, raw in enumerate(args.verify or []):
        name = "task" if i == 0 else f"check_{i}"
        checks.append(VerifierCheck(name, shlex.split(raw), args.timeout))
    return checks


def cmd_eval_list(args: argparse.Namespace) -> int:
    db, service = _eval_service(args)
    try:
        rows = service.list()
    finally:
        db.close()
    if args.json:
        _print_json(rows)
        return 0
    if not rows:
        print("No evaluations yet. Run 'trajweave eval run <review-id>'.")
        return 0
    print(f"{'EVALUATION':<20}  {'REVIEW':<20}  {'REPS':>4}  {'TARGET':<8}  CREATED")
    for r in rows:
        print(
            f"{r['id']:<20}  {r['review_id']:<20}  {int(r['repetitions']):>4}  "
            f"{r['target_agent']:<8}  {r['created_at']}"
        )
    return 0


def _print_eval(payload: dict) -> None:
    spec = payload["spec"]
    print(f"{spec['id']}  review={spec['review_id']}  agent={spec['target_agent']}  placement={spec['placement_type']}")
    print(f"  repo    : {spec['repo_root']} @ {str(spec['repo_commit'])[:12]}")
    print(f"  created : {spec['created_at']}")
    print(f"\n  runs: {len(payload['runs'])}")
    for run in payload["runs"]:
        print(
            f"    {run['id']:<38} {run['condition']:<10} {run['status']:<10} "
            f"dur={run.get('duration_ms')}ms"
        )
        if run.get("error_reason"):
            print(f"       error: {run['error_reason']}")
        for v in run.get("verifier_results") or []:
            mark = "PASS" if v["passed"] else "FAIL"
            print(f"       [{mark}] {v['checker_name']}")
    print(f"\n  comparisons: {len(payload['comparisons'])}")
    for c in payload["comparisons"]:
        print(
            f"    rep {c['repetition_index']}: {c['outcome']:<12} "
            f"delta={c.get('task_success_delta')}  regressions={c['regression_count']}"
        )
        if c.get("invalid_reason"):
            print(f"      reason: {c['invalid_reason']}")


def cmd_eval_show(args: argparse.Namespace) -> int:
    db, service = _eval_service(args)
    try:
        payload = service.history(args.evaluation_id)
    except Exception as exc:
        log.error(str(exc))
        return 2
    finally:
        db.close()
    if args.json:
        _print_json(payload)
    else:
        _print_eval(payload)
    return 0


def cmd_eval_compare(args: argparse.Namespace) -> int:
    db, service = _eval_service(args)
    try:
        payload = service.history(args.evaluation_id)
    except Exception as exc:
        log.error(str(exc))
        return 2
    finally:
        db.close()
    comparisons = payload["comparisons"]
    if args.json:
        _print_json(comparisons)
        return 0
    if not comparisons:
        print("No comparisons recorded yet.")
        return 0
    for c in comparisons:
        print(
            f"rep {c['repetition_index']}: {c['outcome']}  "
            f"delta={c.get('task_success_delta')}  regressions={c['regression_count']}"
        )
        if c.get("invalid_reason"):
            print(f"  reason: {c['invalid_reason']}")
    return 0


def cmd_eval_run(args: argparse.Namespace) -> int:
    from trajweave.evaluation import EvaluationError

    db, service = _eval_service(args)
    try:
        existing = service.repo.get_evaluation_spec(args.ref)
        spec_defining = any((
            args.repo, args.commit, args.task, args.task_file, args.verify, args.agent_cmd,
            args.target_agent, args.target, args.agent_name, args.agent_version, args.model,
            args.model_version, args.reasoning,
        ))
        if existing is not None:
            if spec_defining:
                log.error(
                    "%s is an existing evaluation; its spec is frozen. "
                    "Only --repetitions may be passed when re-running it.", args.ref,
                )
                return 2
            evaluation_id = args.ref
        else:
            if not args.repo:
                log.error("--repo is required when creating a new evaluation")
                return 2
            task: dict = {}
            if args.task_file:
                task = {"description": Path(args.task_file).read_text("utf-8")}
            elif args.task:
                task = {"description": args.task}
            try:
                evaluation_id = service.create_spec(
                    args.ref,
                    repo=args.repo,
                    commit=args.commit,
                    task=task,
                    verifiers=_parse_verifiers(args),
                    agent_command=shlex.split(args.agent_cmd) if args.agent_cmd else None,
                    target_agent=args.target_agent,
                    target_override=args.target,
                    agent_name=args.agent_name,
                    agent_version=args.agent_version,
                    model_name=args.model,
                    model_version=args.model_version,
                    reasoning_config={"reasoning": args.reasoning} if args.reasoning else None,
                    execution_limits={"timeout_seconds": args.timeout},
                    condition_order_mode=args.order,
                    seed=args.seed,
                )
            except EvaluationError as exc:
                log.error(str(exc))
                return 2
        try:
            comparison_ids = service.run_repetition(evaluation_id, count=args.repetitions)
        except EvaluationError as exc:
            log.error(str(exc))
            return 2
    finally:
        db.close()
    if args.json:
        _print_json({"evaluation_id": evaluation_id, "comparison_ids": comparison_ids})
        return 0
    print(f"{evaluation_id}: {len(comparison_ids)} repetition(s) recorded")
    for cid in comparison_ids:
        print(f"  {cid}")
    return 0


def _placement_scope(row: dict) -> str:
    scope_type = row.get("scope_type") or "global"
    scope_value = row.get("scope_value")
    return scope_type if not scope_value else f"{scope_type}: {scope_value}"


def _placement_payload(payload: object) -> dict:
    """Convert sqlite rows / serialized diagnostics for the CLI boundary only."""

    if not isinstance(payload, dict):
        payload = dict(payload)
    out = dict(payload)
    out["proposal_set"] = dict(out.get("proposal_set") or {})
    proposals = []
    for proposal in out.get("proposals") or []:
        proposal = dict(proposal)
        diagnostics = proposal.get("diagnostics")
        if diagnostics is None:
            diagnostics = proposal.pop("diagnostics_json", None)
        if isinstance(diagnostics, str):
            try:
                diagnostics = json.loads(diagnostics)
            except (TypeError, ValueError):
                diagnostics = [diagnostics]
        proposal["diagnostics"] = diagnostics or []
        proposals.append(proposal)
    out["proposals"] = proposals
    out["evidence"] = [dict(item) for item in out.get("evidence") or []]
    return out


def _read_placement_set(repo: Repository, experience_id: str) -> dict | None:
    """Assemble the repository's normalized Stage 6 read contract for CLI.

    The storage layer deliberately exposes rows separately so Stage 7 can use
    them independently.  The CLI needs one evidence-backed display payload.
    """

    proposal_set = repo.get_placement_proposal_set(experience_id)
    if proposal_set is None:
        return None
    proposals = [dict(row) for row in repo.get_placement_proposals(experience_id)]
    by_occurrence: dict[str, dict] = {}
    for proposal in proposals:
        for row in repo.get_placement_proposal_evidence(proposal["id"]):
            item = dict(row)
            item["relationship"] = item.get("classification") or item.get("role")
            item.pop("project_root", None)
            by_occurrence.setdefault(item["occurrence_id"], item)
    return {
        "proposal_set": dict(proposal_set),
        "proposals": proposals,
        "evidence": list(by_occurrence.values()),
    }


def cmd_ui(args: argparse.Namespace) -> int:
    from trajweave.ui.server import serve

    paths = get_paths(args.home).ensure()
    port = args.port if args.port is not None else 8765
    return serve(
        paths.db_path,
        port=port,
        explicit_port=args.port is not None,
        open_browser=not args.no_browser,
        verbose=args.verbose,
    )


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
