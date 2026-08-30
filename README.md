# keep-migrations

Owns the Keep database schema. Ships as a **container image, not a library** —
nothing imports this package.

It runs as an Argo **PreSync hook Job**: once per release, before any pod of the
new ReplicaSet exists. A failure stops the sync and leaves the running pods
untouched, instead of the previous behaviour where all ~14 gateway replicas ran
`alembic upgrade head` at startup and a bad migration became a CrashLoopBackOff
across the Deployment.

```
Argo sync
  ├─ PreSync   → this image        ← non-zero here = sync stops, nothing deployed
  ├─ Sync      → Deployment updated, pods roll
  └─ PostSync
```

## Layout

```
alembic.ini
migrations/
  env.py                # imports NO models -- see below
  versions/             # 123 revisions
keep_migrations/
  runtime.py            # engine, alembic config, advisory lock, stamped revision
  cli.py                # the entrypoint
```

## Usage

```bash
keep-migrate --target head            # converge (the Job's default)
keep-migrate --sql   --target head    # print the SQL, touch nothing
keep-migrate --check --target head    # exit non-zero if the path is destructive
```

Direction is **derived, never commanded**: the target is compared to the revision
stamped in `alembic_version` and the walk goes whichever way the graph says. The
default target is `head` and nothing exists past head, so a normal release can
never downgrade. A run with nothing pending exits 0 without invoking alembic —
which matters because the PreSync hook fires on every sync, not every release.

Downgrades require `--allow-destructive`, and `--check` refuses two shapes: a
path that emits `DROP TABLE`/`DROP COLUMN`, and one whose output is *only*
`UPDATE alembic_version` (a no-op downgrade — 17 of 122 revisions have an empty
`downgrade()`, so they report success while changing nothing).

Exit codes: `0` converged, `1` refused, `2` failed.

## Two rules for writing migrations here

**1. Hand-written only. `--autogenerate` is not used and not supported.**
`env.py` sets no `target_metadata` and imports no models, so alembic refuses the
flag outright. That is deliberate: the last generated revision was 2026-05-12
(`e1932c411f61`) and every one since was written by hand, while the gateway's
import list had silently drifted to 18 modules against 44 declared tables —
leaving five tables that a stray `--autogenerate` would have emitted
`op.drop_table()` for.

Drift between the models and the schema is caught instead by
keep-api-gateway's `schema_drift()`, which runs in CI against a scratch database
with every revision applied, and behind `/readyz` against the live one.

**2. A revision must never import a live model.** Declare the tables it touches
locally with `sa.Table(...)` and inline any constants. A migration is a
statement about the schema *as it stood when the migration was written*; import
a model and a later column rename or default change silently rewrites what an
old revision does to a fresh database. Three revisions used to break this rule
and were frozen when this repo was split out.

## Relationship to keep-api-gateway

The split is clean in both directions. The gateway ships **no** `alembic.ini`,
no `versions/`, no alembic dependency and no code path that migrates — there is
no escape hatch to unset, and `SKIP_DB_CREATION` does nothing there. This repo
ships **no** models, and should not grow any: if a dependency here starts to
look like the gateway's, something has been imported that should not have been.

A revision that does not build what the gateway's models declare is caught at
deploy time, by the gateway's `/readyz`: it asks whether the live database
contains every table and column that image declares, and refuses to start a pod
that is missing one. It needs no revision string and no script, which is what let
the scripts leave this repo's sibling in the first place. Extra tables and
columns are ignored, so an older image rolled back onto a newer schema starts
cleanly.

Deliberately no CI job compares the two repos. The check runs where it matters --
against the real database, on the way into an environment -- and integ gets the
release before prod does.

## Tests

`pytest tests` -- the revision tests, the CLI, and the advisory lock. One of them,
`test_script_head_is_readable_from_the_shipped_migrations`, asserts there is
exactly **one** alembic head: two heads make `upgrade head` undefined, which is
what took out every gateway pod after the multi-tenant merge. If your image build
runs the tests, that outage cannot ship again; if it does not, run `alembic heads`
before merging a revision.
