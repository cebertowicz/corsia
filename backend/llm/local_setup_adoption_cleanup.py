"""Remove obsolete legacy-adoption choice markers after their row is retired."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def cleanup_retired_legacy_setup_adoption() -> None:
    """Remove obsolete consent choices after retirement has removed their row.

    The originals record remains intact: it describes settings CORSIA must not
    restore or overwrite. Missing files are expected after a crash and retry.
    """
    from llm.local_setup_managed import adoption_record_paths

    _originals, consent, use_mine = adoption_record_paths()
    for path in (consent, use_mine):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as error:
            log.warning("could not remove obsolete local oMLX adoption marker %s: %s", path.name, error)
