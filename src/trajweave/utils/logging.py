from __future__ import annotations

import logging
import sys

_CONFIGURED = False
_ROOT_NAME = "trajweave"


def configure_logging(verbose: bool = False, quiet: bool = False) -> None:
    """Configure concise CLI logging. Idempotent."""

    global _CONFIGURED
    level = logging.WARNING
    if verbose:
        level = logging.DEBUG
    elif not quiet:
        level = logging.INFO

    logger = logging.getLogger(_ROOT_NAME)
    logger.setLevel(level)

    if not _CONFIGURED:
        handler = logging.StreamHandler(stream=sys.stderr)
        fmt = "%(message)s" if not verbose else "%(levelname)s %(name)s: %(message)s"
        handler.setFormatter(logging.Formatter(fmt))
        logger.addHandler(handler)
        logger.propagate = False
        _CONFIGURED = True
    else:
        for handler in logger.handlers:
            handler.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    if name == _ROOT_NAME or name.startswith(_ROOT_NAME + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{_ROOT_NAME}.{name}")
