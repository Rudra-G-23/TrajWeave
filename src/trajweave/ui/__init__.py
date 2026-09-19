"""Local trajectory explorer with explicit Stage 7 review actions.

``trajweave ui`` starts a stdlib HTTP server (no third-party dependency) that
serves a small vanilla-JS single-page app plus a read-only JSON API over the
existing SQLite store. Review mutations use explicit POST actions and Apply is
always separate from Accept.
"""

from __future__ import annotations

from trajweave.ui.server import serve

__all__ = ["serve"]
