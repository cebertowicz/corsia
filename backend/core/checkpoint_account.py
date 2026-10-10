"""The checkpoint writer's immutable account at each paid harness boundary."""
from __future__ import annotations

from connections.account_operation import admit_operation, bind_operation_session, require_operation


def admit_checkpoint(request):
    """Freeze direct requests once, independently of the source Turn's binding."""
    return admit_operation(request.target.harness, request.target.catalog_id, request)


def bind_checkpoint_session(request, session):
    """Carry exactly the request's frozen writer into the shared paid transport."""
    return bind_operation_session(request, session, kind='checkpoint')


def require_checkpoint_write(request):
    """Check current eligibility without consulting a newer binding/default."""
    return require_operation(request.target.harness, request.target.catalog_id, request)
