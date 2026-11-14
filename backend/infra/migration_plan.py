"""Pure version-ladder planning, separate from migration execution and backups."""


class MigrationError(RuntimeError):
    """A migration refused or failed; the database is unchanged."""


def plan_steps(from_version, to_version, migrations):
    """Return ordered source versions, refusing downgrade or a missing step."""
    if from_version > to_version:
        raise MigrationError(
            f"database schema version {from_version} is newer than this build's"
            f" {to_version}; a newer CORSIA wrote it, so this build must not"
            " downgrade it. Deploy the newer build instead."
        )
    steps = []
    version = from_version
    while version < to_version:
        if version not in migrations:
            raise MigrationError(
                f"no migration registered from schema version {version};"
                f" cannot reach {to_version} from {from_version}."
                " The database was not modified."
            )
        steps.append(version)
        version += 1
    return steps
