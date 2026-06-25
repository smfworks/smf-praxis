"""Tiny structured-logging helper (stdlib only).

A single namespaced logger (``praxis``) configured once. Level is taken from the
``PRAXIS_LOG`` environment variable (``DEBUG``/``INFO``/``WARNING``/``ERROR``);
default is ``WARNING`` so library use (and the test suite) stays quiet unless the
operator opts in.
"""
from __future__ import annotations

import logging
import os

_CONFIGURED = False


def get_logger(name: str = "praxis") -> logging.Logger:
    global _CONFIGURED
    logger = logging.getLogger(name)
    if not _CONFIGURED:
        level_name = os.environ.get("PRAXIS_LOG", "WARNING").upper()
        level = getattr(logging, level_name, logging.WARNING)
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))
        root = logging.getLogger("praxis")
        root.addHandler(handler)
        root.setLevel(level)
        root.propagate = False
        _CONFIGURED = True
    return logger
