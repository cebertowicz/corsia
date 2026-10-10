"""One scope in which the canonical account-fact readers answer the same fact once.

A discovery commit projects one account's change onto every thread the account
can run, and each of those threads re-reads the same project policy, account row
and model row through the same canonical readers. This module gives that
projection one place to say "read it once": inside ``fact_reads()`` a wrapped
reader answers with its first read of that fact, and outside the scope every
reader behaves exactly as it did before.

A canonical reader takes ``@fact_memo.memoized_reader`` and keeps its body,
signature and docstring. Inside a scope the decorator mechanically keys its answer by
the reader's module and qualified name, the identity of its non-``None`` connection,
and every other bound argument (keyword names sorted; lists converted to tuples).
An unhashable argument fails loudly inside the scope; a missing or ``None`` connection
bypasses it, so a reader that opens its own connection cannot cache a stale snapshot.

Only facts that do not vary by thread are wrapped: project policy, project
existence, account rows, the account an identity resolves to, and a model's
vendor and catalog row. A thread's own binding, project scope, pending account
and queued head are never memoized -- the projection must see each thread's own
state, and those reads stay per thread.

The scope is opened inside the writer transaction that owns the projection, and
nothing inside it writes a row a wrapped reader returns: the projection writes
thread holds, queued-input outcomes and notices. ``test_account_hold_projection``
pins both: it digests every answer the scope stores, asserts the digests are
unchanged when the projection returns, and re-reads every wrapped fact after it to
prove the scope never served a stale answer.
"""
from __future__ import annotations

import functools
import inspect
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, TypeVar

_T = TypeVar("_T")

#: The open scope's answers, or None outside one.
_answers: ContextVar[dict[tuple, Any] | None] = ContextVar("corsia_fact_reads", default=None)


@contextmanager
def fact_reads():
    """Answer wrapped readers from their first read until this block exits."""
    token = _answers.set({})
    try:
        yield
    finally:
        _answers.reset(token)


def memoized(key: tuple, read: Callable[[], _T]) -> _T:
    """This scope's answer for ``key``, read once. Outside a scope: ``read()``.

    The answer is shared with every caller in the scope, so a caller that changes
    it in place would change it for the rest of the projection. Copy before
    mutating -- ``connections.account_evidence.discovery_record`` is the pattern --
    and leave the read-only facts alone; the invariant test digests every stored
    answer around a whole projection and fails on any in-scope mutation.
    """
    scope = _answers.get()
    if scope is None:
        return read()
    if key not in scope:
        scope[key] = read()
    return scope[key]


def memoized_reader(reader: Callable[..., _T]) -> Callable[..., _T]:
    """Wrap a canonical reader with a complete mechanical key inside a scope.

    Facts whose input is itself a dict belong to their caller, rather than this
    shared read memo: a dict (or any other unhashable argument) raises here so a
    future caller cannot silently omit it from the cache key.
    """
    signature = inspect.signature(reader)
    parameters = signature.parameters
    plain = all(item.kind in (item.POSITIONAL_OR_KEYWORD, item.KEYWORD_ONLY) for item in parameters.values())
    positional = [name for name, item in parameters.items() if item.kind is item.POSITIONAL_OR_KEYWORD]
    defaults = {name: item.default for name, item in parameters.items() if item.default is not item.empty}
    keyed = sorted(name for name in parameters if name != "db")

    def arguments_of(args, kwargs) -> dict[str, Any]:
        """Every parameter's value, as ``Signature.bind`` plus ``apply_defaults`` gives them.

        A plain signature is matched by name without ``bind``'s per-call cost; any call the
        fast match cannot place exactly (extra, repeated or missing arguments) goes to
        ``bind``, which owns the error.
        """
        if plain and len(args) <= len(positional):
            arguments = dict(zip(positional, args))
            if not kwargs or all(name in parameters and name not in arguments for name in kwargs):
                arguments.update(kwargs)
                for name, value in defaults.items():
                    arguments.setdefault(name, value)
                if len(arguments) == len(parameters):
                    return arguments
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        return bound.arguments

    @functools.wraps(reader)
    def wrapper(*args, **kwargs) -> _T:
        arguments = arguments_of(args, kwargs)
        db = arguments.get("db")
        if db is None:
            return reader(*args, **kwargs)
        scope = _answers.get()
        if scope is None:
            # The answer cannot be stored, so its other arguments need no key here.
            return memoized((reader.__module__, reader.__qualname__, id(db)), lambda: reader(*args, **kwargs))
        key = (reader.__module__, reader.__qualname__, id(db),
               tuple((name, _normalise(arguments[name])) for name in keyed))
        return memoized(key, lambda: reader(*args, **kwargs))

    return wrapper


def _normalise(value: Any) -> Any:
    """Turn lists into tuples; make every other unhashable reader input fail loudly."""
    if isinstance(value, list):
        return tuple(_normalise(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_normalise(item) for item in value)
    try:
        hash(value)
    except TypeError as error:
        raise TypeError("memoized reader arguments must be hashable inside fact_reads()") from error
    return value


def in_scope() -> bool:
    """Whether a read would be served from the scope, for the tests that bound it."""
    return _answers.get() is not None
