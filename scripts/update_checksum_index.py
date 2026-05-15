#!/usr/bin/env python3
"""
Sync checksums/annotation_checksums.tsv with annotations.tsv on the default branch.

On push after merge:
- Removes index rows for assemblies/URLs no longer present in a project's TSV
  (including when a project's annotations.tsv is deleted).
- Downloads and appends MD5 checksums for newly added rows only.

See .github/workflows/update-checksums.yml.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from registry_checksums import (  # noqa: E402
    CHECKSUM_INDEX_REL,
    ChecksumIndexEntry,
    RowKey,
    drop_index_for_repo_path,
    entries_for_new_rows,
    load_checksum_index,
    normalize_repo_path,
    prune_index_for_repo_path,
    render_checksum_index,
)
from validate_pr import (  # noqa: E402
    DEFAULT_MAX_DOWNLOAD_BYTES,
    SCAN_BYTES,
    build_http_session,
    download_check_gff3_stream,
    git_show_text,
    new_rows,
    parse_row,
    parse_tsv,
    run_git,
)


def git_changed_annotations(repo: str, base_sha: str, head_sha: str) -> list[str]:
    out = run_git(repo, "diff", "--name-only", base_sha, head_sha)
    return sorted(
        p.strip()
        for p in out.splitlines()
        if p.strip().endswith("annotations.tsv")
    )


def repo_path_from_apath(apath: str) -> str:
    return normalize_repo_path(Path(apath).parent.as_posix())


def row_keys_from_annotations_tsv(head_raw: str, repo_path: str) -> set[RowKey]:
    _, head_data, err = parse_tsv(head_raw)
    if err:
        return set()
    rp = normalize_repo_path(repo_path)
    keys: set[RowKey] = set()
    for ln in head_data:
        acc, url, perr = parse_row(ln)
        if not perr and acc and url:
            keys.add((rp, acc, url))
    return keys


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", type=Path, default=Path.cwd())
    ap.add_argument("--base", required=True, help="Previous commit SHA (e.g. github.event.before)")
    ap.add_argument("--head", required=True, help="Current commit SHA")
    ap.add_argument("--index", type=Path, default=None, help="Checksum index path (default: repo/checksums/...)")
    ap.add_argument("--max-download-mb", type=int, default=None)
    args = ap.parse_args()

    repo_root = args.repo.resolve()
    repo = str(repo_root)
    index_path = args.index or (repo_root / CHECKSUM_INDEX_REL)
    max_bytes = DEFAULT_MAX_DOWNLOAD_BYTES
    if args.max_download_mb is not None:
        max_bytes = args.max_download_mb * 1024 * 1024

    zero = "0" * 40
    base_sha = args.base if args.base != zero else args.head

    existing, ierr = load_checksum_index(index_path)
    if ierr:
        print(f"Invalid checksum index: {ierr}", file=sys.stderr)
        return 2

    changed = git_changed_annotations(repo, base_sha, args.head)
    if not changed:
        print("No annotations.tsv changes — index unchanged.")
        return 0

    session = build_http_session()
    index: list[ChecksumIndexEntry] = list(existing)
    additions: list[ChecksumIndexEntry] = []
    index_changed = False

    for apath in changed:
        repo_path = repo_path_from_apath(apath)
        head_raw = git_show_text(repo, args.head, apath)

        if head_raw is None:
            before = len(index)
            index = drop_index_for_repo_path(index, repo_path)
            removed = before - len(index)
            if removed:
                index_changed = True
                print(f"Removed {removed} index row(s) for deleted `{apath}`")
            continue

        valid_keys = row_keys_from_annotations_tsv(head_raw, repo_path)
        before = len(index)
        index = prune_index_for_repo_path(index, repo_path, valid_keys)
        pruned = before - len(index)
        if pruned:
            index_changed = True
            print(f"Pruned {pruned} stale index row(s) for `{repo_path}`")

        base_raw = git_show_text(repo, base_sha, apath)
        nr, nerr = new_rows(base_raw, head_raw)
        if nerr:
            print(f"Skip new rows for {apath}: {nerr}", file=sys.stderr)
            continue

        for nl in nr:
            acc, url, perr = parse_row(nl)
            if perr or not acc or not url:
                print(f"Skip unparseable row in {apath}: {perr}", file=sys.stderr)
                continue
            with tempfile.TemporaryDirectory(prefix="arv_idx_") as tmp:
                dest = Path(tmp) / "download.bin"
                ok_d, msg_d, _, _, file_md5 = download_check_gff3_stream(
                    session, url, dest, max_bytes, SCAN_BYTES
                )
            if not ok_d or not file_md5:
                print(
                    f"Skip {apath} {acc}: download failed ({msg_d})",
                    file=sys.stderr,
                )
                continue
            additions.append(
                ChecksumIndexEntry(
                    md5_checksum=file_md5,
                    assembly_accession=acc,
                    repo_path=repo_path,
                    access_url=url,
                )
            )
            print(f"Hashed {repo_path} {acc} → {file_md5}")

    to_append = entries_for_new_rows(index, additions)
    if to_append:
        index = index + to_append
        index_changed = True
        print(f"Appending {len(to_append)} new index row(s)")

    if not index_changed:
        print("Checksum index unchanged.")
        return 0

    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(render_checksum_index(index), encoding="utf-8")
    print(f"Wrote {len(index)} row(s) → {index_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
