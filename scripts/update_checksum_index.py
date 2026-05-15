#!/usr/bin/env python3
"""
Append MD5 checksums for newly merged annotations.tsv rows to checksums/annotation_checksums.tsv.

Run on push to the default branch after PR merge (see .github/workflows/update-checksums.yml).
Downloads only rows that are new relative to the previous commit.
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
    entries_for_new_rows,
    load_checksum_index,
    render_checksum_index,
)
from validate_pr import (  # noqa: E402
    DEFAULT_MAX_DOWNLOAD_BYTES,
    SCAN_BYTES,
    download_check_gff3_stream,
    build_http_session,
    git_show_text,
    new_rows,
    parse_row,
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
    parent = Path(apath).parent.as_posix()
    return parent if parent != "." else "."


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
    additions: list[ChecksumIndexEntry] = []

    for apath in changed:
        head_raw = git_show_text(repo, args.head, apath)
        if head_raw is None:
            print(f"Skip missing at HEAD: {apath}", file=sys.stderr)
            continue
        base_raw = git_show_text(repo, base_sha, apath)
        nr, nerr = new_rows(base_raw, head_raw)
        if nerr:
            print(f"Skip {apath}: {nerr}", file=sys.stderr)
            continue
        repo_path = repo_path_from_apath(apath)
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

    to_append = entries_for_new_rows(existing, additions)
    if not to_append:
        print("No new checksum rows to append.")
        return 0

    merged = existing + to_append
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(render_checksum_index(merged), encoding="utf-8")
    print(f"Appended {len(to_append)} row(s) → {index_path} ({len(merged)} total)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
