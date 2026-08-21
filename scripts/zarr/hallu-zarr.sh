#!/usr/bin/env bash
################################################################################
# NEMAR SDSC Hallu Zarr conversion (queue-driven)
#
# Purpose: Build the derived, latest-only Zarr serving copies for NEMAR public
#          datasets ON Hallu (ample compute + a 1 Gbps link, no GitHub Actions
#          120-min cap) and push them to s3://nemar/<id>/zarr/. Epic
#          nemarOrg/nemar-cli#684; the conversion engine replaces the
#          run-generate-zarr.yml Actions path for bulk/backfill.
#
# Design: self-contained, EPHEMERAL, and crash-safe via a persistent SQLite
#         queue (scripts/zarr/zarr_queue.py). Each run:
#           1. reconcile -- enqueue every public dataset whose latest version
#              isn't converted yet, and reset any `inprogress` job left by a
#              crashed/rebooted run back to `pending`.
#           2. drain     -- claim the oldest job, clone the dataset's metadata
#              only (`nemar dataset download --no-data`, seconds), then the
#              biosigIO driver STREAMs each recording's annex blob from
#              s3://nemar/<id>/objects/<key> just-in-time -> convert -> push,
#              JOBS recordings in PARALLEL (ProcessPoolExecutor), onto NVMe
#              scratch; `rm -rf` the scratch copy and mark the job done (or fail
#              w/ backoff). Repeat until the queue is empty (or --limit).
#              Streaming + parallelism start converting immediately, never hold
#              the whole dataset, and saturate Hallu's cores on the CPU-bound
#              resample/compress.
#         flock keeps a single long backfill draining across hourly cron ticks
#         (a later tick finds the lock held and exits). As long as the box is on,
#         the cron fires and the queue resumes exactly where it left off.
#
# Usage:
#   ./hallu-zarr.sh                      # reconcile + drain the queue
#   ./hallu-zarr.sh --limit 20           # cap datasets per run (paced)
#   ./hallu-zarr.sh --dataset nm000132   # one dataset now (bypasses the queue)
#   ./hallu-zarr.sh --stats              # print queue status and exit
#
# Every conversion rebuilds the whole dataset (the driver's --clean) so the
# serving copy mirrors the current dataset. --clean RECONCILES rather than
# erases: each store is synced with --delete and only stores that no longer
# exist in HEAD are removed, after a successful conversion (ADR 0023).
#
# Crontab (sibling of hallu-sync, offset to :30):
#   30 * * * * /path/to/hallu-zarr.sh >> /mnt/local/zarr-state/.nm-zarr-cron.log 2>&1
#
# Prereqs: curl, jq, git, git-annex, nemar CLI, aws, uv, python3 in PATH.
################################################################################

set -uo pipefail

# --- PATH bootstrap (Homebrew/Bun/uv installed under $HOME) -------------------
for p in "$HOME/.local/homebrew/bin" "$HOME/.bun/bin" "$HOME/.local/bin"; do
  [[ -d "$p" ]] && PATH="$p:$PATH"
done
export PATH

# --- Config (environment-overridable) ----------------------------------------
# ZARR_BASE is the SINGLE local drive this pipeline lives on. Both the hot
# per-recording scratch AND the persistent state (queue db, venv, driver clone,
# logs) hang off it, so the whole pipeline touches exactly one filesystem and has
# NO network (NFS) dependency: NFS made every Python import, SQLite lock, and stat
# a network round-trip -- too much tension/traffic on the hot path, and
# SQLite-over-NFS locking is fragile. All state here is rebuildable (venv/clone
# via setup(); the queue via `reconcile`) and the real outputs live in S3, so
# single-drive-local is the right durability trade. Moving to another machine is
# a one-line change here (or set ZARR_BASE in the environment / crontab).
#
# WORK_DIR is the EPHEMERAL per-recording scratch: the driver streams each annex
# blob here and N parallel workers build temp Zarr stores before upload, so it
# MUST be fast local disk. Only this subtree is wiped between recordings; the
# sibling STATE_DIR is never touched by the cleanup.
ZARR_BASE="${ZARR_BASE:-/mnt/local}"
WORK_DIR="${ZARR_WORK_DIR:-${ZARR_BASE}/zarr-scratch}"
STATE_DIR="${ZARR_STATE_DIR:-${ZARR_BASE}/zarr-state}"
# Max parallel workers = the driver's ProcessPoolExecutor CPU cap. Default to all
# cores: the driver's RAM-admission control (nemarDatasets/.github#67) dispatches a
# recording only while the SUM of in-flight projected peaks fits usable RAM, so a
# high worker count adds CPU parallelism WITHOUT OOM risk or shrinking the
# per-recording budget (small EEG packs many-wide; large MEG self-limits). Override
# with ZARR_JOBS.
JOBS="${ZARR_JOBS:-$(nproc 2>/dev/null || echo 8)}"
DRIVER_REPO="${ZARR_DRIVER_REPO:-${STATE_DIR}/dotgithub}"   # clone of nemarDatasets/.github
VENV_DIR="${ZARR_VENV_DIR:-${STATE_DIR}/.zarr-venv}"
# Fallback only (used when the clone predates scripts/zarr/requirements.txt, which
# is the real pin). Floor is 1.2.4, not 1.2.3: 1.2.3 added MEF3 .mefd / 4D-BTi
# import (so a run below that floor would discover a .mefd/BTi recording via
# generate_zarr.py's dir_recording_of/bti_recordings and then fail to convert it),
# and 1.2.4 additionally reads MATLAB v7.3 (HDF5) EEGLAB `.set` and recovers three
# false EDF/BDF rejections. Extras are not optional here: [mef3] carries pymef and
# [hdf5] carries h5py, and without either the matching recordings raise ImportError
# at convert time even though discovery finds them.
BIOSIGIO_SPEC="${BIOSIGIO_SPEC:-biosigio[zarr,meg,mef3,hdf5]>=1.2.4}"
API_BASE="${API_BASE:-https://api.nemar.org}"
CALLBACK_URL="${ZARR_CALLBACK_URL:-${API_BASE}/webhooks/zarr-ready}"
S3_BUCKET="${S3_BUCKET:-nemar}"
AWS_REGION="${AWS_DEFAULT_REGION:-us-east-2}"
# Scoped service profile (IAM user nemar-hallu-zarr; s3:Get/Put/Delete on
# nemar/*/zarr/* + ListBucket). The driver's `aws s3 ...` calls inherit it.
export AWS_PROFILE="${ZARR_AWS_PROFILE:-nemar-zarr}"
export AWS_DEFAULT_REGION="$AWS_REGION"
QUEUE_DB="${ZARR_QUEUE_DB:-${STATE_DIR}/zarr-queue.db}"
LOG_FILE="${ZARR_LOG_FILE:-${STATE_DIR}/.nm-zarr.log}"
LOCK_FILE="${ZARR_LOCK_FILE:-${STATE_DIR}/.nm-zarr.lock}"
# NEMAR_WEBHOOK_TOKEN may be exported by the environment; the callback is skipped
# when it is empty (the viewer reads index.json, not D1, so the callback is only
# D1 bookkeeping).
# Load secrets (e.g. NEMAR_WEBHOOK_TOKEN) from a chmod-600 file beside this
# script, so the token lives neither in crontab nor in any repo.
[[ -f "${BASH_SOURCE%/*}/.zarr-secrets.env" ]] && source "${BASH_SOURCE%/*}/.zarr-secrets.env"
NEMAR_WEBHOOK_TOKEN="${NEMAR_WEBHOOK_TOKEN:-}"

ONLY_DATASET=""
LIMIT="${ZARR_LIMIT:-0}"
STATS_ONLY=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dataset) ONLY_DATASET="$2"; shift 2 ;;
    --limit) LIMIT="$2"; shift 2 ;;
    --stats) STATS_ONLY=1; shift ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*" | tee -a "$LOG_FILE"; }
err() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] ERROR: $*" | tee -a "$LOG_FILE" >&2; }

# git-annex marks object files (and their dirs) read-only, so a plain `rm -rf`
# fails with EPERM. Make the tree writable first, then remove.
safe_rm() { [[ -n "${1:-}" && -e "$1" ]] || return 0; chmod -R u+w "$1" 2>/dev/null; rm -rf "$1"; }

mkdir -p "$WORK_DIR" "$STATE_DIR"
# The driver's tempfile.TemporaryDirectory() (per-recording materialize + store)
# follows TMPDIR; pin it to the NVMe scratch, not the system default.
export TMPDIR="$WORK_DIR"

# --- One-time setup: driver repo + biosigIO venv ------------------------------
setup() {
  if [[ -d "$DRIVER_REPO/.git" ]]; then
    git -C "$DRIVER_REPO" fetch -q origin && git -C "$DRIVER_REPO" reset -q --hard origin/main
  else
    git clone -q https://github.com/nemarDatasets/.github "$DRIVER_REPO"
  fi
  if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    uv venv -q "$VENV_DIR"
  fi
  # Install biosigIO from the driver repo's manifest so the pin is single-sourced
  # with the Actions workflow (scripts/zarr/requirements.txt). Fall back to the
  # inline spec if an older clone predates the manifest.
  #
  # --refresh-package/--upgrade-package biosigio are REQUIRED, not cosmetic: a
  # plain `uv pip install` reuses uv's cached index, so right after a biosigIO
  # release the cache still lists the old version and the pin bump silently no-ops
  # (the venv keeps the stale wheel, and the `|| true` below hides the resolution
  # miss). That shipped a run converting on the OLD library. Forcing a refresh +
  # upgrade of just biosigIO makes a pin bump take effect on the very next run
  # (deps are untouched). Verify afterward and fail loud if the pin is still unmet,
  # rather than silently converting with the wrong version.
  local req="$DRIVER_REPO/scripts/zarr/requirements.txt"
  if [[ -f "$req" ]]; then
    VIRTUAL_ENV="$VENV_DIR" uv pip install -q --refresh-package biosigio --upgrade-package biosigio -r "$req" 2>&1 | tail -2 || true
  else
    VIRTUAL_ENV="$VENV_DIR" uv pip install -q --refresh-package biosigio --upgrade-package biosigio "$BIOSIGIO_SPEC" 2>&1 | tail -2 || true
  fi
  # Guard: the pinned biosigIO must actually be importable, else abort setup so the
  # cron does not convert a whole batch on a stale library.
  if ! VIRTUAL_ENV="$VENV_DIR" "$VENV_DIR/bin/python" -c "import biosigio" 2>/dev/null; then
    echo "[setup] FATAL: biosigio not importable after install ($BIOSIGIO_SPEC)" >&2
    exit 1
  fi
  VIRTUAL_ENV="$VENV_DIR" "$VENV_DIR/bin/python" -c "import biosigio; print(f'[setup] biosigio {biosigio.__version__}')"
}

DRIVER="$DRIVER_REPO/scripts/zarr/generate_zarr.py"
QUEUE="$DRIVER_REPO/scripts/zarr/zarr_queue.py"
qpy() { VIRTUAL_ENV="$VENV_DIR" "$VENV_DIR/bin/python" "$QUEUE" --db "$QUEUE_DB" "$@"; }

# --- Per-dataset: download -> convert -> push -> CLEANUP -----------------------
# Returns 0 on success. The store is on S3; the scratch copy is always deleted.
convert_dataset() {
  local id="$1" version="${2:-}"
  local dir="$WORK_DIR/$id"
  local cb="$WORK_DIR/$id.callback.json"
  # Reset BEFORE any early return so the drain loop never reads an unbound (set -u
  # aborts) or stale value: a clone-failure early-return below must NOT inherit
  # the previous dataset's `deterministic` and get mis-marked terminal. Set from
  # the callback further down on a real conversion run.
  LAST_DETERMINISTIC=false
  log "[$id] start (version=${version:-?})"

  # In-progress signal so the observability dashboard's "Processing" tile reflects
  # live conversions (the cron has no Actions dispatch to set zarr_status=pending;
  # #774). Best-effort: a failed/skipped POST never blocks the conversion -- the
  # terminal ready/failed callback below is the authoritative state.
  if [[ -n "$NEMAR_WEBHOOK_TOKEN" ]]; then
    curl -sS --connect-timeout 10 --max-time 30 -X POST "$CALLBACK_URL" \
      -H "Content-Type: application/json" \
      -H "X-Webhook-Token: ${NEMAR_WEBHOOK_TOKEN}" \
      --data "{\"dataset_id\":\"$id\",\"status\":\"converting\"}" >>"$LOG_FILE" 2>&1 \
      || err "[$id] converting callback failed (non-fatal)"
  fi

  # Metadata-only clone (git history + annex pointers, no content -- seconds, not
  # the whole 18 GB). The driver then STREAMS each recording's annex blob from
  # s3://nemar/<id>/objects/<key> just-in-time, converts, pushes, and moves on,
  # so we start converting immediately and never hold the whole dataset on disk.
  safe_rm "$dir"
  if ! nemar dataset download "$id" --no-data -o "$dir" >>"$LOG_FILE" 2>&1; then
    err "[$id] metadata clone failed"
    safe_rm "$dir"
    return 1
  fi

  local rc=0
  # --clean: wipe s3://<id>/zarr/ then full-rebuild, so the serving copy mirrors
  # the current dataset exactly (no orphaned stores / stale groups). With
  # streaming + JOBS-way parallelism a whole-dataset rebuild is cheap enough that
  # we always remake rather than reason about incremental diffs.
  VIRTUAL_ENV="$VENV_DIR" "$VENV_DIR/bin/python" "$DRIVER" \
    --dataset-id "$id" --repo-dir "$dir" \
    --bucket "$S3_BUCKET" --region "$AWS_REGION" --clean \
    --jobs "$JOBS" --callback-out "$cb" >>"$LOG_FILE" 2>&1 || rc=$?

  # Read the driver's classification BEFORE the scratch is reclaimed. The
  # converter now writes the callback on EVERY outcome (incl. a total failure),
  # carrying `deterministic` = all failures are typed DATA failures. The drain
  # loop only consults LAST_DETERMINISTIC in the failure (rc!=0) branch: a
  # partial success returns rc=0 -> `done` regardless of this value (#774).
  if [[ -f "$cb" ]]; then
    LAST_DETERMINISTIC="$(jq -r '.deterministic // false' "$cb" 2>/dev/null || echo false)"
    # POST on every outcome (not just rc==0) so the backend records failures too.
    if [[ -n "$NEMAR_WEBHOOK_TOKEN" ]]; then
      curl -sS --connect-timeout 10 --max-time 30 -X POST "$CALLBACK_URL" \
        -H "Content-Type: application/json" \
        -H "X-Webhook-Token: ${NEMAR_WEBHOOK_TOKEN}" \
        --data @"$cb" >>"$LOG_FILE" 2>&1 || err "[$id] callback failed (non-fatal)"
    fi
  fi

  # EPHEMERAL: always reclaim the scratch copy, success or failure.
  safe_rm "$dir"; rm -f "$cb"
  if [[ "$rc" -eq 0 ]]; then log "[$id] done"; else err "[$id] driver rc=$rc"; fi
  return "$rc"
}

# --- Single-instance lock -----------------------------------------------------
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "another hallu-zarr instance holds the lock; exiting"
  exit 3
fi

# --- Reclaim orphaned scratch -------------------------------------------------
# The driver's per-recording `tempfile.TemporaryDirectory()` unwinds on a normal
# exit, but a SIGKILL, an OOM, or an operator killing the drain's process group
# leaves the whole tree behind. Those are invisible (random `tmp*` names, not
# dataset-named) and they are BIG: 130 GB across 10 dirs by 2026-08-12, one of
# them 117 GB on its own. nemarOrg/nemar-cli#1068.
#
# Sweeping is safe HERE and only here: we hold the single-instance lock and have
# not started the driver, so nothing owns anything under WORK_DIR and every
# `tmp*` entry is by definition from a dead run. Dataset-named scratch dirs are
# left alone -- convert_dataset safe_rm's those itself before each clone.
sweep_orphaned_scratch() {
  local n=0 kb=0 sz
  while IFS= read -r -d '' p; do
    # `du -sk` (KiB) not `-sb`: -b is GNU-only. The regex guard keeps a failed or
    # empty du from turning the arithmetic into a syntax error and aborting the
    # sweep mid-loop -- reporting the size must never cost us the reclaim.
    sz=$(du -sk "$p" 2>/dev/null | cut -f1)
    [[ "$sz" =~ ^[0-9]+$ ]] || sz=0
    kb=$((kb + sz))
    safe_rm "$p"
    n=$((n + 1))
  done < <(find "$WORK_DIR" -maxdepth 1 -name 'tmp*' -print0 2>/dev/null)
  if [[ "$n" -gt 0 ]]; then
    log "swept $n orphaned scratch entries from a previous run (~$((kb / 1024)) MiB reclaimed)"
  fi
  return 0
}
sweep_orphaned_scratch

setup
if [[ ! -f "$DRIVER" || ! -f "$QUEUE" ]]; then
  err "driver/queue not found under $DRIVER_REPO after setup"; exit 1
fi

# Drift guard. setup() refreshes DRIVER_REPO from origin/main every run, so the
# Python driver deploys itself -- but THIS script cannot: it has to exist before
# the clone does, so it is a hand-placed copy that git never touches. That copy
# silently fell ~5 weeks behind main once already (nemarDatasets/.github#92's
# scratch sweep merged and did nothing here). Compare and warn; do NOT self-copy,
# because bash reads a script incrementally and rewriting the running file mid-run
# resumes execution at a garbage byte offset. Deploy with an atomic rename:
#   scp hallu-zarr.sh hallu:/path/.hallu-zarr.sh.new && ssh hallu 'mv /path/.hallu-zarr.sh.new /path/hallu-zarr.sh'
# dirname/basename rather than ${BASH_SOURCE%/*}: invoked by bare name off $PATH
# there is no slash to strip, and the parameter expansion would yield the filename
# as the directory, making every run report a bogus drift.
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)/$(basename "${BASH_SOURCE[0]}")"
REPO_SELF="$DRIVER_REPO/scripts/zarr/$(basename "${BASH_SOURCE[0]}")"
if [[ -f "$REPO_SELF" && "$SELF" != "$REPO_SELF" ]] && ! cmp -s "$SELF" "$REPO_SELF"; then
  err "DRIFT: $SELF differs from $REPO_SELF (origin/main). Changes merged to this script are NOT live; deploy it."
fi

if [[ -n "$STATS_ONLY" ]]; then
  qpy stats
  exit 0
fi

# Targeted single-dataset run bypasses the queue (manual rebuild / test).
if [[ -n "$ONLY_DATASET" ]]; then
  v="$(curl -sS --max-time 30 "${API_BASE}/datasets/${ONLY_DATASET}" 2>/dev/null \
        | jq -r '.dataset.latest_version // ""' 2>/dev/null)"
  convert_dataset "$ONLY_DATASET" "$v"
  exit $?
fi

# Reconcile (enqueue pending + recover stale inprogress), then drain the queue.
log "reconcile: $(qpy reconcile --api-base "$API_BASE")"
n=0
while :; do
  line="$(qpy next)"
  [[ -z "$line" ]] && break
  id="${line%%$'\t'*}"; version="${line#*$'\t'}"
  if convert_dataset "$id" "$version"; then
    qpy done "$id" "$version"
  elif [[ "$LAST_DETERMINISTIC" == "true" ]]; then
    # Every recording is an unreadable DATA failure -- terminal, no retry (#774).
    qpy fail "$id" "all recordings failed to convert (typed data failures; see ${LOG_FILE})" --deterministic
  else
    qpy fail "$id" "conversion failed (see ${LOG_FILE})"
  fi
  n=$((n + 1))
  if [[ "$LIMIT" -gt 0 && "$n" -ge "$LIMIT" ]]; then
    log "reached --limit $LIMIT; stopping (queue persists; next run continues)"
    break
  fi
done

log "run complete: processed $n dataset(s); $(qpy stats | head -1)"
