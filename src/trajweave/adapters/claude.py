"""Claude Code session adapter.

Discovered format: one JSONL file per session at
``~/.claude/projects/<slugified-cwd>/<session-uuid>.jsonl``. Lines are mixed
record types; the ones that matter for a trajectory:

* ``user``      - ``message.role == "user"``; either a typed human prompt
                  (``message.content`` is a string / text blocks, ``origin.kind
                  == "human"``) or tool results (``content`` is ``tool_result``
                  blocks, with a richer ``toolUseResult`` sibling field).
* ``assistant`` - ``message.content`` blocks: ``thinking`` / ``text`` /
                  ``tool_use``. ``message.model`` + ``message.usage`` carried here.
* ``ai-title`` / ``last-prompt`` - fallbacks for the task string.
* ``system``    - ``compact_boundary`` etc.

Tool calls and their results live on *different* records, so we buffer a pending
tool call on ``tool_use`` and finalize it (into file_*/command/tool_call events)
when its ``tool_result`` arrives.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

from trajweave.adapters.base import BaseAdapter, iter_jsonl
from trajweave.models.enums import Agent, EventType, TaskSource
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

log = get_logger("adapters.claude")

_EDIT_TOOLS = {"Edit", "MultiEdit", "NotebookEdit"}
_SEARCH_TOOLS = {"Grep", "Glob", "LS"}
_PEEK_LINES = 60


class ClaudeAdapter(BaseAdapter):
    agent = Agent.CLAUDE
    agent_name = "claude"
    root_env_var = "TRAJWEAVE_CLAUDE_ROOT"

    @classmethod
    def default_root(cls) -> Path:
        return Path.home() / ".claude" / "projects"

    # ------------------------------------------------------------------
    # discovery
    # ------------------------------------------------------------------
    def discover(self) -> Iterator[DiscoveredSession]:
        if not self.root.is_dir():
            return
        # Top-level session transcripts plus the per-session sub-agent
        # transcripts newer Claude Code writes under <session>/subagents/.
        seen: set[Path] = set()
        patterns = ("*/*.jsonl", "*/*/subagents/*.jsonl")
        paths = sorted(
            {p for pat in patterns for p in self.root.glob(pat)}
        )
        for path in paths:
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            try:
                stat = path.stat()
            except OSError:
                continue
            meta = self._peek(path)
            is_subagent = path.parent.name == "subagents"
            # Sub-agent files carry the PARENT sessionId, so key them by filename.
            session_id = path.stem if is_subagent else (meta.get("session_id") or path.stem)
            yield DiscoveredSession(
                agent=self.agent,
                source_session_id=session_id,
                path=path,
                mtime=stat.st_mtime,
                size_bytes=stat.st_size,
                cwd=meta.get("cwd"),
                git_branch=meta.get("git_branch"),
            )

    def _peek(self, path: Path) -> dict[str, Any]:
        out: dict[str, Any] = {}
        try:
            for i, (_, obj, _) in enumerate(iter_jsonl(path)):
                if i > _PEEK_LINES:
                    break
                if not obj:
                    continue
                if not out.get("session_id"):
                    out["session_id"] = obj.get("sessionId") or obj.get("session_id")
                if not out.get("cwd") and obj.get("cwd"):
                    out["cwd"] = obj.get("cwd")
                if not out.get("git_branch") and obj.get("gitBranch"):
                    out["git_branch"] = obj.get("gitBranch")
                if out.get("cwd") and out.get("session_id"):
                    break
        except OSError:
            pass
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
        ctx = _ClaudeState(repo_root=repo_root)
        ctx.is_subagent = path.parent.name == "subagents"

        for lineno, obj, err in iter_jsonl(path):
            if err:
                traj.parse_warnings.append(err)
                continue
            assert obj is not None
            try:
                self._handle_record(obj, traj, ctx)
            except Exception as exc:  # pragma: no cover - defensive
                traj.parse_warnings.append(f"line {lineno}: {type(exc).__name__}: {exc}")

        self._flush_pending(traj, ctx)
        self._finalize(traj, ctx, session)
        return traj

    def _handle_record(self, obj: dict, traj: NormalizedTrajectory, ctx: "_ClaudeState") -> None:
        rtype = obj.get("type")
        ts = to_iso(obj.get("timestamp"))

        if obj.get("cwd") and not traj.cwd:
            traj.cwd = obj["cwd"]
        if obj.get("gitBranch") and not traj.git_branch:
            traj.git_branch = obj["gitBranch"]
        if obj.get("version"):
            ctx.version = obj["version"]

        if rtype == "user":
            self._handle_user(obj, ts, traj, ctx)
        elif rtype == "assistant":
            self._handle_assistant(obj, ts, traj, ctx)
        elif rtype == "ai-title":
            ctx.ai_title = ctx.ai_title or obj.get("aiTitle")
        elif rtype == "last-prompt":
            ctx.last_prompt = obj.get("lastPrompt") or ctx.last_prompt
        elif rtype == "custom-title":
            ctx.custom_title = obj.get("customTitle")
        elif rtype == "system":
            self._handle_system(obj, ts, traj, ctx)
        # mode / permission-mode / bridge-session / atis-latch / attachment /
        # file-history-* / queue-operation / cost-state / agent-* : ignored.

        if obj.get("isApiErrorMessage"):
            traj.add_event(NormalizedEvent(
                type=EventType.ERROR, timestamp=ts, summary="API error message",
                metadata={"sidechain": bool(obj.get("isSidechain"))},
            ))
        for flag in ("interruptedByShutdown", "isAbortedMidStream"):
            if obj.get(flag):
                ctx.aborted = True

    # -- user records -------------------------------------------------
    def _handle_user(
        self, obj: dict, ts: str | None, traj: NormalizedTrajectory, ctx: "_ClaudeState"
    ) -> None:
        msg = obj.get("message")
        if not isinstance(msg, dict):
            return
        sidechain = bool(obj.get("isSidechain"))
        content = msg.get("content")

        # tool_result blocks
        if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        ):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    self._resolve_tool_result(block, obj.get("toolUseResult"), ts, traj, ctx)
            return

        text = _join_text(content)
        if obj.get("isMeta"):
            return
        if "[Request interrupted by user" in text:
            ctx.aborted = True
            return  # an interruption marker, not a prompt
        human = strip_injected_context(text)
        if not human:
            return

        origin = obj.get("origin")
        if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
            return  # e.g. origin.kind == "hook" / "slash-command output"

        clean, r = redact(human)
        if traj.task is None and (not sidechain or ctx.is_subagent):
            traj.task = bound_text(clean, DEFAULT_TASK_LIMIT)
            traj.task_source = TaskSource.USER_PROMPT
            etype = EventType.USER_PROMPT
        else:
            etype = (
                EventType.HUMAN_CORRECTION
                if ctx.saw_assistant_activity and not sidechain
                else EventType.USER_PROMPT
            )
        traj.add_event(NormalizedEvent(
            type=etype, timestamp=ts, summary=summarize(clean), redacted=r,
            metadata={"sidechain": True} if sidechain else {},
        ))
        ctx.saw_assistant_activity = False

    # -- assistant records ------------------------------------------
    def _handle_assistant(
        self, obj: dict, ts: str | None, traj: NormalizedTrajectory, ctx: "_ClaudeState"
    ) -> None:
        msg = obj.get("message")
        if not isinstance(msg, dict):
            return
        sidechain = bool(obj.get("isSidechain"))
        if msg.get("model"):
            ctx.model = msg["model"]
        _accumulate_usage(ctx, msg.get("usage"))

        stop_reason = msg.get("stop_reason")
        content = msg.get("content")
        if not isinstance(content, list):
            return

        last_text = None
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "thinking":
                continue
            if btype == "text":
                txt = block.get("text") or ""
                if not txt.strip():
                    continue
                clean, r = redact(txt)
                last_text = clean
                traj.add_event(NormalizedEvent(
                    type=EventType.ASSISTANT_MESSAGE, timestamp=ts,
                    summary=summarize(clean), redacted=r,
                    metadata={"sidechain": True} if sidechain else {},
                ))
                ctx.saw_assistant_activity = True
            elif btype == "tool_use":
                ctx.pending[block.get("id")] = {
                    "name": block.get("name"),
                    "input": block.get("input") or {},
                    "ts": ts,
                    "sidechain": sidechain,
                }
                ctx.saw_assistant_activity = True

        if (not sidechain or ctx.is_subagent) and stop_reason in ("end_turn", "stop_sequence") and last_text:
            ctx.explicit_completion = True
            ctx.completion_message = last_text
            ctx.aborted = False  # a clean turn after an earlier interruption = recovered
        elif stop_reason == "aborted":
            ctx.aborted = True

    def _handle_system(
        self, obj: dict, ts: str | None, traj: NormalizedTrajectory, ctx: "_ClaudeState"
    ) -> None:
        subtype = obj.get("subtype")
        if subtype == "compact_boundary":
            traj.add_event(NormalizedEvent(
                type=EventType.UNKNOWN, timestamp=ts, summary="context compacted",
                metadata={"system_subtype": subtype},
            ))

    # -- tool call resolution -------------------------------------
    def _resolve_tool_result(
        self,
        block: dict,
        tool_use_result: Any,
        ts: str | None,
        traj: NormalizedTrajectory,
        ctx: "_ClaudeState",
    ) -> None:
        call = ctx.pending.pop(block.get("tool_use_id"), None)
        name = (call or {}).get("name") or "unknown"
        tinput = (call or {}).get("input") or {}
        sidechain = (call or {}).get("sidechain", False)
        is_error = bool(block.get("is_error"))
        meta_extra = {"sidechain": True} if sidechain else {}
        tur = tool_use_result if isinstance(tool_use_result, dict) else {}

        if name == "Read":
            rel = relativize(tinput.get("file_path"), ctx.repo_root)
            traj.touch_file(rel, read=True)
            traj.add_event(NormalizedEvent(
                type=EventType.FILE_READ, timestamp=ts, path=rel,
                summary=f"read {rel}" if rel else "read",
                metadata={**meta_extra, "error": is_error or None},
            ))
            return

        if name in _EDIT_TOOLS:
            rel = relativize(tinput.get("file_path") or tinput.get("notebook_path"), ctx.repo_root)
            added, removed = _structured_patch_stats(tur.get("structuredPatch"))
            traj.touch_file(rel, modified=not is_error)
            traj.add_event(NormalizedEvent(
                type=EventType.FILE_EDIT, timestamp=ts, path=rel,
                summary=f"edit {rel} (+{added}/-{removed})" if rel else "edit",
                redacted=False,
                metadata={**meta_extra, "lines_added": added, "lines_removed": removed,
                          "error": is_error or None},
            ))
            return

        if name == "Write":
            rel = relativize(tinput.get("file_path"), ctx.repo_root)
            created = str(tur.get("type") or "").lower() != "update"
            traj.touch_file(rel, created=created and not is_error, modified=(not created) and not is_error)
            traj.add_event(NormalizedEvent(
                type=EventType.FILE_CREATE if created else EventType.FILE_EDIT,
                timestamp=ts, path=rel,
                summary=f"{'create' if created else 'update'} {rel}" if rel else "write",
                metadata={**meta_extra, "error": is_error or None},
            ))
            return

        if name == "Bash":
            self._emit_bash(tinput, tur, block, ts, traj, ctx, meta_extra)
            return

        if name in _SEARCH_TOOLS:
            traj.add_event(NormalizedEvent(
                type=EventType.TOOL_CALL, timestamp=ts, tool_name=name,
                summary=summarize(redact(str(tinput.get("pattern") or tinput.get("path") or ""))[0]),
                metadata={**meta_extra, "kind": "search"},
            ))
            return

        # Generic tool call (Task/Agent, WebFetch, Skill, mcp__*, TodoWrite, ...)
        summary = None
        if name in ("Task", "Agent"):
            summary = summarize(redact(str(tinput.get("description") or tinput.get("prompt") or ""))[0])
        elif name == "Skill":
            summary = f"skill: {tinput.get('command') or tinput.get('skill') or ''}".strip()
        elif name == "TodoWrite":
            todos = tinput.get("todos")
            summary = f"{len(todos)} todos" if isinstance(todos, list) else "todos"
        elif name == "unknown":
            summary = "tool call (name not recorded)"
        traj.add_event(NormalizedEvent(
            type=EventType.TOOL_CALL, timestamp=ts,
            tool_name=None if name == "unknown" else name, summary=summary,
            metadata={**meta_extra, "error": is_error or None},
        ))

    def _emit_bash(
        self,
        tinput: dict,
        tur: dict,
        block: dict,
        ts: str | None,
        traj: NormalizedTrajectory,
        ctx: "_ClaudeState",
        meta_extra: dict,
    ) -> None:
        command = tinput.get("command") or ""
        is_error = bool(block.get("is_error"))
        interrupted = bool(tur.get("interrupted"))
        # Claude does not persist a numeric exit code; derive a coarse one.
        exit_code: int | None
        if interrupted:
            exit_code = None
        elif is_error:
            exit_code = 1
        else:
            exit_code = 0

        kind = classify_command(command, None)
        clean_cmd, r1 = redact(command)
        tail = _bash_tail(block, tur)
        clean_tail, r2 = redact(tail) if tail else (None, False)

        traj.add_event(NormalizedEvent(
            type=EventType.COMMAND, timestamp=ts,
            command=bound_text(clean_cmd, DEFAULT_COMMAND_LIMIT),
            exit_code=exit_code,
            summary=summarize(clean_tail) if clean_tail else summarize(redact(str(tinput.get("description") or ""))[0]),
            redacted=r1 or r2,
            metadata={
                **meta_extra,
                "kind": str(kind),
                "interrupted": interrupted or None,
                "description": bound_text(tinput.get("description"), 200),
            },
        ))
        ctx.saw_assistant_activity = True

        outcome = command_event_family(kind, exit_code)
        if outcome is not None:
            traj.add_event(NormalizedEvent(
                type=outcome, timestamp=ts, summary=f"{kind} exit {exit_code}",
                metadata={"command_kind": str(kind), "exit_code": exit_code},
            ))

    def _flush_pending(self, traj: NormalizedTrajectory, ctx: "_ClaudeState") -> None:
        for call in ctx.pending.values():
            name = call.get("name") or "unknown"
            traj.add_event(NormalizedEvent(
                type=EventType.TOOL_CALL, timestamp=call.get("ts"), tool_name=name,
                summary="tool call without recorded result",
                metadata={"unresolved": True},
            ))
        ctx.pending.clear()

    # -- finalization ----------------------------------------------
    def _finalize(
        self, traj: NormalizedTrajectory, ctx: "_ClaudeState", session: DiscoveredSession
    ) -> None:
        traj.cwd = traj.cwd or session.cwd
        traj.git_branch = traj.git_branch or session.git_branch
        traj.model = ctx.model
        traj.cli_version = ctx.version
        if ctx.usage:
            traj.token_usage = ctx.usage

        if traj.task is None:
            if ctx.ai_title:
                traj.task = bound_text(ctx.ai_title, DEFAULT_TASK_LIMIT)
                traj.task_source = TaskSource.AGENT_TITLE
            elif ctx.last_prompt:
                clean, _ = redact(ctx.last_prompt)
                traj.task = bound_text(clean, DEFAULT_TASK_LIMIT)
                traj.task_source = TaskSource.LAST_PROMPT

        status, reason = infer_final_status(
            traj.events,
            aborted=ctx.aborted,
            explicit_completion=ctx.explicit_completion,
            completion_message=ctx.completion_message,
        )
        traj.final_status = status
        traj.final_status_reason = reason
        traj.finalize()


class _ClaudeState:
    def __init__(self, repo_root: str | None):
        self.repo_root = repo_root
        self.is_subagent = False
        self.pending: dict[str, dict] = {}
        self.model: str | None = None
        self.version: str | None = None
        self.usage: dict[str, int] = {}
        self.ai_title: str | None = None
        self.last_prompt: str | None = None
        self.custom_title: str | None = None
        self.explicit_completion = False
        self.completion_message: str | None = None
        self.aborted = False
        self.saw_assistant_activity = False


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
            if block.get("type") in (None, "text") and isinstance(block.get("text"), str):
                parts.append(block["text"])
    return "\n".join(p for p in parts if p)


def _accumulate_usage(ctx: "_ClaudeState", usage: Any) -> None:
    if not isinstance(usage, dict):
        return
    for key in (
        "input_tokens", "output_tokens",
        "cache_creation_input_tokens", "cache_read_input_tokens",
    ):
        val = usage.get(key)
        if isinstance(val, int):
            ctx.usage[key] = ctx.usage.get(key, 0) + val


def _bash_tail(block: dict, tur: dict, limit: int = 400) -> str | None:
    for source in (tur.get("stdout"), tur.get("stderr")):
        if isinstance(source, str) and source.strip():
            return source.strip()[-limit:]
    content = block.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()[-limit:]
    if isinstance(content, list):
        joined = " ".join(
            b.get("text", "") for b in content if isinstance(b, dict)
        ).strip()
        if joined:
            return joined[-limit:]
    return None


def _structured_patch_stats(patch: Any) -> tuple[int, int]:
    if not isinstance(patch, list):
        return 0, 0
    added = removed = 0
    for hunk in patch:
        if not isinstance(hunk, dict):
            continue
        for line in hunk.get("lines", []) or []:
            if isinstance(line, str):
                if line.startswith("+"):
                    added += 1
                elif line.startswith("-"):
                    removed += 1
    return added, removed
