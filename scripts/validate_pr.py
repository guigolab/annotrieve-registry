#!/usr/bin/env python3
"""
Validate annotrieve-registry pull requests: manifests, new TSV rows, assemblies,
URLs, GFF3 shape (ID / Parent), and tabix-compatible processing (Annotrieve-style).
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
import yaml
from jsonschema import Draft202012Validator
from jsonschema import FormatChecker

REQUIRED_TSV_HEADER = "assembly_accession\taccess_url"
ASSEMBLY_RE = re.compile(r"^(GCA|GCF)_\d+\.\d+$")
COMMENT_MARKER = "<!-- annotrieve-registry-validation -->"

SCAN_BYTES = int(os.environ.get("VALIDATE_SCAN_BYTES", str(50 * 1024 * 1024)))
DEFAULT_MAX_DOWNLOAD_BYTES = int(
    os.environ.get("VALIDATE_MAX_DOWNLOAD_BYTES", str(500 * 1024 * 1024))
)
HTTP_TIMEOUT = int(os.environ.get("VALIDATE_HTTP_TIMEOUT", "120"))


def run_git(repo: str, *args: str) -> str:
    r = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {r.stderr or r.stdout}"
        )
    return r.stdout


def git_merge_base(repo: str, base_sha: str, head_sha: str) -> str:
    return run_git(repo, "merge-base", base_sha, head_sha).strip()


def git_changed_files(repo: str, merge_base: str, head_sha: str) -> list[str]:
    out = run_git(repo, "diff", "--name-only", merge_base, head_sha)
    return [p.strip() for p in out.splitlines() if p.strip()]


def git_show_text(repo: str, rev: str, path: str) -> str | None:
    r = subprocess.run(
        ["git", "-C", repo, "show", f"{rev}:{path}"],
        capture_output=True,
    )
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", errors="replace")


def load_schema(repo_root: Path) -> dict[str, Any]:
    p = repo_root / "schema" / "manifest.schema.json"
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def validate_manifest_doc(
    doc: Any, schema: dict[str, Any]
) -> list[str]:
    errors: list[str] = []
    v = Draft202012Validator(
        schema, format_checker=FormatChecker()
    )
    for e in v.iter_errors(doc):
        loc = "/".join(str(p) for p in e.path) or "."
        errors.append(f"{loc}: {e.message}")
    return errors


def is_probably_gzip(path: Path) -> bool:
    with open(path, "rb") as f:
        sig = f.read(2)
    return len(sig) == 2 and sig[0] == 0x1F and sig[1] == 0x8B


def open_text_stream(path: Path) -> io.TextIOBase:
    if is_probably_gzip(path):
        return io.TextIOWrapper(
            gzip.open(path, "rb"), encoding="utf-8", errors="replace"
        )
    return open(path, encoding="utf-8", errors="replace")


def check_gff3_id_parent(path: Path, max_read_bytes: int) -> tuple[bool, str]:
    """
    Stream up to max_read_bytes of decompressed text; require at least one
    feature line with ID= and one with Parent= in column 9 (GFF3 attributes).
    """
    has_id = False
    has_parent = False
    read_bytes = 0
    with open_text_stream(path) as fh:
        for line in fh:
            if not line or line.startswith("#"):
                continue
            read_bytes += len(line.encode("utf-8", errors="replace"))
            if read_bytes > max_read_bytes:
                break
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 9:
                continue
            attrs = parts[8]
            if "ID=" in attrs:
                has_id = True
            if "Parent=" in attrs:
                has_parent = True
            if has_id and has_parent:
                return True, ""
    if not has_id and not has_parent:
        return False, "no feature lines with both ID= and Parent= found in scanned region (need at least one of each in GFF3 attributes)"
    if not has_id:
        return False, "no ID= found in GFF3 attributes in scanned region"
    if not has_parent:
        return False, "no Parent= found in GFF3 attributes in scanned region"
    return True, ""


def head_url_ok(url: str) -> tuple[bool, str]:
    try:
        r = requests.head(
            url, allow_redirects=True, timeout=HTTP_TIMEOUT, stream=True
        )
        if r.status_code in (405, 501) or r.status_code == 404:
            g = requests.get(
                url,
                allow_redirects=True,
                timeout=HTTP_TIMEOUT,
                stream=True,
                headers={"Range": "bytes=0-0"},
            )
            g.close()
            if g.status_code >= 400:
                return False, f"HTTP {g.status_code} on GET range"
            return True, ""
        r.close()
        if r.status_code >= 400:
            return False, f"HTTP {r.status_code} on HEAD"
        return True, ""
    except requests.RequestException as e:
        return False, str(e)


def download_to_path(
    url: str, dest: Path, max_bytes: int | None
) -> tuple[bool, str]:
    try:
        with requests.get(
            url, allow_redirects=True, timeout=HTTP_TIMEOUT, stream=True
        ) as r:
            r.raise_for_status()
            n = 0
            with open(dest, "wb") as out:
                for chunk in r.iter_content(chunk_size=1024 * 64):
                    if not chunk:
                        continue
                    n += len(chunk)
                    if max_bytes is not None and n > max_bytes:
                        return (
                            False,
                            f"download exceeded max bytes ({max_bytes})",
                        )
                    out.write(chunk)
        return True, ""
    except requests.RequestException as e:
        return False, str(e)
    except OSError as e:
        return False, str(e)


def run_tabix_pipeline(
    in_path: Path, work: Path, label: str
) -> tuple[bool, str]:
    """
    Match Annotrieve: (decompress|cat) with comment lines first, then sort, bgzip, tabix -p gff --csi.
    """
    out_gz = work / f"{label}.gff.gz"
    csi = out_gz.with_suffix(out_gz.suffix + ".csi")
    for p in (out_gz, csi):
        if p.exists():
            p.unlink()
    decomp = "zcat" if is_probably_gzip(in_path) else "cat"
    in_q = shlex.quote(str(in_path))
    out_q = shlex.quote(str(out_gz))
    # Same sort key as annotrieve server: tab, k1 seqid, k4 start numeric
    stream_cmd = (
        f"({decomp} {in_q} | grep '^#'; "
        f"{decomp} {in_q} | grep -v '^#' | sort -t\"$(printf '\\t')\" -k1,1 -k4,4n) "
        f"| bgzip > {out_q}"
    )
    p1 = subprocess.run(
        ["bash", "-lc", stream_cmd],
        capture_output=True,
        text=True,
    )
    if p1.returncode != 0:
        return False, (p1.stderr or p1.stdout or "bgzip pipeline failed")[:2000]
    if not out_gz.exists() or out_gz.stat().st_size == 0:
        return False, "bgzip output missing or empty"
    tabix_cmd = f"tabix -p gff --csi {out_q}"
    p2 = subprocess.run(
        ["bash", "-lc", tabix_cmd],
        capture_output=True,
        text=True,
    )
    if p2.returncode != 0:
        return False, (p2.stderr or p2.stdout or "tabix failed")[:2000]
    if not csi.exists() or csi.stat().st_size == 0:
        return False, "CSI index missing or empty"
    return True, ""


def ncbi_assembly_exists(datasets_bin: str, accession: str) -> tuple[bool, str]:
    r = subprocess.run(
        [
            datasets_bin,
            "summary",
            "genome",
            "accession",
            accession,
            "--as-json",
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode == 0 and r.stdout.strip():
        return True, ""
    err = (r.stderr or r.stdout or "unknown error")[:500]
    return False, err


def parse_tsv_data_lines(
    content: str | None,
) -> tuple[list[str] | None, list[str], str | None]:
    """Return (header_cols, data_lines_raw, error)."""
    if content is None:
        return [], [], None
    lines = [ln.rstrip("\n\r") for ln in content.splitlines()]
    if not lines:
        return [], [], "empty file"
    header = lines[0].replace(" ", "").strip().split("\t")
    exp = REQUIRED_TSV_HEADER.split("\t")
    norm_header = lines[0].strip().split("\t")
    if norm_header != exp:
        return (
            None,
            [],
            f"invalid header: expected tab-separated '{REQUIRED_TSV_HEADER}'",
        )
    data = []
    for ln in lines[1:]:
        if not ln.strip():
            continue
        if ln.startswith("#"):
            continue
        data.append(ln.strip("\n\r"))
    return header, data, None


def new_rows_from_diff(base_content: str | None, head_content: str) -> tuple[list[str], str | None]:
    _, base_lines, err = parse_tsv_data_lines(base_content)
    if err:
        return [], err
    _, head_lines, err_h = parse_tsv_data_lines(head_content)
    if err_h:
        return [], err_h
    base_set = set(base_lines)
    return [ln for ln in head_lines if ln not in base_set], None


def parse_row_line(line: str) -> tuple[str | None, str | None, str | None]:
    parts = line.split("\t")
    if len(parts) != 2:
        return None, None, f"expected 2 columns, got {len(parts)}"
    acc, url = parts[0].strip(), parts[1].strip()
    return acc, url, None


def split_projects(paths: list[str]) -> set[str]:
    projects: set[str] = set()
    for p in paths:
        pl = Path(p)
        if pl.name == "annotations.tsv" or pl.name == "manifest.yaml":
            projects.add(str(pl.parent.as_posix()))
    return projects


def duplicate_accessions(head_lines: list[str]) -> list[str]:
    accs = []
    for ln in head_lines:
        acc, _, err = parse_row_line(ln)
        if err:
            continue
        if acc:
            accs.append(acc)
    counts = Counter(accs)
    return [a for a, c in counts.items() if c > 1]


def validate_row(
    datasets_bin: str,
    line: str,
    max_download_bytes: int | None,
) -> list[str]:
    errs: list[str] = []
    acc, url, perr = parse_row_line(line)
    if perr:
        return [perr]
    if not acc or not url:
        return ["empty assembly_accession or access_url"]
    if not ASSEMBLY_RE.match(acc):
        errs.append(f"assembly_accession format invalid (need GCA_/GCF_…): {acc!r}")
        return errs

    ok_a, msg_a = ncbi_assembly_exists(datasets_bin, acc)
    if not ok_a:
        errs.append(f"NCBI assembly check failed: {msg_a}")

    try:
        if urlparse(url).scheme not in ("http", "https"):
            errs.append(f"URL must be http(s): {url!r}")
    except Exception as e:
        errs.append(f"URL parse error: {e}")

    ok_u, msg_u = head_url_ok(url)
    if not ok_u:
        errs.append(f"URL not reachable: {msg_u}")

    with tempfile.TemporaryDirectory(prefix="arv_") as tmp:
        tdir = Path(tmp)
        dl = tdir / "download.bin"
        ok_d, msg_d = download_to_path(url, dl, max_download_bytes)
        if not ok_d:
            errs.append(f"download failed: {msg_d}")
            return errs

        ok_g, msg_g = check_gff3_id_parent(dl, SCAN_BYTES)
        if not ok_g:
            errs.append(f"GFF3 check: {msg_g}")

        ok_t, msg_t = run_tabix_pipeline(dl, tdir, "pipe")
        if not ok_t:
            errs.append(f"tabix pipeline: {msg_t}")

    return errs


def build_report(
    repo_root: Path,
    base_sha: str,
    head_sha: str,
    merge_base: str,
    schema: dict[str, Any],
    datasets_bin: str,
    max_download_bytes: int | None,
) -> tuple[str, bool]:
    repo = str(repo_root)
    lines_out: list[str] = []
    lines_out.append(COMMENT_MARKER)
    lines_out.append("### Annotrieve registry validation")
    lines_out.append("")

    changed = git_changed_files(repo, merge_base, head_sha)
    projects = split_projects(changed)

    overall_ok = True
    manifest_errors: dict[str, list[str]] = {}

    # Validate manifests for touched projects
    for proj in sorted(projects):
        mpath = f"{proj}/manifest.yaml"
        raw = git_show_text(repo, head_sha, mpath)
        if raw is None:
            manifest_errors[proj] = [
                f"missing `{mpath}` on PR branch (required for every touched project)"
            ]
            overall_ok = False
            continue
        try:
            doc = yaml.safe_load(raw)
        except yaml.YAMLError as e:
            manifest_errors[proj] = [f"YAML parse error: {e}"]
            overall_ok = False
            continue
        if doc is None:
            manifest_errors[proj] = ["empty YAML document"]
            overall_ok = False
            continue
        merrs = validate_manifest_doc(doc, schema)
        if merrs:
            manifest_errors[proj] = merrs
            overall_ok = False

    lines_out.append("#### Manifest (JSON Schema)")
    lines_out.append("")
    if not manifest_errors:
        lines_out.append("- **OK** — no manifest issues for touched projects.")
    else:
        for proj, errs in sorted(manifest_errors.items()):
            lines_out.append(f"- **`{proj}`**")
            for e in errs:
                lines_out.append(f"  - {e}")
    lines_out.append("")

    # TSV: duplicates + new rows
    row_failures: list[tuple[str, str, str, list[str]]] = []
    dup_issue_paths: list[str] = []

    for proj in sorted(projects):
        apath = f"{proj}/annotations.tsv"
        if apath not in changed:
            continue
        head_raw = git_show_text(repo, head_sha, apath)
        if head_raw is None:
            lines_out.append(f"#### `{apath}`")
            lines_out.append("- missing on PR branch")
            overall_ok = False
            continue
        base_raw = git_show_text(repo, merge_base, apath)
        _, head_data_lines, herr = parse_tsv_data_lines(head_raw)
        if herr:
            lines_out.append(f"#### `{apath}`")
            lines_out.append(f"- **parse error**: {herr}")
            overall_ok = False
            continue

        dups = duplicate_accessions(head_data_lines)
        if dups:
            overall_ok = False
            dup_issue_paths.append(apath)
            lines_out.append(f"#### `{apath}` — duplicate assembly_accession")
            for d in dups:
                lines_out.append(f"- duplicated: `{d}`")
            lines_out.append("")

        new_lines, nerr = new_rows_from_diff(base_raw, head_raw)
        if nerr:
            overall_ok = False
            lines_out.append(f"#### `{apath}`")
            lines_out.append(f"- **diff error**: {nerr}")
            lines_out.append("")
            continue

        lines_out.append(f"#### `{apath}` — new rows ({len(new_lines)})")
        if not new_lines:
            lines_out.append("- no new data lines compared to merge-base.")
        lines_out.append("")

        for nl in new_lines:
            acc, url, _ = parse_row_line(nl)
            errs = validate_row(
                datasets_bin,
                nl,
                max_download_bytes,
            )
            if errs:
                overall_ok = False
                row_failures.append((apath, acc or "?", url or "?", errs))
            preview = nl.replace("|", "\\|")[:200]
            status = "PASS" if not errs else "FAIL"
            lines_out.append(f"- **{status}** `{acc}` — `{url}`")
            lines_out.append(f"  - line: `{preview}`")
            for e in errs:
                lines_out.append(f"  - {e}")
            lines_out.append("")

    lines_out.append("---")
    lines_out.append("")

    summary_parts = [
        f"Touched projects: **{len(projects)}**.",
        f"Manifest failures: **{len(manifest_errors)}**.",
        f"TSV files with duplicate `assembly_accession`: **{len(dup_issue_paths)}**.",
        f"New-row validation failures: **{len(row_failures)}**.",
    ]
    summary_block = [
        "#### Summary",
        "",
        "- " + " ".join(summary_parts),
        f"- merge-base: `{merge_base[:7]}…` — base `{base_sha[:7]}…` → head `{head_sha[:7]}…`",
        "",
    ]
    # Insert summary directly under the title block
    insert_at = 3
    for i, line in enumerate(summary_block):
        lines_out.insert(insert_at + i, line)

    return "\n".join(lines_out), overall_ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="Git repository root",
    )
    ap.add_argument("--base", required=True, help="Base commit SHA (e.g. PR base)")
    ap.add_argument("--head", required=True, help="Head commit SHA (e.g. PR head)")
    ap.add_argument(
        "--datasets-binary",
        default=os.environ.get("DATASETS_BINARY", "datasets"),
        help="Path to NCBI datasets CLI",
    )
    ap.add_argument(
        "--max-download-mb",
        type=int,
        default=None,
        help="Max download size per row in MiB (default from VALIDATE_MAX_DOWNLOAD_BYTES)",
    )
    ap.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write markdown report to this file (default: stdout only)",
    )
    args = ap.parse_args()

    repo_root = args.repo.resolve()
    if not (repo_root / ".git").exists():
        print("Not a git repository (missing .git)", file=sys.stderr)
        return 2

    ds_arg = args.datasets_binary
    if os.path.isfile(ds_arg):
        datasets_bin = os.path.abspath(ds_arg)
    elif shutil.which(ds_arg):
        datasets_bin = shutil.which(ds_arg)
    else:
        print(f"NCBI datasets CLI not found: {ds_arg}", file=sys.stderr)
        return 2

    max_bytes = DEFAULT_MAX_DOWNLOAD_BYTES
    if args.max_download_mb is not None:
        max_bytes = args.max_download_mb * 1024 * 1024

    merge_base = git_merge_base(str(repo_root), args.base, args.head)
    schema = load_schema(repo_root)

    report, ok = build_report(
        repo_root,
        args.base,
        args.head,
        merge_base,
        schema,
        datasets_bin,
        max_bytes,
    )

    print(report)
    if args.output:
        args.output.write_text(report, encoding="utf-8")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
