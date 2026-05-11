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
from dataclasses import dataclass, field
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


def git_diff_paths(repo: str, merge_base: str, head_sha: str, path: str) -> str:
    """Unified diff for one path (may be empty)."""
    r = subprocess.run(
        ["git", "-C", repo, "diff", merge_base, head_sha, "--", path],
        capture_output=True,
        text=True,
    )
    return r.stdout if r.returncode == 0 else ""


def git_added_line_numbers_right(
    repo: str, merge_base: str, head_sha: str, path: str
) -> set[int]:
    """
    1-based line numbers in `path` at head that appear as '+' additions in the
    diff vs merge_base (GitHub PR inline comments must target changed lines).
    """
    diff = git_diff_paths(repo, merge_base, head_sha, path)
    return parse_unified_diff_added_lines(diff)


def parse_unified_diff_added_lines(diff_text: str) -> set[int]:
    """Collect new-file line numbers for '+' rows in a single-file git diff."""
    added: set[int] = set()
    line_new: int | None = None
    for line in diff_text.splitlines():
        if line.startswith("@@"):
            m = re.match(
                r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@",
                line,
            )
            if not m:
                continue
            line_new = int(m.group(1))
            continue
        if line_new is None:
            continue
        if line.startswith("+++ ") or line.startswith("--- "):
            continue
        if not line:
            continue
        prefix = line[0]
        if prefix == "+":
            added.add(line_new)
            line_new += 1
        elif prefix == " ":
            line_new += 1
        elif prefix == "-":
            pass
        elif prefix == "\\":
            pass
    return added


def line_numbers_matching_row(head_raw: str, row_content: str) -> list[int]:
    """All 1-based lines whose stripped text equals row_content.strip()."""
    target = row_content.strip()
    return [
        i
        for i, ln in enumerate(head_raw.splitlines(), start=1)
        if ln.strip() == target
    ]


def iter_tsv_data_line_numbers(head_raw: str) -> list[tuple[int, str]]:
    """Skip header (line 1); yield (line_no, raw line) for each data row."""
    lines = head_raw.splitlines()
    out: list[tuple[int, str]] = []
    for i, ln in enumerate(lines[1:], start=2):
        if not ln.strip() or ln.strip().startswith("#"):
            continue
        out.append((i, ln))
    return out


def pick_inline_line(
    commentable: set[int], preferred_lines: list[int]
) -> int | None:
    """First preferred line that appears in the PR diff additions, else None."""
    for ln in preferred_lines:
        if ln in commentable:
            return ln
    return None


def append_inline_review(
    bucket: list[dict[str, Any]],
    path: str,
    commentable: set[int],
    preferred_lines: list[int],
    body: str,
) -> None:
    """Attach one PR review comment if a suitable line exists in the diff."""
    line_no = pick_inline_line(commentable, preferred_lines)
    if line_no is None:
        return
    bucket.append({"path": path, "line": line_no, "body": body.strip()})


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


def validate_parsed_row(
    datasets_bin: str,
    acc: str,
    url: str,
    max_download_bytes: int | None,
) -> list[str]:
    """Run checks after assembly_accession and access_url have been parsed."""
    errs: list[str] = []
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


def validate_row(
    datasets_bin: str,
    line: str,
    max_download_bytes: int | None,
) -> list[str]:
    acc, url, perr = parse_row_line(line)
    if perr:
        return [perr]
    return validate_parsed_row(datasets_bin, acc, url, max_download_bytes)


@dataclass
class ValidationOutput:
    ok: bool
    summary_markdown: str
    inline_comments: list[dict[str, Any]] = field(default_factory=list)


def _fmt_inline_body(title: str, bullets: list[str]) -> str:
    lines = [f"**{title}**", ""]
    for b in bullets:
        lines.append(f"- {b}")
    return "\n".join(lines)


def run_validation(
    repo_root: Path,
    base_sha: str,
    head_sha: str,
    merge_base: str,
    schema: dict[str, Any],
    datasets_bin: str,
    max_download_bytes: int | None,
) -> ValidationOutput:
    repo = str(repo_root)
    changed = git_changed_files(repo, merge_base, head_sha)
    projects = split_projects(changed)

    overall_ok = True
    manifest_errors: dict[str, list[str]] = {}
    inline_comments: list[dict[str, Any]] = []

    valid_new_rows = 0
    invalid_new_rows = 0
    tsv_file_parse_errors = 0

    # --- Manifests
    for proj in sorted(projects):
        mpath = f"{proj}/manifest.yaml"
        raw = git_show_text(repo, head_sha, mpath)
        if raw is None:
            manifest_errors[proj] = [
                f"missing `{mpath}` on PR branch (required for every touched project)"
            ]
            overall_ok = False
            continue
        m_commentable = git_added_line_numbers_right(repo, merge_base, head_sha, mpath)
        try:
            doc = yaml.safe_load(raw)
        except yaml.YAMLError as e:
            manifest_errors[proj] = [f"YAML parse error: {e}"]
            overall_ok = False
            preferred = list(range(1, min(5, len(raw.splitlines()) + 1)))
            append_inline_review(
                inline_comments,
                mpath,
                m_commentable,
                preferred,
                _fmt_inline_body("YAML parse error", [str(e)]),
            )
            continue
        if doc is None:
            manifest_errors[proj] = ["empty YAML document"]
            overall_ok = False
            append_inline_review(
                inline_comments,
                mpath,
                m_commentable,
                [1],
                _fmt_inline_body("Empty manifest", ["manifest.yaml is empty or null."]),
            )
            continue
        merrs = validate_manifest_doc(doc, schema)
        if merrs:
            manifest_errors[proj] = merrs
            overall_ok = False
            append_inline_review(
                inline_comments,
                mpath,
                m_commentable,
                [1],
                _fmt_inline_body("manifest.yaml (JSON Schema)", merrs),
            )

    # --- annotations.tsv per project
    dup_issue_paths: list[str] = []

    for proj in sorted(projects):
        apath = f"{proj}/annotations.tsv"
        if apath not in changed:
            continue
        a_commentable = git_added_line_numbers_right(repo, merge_base, head_sha, apath)

        head_raw = git_show_text(repo, head_sha, apath)
        if head_raw is None:
            overall_ok = False
            continue
        base_raw = git_show_text(repo, merge_base, apath)
        _, head_data_lines, herr = parse_tsv_data_lines(head_raw)
        if herr:
            overall_ok = False
            tsv_file_parse_errors += 1
            append_inline_review(
                inline_comments,
                apath,
                a_commentable,
                [1],
                _fmt_inline_body("annotations.tsv header / parse", [herr]),
            )
            continue

        dups = duplicate_accessions(head_data_lines)
        dup_set = set(dups)
        if dups:
            overall_ok = False
            dup_issue_paths.append(apath)
            for line_no, ln in iter_tsv_data_line_numbers(head_raw):
                acc, _, perr = parse_row_line(ln)
                if perr or not acc:
                    continue
                if acc in dup_set:
                    append_inline_review(
                        inline_comments,
                        apath,
                        a_commentable,
                        [line_no],
                        _fmt_inline_body(
                            "Duplicate assembly_accession",
                            [
                                f"`{acc}` appears more than once in this file; "
                                "keep at most one row per assembly."
                            ],
                        ),
                    )

        new_lines, nerr = new_rows_from_diff(base_raw, head_raw)
        if nerr:
            overall_ok = False
            tsv_file_parse_errors += 1
            append_inline_review(
                inline_comments,
                apath,
                a_commentable,
                [2],
                _fmt_inline_body("Could not diff rows", [nerr]),
            )
            continue

        for nl in new_lines:
            preferred = line_numbers_matching_row(head_raw, nl)
            acc, url, perr = parse_row_line(nl)
            if perr:
                overall_ok = False
                invalid_new_rows += 1
                append_inline_review(
                    inline_comments,
                    apath,
                    a_commentable,
                    preferred,
                    _fmt_inline_body(
                        "Could not parse columns",
                        [
                            "Expected `assembly_accession` then `access_url` "
                            "(tab between columns).",
                            perr,
                        ],
                    ),
                )
                continue

            errs = validate_parsed_row(
                datasets_bin,
                acc,
                url,
                max_download_bytes,
            )
            if errs:
                overall_ok = False
                invalid_new_rows += 1
                append_inline_review(
                    inline_comments,
                    apath,
                    a_commentable,
                    preferred,
                    _fmt_inline_body(
                        f"`{acc}`",
                        errs,
                    ),
                )
            else:
                valid_new_rows += 1

    summary_lines = [
        COMMENT_MARKER,
        "### Registry validation summary",
        "",
        "| | Count |",
        "|--|--:|",
        f"| Valid **new** rows | **{valid_new_rows}** |",
        f"| Invalid **new** rows | **{invalid_new_rows}** |",
        f"| Projects with manifest issues | **{len(manifest_errors)}** |",
        f"| TSV files with duplicate assemblies | **{len(dup_issue_paths)}** |",
        f"| TSV files with header / diff parse errors | **{tsv_file_parse_errors}** |",
        "",
        f"Merge-base `{merge_base[:7]}…` · base `{base_sha[:7]}…` → head `{head_sha[:7]}…`",
        "",
        "**Details:** Open **Files changed** — inline review comments mark each issue on the affected line.",
        "",
        "_This summary comment is updated every validation run._",
    ]
    summary_md = "\n".join(summary_lines)

    return ValidationOutput(
        ok=overall_ok,
        summary_markdown=summary_md,
        inline_comments=inline_comments,
    )


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
        "--output-summary",
        type=Path,
        default=None,
        help="Write PR summary comment markdown (sticky summary)",
    )
    ap.add_argument(
        "--output-inline-json",
        type=Path,
        default=None,
        help="Write JSON array of {path, line, body} for pull request review comments",
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

    result = run_validation(
        repo_root,
        args.base,
        args.head,
        merge_base,
        schema,
        datasets_bin,
        max_bytes,
    )

    print(result.summary_markdown)
    if args.output_summary:
        args.output_summary.write_text(result.summary_markdown, encoding="utf-8")
    if args.output_inline_json:
        args.output_inline_json.write_text(
            json.dumps(result.inline_comments, indent=2),
            encoding="utf-8",
        )

    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
