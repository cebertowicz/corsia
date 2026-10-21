"""A disposable fork writer's immutable account at each paid harness boundary."""
from __future__ import annotations

from connections.account_operation import admit_operation, bind_operation_session, require_operation


def admit_fork(request):
    """Freeze direct requests once, independently of the source Turn's binding."""
    return admit_operation(request.target.harness, request.target.catalog_id, request)


def bind_fork_session(request, session, *, kind):
    """Carry a fork's frozen writer and kind into the paid transport."""
    return bind_operation_session(request, session, kind=kind.name)


def require_fork_write(request):
    """Check current eligibility without consulting a newer binding/default."""
    return require_operation(request.target.harness, request.target.catalog_id, request)
