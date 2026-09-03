# The migration Job — what to add to the Helm chart

The chart that deploys to OpenShift lives outside this repo, so this is the
paste-ready spec rather than a template. It runs the **keep-migrations image**,
which is the only artifact that contains migration scripts: the gateway image no
longer ships `alembic.ini`, `versions/`, or an alembic dependency, and its pods
have no code path that migrates.

That is why there is no `SKIP_DB_CREATION=true` step here and no ordering
constraint against the gateway's rollout. There is nothing left to switch off.

---

## 1. The files

It follows `templates/workflows.yaml`'s conventions, so it should read as one of
the family: same label set, same `global.keep.namespace`, same
`image.repository`/`tag`/`pullPolicy` shape, the same map-or-list `env` handling,
`resources` with optional `ephemeralStorage`, `serviceAccountName: keep`, empty
`securityContext`, and `terminationMessagePath`/`Policy`.

What a Deployment has and a run-to-completion Job cannot: no `replicas`,
`selector`, `ports`, probes, Service or Route, and `restartPolicy: Never` rather
than `Always`. No volumes either — this container writes nothing.

One deliberate difference from workflows.yaml: `envFrom` is **not** conditional
on `env` being absent. This container's single requirement is
`DATABASE_CONNECTION_STRING` from `secrets[0]`, so making it conditional would
mean adding one env var silently drops the connection string and the Job dies on
connect.


Copy them out of this repo rather than from a snippet here — a second copy in a
markdown file drifts the moment the template changes.

| file | where it goes |
|---|---|
| `chart/templates/migration-job.yaml` | your chart's `templates/` |
| the `migrations:` block in `chart/values.yaml` | your chart's `values.yaml` |

There is deliberately no `Chart.yaml` here -- nothing in this directory is meant
to be installed, and one lying around only invites the question of whether it
should be copied too. To render the template locally, borrow a throwaway one:

```bash
printf 'apiVersion: v2\nname: keep\nversion: 0.0.0\n' > chart/Chart.yaml
helm template rel chart/ --set migrations.enabled=true
rm chart/Chart.yaml
```

The only required value is `migrations.dbSecretName` — the secret already
carrying `DATABASE_CONNECTION_STRING` for the app Deployment. The template fails
to render without it rather than producing a Job that dies on connect.

## 2. What the values control

Three things move independently, which is what makes rollback declarative: the
app image, the migrations image, and the schema target.

```yaml
image:
  tag: "1.5"                                     # the app

migrations:
  image:
    repository: <registry>/keep-migrations       # append-only: every revision ever written
    tag: "2026-09-01"
  target: head                                   # what the schema should be
  allowDestructive: false                        # required for any downgrade
  deadline: 900                                  # activeDeadlineSeconds; raise for prod
  dbSecretName: keep-db
```

The migrations image is **not** rolled back with the app. Because it is
append-only, the current tag already contains every `downgrade()` ever written —
there is no old tag to find or pin.

## 3. Turning it on

`migrations.enabled` defaults to **false**, so merging the template changes
nothing — it renders no Job and the sync behaves exactly as it does today.

Enable it one environment at a time, and **enable it before deploying the new
gateway image**, not after:

| step | state | what it proves |
|---|---|---|
| 1 | template merged, `enabled: false` | nothing rendered, no risk |
| 2 | `enabled: true`, **old** gateway image | the Job runs, finds nothing pending, exits 0. Image pull, db secret, RBAC and PreSync ordering all verified against a real sync, with no schema change |
| 3 | new gateway image | its pods no longer migrate; the Job already is |
| 4 | repeat 2–3 for prep, then prod | |

Step 2 is safe to leave running for as long as you like: the old pods still
migrate at startup, and both they and the Job take advisory lock
`8274419300112233`, so they serialize rather than collide.

Do **not** deploy the new gateway image while this is still false. That image
contains no migration code, so nothing would migrate at all. A release with no
pending revisions survives it; the next one that adds a column leaves the new
pods stuck at 503 on `/readyz`.

## 4. The gateway Deployment — no change

Nothing. Pods contain no migration code. If you find `SKIP_DB_CREATION` set on
the gateway Deployment, it is a leftover and does nothing there; in
keep-event-handler and keep-workflows the same variable skips the schema *wait*,
which is a different thing, so never set it globally.

What the gateway does still do is refuse to serve on a schema its models do not
satisfy: `/readyz` compares the live schema against the tables and columns this
image declares. It reads no revision and no script — that is what let the scripts
leave. Extra tables and columns are ignored, so an older image rolled back onto a
newer schema starts cleanly.

---

## What this buys

```
Argo sync
  ├─ PreSync   → migration Job     ← non-zero here = sync stops, nothing deployed
  ├─ Sync      → Deployment updated, pods roll
  └─ PostSync
```

Argo waits for each phase to be healthy before the next, and for a `Job`
"healthy" means completed successfully — so it genuinely blocks. A failed
migration leaves the Deployment untouched and the old pods serving.

Before this, all ~14 gateway replicas ran `alembic upgrade head` in gunicorn's
`on_starting` on every start — including OOM kills, node drains and HPA
scale-ups — serialized behind an advisory lock, with thirteen of them polling and
burning their startup-probe budget.

## Things worth knowing before you ship it

**The hook fires on every sync, not every release.** A ConfigMap change or an
Argo self-heal triggers it too. That is fine only because a run with nothing
pending is a revision comparison and an exit — `keep-migrate` returns 0 without
invoking alembic when the database is already at the target.

**Expand/contract becomes mandatory.** PreSync means the DDL lands while the
*previous* image is still serving, so every migration must be safe against the
old code.

**A failed sync is silent from the app's side.** No CrashLoop, no pod alerts —
just an unchanged Deployment and an Argo Application sitting in Failed. That is a
soft failure (the old version keeps serving), but nothing pages you, and this is
the one regression against the old behaviour: a failed migration used to page
someone by accident, via the CrashLoopBackOff. **Ship this with an alert** —
`argocd-notifications`' `on-sync-failed` is the simplest, or Prometheus on
`argocd_app_info{sync_status="OutOfSync"}` persisting past N minutes. Name the
rotation that receives it; whoever owns pod alerts today is not necessarily
watching Argo.

**Rolling the app back needs no schema change.** Change `image.tag`, commit. The
Job sees the database already at target and exits 0; pods roll back and `/readyz`
passes because the extra columns are ignored. One commit.

**Rolling the *schema* back is a second commit** — set `migrations.target` to the
older revision and `migrations.allowDestructive: true`. Two commits rather than
one *is* the ordering guarantee: PreSync runs before the Deployment updates, so
moving both at once would drop the schema under pods still running new code.
Downgrade one release, not many — long paths break the single-transaction
guarantee, and most `downgrade()` functions have never executed anywhere.

**After any failed Job, check for invalid indexes.** Four migrations use
`autocommit_block()` for `CREATE INDEX CONCURRENTLY`; those commit independently,
so a mid-build failure leaves an INVALID index and the retry's
`if_not_exists=True` skips rather than rebuilds it — the retry goes green while
the index stays unused.

```sql
SELECT c.relname FROM pg_index i
JOIN pg_class c ON c.oid = i.indexrelid
WHERE NOT i.indisvalid;
```

**There is no escape hatch any more.** Unsetting an env var will not make pods
migrate; the code and the scripts are both gone from that image. If the Job
cannot run, the fallback is running this image by hand against the database.

## Dry runs

Both are safe against a live database — neither writes.

```bash
keep-migrate --sql   --target head    # print the SQL, touch nothing
keep-migrate --check --target head    # exit non-zero if the path is destructive
```
