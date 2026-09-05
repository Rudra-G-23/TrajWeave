from __future__ import annotations

from trajweave.adapters.base import BaseAdapter, iter_jsonl
from trajweave.adapters.claude import ClaudeAdapter
from trajweave.adapters.codex import CodexAdapter

#: Registry of adapters keyed by agent name.
ADAPTERS: dict[str, type[BaseAdapter]] = {
    CodexAdapter.agent_name: CodexAdapter,
    ClaudeAdapter.agent_name: ClaudeAdapter,
}

__all__ = ["BaseAdapter", "iter_jsonl", "CodexAdapter", "ClaudeAdapter", "ADAPTERS"]
