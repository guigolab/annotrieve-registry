#!/usr/bin/env python3
"""
Validate annotrieve-registry pull requests: manifests, new TSV rows, assemblies,
URLs, GFF3 shape (ID / Parent), tabix-compatible processing (Annotrieve-style),
duplicate access_url rows, duplicate annotation files (MD5), and collisions with
checksums/annotation_checksums.tsv on the base branch.

GitHub Actions runs this script inside a prebuilt container (see `docker/ci-validator/`
and `.github/workflows/publish-ci-validator.yml`): Python 3.11, `tabix`/`bgzip`, pinned
`datasets`, and `reviewdog` are on `PATH`; `DATASETS_BINARY` defaults to `datasets`.

Assembly accession validation uses the NCBI `datasets` CLI with --inputfile in batches
(no per-accession HTTP calls to NCBI). URL reachability and GFF3/tabix checks run
concurrently with a bounded thread pool. GFF3 validity is checked during the same
streaming download used for the tabix pipeline (single HTTP connection per file).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import zlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
import yaml
from jsonschema import Draft202012Validator
from jsonschema import FormatChecker

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from registry_checksums import (  # noqa: E402
    CHECKSUM_INDEX_REL,
    ChecksumIndexEntry,
    find_index_md5_collisions,
    format_index_collision,
    index_by_md5,
    parse_checksum_index,
)

# ──────────────────────────────────────────────────────────────────────────────
# Configuration (all overrideable via environment variables)
# ──────────────────────────────────────────────────────────────────────────────

REQUIRED_TSV_HEADER = "assembly_accession\taccess_url"
ASSEMBLY_RE = re.compile(r"^(GCA|GCF)_\d+\.\d+$")
COMMENT_MARKER = "<!-- annotrieve-registry-validation -->"

# Streaming GFF3 scan limit (decompressed bytes)
SCAN_BYTES = int(os.environ.get("VALIDATE_SCAN_BYTES", str(50 * 1024 * 1024)))
# Max total download size per annotation file
DEFAULT_MAX_DOWNLOAD_BYTES = int(
    os.environ.get("VALIDATE_MAX_DOWNLOAD_BYTES", str(500 * 1024 * 1024))
)
HTTP_TIMEOUT = int(os.environ.get("VALIDATE_HTTP_TIMEOUT", "120"))

# NCBI datasets CLI — batch size ≤ 2000 keeps the call fast and well within limits
DATASETS_BINARY = os.environ.get("DATASETS_BINARY", "datasets")
DATASETS_BATCH_SIZE = max(
    1, int(os.environ.get("VALIDATE_DATASETS_BATCH_SIZE", "2000"))
)
DATASETS_TIMEOUT = int(os.environ.get("VALIDATE_DATASETS_TIMEOUT", "300"))

# Thread pool for GFF/tabix downloads (NCBI assembly uses CLI; URL check is implicit in download)
DOWNLOAD_VALIDATE_WORKERS = max(
    1, int(os.environ.get("VALIDATE_DOWNLOAD_WORKERS", "3"))
)

USER_AGENT = os.environ.get(
    "VALIDATE_HTTP_USER_AGENT",
    "annotrieve-registry-validator/1.0 (+https://github.com)",
)

# Retries on 429 / 503 for URL HEAD and GFF file downloads
_HTTP_RETRY_TOTAL = max(2, int(os.environ.get("VALIDATE_HTTP_RETRY_TOTAL", "6")))
_HTTP_RETRY_BACKOFF = float(os.environ.get("VALIDATE_HTTP_RETRY_BACKOFF", "2"))
_HTTP_RETRY_STATUS: tuple[int, ...] = tuple(
    int(x.strip())
    for x in os.environ.get("VALIDATE_HTTP_RETRY_STATUS", "429,503").split(",")
    if x.strip().isdigit()
) or (429, 503)


# ──────────────────────────────────────────────────────────────────────────────
# HTTP session factory (URL HEAD + GFF downloads only; NOT used for NCBI assembly)
# ──────────────────────────────────────────────────────────────────────────────

def build_http_session() -> requests.Session:
    """Session with retry-on-429/503 and a connection pool sized to download workers."""
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    retry = Retry(
        total=_HTTP_RETRY_TOTAL,
        backoff_factor=_HTTP_RETRY_BACKOFF,
        status_forcelist=_HTTP_RETRY_STATUS,
        allowed_methods=("GET",),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    pool = DOWNLOAD_VALIDATE_WORKERS + 4
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=pool,
        pool_maxsize=pool,
    )
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


_worker_tls = threading.local()


def worker_http_session() -> requests.Session:
    """
    One Session per thread-pool worker thread (requests.Session is not thread-safe).
    All workers share the same Retry / User-Agent settings via build_http_session().
    """
    sess = getattr(_worker_tls, "http_sess", None)
    if sess is None:
        sess = build_http_session()
        _worker_tls.http_sess = sess
    return sess


# ──────────────────────────────────────────────────────────────────────────────
# Git helpers
# ──────────────────────────────────────────────────────────────────────────────

def run_git(repo: str, *args: str) -> str:
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr or r.stdout}")
    return r.stdout


def git_merge_base(repo: str, base_sha: str, head_sha: str) -> str:
    return run_git(repo, "merge-base", base_sha, head_sha).strip()


def git_changed_files(repo: str, merge_base: str, head_sha: str) -> list[str]:
    out = run_git(repo, "diff", "--name-only", merge_base, head_sha)
    return [p.strip() for p in out.splitlines() if p.strip()]


def git_show_text(repo: str, rev: str, path: str) -> str | None:
    r = subprocess.run(
        ["git", "-C", repo, "show", f"{rev}:{path}"], capture_output=True
    )
    return r.stdout.decode("utf-8", errors="replace") if r.returncode == 0 else None


# ──────────────────────────────────────────────────────────────────────────────
# reviewdog rdjsonl helpers (filter-mode=added in CI)
# ──────────────────────────────────────────────────────────────────────────────

def line_numbers_matching_row(head_raw: str, row: str) -> list[int]:
    target = row.strip()
    return [i for i, ln in enumerate(head_raw.splitlines(), 1) if ln.strip() == target]


def iter_data_line_numbers(head_raw: str) -> list[tuple[int, str]]:
    out = []
    for i, ln in enumerate(head_raw.splitlines()[1:], 2):
        if ln.strip() and not ln.strip().startswith("#"):
            out.append((i, ln))
    return out


def emit_diagnostic(
    bucket: list[dict[str, Any]],
    path: str,
    line: int,
    message: str,
    severity: str = "ERROR",
) -> None:
    """One reviewdog rdjsonl diagnostic (line is 1-based)."""
    bucket.append({
        "message": message.strip(),
        "location": {"path": path, "range": {"start": {"line": max(1, line)}}},
        "severity": severity,
    })


def _line_from_preferred(preferred: list[int], fallback: int = 1) -> int:
    return preferred[0] if preferred else fallback


def _fmt_body(title: str, bullets: list[str]) -> str:
    return "\n".join([f"**{title}**", "", *[f"- {b}" for b in bullets]])


# ──────────────────────────────────────────────────────────────────────────────
# Schema / manifest
# ──────────────────────────────────────────────────────────────────────────────

def load_schema(repo_root: Path) -> dict[str, Any]:
    with open(repo_root / "schema" / "manifest.schema.json", encoding="utf-8") as f:
        return json.load(f)


def validate_manifest_doc(doc: Any, schema: dict[str, Any]) -> list[str]:
    v = Draft202012Validator(schema, format_checker=FormatChecker())
    return [
        f"{'/'.join(str(p) for p in e.path) or '.'}: {e.message}"
        for e in v.iter_errors(doc)
    ]


# ──────────────────────────────────────────────────────────────────────────────
# NCBI assembly validation via `datasets` CLI (batch, not per-accession HTTP)
# ──────────────────────────────────────────────────────────────────────────────

def bulk_assembly_lookup_datasets(
    datasets_bin: str,
    accessions: list[str],
    batch_size: int = DATASETS_BATCH_SIZE,
) -> dict[str, tuple[bool, str]]:
    """
    Validate assembly accessions using:
        datasets summary genome accession --inputfile <file> --as-json-lines

    Batches of `batch_size` (≤ 2000) to stay within NCBI's rate limits.
    Returns {accession: (exists, error_message)}.
    """
    unique = sorted(set(accessions))
    if not unique:
        return {}

    results: dict[str, tuple[bool, str]] = {}

    for i in range(0, len(unique), batch_size):
        batch = unique[i : i + batch_size]
        batch_set = set(batch)

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", prefix="arv_acc_", delete=False
        ) as fh:
            fh.writelines(acc + "\n" for acc in batch)
            acc_file = fh.name

        try:
            r = subprocess.run(
                [
                    datasets_bin,
                    "summary",
                    "genome",
                    "accession",
                    "--inputfile",
                    acc_file,
                    "--as-json-lines",
                ],
                capture_output=True,
                text=True,
                timeout=DATASETS_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            for acc in batch:
                results[acc] = (False, f"datasets CLI timed out after {DATASETS_TIMEOUT}s")
            continue
        except FileNotFoundError:
            for acc in batch:
                results[acc] = (
                    False,
                    f"`{datasets_bin}` not found — install the NCBI datasets CLI",
                )
            continue
        finally:
            Path(acc_file).unlink(missing_ok=True)

        found: set[str] = set()
        if r.returncode == 0 and r.stdout.strip():
            for line in r.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    # datasets --as-json-lines puts "accession" at top level
                    acc_out = obj.get("accession") or obj.get("assembly_accession", "")
                    if acc_out and acc_out in batch_set:
                        found.add(acc_out)
                except json.JSONDecodeError:
                    # Fallback: substring match (handles schema changes across CLI versions)
                    for acc in batch_set:
                        if acc in line:
                            found.add(acc)
        elif r.returncode != 0:
            err = (r.stderr or r.stdout or "non-zero exit")[:300].strip()
            for acc in batch:
                results[acc] = (False, f"datasets CLI error: {err}")
            continue

        for acc in batch:
            results[acc] = (
                (True, "") if acc in found
                else (False, "assembly accession not found in NCBI")
            )

    return results


# ──────────────────────────────────────────────────────────────────────────────
# GFF3 streaming helpers
# ──────────────────────────────────────────────────────────────────────────────

def _gff3_result(has_id: bool, has_parent: bool) -> tuple[bool, str]:
    if has_id and has_parent:
        return True, ""
    if not has_id and not has_parent:
        return False, "no ID= or Parent= found in GFF3 attributes in scanned region"
    if not has_id:
        return False, "no ID= found in GFF3 attributes in scanned region"
    return False, "no Parent= found in GFF3 attributes in scanned region"


def download_check_gff3_stream(
    session: requests.Session,
    url: str,
    dest: Path,
    max_bytes: int | None,
    scan_bytes: int,
) -> tuple[bool, str, bool, str, str | None]:
    """
    Stream-download `url` to `dest` while scanning the first `scan_bytes`
    (decompressed) for GFF3 validity (ID= and Parent= in column 9).

    Returns (download_ok, download_err, gff3_ok, gff3_err, file_md5_hex).
    MD5 is computed over the raw downloaded bytes (same on-disk representation).

    Single HTTP connection: writes every byte to disk AND decompresses on-the-fly
    for GFF3 scanning. No second download needed for tabix.
    """
    has_id = False
    has_parent = False
    gff3_decided = False
    bytes_scanned = 0
    leftover = b""
    decomp: zlib.Decompress | None = None
    magic: bytes = b""
    file_md5 = hashlib.md5()

    try:
        with session.get(
            url, stream=True, allow_redirects=True, timeout=HTTP_TIMEOUT
        ) as r:
            if r.status_code >= 400:
                return False, f"HTTP {r.status_code}", False, "", None
            n = 0
            with open(dest, "wb") as out:
                for chunk in r.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    n += len(chunk)
                    if max_bytes is not None and n > max_bytes:
                        return (
                            False,
                            f"download exceeded {max_bytes} bytes",
                            False,
                            "",
                            None,
                        )
                    file_md5.update(chunk)
                    out.write(chunk)

                    if gff3_decided:
                        continue

                    # Detect gzip from magic bytes on the first chunk
                    if decomp is None and len(magic) < 2:
                        magic = (magic + chunk)[:2]
                        if (
                            len(magic) == 2
                            and magic[0] == 0x1F
                            and magic[1] == 0x8B
                        ):
                            decomp = zlib.decompressobj(
                                wbits=zlib.MAX_WBITS | 16
                            )

                    # Decompress chunk (or use raw for plain GFF3)
                    try:
                        raw = decomp.decompress(chunk) if decomp else chunk
                    except Exception:
                        raw = b""

                    leftover += raw
                    bytes_scanned += len(raw)

                    # Scan complete lines from leftover
                    while True:
                        nl = leftover.find(b"\n")
                        if nl == -1:
                            break
                        line_bytes, leftover = leftover[:nl], leftover[nl + 1 :]
                        ls = line_bytes.decode("utf-8", errors="replace").strip()
                        if not ls or ls.startswith("#"):
                            continue
                        cols = ls.split("\t")
                        if len(cols) >= 9:
                            attrs = cols[8]
                            if "ID=" in attrs:
                                has_id = True
                            if "Parent=" in attrs:
                                has_parent = True

                    if (has_id and has_parent) or bytes_scanned >= scan_bytes:
                        gff3_decided = True

        ok_g, msg_g = _gff3_result(has_id, has_parent)
        return True, "", ok_g, msg_g, file_md5.hexdigest()

    except requests.RequestException as e:
        return False, str(e), False, "", None
    except OSError as e:
        return False, str(e), False, "", None



# ──────────────────────────────────────────────────────────────────────────────
# Tabix pipeline
# ──────────────────────────────────────────────────────────────────────────────

def _is_gzip(path: Path) -> bool:
    with open(path, "rb") as f:
        sig = f.read(2)
    return len(sig) == 2 and sig[0] == 0x1F and sig[1] == 0x8B


def run_tabix_pipeline(in_path: Path, work: Path, label: str) -> tuple[bool, str]:
    """
    Annotrieve-compatible pipeline: comments first, then sort by seqid + start,
    bgzip, tabix -p gff --csi.
    """
    out_gz = work / f"{label}.gff.gz"
    csi = Path(str(out_gz) + ".csi")
    for p in (out_gz, csi):
        if p.exists():
            p.unlink()

    decomp = "zcat" if _is_gzip(in_path) else "cat"
    in_q, out_q = shlex.quote(str(in_path)), shlex.quote(str(out_gz))
    stream_cmd = (
        f"({decomp} {in_q} | grep '^#'; "
        f"{decomp} {in_q} | grep -v '^#'"
        f' | sort -t"$(printf \'\\t\')" -k1,1 -k4,4n) | bgzip > {out_q}'
    )
    r1 = subprocess.run(["bash", "-lc", stream_cmd], capture_output=True, text=True)
    if r1.returncode != 0:
        return False, (r1.stderr or r1.stdout or "bgzip pipeline failed")[:2000]
    if not out_gz.exists() or out_gz.stat().st_size == 0:
        return False, "bgzip output missing or empty"

    r2 = subprocess.run(
        ["bash", "-lc", f"tabix -p gff --csi {out_q}"],
        capture_output=True,
        text=True,
    )
    if r2.returncode != 0:
        return False, (r2.stderr or r2.stdout or "tabix failed")[:2000]
    if not csi.exists() or csi.stat().st_size == 0:
        return False, "CSI index missing or empty"
    return True, ""


# ──────────────────────────────────────────────────────────────────────────────
# Per-row heavy validation (download + GFF3 + tabix — runs in thread pool)
# ──────────────────────────────────────────────────────────────────────────────

def validate_row_heavy(
    acc: str,
    url: str,
    max_download_bytes: int | None,
) -> tuple[list[str], str | None]:
    """Download GFF3, check attributes while streaming, then run tabix pipeline."""
    with tempfile.TemporaryDirectory(prefix="arv_h_") as tmp:
        tdir = Path(tmp)
        dl = tdir / "download.bin"
        ok_d, msg_d, ok_g, msg_g, file_md5 = download_check_gff3_stream(
            worker_http_session(), url, dl, max_download_bytes, SCAN_BYTES
        )
        if not ok_d:
            return [f"download failed: {msg_d}"], None
        if not ok_g:
            # Don't run tabix if content isn't valid GFF3 — saves CPU and I/O
            return [f"GFF3 check: {msg_g}"], file_md5

        ok_t, msg_t = run_tabix_pipeline(dl, tdir, "pipe")
        errs = [f"tabix pipeline: {msg_t}"] if not ok_t else []
        return errs, file_md5


# ──────────────────────────────────────────────────────────────────────────────
# TSV parsing helpers
# ──────────────────────────────────────────────────────────────────────────────

def parse_tsv(content: str | None) -> tuple[list[str] | None, list[str], str | None]:
    """Return (header_cols, data_lines, error)."""
    if content is None:
        return [], [], None
    lines = [ln.rstrip("\n\r") for ln in content.splitlines()]
    if not lines:
        return [], [], "empty file"
    if lines[0].strip().split("\t") != REQUIRED_TSV_HEADER.split("\t"):
        return None, [], f"invalid header; expected: '{REQUIRED_TSV_HEADER}'"
    data = [
        ln.strip("\n\r")
        for ln in lines[1:]
        if ln.strip() and not ln.strip().startswith("#")
    ]
    return lines[0].split("\t"), data, None


def new_rows(base: str | None, head: str) -> tuple[list[str], str | None]:
    _, base_lines, err = parse_tsv(base)
    if err:
        return [], err
    _, head_lines, err_h = parse_tsv(head)
    if err_h:
        return [], err_h
    base_set = set(base_lines)
    return [ln for ln in head_lines if ln not in base_set], None


def parse_row(line: str) -> tuple[str | None, str | None, str | None]:
    """Strict TSV: exactly 2 tab-separated columns."""
    raw = line.strip()
    if not raw:
        return None, None, "empty line"
    parts = raw.split("\t")
    if len(parts) != 2:
        return None, None, f"expected 2 tab-separated columns, got {len(parts)}"
    acc, url = parts[0].strip(), parts[1].strip()
    if not acc or not url:
        return None, None, "empty assembly_accession or access_url"
    return acc, url, None


def split_projects(paths: list[str]) -> set[str]:
    projects: set[str] = set()
    for p in paths:
        pl = Path(p)
        if pl.name in ("annotations.tsv", "manifest.yaml"):
            projects.add(str(pl.parent.as_posix()))
    return projects


def duplicate_accessions(data_lines: list[str]) -> list[str]:
    accs = [acc for ln in data_lines for acc, _, err in [parse_row(ln)] if not err and acc]
    return [a for a, c in Counter(accs).items() if c > 1]


def duplicate_urls(data_lines: list[str]) -> list[str]:
    urls = [url for ln in data_lines for _, url, err in [parse_row(ln)] if not err and url]
    return [u for u, c in Counter(urls).items() if c > 1]


def duplicate_file_md5s(md5_by_row: dict[tuple[str, str], str]) -> list[str]:
    """MD5 hex digests that appear on more than one validated row."""
    return [m for m, c in Counter(md5_by_row.values()).items() if c > 1]


def classify_cheap(
    acc: str | None,
    url: str | None,
    perr: str | None,
    asm_cache: dict[str, tuple[bool, str]],
) -> tuple[list[str], bool]:
    """
    Fast pre-checks (parse error, accession format, NCBI existence, URL scheme).
    URL reachability is verified implicitly during the streaming download.
    Returns (errors, needs_heavy).
    """
    if perr:
        return [perr], False
    if not acc or not url:
        return ["empty assembly_accession or access_url"], False
    if not ASSEMBLY_RE.match(acc):
        return [f"assembly_accession format invalid (need GCA_/GCF_…): {acc!r}"], False

    ok_a, msg_a = asm_cache.get(acc, (False, "not checked"))
    if not ok_a:
        return [f"NCBI assembly check failed: {msg_a}"], False

    try:
        if urlparse(url).scheme not in ("http", "https"):
            return [f"URL must be http(s): {url!r}"], False
    except Exception as e:
        return [f"URL parse error: {e}"], False

    return [], True


# ──────────────────────────────────────────────────────────────────────────────
# Validation output
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ValidationOutput:
    ok: bool
    summary_markdown: str
    diagnostics: list[dict[str, Any]] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────────
# Main validation orchestrator
# ──────────────────────────────────────────────────────────────────────────────

def _repo_path_from_apath(apath: str) -> str:
    parent = Path(apath).parent.as_posix()
    return parent if parent != "." else "."


def run_validation(
    repo_root: Path,
    base_sha: str,
    head_sha: str,
    merge_base: str,
    schema: dict[str, Any],
    datasets_bin: str,
    max_download_bytes: int | None,
    checksum_index_rel: str = CHECKSUM_INDEX_REL,
) -> ValidationOutput:
    repo = str(repo_root)
    changed = git_changed_files(repo, merge_base, head_sha)
    projects = split_projects(changed)

    overall_ok = True
    manifest_errors: dict[str, list[str]] = {}
    diagnostics: list[dict[str, Any]] = []
    valid_rows = 0
    invalid_rows = 0
    tsv_parse_errors = 0
    dup_paths: list[str] = []
    dup_url_paths: list[str] = []
    dup_file_paths: list[str] = []
    dup_index_paths: list[str] = []

    index_entries: list[ChecksumIndexEntry] = []
    index_by_md5_map: dict[str, list[ChecksumIndexEntry]] = {}
    index_raw = git_show_text(repo, base_sha, checksum_index_rel)
    if index_raw is not None:
        index_entries, index_err = parse_checksum_index(index_raw)
        if index_err:
            overall_ok = False
            emit_diagnostic(
                diagnostics,
                checksum_index_rel,
                1,
                _fmt_body("annotation_checksums.tsv parse error", [index_err]),
            )
        else:
            index_by_md5_map = index_by_md5(index_entries)

    # ── 1. Manifest validation ────────────────────────────────────────────────
    for proj in sorted(projects):
        mpath = f"{proj}/manifest.yaml"
        raw = git_show_text(repo, head_sha, mpath)
        if raw is None:
            manifest_errors[proj] = [
                f"missing `{mpath}` (required for every touched project)"
            ]
            overall_ok = False
            continue
        try:
            doc = yaml.safe_load(raw)
        except yaml.YAMLError as e:
            manifest_errors[proj] = [f"YAML parse error: {e}"]
            overall_ok = False
            emit_diagnostic(
                diagnostics,
                mpath,
                1,
                _fmt_body("YAML parse error", [str(e)]),
            )
            continue
        if doc is None:
            manifest_errors[proj] = ["empty YAML document"]
            overall_ok = False
            emit_diagnostic(
                diagnostics,
                mpath,
                1,
                _fmt_body("Empty manifest", ["manifest.yaml is empty or null."]),
            )
            continue
        merrs = validate_manifest_doc(doc, schema)
        if merrs:
            manifest_errors[proj] = merrs
            overall_ok = False
            emit_diagnostic(
                diagnostics,
                mpath,
                1,
                _fmt_body("manifest.yaml (JSON Schema)", merrs),
            )

    # ── 2. Collect new TSV rows across all projects ───────────────────────────
    all_jobs: list[dict[str, Any]] = []

    for proj in sorted(projects):
        apath = f"{proj}/annotations.tsv"
        if apath not in changed:
            continue
        head_raw = git_show_text(repo, head_sha, apath)
        if head_raw is None:
            overall_ok = False
            continue
        base_raw = git_show_text(repo, merge_base, apath)

        _, head_data, herr = parse_tsv(head_raw)
        if herr:
            overall_ok = False
            tsv_parse_errors += 1
            emit_diagnostic(
                diagnostics,
                apath,
                1,
                _fmt_body("annotations.tsv header / parse", [herr]),
            )
            continue

        dups = duplicate_accessions(head_data)
        if dups:
            overall_ok = False
            dup_paths.append(apath)
            dup_set = set(dups)
            for line_no, ln in iter_data_line_numbers(head_raw):
                acc, _, _ = parse_row(ln)
                if acc and acc in dup_set:
                    emit_diagnostic(
                        diagnostics,
                        apath,
                        line_no,
                        _fmt_body("Duplicate assembly_accession", [
                            f"`{acc}` appears more than once; keep at most one row per assembly."
                        ]),
                    )

        url_dups = duplicate_urls(head_data)
        if url_dups:
            overall_ok = False
            dup_url_paths.append(apath)
            url_dup_set = set(url_dups)
            for line_no, ln in iter_data_line_numbers(head_raw):
                _, url, _ = parse_row(ln)
                if url and url in url_dup_set:
                    emit_diagnostic(
                        diagnostics,
                        apath,
                        line_no,
                        _fmt_body("Duplicate access_url", [
                            f"`{url}` appears more than once; each row must use a distinct URL."
                        ]),
                    )

        nr, nerr = new_rows(base_raw, head_raw)
        if nerr:
            overall_ok = False
            tsv_parse_errors += 1
            emit_diagnostic(
                diagnostics,
                apath,
                2,
                _fmt_body("Could not diff rows", [nerr]),
            )
            continue

        for nl in nr:
            all_jobs.append({
                "apath": apath,
                "nl": nl,
                "preferred": line_numbers_matching_row(head_raw, nl),
            })

    # ── 3. Parse rows and populate caches ────────────────────────────────────
    row_results: dict[tuple[str, str], list[str]] = {}

    if all_jobs:
        for job in all_jobs:
            acc, url, perr = parse_row(job["nl"])
            job["acc"] = acc
            job["url"] = url
            job["perr"] = perr

        # 3a. Bulk assembly lookup via datasets CLI (batched, no NCBI HTTP)
        valid_accs = sorted({
            j["acc"]
            for j in all_jobs
            if not j["perr"] and j["acc"] and ASSEMBLY_RE.match(j["acc"])
        })
        asm_cache = bulk_assembly_lookup_datasets(
            datasets_bin, valid_accs, DATASETS_BATCH_SIZE
        )

        # 3b. Split into cheap-fail vs heavy (download + GFF3 + tabix)
        # URL reachability is checked implicitly at the start of the streaming download.
        heavy_jobs: list[dict[str, Any]] = []
        for j in all_jobs:
            errs, need_heavy = classify_cheap(
                j["acc"], j["url"], j["perr"], asm_cache
            )
            key = (j["apath"], j["nl"])
            if not need_heavy:
                row_results[key] = errs
            else:
                heavy_jobs.append(j)

        # 3c. Heavy validation (parallel, bounded)
        md5_by_row: dict[tuple[str, str], str] = {}
        if heavy_jobs:
            w = min(DOWNLOAD_VALIDATE_WORKERS, len(heavy_jobs))
            with ThreadPoolExecutor(max_workers=w) as ex:
                fut_map = {
                    ex.submit(validate_row_heavy, j["acc"], j["url"], max_download_bytes): j
                    for j in heavy_jobs
                }
                for fut in as_completed(fut_map):
                    job = fut_map[fut]
                    key = (job["apath"], job["nl"])
                    try:
                        errs, file_md5 = fut.result()
                        row_results[key] = errs
                        if file_md5:
                            md5_by_row[key] = file_md5
                    except Exception as e:
                        row_results[key] = [f"internal error: {e}"]

            # 3d. Duplicate annotation files (MD5 among new rows in the same TSV)
            by_apath: dict[str, dict[str, list[dict[str, Any]]]] = {}
            for j in heavy_jobs:
                key = (j["apath"], j["nl"])
                md5 = md5_by_row.get(key)
                if not md5:
                    continue
                by_apath.setdefault(j["apath"], {}).setdefault(md5, []).append(j)

            for apath, md5_map in by_apath.items():
                apath_md5_by_row = {
                    (j["apath"], j["nl"]): md5
                    for md5, jobs in md5_map.items()
                    for j in jobs
                }
                dup_set = set(duplicate_file_md5s(apath_md5_by_row))
                if not dup_set:
                    continue
                overall_ok = False
                dup_file_paths.append(apath)
                for md5 in dup_set:
                    for j in md5_map[md5]:
                        ln = _line_from_preferred(j["preferred"], 1)
                        emit_diagnostic(
                            diagnostics,
                            apath,
                            ln,
                            _fmt_body("Duplicate annotation file (MD5)", [
                                f"checksum `{md5}` appears more than once; "
                                "each row must refer to a distinct annotation file.",
                            ]),
                        )

            # 3e. Duplicate file vs repo-wide checksum index (merged rows on base branch)
            if index_by_md5_map:
                for j in heavy_jobs:
                    key = (j["apath"], j["nl"])
                    md5 = md5_by_row.get(key)
                    acc, url = j.get("acc"), j.get("url")
                    if not md5 or not acc or not url:
                        continue
                    repo_path = _repo_path_from_apath(j["apath"])
                    collisions = find_index_md5_collisions(
                        md5, repo_path, acc, url, index_by_md5_map
                    )
                    if not collisions:
                        continue
                    overall_ok = False
                    if j["apath"] not in dup_index_paths:
                        dup_index_paths.append(j["apath"])
                    ln = _line_from_preferred(j["preferred"], 1)
                    bullets = [format_index_collision(c) for c in collisions[:5]]
                    if len(collisions) > 5:
                        bullets.append(f"…and {len(collisions) - 5} more registered row(s).")
                    emit_diagnostic(
                        diagnostics,
                        j["apath"],
                        ln,
                        _fmt_body("Duplicate annotation file (MD5, registry index)", [
                            f"checksum `{md5}` matches an already merged entry:",
                            *bullets,
                        ]),
                    )

    # ── 4. Emit inline comments and count pass/fail ───────────────────────────
    for j in all_jobs:
        key = (j["apath"], j["nl"])
        errs = row_results[key]
        acc, perr = j["acc"], j["perr"]
        if errs:
            overall_ok = False
            invalid_rows += 1
            if perr:
                title, bullets = "Could not parse columns", [
                    "Expected `assembly_accession<TAB>access_url`.", *errs
                ]
            else:
                title, bullets = f"`{acc}`" if acc else "Row validation", errs
            ln = _line_from_preferred(j["preferred"], 1)
            emit_diagnostic(
                diagnostics,
                j["apath"],
                ln,
                _fmt_body(title, bullets),
            )
        else:
            valid_rows += 1

    # ── 5. Build summary comment ──────────────────────────────────────────────
    summary_md = "\n".join([
        COMMENT_MARKER,
        "### Registry validation",
        "",
        f"- New rows: **{valid_rows}** valid, **{invalid_rows}** invalid",
        f"- Manifest issues: **{len(manifest_errors)}**",
        f"- Duplicate assemblies (TSV files): **{len(dup_paths)}**",
        f"- Duplicate URLs (TSV files): **{len(dup_url_paths)}**",
        f"- Duplicate annotation files (MD5, TSV files): **{len(dup_file_paths)}**",
        f"- Duplicate files vs registry index: **{len(dup_index_paths)}**",
        f"- TSV header/parse errors: **{tsv_parse_errors}**",
        "",
        "_Inline annotations on each failing row — open **Files changed** for details._",
        "_Updated on every push._",
    ])

    return ValidationOutput(
        ok=overall_ok,
        summary_markdown=summary_md,
        diagnostics=diagnostics,
    )


# ──────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ──────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", type=Path, default=Path.cwd())
    ap.add_argument("--base", required=True, help="Base commit SHA")
    ap.add_argument("--head", required=True, help="Head commit SHA")
    ap.add_argument(
        "--datasets-binary",
        default=os.environ.get("DATASETS_BINARY", DATASETS_BINARY),
        help="Path / name of the NCBI datasets CLI binary",
    )
    ap.add_argument("--max-download-mb", type=int, default=None)
    ap.add_argument(
        "--checksum-index",
        default=CHECKSUM_INDEX_REL,
        help="Repo-relative path to the MD5 checksum index TSV on the base branch",
    )
    ap.add_argument("--output-summary", type=Path, default=None)
    ap.add_argument(
        "--output-rdjsonl",
        type=Path,
        default=None,
        help="reviewdog rdjsonl (one JSON object per line)",
    )
    args = ap.parse_args()

    repo_root = args.repo.resolve()
    if not (repo_root / ".git").exists():
        print("Not a git repository", file=sys.stderr)
        return 2

    ds_bin = args.datasets_binary
    resolved = shutil.which(ds_bin) or (ds_bin if Path(ds_bin).is_file() else None)
    if resolved is None:
        print(
            f"NCBI datasets CLI not found: {ds_bin!r}\n"
            "Install it from https://www.ncbi.nlm.nih.gov/datasets/docs/v2/download-and-install/",
            file=sys.stderr,
        )
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
        resolved,
        max_bytes,
        args.checksum_index,
    )

    if args.output_summary:
        args.output_summary.write_text(result.summary_markdown, encoding="utf-8")
    else:
        print(result.summary_markdown)
    if args.output_rdjsonl:
        lines = [json.dumps(d, ensure_ascii=False) for d in result.diagnostics]
        args.output_rdjsonl.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
