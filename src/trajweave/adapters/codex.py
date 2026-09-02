"""Codex CLI session adapter.

Discovered format (Codex CLI ``0.14x``+): one JSONL file per session at
``~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<iso>-<uuid>.jsonl``. Every line is
``{"timestamp", "ordinal", "type", "payload"}`` where ``type`` is one of
``session_meta``, ``turn_context``, ``event_msg``, ``response_item``,
``world_state``, ``compacted``, ``inter_agent_communication_metadata``.

The richest signal is ``event_msg`` / ``item_completed`` - Codex's own
semantic item stream (``UserMessage``, ``AgentMessage``, ``CommandExecution``,
``FileChange``, ``McpToolCall``, ``Reasoning`` ...). We normalize primarily off
that, and fall back to the raw ``response_item`` stream for older sessions that
predate it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterator

from trajweave.adapters.base import BaseAdapter, first_jsonl_object, iter_jsonl
from trajweave.models.enums import Agent, CommandKind, EventType, TaskSource
from trajweave.models.events import NormalizedEvent
from trajweave.models.trajectory import DiscoveredSession, NormalizedTrajectory, SourceSessionRef
from trajweave.normalization.commands import classify_command, command_event_family
from trajweave.normalization.paths import relativize
from trajweave.normalization.redaction import redact
from trajweave.normalization.status import infer_final_status
from trajweave.normalization.text import (
    DEFAULT_COMMAND_LIMIT,
    DEFAULT_TASK_LIMIT,
    bound_text,
    strip_injected_context,
    summarize,
)
from trajweave.utils.hashing import file_sha256
from trajweave.utils.logging import get_logger
from trajweave.utils.timeparse import to_iso

log = get_logger("adapters.codex")

_FILENAME_RE = re.compile(r"rollout-.*?-([0-9a-fA-F-]{36})\.jsonl$")


class CodexAdapter(BaseAdapter):
    agent = Agent.CODEX
    agent_name = "codex"
    root_env_var = "TRAJWEAVE_CODEX_ROOT"

    @classmethod
    def default_root(cls) -> Path:
        return Path.home() / ".codex" / "sessions"

    # ------------------------------------------------------------------
    # discovery
    # ------------------------------------------------------------------
    def discover(self) -> Iterator[DiscoveredSession]:
        if not self.root.is_dir():
            return
        for path in sorted(self.root.rglob("rollout-*.jsonl")):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            meta = self._peek_meta(path)
            yield DiscoveredSession(
                agent=self.agent,
                source_session_id=meta.get("session_id") or self._id_from_name(path),
                path=path,
                mtime=stat.st_mtime,
                size_bytes=stat.st_size,
                cwd=meta.get("cwd"),
                git_remote=meta.get("git_remote"),
                git_branch=meta.get("git_branch"),
            )

    def _id_from_name(self, path: Path) -> str:
        m = _FILENAME_RE.search(path.name)
        return m.group(1) if m else path.stem

    def _peek_meta(self, path: Path) -> dict[str, Any]:
        obj = first_jsonl_object(path)
        out: dict[str, Any] = {}
        if not obj:
            return out
        payload = obj.get("payload") if isinstance(obj, dict) else None
        if isinstance(payload, dict) and obj.get("type") == "session_meta":
            out["session_id"] = payload.get("session_id") or payload.get("id")
            out["cwd"] = payload.get("cwd")
            git = payload.get("git")
            if isinstance(git, dict):
                out["git_remote"] = git.get("repository_url")
                out["git_branch"] = git.get("branch")
        return out

    # ------------------------------------------------------------------
    # parsing
    # ------------------------------------------------------------------
    def parse(
        self, session: DiscoveredSession, repo_root: str | None = None
    ) -> NormalizedTrajectory:
        path = Path(session.path)
        source = SourceSessionRef(
            agent=self.agent,
            source_session_id=session.source_session_id,
            source_path=str(path),
            source_hash=file_sha256(path),
            source_mtime=session.mtime,
            size_bytes=session.size_bytes,
        )
        traj = NormalizedTrajectory(agent=self.agent, source=source)

        ctx = _ParseState(repo_root=repo_root)
        used_item_stream = False

        for lineno, obj, err in iter_jsonl(path):
            if err:
                traj.parse_warnings.append(err)
                continue
            assert obj is not None
            rtype = obj.get("type")
            payload = obj.get("payload")
            ts = to_iso(obj.get("timestamp"))
            if not isinstance(payload, dict):
                if rtype not in ("world_state",):
                    traj.parse_warnings.append(f"line {lineno}: {rtype} without dict payload")
                continue

            try:
                if rtype == "session_meta":
                    self._handle_meta(payload, traj, ctx)
                elif rtype == "turn_context":
                    self._handle_turn_context(payload, traj, ctx)
                elif rtype == "event_msg":
                    handled = self._handle_event_msg(payload, ts, traj, ctx)
                    used_item_stream = used_item_stream or handled
                elif rtype == "response_item":
                    ctx.raw_response_items.append((ts, payload))
                elif rtype == "compacted":
                    traj.add_event(NormalizedEvent(
                        type=EventType.UNKNOWN, timestamp=ts,
                        summary="context compacted",
                        metadata={"codex_record": "compacted"},
                    ))
                elif rtype in ("inter_agent_communication_metadata", "world_state"):
                    pass
                else:
                    traj.parse_warnings.append(f"line {lineno}: unknown record type {rtype!r}")
            except Exception as exc:  # pragma: no cover - defensive per-line guard
                traj.parse_warnings.append(f"line {lineno}: {type(exc).__name__}: {exc}")

        if not used_item_stream:
            self._normalize_from_response_items(ctx, traj)

        self._finalize(traj, ctx, session)
        return traj

    # -- record handlers ------------------------------------------------
    def _handle_meta(self, payload: dict, traj: NormalizedTrajectory, ctx: "_ParseState") -> None:
        traj.cwd = traj.cwd or payload.get("cwd")
        traj.cli_version = payload.get("cli_version") or traj.cli_version
        git = payload.get("git")
        if isinstance(git, dict):
            traj.git_remote = traj.git_remote or git.get("repository_url")
            traj.git_branch = traj.git_branch or git.get("branch")
            traj.git_commit = traj.git_commit or git.get("commit_hash")
        if ctx.repo_root is None and payload.get("cwd"):
            ctx.cwd_hint = payload.get("cwd")

    def _handle_turn_context(self, payload: dict, traj: NormalizedTrajectory, ctx: "_ParseState") -> None:
        model = payload.get("model")
        if model:
            traj.model = model
        if payload.get("cwd"):
            traj.cwd = traj.cwd or payload.get("cwd")

    def _handle_event_msg(
        self, payload: dict, ts: str | None, traj: NormalizedTrajectory, ctx: "_ParseState"
    ) -> bool:
        ptype = payload.get("type")
        if ptype == "token_count":
            info = payload.get("info") or {}
            usage = info.get("total_token_usage") or info.get("last_token_usage")
            if isinstance(usage, dict):
                ctx.token_usage = {
                    k: usage.get(k)
                    for k in (
                        "input_tokens", "cached_input_tokens", "output_tokens",
                        "reasoning_output_tokens", "total_tokens",
                    )
                    if usage.get(k) is not None
                }
            return False
        if ptype == "task_started":
            return False
        if ptype == "task_complete":
            msg = payload.get("last_agent_message") or ""
            ctx.explicit_completion = True
            ctx.completion_message = msg or ctx.completion_message
            ctx.last_terminal = "complete"
            traj.add_event(NormalizedEvent(
                type=EventType.COMPLETION, timestamp=ts,
                summary=summarize(redact(msg)[0]) if msg else "task complete",
                metadata={"duration_ms": payload.get("duration_ms")},
            ))
            return False
        if ptype == "turn_aborted":
            ctx.last_terminal = "aborted"
            # A trailing abort with no work done in that turn is almost always
            # the user quitting the CLI after the task already finished - not a
            # real abort of substantive work.
            if ctx.work_since_last_prompt > 0:
                ctx.real_abort = True
            traj.add_event(NormalizedEvent(
                type=EventType.UNKNOWN, timestamp=ts, summary="turn aborted",
                metadata={
                    "reason": payload.get("reason"),
                    "benign": ctx.work_since_last_prompt == 0,
                },
            ))
            return False
        if ptype == "item_completed":
            item = payload.get("item")
            if isinstance(item, dict):
                self._handle_item(item, ts, traj, ctx)
                return True
            return False
        return False

    def _handle_item(
        self, item: dict, ts: str | None, traj: NormalizedTrajectory, ctx: "_ParseState"
    ) -> None:
        itype = item.get("type")

        if itype == "UserMessage":
            self._emit_user_message(_join_text(item.get("content")), ts, traj, ctx)
        elif itype == "AgentMessage":
            text = _join_text(item.get("content"))
            clean, r = redact(text)
            traj.add_event(NormalizedEvent(
                type=EventType.ASSISTANT_MESSAGE, timestamp=ts,
                summary=summarize(clean), redacted=r,
                metadata={"phase": item.get("phase")},
            ))
            ctx.note_work()
        elif itype == "Reasoning":
            pass  # deliberately dropped (often empty / encrypted)
        elif itype == "CommandExecution":
            self._emit_command(item, ts, traj, ctx)
        elif itype == "FileChange":
            self._emit_file_change(item, ts, traj, ctx)
        elif itype == "McpToolCall":
            server = item.get("server") or item.get("appName") or "mcp"
            tool = item.get("tool") or item.get("actionName") or "call"
            traj.add_event(NormalizedEvent(
                type=EventType.TOOL_CALL, timestamp=ts, tool_name=f"{server}.{tool}",
                summary=f"MCP {server}.{tool} ({item.get('status', 'unknown')})",
                metadata={"read_only": item.get("readOnlyHint"), "status": item.get("status")},
            ))
            ctx.note_work()
        elif itype == "Extension":
            kind = item.get("kind", "extension")
            results = item.get("results")
            traj.add_event(NormalizedEvent(
                type=EventType.TOOL_CALL, timestamp=ts, tool_name=f"extension:{kind}",
                summary=summarize(redact(str(item.get("query") or ""))[0]) or kind,
                metadata={
                    "kind": kind,
                    "result_count": len(results) if isinstance(results, list) else None,
                },
            ))
            ctx.note_work()
        elif itype == "SubAgentActivity":
            traj.add_event(NormalizedEvent(
                type=EventType.TOOL_CALL, timestamp=ts, tool_name="subagent",
                summary=f"subagent {item.get('kind', '')} {item.get('agent_path', '')}".strip(),
                metadata={"kind": item.get("kind"), "agent_path": item.get("agent_path")},
            ))
        elif itype == "Plan":
            clean, r = redact(item.get("text") or "")
            traj.add_event(NormalizedEvent(
                type=EventType.ASSISTANT_MESSAGE, timestamp=ts,
                summary=summarize(clean), redacted=r, metadata={"subtype": "plan"},
            ))
            ctx.note_work()
        elif itype == "ContextCompaction":
            traj.add_event(NormalizedEvent(
                type=EventType.UNKNOWN, timestamp=ts, summary="context compaction",
                metadata={"item_type": itype},
            ))
        else:
            traj.add_event(NormalizedEvent(
                type=EventType.UNKNOWN, timestamp=ts,
                summary=f"unhandled item {itype}", metadata={"item_type": itype},
            ))

    # -- item emitters ------------------------------------------------
    def _emit_user_message(
        self, text: str, ts: str | None, traj: NormalizedTrajectory, ctx: "_ParseState"
    ) -> None:
        human = strip_injected_context(text)
        if not human:
            return
        clean, r = redact(human)
        if traj.task is None:
            traj.task = bound_text(clean, DEFAULT_TASK_LIMIT)
            traj.task_source = TaskSource.USER_PROMPT
            evt_type = EventType.USER_PROMPT
        else:
            evt_type = (
                EventType.HUMAN_CORRECTION
                if ctx.saw_assistant_activity
                else EventType.USER_PROMPT
            )
        traj.add_event(NormalizedEvent(
            type=evt_type, timestamp=ts, summary=summarize(clean), redacted=r,
        ))
        ctx.saw_assistant_activity = False
        ctx.work_since_last_prompt = 0

    def _emit_command(
        self, item: dict, ts: str | None, traj: NormalizedTrajectory, ctx: "_ParseState"
    ) -> None:
        argv = item.get("command")
        command = _extract_command(argv)
        exit_code = item.get("exit_code")
        status = item.get("status")
        parsed = item.get("parsed_cmd") or []
        hint = parsed[0].get("type") if parsed and isinstance(parsed[0], dict) else None
        kind = classify_command(command, hint)

        clean_cmd, r1 = redact(command or "")
        tail = _output_tail(item)
        clean_tail, r2 = redact(tail) if tail else (None, False)

        cwd = item.get("cwd")
        traj.add_event(NormalizedEvent(
            type=EventType.COMMAND, timestamp=ts,
            command=bound_text(clean_cmd, DEFAULT_COMMAND_LIMIT),
            exit_code=exit_code if isinstance(exit_code, int) else None,
            summary=summarize(clean_tail) if clean_tail else None,
            redacted=r1 or r2,
            metadata={
                "kind": str(kind),
                "status": status,
                "duration_ms": _duration_ms(item.get("duration")),
                "cwd": relativize(cwd, ctx.repo_root) if cwd else None,
                "parsed_kind": hint,
            },
        ))
        ctx.note_work()

        outcome = command_event_family(kind, exit_code if isinstance(exit_code, int) else None)
        if outcome is not None:
            traj.add_event(NormalizedEvent(
                type=outcome, timestamp=ts,
                summary=f"{kind} exit {exit_code}",
                metadata={"command_kind": str(kind), "exit_code": exit_code},
            ))
        ctx.register_verification(kind, exit_code)

        # Codex's own parsed_cmd tells us which files a read/grep command touched.
        for pc in parsed:
            if isinstance(pc, dict) and pc.get("type") == "read" and pc.get("path"):
                rel = relativize(pc["path"], ctx.repo_root)
                traj.touch_file(rel, read=True)
                traj.add_event(NormalizedEvent(
                    type=EventType.FILE_READ, timestamp=ts, path=rel,
                    summary=f"read {rel}" if rel else "read",
                    metadata={"via": "command"},
                ))

    def _emit_file_change(
        self, item: dict, ts: str | None, traj: NormalizedTrajectory, ctx: "_ParseState"
    ) -> None:
        changes = item.get("changes")
        if not isinstance(changes, dict):
            return
        for raw_path, spec in changes.items():
            if not isinstance(spec, dict):
                continue
            change_type = spec.get("type")
            move_path = spec.get("move_path")
            added, removed = _diff_stats(spec.get("unified_diff"))
            rel = relativize(raw_path, ctx.repo_root)

            if change_type == "add":
                etype = EventType.FILE_CREATE
                traj.touch_file(rel, created=True)
            elif change_type == "delete":
                etype = EventType.FILE_DELETE
                traj.touch_file(rel, deleted=True)
            else:
                etype = EventType.FILE_EDIT
                traj.touch_file(rel, modified=True)

            meta = {"lines_added": added, "lines_removed": removed, "change_type": change_type}
            traj.add_event(NormalizedEvent(
                type=etype, timestamp=ts, path=rel,
                summary=f"{change_type} {rel} (+{added}/-{removed})" if rel else change_type,
                metadata=meta,
            ))
            if move_path:
                move_rel = relativize(move_path, ctx.repo_root)
                traj.touch_file(move_rel, created=True)
                traj.add_event(NormalizedEvent(
                    type=EventType.FILE_CREATE, timestamp=ts, path=move_rel,
                    summary=f"moved to {move_rel}", metadata={"moved_from": rel},
                ))
        ctx.note_work()

    # -- fallback: raw response_item stream --------------------------
    def _normalize_from_response_items(self, ctx: "_ParseState", traj: NormalizedTrajectory) -> None:
        traj.parse_warnings.append("no item_completed stream; used response_item fallback")
        pending_calls: dict[str, dict] = {}
        for ts, payload in ctx.raw_response_items:
            ptype = payload.get("type")
            if ptype == "message":
                role = payload.get("role")
                text = _join_text(payload.get("content"))
                if role == "user":
                    self._emit_user_message(text, ts, traj, ctx)
                elif role == "assistant":
                    clean, r = redact(text)
                    traj.add_event(NormalizedEvent(
                        type=EventType.ASSISTANT_MESSAGE, timestamp=ts,
                        summary=summarize(clean), redacted=r,
                    ))
                    ctx.note_work()
            elif ptype in ("function_call", "custom_tool_call", "local_shell_call"):
                name = payload.get("name") or ptype
                raw_input = payload.get("input") or payload.get("arguments") or ""
                call_id = payload.get("call_id") or payload.get("id")
                command = _command_from_freeform(raw_input)
                if command:
                    kind = classify_command(command, None)
                    clean_cmd, r = redact(command)
                    ev = traj.add_event(NormalizedEvent(
                        type=EventType.COMMAND, timestamp=ts,
                        command=bound_text(clean_cmd, DEFAULT_COMMAND_LIMIT), redacted=r,
                        metadata={"kind": str(kind), "tool": name},
                    ))
                    if call_id:
                        pending_calls[call_id] = {"kind": kind, "event": ev}
                else:
                    traj.add_event(NormalizedEvent(
                        type=EventType.TOOL_CALL, timestamp=ts, tool_name=name,
                        summary=summarize(redact(str(raw_input))[0]),
                    ))
                ctx.note_work()
            elif ptype in ("function_call_output", "custom_tool_call_output"):
                call_id = payload.get("call_id")
                info = pending_calls.pop(call_id, None)
                exit_code = _exit_from_output(payload.get("output"))
                if info and exit_code is not None:
                    info["event"].exit_code = exit_code
                    outcome = command_event_family(info["kind"], exit_code)
                    if outcome is not None:
                        traj.add_event(NormalizedEvent(
                            type=outcome, timestamp=ts,
                            summary=f"{info['kind']} exit {exit_code}",
                        ))
                    ctx.register_verification(info["kind"], exit_code)
            elif ptype == "reasoning":
                pass

    # -- finalization ------------------------------------------------
    def _finalize(
        self, traj: NormalizedTrajectory, ctx: "_ParseState", session: DiscoveredSession
    ) -> None:
        traj.cwd = traj.cwd or session.cwd or ctx.cwd_hint
        traj.git_remote = traj.git_remote or session.git_remote
        traj.git_branch = traj.git_branch or session.git_branch
        if ctx.token_usage:
            traj.token_usage = ctx.token_usage

        status, reason = infer_final_status(
            traj.events,
            aborted=ctx.real_abort,
            explicit_completion=ctx.explicit_completion,
            completion_message=ctx.completion_message,
        )
        traj.final_status = status
        traj.final_status_reason = reason

        if traj.task is None:
            traj.task_source = TaskSource.NONE

        traj.finalize()


class _ParseState:
    def __init__(self, repo_root: str | None):
        self.repo_root = repo_root
        self.cwd_hint: str | None = None
        self.token_usage: dict[str, Any] | None = None
        self.explicit_completion = False
        self.completion_message: str | None = None
        self.last_terminal: str | None = None
        self.real_abort = False
        self.saw_assistant_activity = False
        self.work_since_last_prompt = 0
        self.raw_response_items: list[tuple[str | None, dict]] = []
        self.verifications: list[tuple[str, int | None]] = []

    def note_work(self) -> None:
        self.saw_assistant_activity = True
        self.work_since_last_prompt += 1

    def register_verification(self, kind: CommandKind, exit_code: Any) -> None:
        if kind in (CommandKind.TEST, CommandKind.LINT, CommandKind.BUILD):
            self.verifications.append((str(kind), exit_code if isinstance(exit_code, int) else None))


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _join_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            if isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif block.get("type") == "encrypted_content":
                parts.append("[encrypted]")
    return "\n".join(p for p in parts if p)


def _extract_command(argv: Any) -> str | None:
    if isinstance(argv, str):
        return argv
    if not isinstance(argv, list) or not argv:
        return None
    if len(argv) >= 3 and any(str(argv[0]).endswith(s) for s in ("sh", "bash", "zsh")):
        if str(argv[1]) in ("-lc", "-c", "-ic"):
            return str(argv[2])
    return " ".join(str(a) for a in argv)


def _command_from_freeform(raw: Any) -> str | None:
    """Pull a shell command out of a tool ``input``/``arguments`` blob."""

    if isinstance(raw, dict):
        for key in ("command", "cmd", "script", "shell"):
            val = raw.get(key)
            if isinstance(val, str) and val.strip():
                return val
            if isinstance(val, list) and val:
                return _extract_command(val)
        return None
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("{"):
            try:
                import json

                return _command_from_freeform(json.loads(text))
            except (ValueError, TypeError):
                return None
        # e.g. tools.exec_command({cmd:"..."}) -- grab a quoted cmd:
        m = re.search(r'(?:cmd|command)\s*:\s*"([^"]+)"', text)
        if m:
            return m.group(1)
        return text if len(text) < DEFAULT_COMMAND_LIMIT else None
    return None


def _exit_from_output(output: Any) -> int | None:
    text = ""
    if isinstance(output, str):
        text = output
    elif isinstance(output, list):
        text = " ".join(
            b.get("text", "") for b in output if isinstance(b, dict)
        )
    elif isinstance(output, dict):
        if isinstance(output.get("exit_code"), int):
            return output["exit_code"]
        text = str(output.get("output") or "")
    m = re.search(r"exit(?:\s+code)?[:\s]+(\d{1,3})", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    if "Script completed" in text or "succeeded" in text.lower():
        return 0
    return None


def _output_tail(item: dict, limit: int = 400) -> str | None:
    for key in ("formatted_output", "aggregated_output", "stdout", "stderr"):
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()[-limit:]
    return None


def _duration_ms(duration: Any) -> int | None:
    if isinstance(duration, (int, float)):
        return int(duration)
    if isinstance(duration, dict):
        if "secs" in duration:
            return int(duration.get("secs", 0) * 1000 + duration.get("nanos", 0) / 1e6)
    return None


_DIFF_ADD = re.compile(r"^\+(?!\+\+)", re.MULTILINE)
_DIFF_DEL = re.compile(r"^-(?!--)", re.MULTILINE)


def _diff_stats(unified_diff: Any) -> tuple[int, int]:
    if not isinstance(unified_diff, str):
        return 0, 0
    return len(_DIFF_ADD.findall(unified_diff)), len(_DIFF_DEL.findall(unified_diff))
