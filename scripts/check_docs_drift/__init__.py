"""Documentation drift checks for release promotions (issue #1888).

Two independent checks validate cited commands/routes/paths/versions against the
current tree and scan publicly-shipped files for private names/paths. ``cli.py``
wires them together; see ``scripts/check-docs-drift`` for the entry point.
"""
