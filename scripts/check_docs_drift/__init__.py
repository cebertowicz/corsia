"""Documentation drift checks for release promotions (issue #1888).

Three independent checks, one module each: starter-kit copies against their
workspace sources, cited commands/routes/paths/versions against the current
tree, and private names/paths in publicly-shipped files. ``cli.py`` wires
them together; see ``scripts/check-docs-drift`` for the entry point.
"""
