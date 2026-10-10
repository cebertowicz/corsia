"""CORSIA's own backend log records, captured in-process for conductors.

``capture`` copies loguru and stdlib records onto a bounded queue, ``writer``
drains it into ``store`` (a capped SQLite file separate from ``corsia.db``),
and ``lifecycle`` starts and stops the pipeline. ``tools.log_read`` is the one
reader. ``backend.log`` is unchanged and is not read here.

Modules in this package never log through loguru or stdlib ``logging``: the
capture sinks exclude this package by name, and a pipeline failure writes one
plain stderr line instead.
"""
