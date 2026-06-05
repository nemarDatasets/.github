#!/usr/bin/env python3
"""
NEMAR manifest + summary emitter.

Productionized port of /tmp/manifest_recover.py for the central
`generate-manifest` GitHub Actions workflow on nemarOrg/nemar-cli.

Reads the git tree at a tag from an already-cloned dataset repo and emits
two artifacts:

  - manifest.json: matches backend/src/services/manifest.ts VersionManifest
    shape exactly (dataset_id, version, doi, concept_doi, created, files).
  - summary.json: compact dataset summary per epic state contract
    (schema_version, totals, modalities, subjects, readme, paths).
    Schema 1.1 (epic #618 / issue #619): `readme` now embeds the raw
    markdown content under `readme.content` for non-annexed READMEs up
    to 256 KB. Over-cap, annexed, or unreadable READMEs ship
    `truncated=true` with `content=null` so consumers fall back.

The script does NOT clone; the workflow's checkout step is responsible for
that. The script does NOT touch the GitHub API; everything comes from local
git plumbing. The optional --verify-canary flag HEAD-checks a small sample
of git:-keyed files against raw.githubusercontent.com to mirror the
verifyGitBackedFiles policy in manifest.ts.

Usage:
    emit_manifest.py --dataset-id nm099999 --version 1.0.0 \\
        --doi 10.82901/nemar.nm099999.v1.0.0 \\
        --concept-doi 10.82901/nemar.nm099999 \\
        --repo-dir /tmp/repo --out-dir /tmp/out \\
        [--verify-canary | --no-verify-canary]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Match git-annex symlink targets in either form:
#   ../../.git/annex/objects/03/fK/MD5E-s59778400--abc.set/MD5E-s59778400--abc.set
# The trailing `/<KEY>` repeats the key dir name so we anchor on that.
ANNEX_TARGET_RE = re.compile(
    r"\.git/annex/objects/[A-Za-z0-9]+/[A-Za-z0-9]+/([^/]+)/\1$"
)
# Unlocked-mode annex pointer files: regular blobs whose content is
# `/annex/objects/<KEY>` (no .git/ prefix, no trailing duplicate). This
# mirrors the first branch of parseAnnexPointer() in
# backend/src/services/manifest.ts.
ANNEX_POINTER_CONTENT_RE = re.compile(r"^/annex/objects/(.+)$")
KEY_SIZE_RE = re.compile(r"-s(\d+)--")
KEY_CHECKSUM_RE = re.compile(r"--([a-fA-F0-9]+)")
KEY_ALGO_RE = re.compile(r"^([A-Z0-9]+?)E?-s")

# Datatype directories that count as BIDS modalities. Matches the set used
# by the Worker's metadata enrichment pipeline. Detected by scanning each
# manifested path for "/<datatype>/" substrings.
BIDS_MODALITIES = (
    "eeg",
    "emg",
    "meg",
    "func",
    "anat",
    "dwi",
    "fmap",
    "beh",
    "ieeg",
    "pet",
    "perf",
    "motion",
)

# README filenames searched at the BIDS root, in priority order.
README_CANDIDATES = ("README", "README.md", "README.txt")

# Maximum README byte size to embed inline in summary.json. Over this cap
# the summary still records the path + sha256 + content_bytes, but sets
# truncated=true and leaves content=null so the website can fall back to
# fetching the file from data.nemar.org / GitHub.
README_INLINE_MAX_BYTES = 256 * 1024

# raw.githubusercontent.com base for the canary HEAD checks.
RAW_GITHUB_BASE = "https://raw.githubusercontent.com/nemarDatasets"
# Canonical public data-plane origin for the additive bytes_url (#615). This is
# a build-time script with no request context, so it emits the canonical host;
# the served manifest.json mirrors the same shape per-request (nemar-cli
# backend/src/services/data-router.ts:buildBytesUrl).
DATA_NEMAR_BASE = "https://data.nemar.org"
CANARY_TIMEOUT_S = 10
CANARY_MAX_ADDITIONAL = 4  # +1 for dataset_description.json = 5 total


def run(cmd: list[str], cwd: str | None = None) -> str:
    """Run a subprocess and return stdout as text. Raises on non-zero exit."""
    return subprocess.check_output(cmd, cwd=cwd, text=True)


def parse_annex_key(symlink_target: str) -> str | None:
    """Return the annex key embedded in a symlink target, or None."""
    m = ANNEX_TARGET_RE.search(symlink_target.strip())
    return m.group(1) if m else None


def parse_annex_pointer_content(content: str) -> str | None:
    """Parse an unlocked-mode git-annex pointer blob and return the key.

    Mirrors backend/src/services/manifest.ts:parseAnnexPointer(). Two
    branches are checked, in order:

      1. Plain pointer content: ``/annex/objects/<KEY>`` (one line, no
         trailing slash, no .git/ prefix). This is what unlocked-mode
         git-annex repos commit as regular 100644 blobs.
      2. Symlink-target content: ``.git/annex/objects/XX/YY/<KEY>/<KEY>``.
         Only relevant if someone hands us locked-mode symlink-target text
         as a blob (defensive parity with the TS helper).

    Returns the annex key, or None if neither pattern matches.
    """
    trimmed = content.strip()
    m = ANNEX_POINTER_CONTENT_RE.match(trimmed)
    if m:
        return m.group(1)
    # Fall through to the locked-mode symlink target form for safety.
    m = ANNEX_TARGET_RE.search(trimmed)
    return m.group(1) if m else None


def extract_size_from_key(key: str) -> int:
    m = KEY_SIZE_RE.search(key)
    if not m:
        print(
            f"[emit_manifest] warning: could not extract size from annex key {key!r}; "
            f"defaulting to 0",
            file=sys.stderr,
            flush=True,
        )
        return 0
    return int(m.group(1))


def extract_checksum_from_key(key: str) -> str:
    m = KEY_CHECKSUM_RE.search(key)
    if not m:
        print(
            f"[emit_manifest] warning: could not extract checksum from annex key "
            f"{key!r}; defaulting to empty string",
            file=sys.stderr,
            flush=True,
        )
        return ""
    return m.group(1)


def extract_algo_from_key(key: str) -> str:
    """Annex key formats: SHA256E-sNN--HEX.ext, MD5E-sNN--HEX.ext, etc.

    The trailing E (when present) marks "Extension" backends; the algorithm
    name is the prefix before it.
    """
    m = KEY_ALGO_RE.match(key)
    if not m:
        print(
            f"[emit_manifest] warning: could not extract hash algorithm from annex "
            f"key {key!r}; defaulting to sha256",
            file=sys.stderr,
            flush=True,
        )
        return "sha256"
    return m.group(1).lower()


def bytes_url_for(dataset_id: str, tag: str, path: str, key: str) -> str:
    """Stable public contract URL for a file's bytes (#615).

    Additive sibling to `key`/`size`/`checksum`. Unlike a presigned S3 URL it
    never expires. git-keyed files -> raw.githubusercontent.com pinned to the
    tag; annex-keyed files -> the per-file data-plane route on data.nemar.org
    (which 302s to the bytes). Mirrors nemar-cli's served-manifest buildBytesUrl
    so the raw S3 manifest and the served manifest.json agree.
    """
    encoded = "/".join(urllib_quote(seg) for seg in path.split("/"))
    if key.startswith("git:"):
        return f"{RAW_GITHUB_BASE}/{dataset_id}/{tag}/{encoded}"
    return f"{DATA_NEMAR_BASE}/{dataset_id}/{tag}/{encoded}"


def build_manifest(
    *,
    dataset_id: str,
    version: str,
    doi: str | None,
    concept_doi: str | None,
    repo_dir: str,
) -> dict:
    """Walk the git tree at v<version> and assemble VersionManifest dict."""
    bare_version = version.lstrip("v")
    tag = f"v{bare_version}"

    raw = run(["git", "-C", repo_dir, "ls-tree", "-r", tag])

    files: dict[str, dict] = {}

    for line in raw.splitlines():
        meta, _, path = line.partition("\t")
        if not path:
            continue
        mode, _, rest = meta.partition(" ")
        _, _, sha = rest.partition(" ")

        # Match the isInternal() policy in manifest.ts. Trailing slash is
        # intentional so .gitattributes / .gitignore at BIDS root are kept.
        if path.startswith(".git/") or path.startswith(".github/"):
            continue

        if mode == "120000":
            target = run(["git", "-C", repo_dir, "cat-file", "blob", sha]).strip()
            key = parse_annex_key(target)
            if key:
                algo = extract_algo_from_key(key)
                files[path] = {
                    "key": key,
                    "size": extract_size_from_key(key),
                    "checksum": f"{algo}:{extract_checksum_from_key(key)}",
                }
                continue
            # Fall through: symlink not pointing at the annex. Treat as a
            # regular git blob so it still appears in the manifest.

        if mode in ("100644", "100755", "120000"):
            size = int(run(["git", "-C", repo_dir, "cat-file", "-s", sha]).strip())
            # Unlocked-mode (v7+) git-annex repos commit pointer files as
            # regular blobs whose content is `/annex/objects/<KEY>`. If we
            # naively keyed these as `git:<sha>` the manifest would record
            # the ~80-byte pointer blob's size and SHA, not the real annex
            # file. Mirrors the first branch of parseAnnexPointer() in
            # backend/src/services/manifest.ts.
            #
            # Pointer blobs are always small (typically <100 bytes). Guard
            # the cat-file blob read on size so we don't pull big text
            # files (e.g. participants.tsv) into memory just to discard
            # them. The 512-byte ceiling is generous; real pointer blobs
            # rarely exceed ~120 bytes even with long extensions.
            if mode in ("100644", "100755") and size <= 512:
                content = run(["git", "-C", repo_dir, "cat-file", "blob", sha])
                key = parse_annex_pointer_content(content)
                if key:
                    algo = extract_algo_from_key(key)
                    files[path] = {
                        "key": key,
                        "size": extract_size_from_key(key),
                        "checksum": f"{algo}:{extract_checksum_from_key(key)}",
                    }
                    continue

            files[path] = {
                "key": f"git:{sha}",
                "size": size,
                "checksum": f"git:{sha}",
            }

    # Additive bytes_url per entry (#615): a stable, non-expiring contract URL,
    # derived from the key already on each entry. One pass keeps the three
    # entry-build sites above focused on the key/size/checksum core.
    for entry_path, entry in files.items():
        entry["bytes_url"] = bytes_url_for(dataset_id, tag, entry_path, entry["key"])

    # Match JS Date#toISOString(): millisecond precision, trailing Z.
    now = datetime.now(timezone.utc)
    created = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

    return {
        "dataset_id": dataset_id,
        "version": bare_version,
        "doi": doi or None,
        "concept_doi": concept_doi or None,
        "created": created,
        "files": files,
    }


def derive_subjects(paths: list[str]) -> list[str]:
    """Extract sorted unique top-level sub-XXX/ prefixes from paths."""
    subjects: set[str] = set()
    for p in paths:
        head, _, _ = p.partition("/")
        if head.startswith("sub-") and len(head) > 4:
            subjects.add(head)
    return sorted(subjects)


def derive_modalities(paths: list[str]) -> list[str]:
    """Detect BIDS datatype directories present in the path set."""
    found: set[str] = set()
    for p in paths:
        parts = p.split("/")
        # Any interior directory segment equal to a known datatype counts.
        # `parts[1:-1]` is overly strict (would miss derivatives/sub-X/eeg/...);
        # check every segment except the filename instead.
        for seg in parts[:-1]:
            if seg in BIDS_MODALITIES:
                found.add(seg)
    return sorted(found)


def _read_readme_blob(repo_dir: str, sha: str, *, dataset_id: str, version: str, candidate: str) -> bytes | None:
    """Return the raw bytes of a git blob, or None if cat-file fails.

    cat-file failure here means the manifest references a blob the repo
    can't produce — corruption, wrong repo_dir, or the manifest is stale.
    None of those are normal; emit a structured stderr line so ops sees it.
    """
    try:
        return subprocess.check_output(
            ["git", "-C", repo_dir, "cat-file", "blob", sha]
        )
    except subprocess.CalledProcessError as e:
        print(
            f"[emit_manifest] readme_extract_failed dataset={dataset_id} "
            f"version={version} candidate={candidate} sha={sha} "
            f"reason=cat_file_error error={e}",
            file=sys.stderr,
            flush=True,
        )
        return None


def derive_readme(*, paths: set[str], files: dict, repo_dir: str, dataset_id: str, version: str) -> dict | None:
    """Find the BIDS-root README and embed its content into the summary.

    Schema 1.1 contract — four output shapes:
      1. Absent (no README at root): None
      2. Present, readable, fits cap (git-keyed, UTF-8, <= 256 KB):
         {path, content=<str>, content_bytes=<int>, sha256=<hex>, truncated=False}
      3. Present, oversize or annexed (no read attempted):
         {path, content=None, content_bytes=<int from key/git>, sha256=None, truncated=True}
      4. Present, read attempted but unusable (cat-file failed or binary):
         {path, content=None, content_bytes=<None or actual bytes>, sha256=<hex or None>, truncated=True}

    `content_bytes` reports the **actual byte length of content read** when a
    read happened; it is None on cat-file failure (no bytes ever seen) and
    equal to the manifest/annex `size` field for oversize/annexed branches
    (where we deliberately skipped the read). Consumers can rely on
    `content_bytes is None` as the "we never opened the blob" signal.

    Every truncated branch emits a structured stderr warning (`reason=…`)
    so ops sees that the inline-content fast path didn't fire.
    """
    for candidate in README_CANDIDATES:
        if candidate not in paths:
            continue
        meta = files.get(candidate, {})
        key = str(meta.get("key", ""))
        size = int(meta.get("size") or 0)

        # Annexed README — content lives in S3, not git. We don't dereference
        # here because the worker has IAM, not this script. Mark truncated so
        # the website knows to fall back without retrying.
        if not key.startswith("git:"):
            print(
                f"[emit_manifest] readme_truncated dataset={dataset_id} "
                f"version={version} candidate={candidate} reason=annexed size={size}",
                file=sys.stderr,
                flush=True,
            )
            return {
                "path": candidate,
                "content": None,
                "content_bytes": size,
                "sha256": None,
                "truncated": True,
            }

        # Git-keyed: cat-file the blob. Size cap avoids pulling unbounded
        # blobs into memory — generous since typical READMEs are < 20 KB.
        if size > README_INLINE_MAX_BYTES:
            print(
                f"[emit_manifest] readme_truncated dataset={dataset_id} "
                f"version={version} candidate={candidate} reason=oversize "
                f"size={size} cap={README_INLINE_MAX_BYTES}",
                file=sys.stderr,
                flush=True,
            )
            return {
                "path": candidate,
                "content": None,
                "content_bytes": size,
                "sha256": None,
                "truncated": True,
            }

        sha = key[len("git:") :]
        blob = _read_readme_blob(
            repo_dir, sha, dataset_id=dataset_id, version=version, candidate=candidate
        )
        if blob is None:
            return {
                "path": candidate,
                "content": None,
                # content_bytes=None signals "we never read the blob"; the
                # caller can't tell git's reported size apart from the actual
                # bytes here, so don't pretend.
                "content_bytes": None,
                "sha256": None,
                "truncated": True,
            }

        sha256 = hashlib.sha256(blob).hexdigest()
        try:
            content = blob.decode("utf-8")
        except UnicodeDecodeError:
            # BIDS spec requires UTF-8 README. A non-UTF-8 blob means an
            # upstream encoding error in the dataset; warn loudly so the
            # owner can fix it, but don't fail the publish (the manifest
            # is still valid and downloads still work). The truncated flag
            # tells the website to fall back instead of rendering garbage.
            print(
                f"[emit_manifest] readme_truncated dataset={dataset_id} "
                f"version={version} candidate={candidate} reason=non_utf8 "
                f"size={len(blob)} sha256={sha256}",
                file=sys.stderr,
                flush=True,
            )
            return {
                "path": candidate,
                "content": None,
                "content_bytes": len(blob),
                "sha256": sha256,
                "truncated": True,
            }

        return {
            "path": candidate,
            "content": content,
            "content_bytes": len(blob),
            "sha256": sha256,
            "truncated": False,
        }
    return None


def build_summary(*, manifest: dict, repo_dir: str) -> dict:
    """Build the compact summary.json artifact from the manifest.

    `repo_dir` is needed to cat-file the README blob for the schema-1.1
    inline content. Callers always have it (the workflow's checkout step
    is what produced the manifest in the first place).
    """
    files: dict = manifest["files"]
    paths_sorted = sorted(files.keys())
    paths_set = set(paths_sorted)

    total_bytes = sum(int(meta.get("size") or 0) for meta in files.values())
    subjects = derive_subjects(paths_sorted)
    modalities = derive_modalities(paths_sorted)
    readme = derive_readme(
        paths=paths_set,
        files=files,
        repo_dir=repo_dir,
        dataset_id=manifest["dataset_id"],
        version=manifest["version"],
    )

    return {
        "schema_version": "1.1",
        "dataset_id": manifest["dataset_id"],
        "version": manifest["version"],
        "doi": manifest["doi"],
        "concept_doi": manifest["concept_doi"],
        "created": manifest["created"],
        "totals": {
            "files": len(files),
            "bytes": total_bytes,
            "subjects": len(subjects),
        },
        "modalities": modalities,
        "subjects": subjects,
        "readme": readme,
        "paths": paths_sorted,
    }


def select_git_canaries(files: dict, max_additional: int = CANARY_MAX_ADDITIONAL) -> list[str]:
    """Mirror selectGitBackedCanaries() in manifest.ts."""
    git_paths = sorted(p for p, meta in files.items() if meta["key"].startswith("git:"))
    if not git_paths:
        return []
    canaries: list[str] = []
    if "dataset_description.json" in git_paths:
        canaries.append("dataset_description.json")
    for p in git_paths:
        if len(canaries) >= 1 + max_additional:
            break
        if p not in canaries:
            canaries.append(p)
    return canaries


def verify_canaries(*, dataset_id: str, version: str, files: dict) -> None:
    """HEAD-check git:-keyed canaries on raw.githubusercontent.com.

    Raises RuntimeError on any failure. Mirrors verifyGitBackedFiles() in
    backend/src/services/manifest.ts: same selection, same retry-once
    behaviour, same loud-fail policy.
    """
    bare_version = version.lstrip("v")
    tag = f"v{bare_version}"
    canaries = select_git_canaries(files)
    if not canaries:
        print(f"[canary] no git:-keyed files in manifest dataset={dataset_id} tag={tag}", flush=True)
        return

    failures: list[tuple[str, int]] = []
    for path in canaries:
        url_path = "/".join(urllib_quote(seg) for seg in path.split("/"))
        url = f"{RAW_GITHUB_BASE}/{dataset_id}/{tag}/{url_path}"
        ok_status = 0
        for attempt in range(2):
            try:
                req = urllib.request.Request(url, method="HEAD")
                with urllib.request.urlopen(req, timeout=CANARY_TIMEOUT_S) as resp:
                    ok_status = resp.status
                    if 200 <= resp.status < 400:
                        break
            except urllib.error.HTTPError as e:
                ok_status = e.code
            except (urllib.error.URLError, TimeoutError) as e:
                print(
                    f"[canary] HEAD threw dataset={dataset_id} tag={tag} path={path} "
                    f"attempt={attempt + 1}: {e}",
                    file=sys.stderr,
                    flush=True,
                )
                ok_status = 0
            if attempt == 0:
                # Mirror the 2s backoff used in manifest.ts for CDN propagation.
                time.sleep(2)
        if not (200 <= ok_status < 400):
            failures.append((path, ok_status))

    if failures:
        summary = ", ".join(f"{p} (HTTP {s})" for p, s in failures)
        raise RuntimeError(
            f"Manifest canary failed: {len(failures)}/{len(canaries)} git:-keyed "
            f"files do not resolve on raw.githubusercontent.com at tag {tag}. "
            f"Failing paths: {summary}. The version tag may not exist on GitHub "
            f"yet, the repo may be private, or the blob may have been removed by "
            f"a retag."
        )
    print(
        f"[canary] OK dataset={dataset_id} tag={tag} checked={len(canaries)}",
        flush=True,
    )


def urllib_quote(seg: str) -> str:
    from urllib.parse import quote
    return quote(seg, safe="")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Emit NEMAR manifest.json + summary.json from a cloned dataset repo.",
    )
    p.add_argument("--dataset-id", required=True)
    p.add_argument("--version", required=True, help="X.Y.Z or vX.Y.Z; tag is v<X.Y.Z>")
    p.add_argument("--doi", default="", help="empty string treated as null")
    p.add_argument("--concept-doi", default="", help="empty string treated as null")
    p.add_argument("--repo-dir", required=True, help="path to already-cloned dataset repo")
    p.add_argument("--out-dir", required=True, help="directory to write manifest.json + summary.json")
    canary = p.add_mutually_exclusive_group()
    canary.add_argument(
        "--verify-canary",
        dest="verify_canary",
        action="store_true",
        help="HEAD-check git:-keyed files on raw.githubusercontent.com (default)",
    )
    canary.add_argument(
        "--no-verify-canary",
        dest="verify_canary",
        action="store_false",
        help="skip the raw.githubusercontent.com canary (offline tests, recovery)",
    )
    p.set_defaults(verify_canary=True)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    repo_dir = Path(args.repo_dir)
    if not (repo_dir / ".git").exists() and not (repo_dir / "HEAD").exists():
        # Tolerate both worktrees and bare clones. .git is the common case.
        print(f"[emit_manifest] error: --repo-dir {repo_dir} is not a git repo", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest = build_manifest(
        dataset_id=args.dataset_id,
        version=args.version,
        doi=args.doi or None,
        concept_doi=args.concept_doi or None,
        repo_dir=str(repo_dir),
    )

    if args.verify_canary:
        verify_canaries(
            dataset_id=args.dataset_id,
            version=args.version,
            files=manifest["files"],
        )

    summary = build_summary(manifest=manifest, repo_dir=str(repo_dir))

    manifest_path = out_dir / "manifest.json"
    summary_path = out_dir / "summary.json"
    totals_path = out_dir / "totals.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    summary_path.write_text(json.dumps(summary, indent=2))

    annex_count = sum(1 for m in manifest["files"].values() if not m["key"].startswith("git:"))
    git_count = len(manifest["files"]) - annex_count
    totals = {
        "files": len(manifest["files"]),
        "bytes": summary["totals"]["bytes"],
        "annex": annex_count,
        "git": git_count,
    }
    # `totals.json` is consumed by the GitHub Actions workflow to build the
    # webhook callback body without nested-heredoc gymnastics. Not uploaded
    # to S3; runner-local artifact only.
    totals_path.write_text(json.dumps(totals))

    print(
        f"[emit_manifest] dataset={args.dataset_id} version={manifest['version']} "
        f"files={len(manifest['files'])} (annex={annex_count}, git={git_count}) "
        f"bytes={summary['totals']['bytes']} subjects={summary['totals']['subjects']} "
        f"modalities={summary['modalities']}",
        flush=True,
    )
    print(f"[emit_manifest] wrote {manifest_path}", flush=True)
    print(f"[emit_manifest] wrote {summary_path}", flush=True)
    print(f"[emit_manifest] wrote {totals_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
