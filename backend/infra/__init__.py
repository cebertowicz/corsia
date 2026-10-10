"""Infrastructure layer — database, config, constants, shared mutable state.

Modules in this package must have no imports from ``core.*``, ``jobs.*``,
``api.*``, ``memory.*``, or ``llm.*``. Only infrastructure and external
libraries. This is the base of the dependency graph.
"""

# Re-export the product-events emit surface so callers can write
# ``from infra import emit_product_event`` without reaching into the
# submodule path. Submodule import path also works. Resolved on first use:
# product_events imports infra.config, which needs a configured workspace, and
# first-run setup (``cli init``) imports infra modules before one exists.
__all__ = [
    "emit_product_event",
    "flush",
    "identify_user",
]


def __getattr__(name: str):
    if name in __all__:
        import infra.product_events as product_events

        return getattr(product_events, name)
    raise AttributeError(f"module 'infra' has no attribute {name!r}")
