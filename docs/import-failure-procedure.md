# OpenNeuro import-failure procedure

How an import or recovery failure for a dataset is detected,
tracked, triaged, and resolved.

## How failures are detected and tracked

The `onboard-openneuro.yml` workflow runs `prepare -> copy -> finalize` per dataset.
The `finalize` step is a hardened publish gate:
it re-lists S3 and verifies every git-annex key is present at its declared size,
and it refuses to publish (`exit 1`) if any object is missing or wrong-size.
So a failed dataset **keeps its real, partial state**;
nothing empty is ever published.

The `report` job records the terminal state two ways:

1. **Backend webhook** (`POST /webhooks/import-state`, `status=failed`) ->
   `import_jobs` in D1 (the machine record; the sweep and retry engine read it).
2. **GitHub issue** on `nemarDatasets/.github`, labeled `import-failure`,
   opened or updated (deduped by title `Import failure: on###### (ds######)`)
   so every failure has a human-visible, triageable record.
   Use the `import-failure` issue template for anything filed by hand.

`datasets.data_complete = 0` is the honest catalog state for an unresolved dataset.

## Failure classes and actions

| Class | Signal | Action |
|---|---|---|
| `git-divergence` | `prepare` fails with `git push` "origin/main has diverging commits / auto-rebase failed" on a re-import onto an already-published repo | Fix the re-import divergence (code), then re-run. Do not force-push over a published repo by hand. |
| `upstream-403` | `copy` shard logs show specific `.nii.gz` objects return `403 Forbidden` from OpenNeuro S3 (HeadObject), curl fallback also blocked | **Unrecoverable** via copy. Leave `data_complete=0` (honest) if a few files, or withdraw (`nemar admin withdraw`) if badly blocked. |
| `shard-gap` | `finalize` reports "N of M objects missing... a shard likely failed or was cancelled", but the objects ARE available upstream | Transient. Re-run `nemar admin recover <id>` (copy resumes, skipping already-present keys). |
| `no-import-row` | `nemar admin import status <id>` returns "No import jobs match" | Imported before the `import_jobs` table (or the row was pruned). Dispatch `nemar admin import-openneuro ds######` (by-number source) to seed a row. May then hit `git-divergence`. |
| `matrix-cap` | A whole batch fails with 0 `copy` jobs instantiated | The `copy` matrix is `datasets x 8 shards`; GitHub caps a matrix at 256 jobs. Keep batches **<= 30 datasets** (240 jobs). Re-dispatch smaller. |

## Triage steps

1. **Confirm the real state** with the authoritative per-key check:
   `nemar admin import verify <on-id>` -> `complete (M/M)` or `incomplete (N/M missing)`.
   Do NOT trust batch `finalize` counts; a mixed batch's aggregate result is coarse.
2. **Classify** using the table above (read the failing `prepare`/`copy` job logs).
3. **Act** per the class; update the issue with what you did.
4. **Close** the issue when `import verify` shows `complete`,
   or when the dataset is consciously accepted-incomplete / withdrawn (say which).

## Re-running safely

- Batches must be **<= 30 datasets** (the 256-job matrix cap).
- `recover` reclassifies (`verify`) then re-dispatches; the copy is **resumable**
  (already-present keys at the declared size are skipped).
- `data-integrity-sweep --older-than 0` does **not** converge
  (every already-checked row stays perpetually re-eligible, so the progress guard bails);
  use the default sweep (`data_checked_at IS NULL`) or a per-id `import verify`.

Refs: epic nemarOrg/nemar-cli#967; `nemarOrg/nemar-cli:.context/recover-runbook.md`.
