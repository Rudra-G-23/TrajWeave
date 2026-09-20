"""Pure transforms applied to parsed transcript data before it is stored.

Repo-relative path rewriting, conservative secret redaction, shell-command
classification, injected-context stripping, and evidence-based ``final_status``
inference. Nothing here touches the database or the filesystem.
"""

from __future__ import annotations

from trajweave.normalization.commands import classify_command, command_event_family
from trajweave.normalization.paths import relativize
from trajweave.normalization.redaction import redact, redact_mapping
from trajweave.normalization.status import infer_final_status
from trajweave.normalization.text import bound_text, strip_injected_context, summarize

__all__ = [
    "relativize",
    "redact",
    "redact_mapping",
    "infer_final_status",
    "classify_command",
    "command_event_family",
    "bound_text",
    "summarize",
    "strip_injected_context",
]
