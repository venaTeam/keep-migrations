"""Which way the graph says to walk.

Direction is derived, never commanded: the CLI takes a target, compares it to
the revision stamped in `alembic_version`, and this is the answer. The default
target is `head` and nothing exists past head, so a normal release can only ever
be UPGRADE or CONVERGED.

CONVERGED is the common case, not an edge case: the PreSync hook fires on every
Argo sync -- a ConfigMap edit, a self-heal, a re-sync -- not only on releases
that add migrations. Those runs must exit 0 in seconds without invoking alembic.
"""

from enum import Enum


class Direction(str, Enum):
    """`str` mixin so it logs and formats as its value rather than
    `Direction.UPGRADE`."""

    #: The target is ahead of the database: apply the revisions between them.
    UPGRADE = "upgrade"
    #: The target is behind it: run each crossed `downgrade()`. Requires
    #: `--allow-destructive`, and is never reachable with the default target.
    DOWNGRADE = "downgrade"
    #: Already stamped at the target. Nothing to do; exit 0 without alembic.
    CONVERGED = "converged"

    def __str__(self) -> str:
        return self.value
