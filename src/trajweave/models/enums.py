"""Small, stable enumerations for the normalized schema.

Deliberately conservative - a handful of values that both Codex and Claude
sessions can be mapped onto without guessing. Anything that does not fit maps to
``EventType.UNKNOWN`` / ``FinalStatus.UNKNOWN`` and keeps its raw detail in the
event ``metadata`` blob.
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class Agent(StrEnum):
    CODEX = "codex"
    CLAUDE = "claude"


class TaskSource(StrEnum):
    """How the trajectory ``task`` string was obtained."""

    USER_PROMPT = "user_prompt"          # first genuine human prompt in the session
    AGENT_TITLE = "agent_title"          # agent-generated title (Claude ai-title)
    LAST_PROMPT = "last_prompt"          # cached "last prompt" record
    NONE = "none"                        # nothing reliable found


class EventType(StrEnum):
    USER_PROMPT = "user_prompt"
    ASSISTANT_MESSAGE = "assistant_message"
    FILE_READ = "file_read"
    FILE_CREATE = "file_create"
    FILE_EDIT = "file_edit"
    FILE_DELETE = "file_delete"
    COMMAND = "command"
    TOOL_CALL = "tool_call"
    TEST_RUN = "test_run"
    TEST_PASS = "test_pass"
    TEST_FAIL = "test_fail"
    LINT_RUN = "lint_run"
    LINT_PASS = "lint_pass"
    LINT_FAIL = "lint_fail"
    BUILD_RUN = "build_run"
    BUILD_PASS = "build_pass"
    BUILD_FAIL = "build_fail"
    ERROR = "error"
    RETRY = "retry"
    HUMAN_CORRECTION = "human_correction"
    COMPLETION = "completion"
    UNKNOWN = "unknown"


class FinalStatus(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"
    ABORTED = "aborted"
    UNKNOWN = "unknown"


#: Command "kinds" recognised by the normalization layer, used to derive the
#: test_/lint_/build_ event families from a raw shell command.
class CommandKind(StrEnum):
    TEST = "test"
    LINT = "lint"
    BUILD = "build"
    READ = "read"
    SEARCH = "search"
    LIST = "list"
    VCS = "vcs"
    PACKAGE = "package"
    OTHER = "other"
