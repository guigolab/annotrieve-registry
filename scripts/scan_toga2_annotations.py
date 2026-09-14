#!/usr/bin/env python3
"""
Scan the Hiller Lab TOGA2 HTTPS directory listings for new GCA/GCF annotations
and append missing rows to TOGA2/annotations.tsv.

Designed for a scheduled GitHub Action that treats TOGA2 as a trusted source
(no PR validator). The remote server is fragile, so all HTTP calls use retries,
exponential backoff with jitter, and a polite inter-request delay.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

DEFAULT_BASE_URL = (
    "https://genome.senckenberg.de/download/TOGA2/TOGA2integration/v1/"
)
DEFAULT_TSV = "TOGA2/annotations.tsv"
REQUIRED_TSV_HEADER = "assembly_accession\taccess_url"
GFF_FILENAME = "query_annotation.gff.gz"

ACCESSION_RE = re.compile(r"GC[AF]_\d+\.\d+")
HREF_DIR_RE = re.compile(r'href="([^"]+/)"', re.IGNORECASE)

USER_AGENT = os.environ.get(
    "TOGA2_SCAN_USER_AGENT",
    "annotrieve-registry-toga2-tracker/1.0 (+https://github.com/guigolab/annotrieve-registry)",
)

_HTTP_RETRY_TOTAL = max(2, int(os.environ.get("TOGA2_HTTP_RETRY_TOTAL", "6")))
_HTTP_RETRY_BACKOFF = float(os.environ.get("TOGA2_HTTP_RETRY_BACKOFF", "2"))
_HTTP_RETRY_STATUS: tuple[int, ...] = tuple(
    int(x.strip())
    for x in os.environ.get("TOGA2_HTTP_RETRY_STATUS", "429,503").split(",")
    if x.strip().isdigit()
) or (429, 503)

_REQUEST_ATTEMPTS = max(1, int(os.environ.get("TOGA2_REQUEST_ATTEMPTS", "5")))
_HTTP_TIMEOUT = int(os.environ.get("TOGA2_HTTP_TIMEOUT", "60"))
_POLITE_DELAY = float(os.environ.get("TOGA2_POLITE_DELAY", "0.3"))

_RETRYABLE_MSG_FRAGMENTS = (
    "incompleteread",
    "connection broken",
    "connection reset",
    "connection aborted",
    "timed out",
    "temporarily unavailable",
    "remote end closed",
    "server disconnected",
)


# ──────────────────────────────────────────────────────────────────────────────
# Data structures
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Candidate:
    assembly_accession: str
    clade: str
    folder: str
    access_url: str


@dataclass
class ScanReport:
    base_url: str
    existing_rows: int = 0
    clades_scanned: list[str] = field(default_factory=list)
    folders_seen: int = 0
    skipped_no_accession: list[str] = field(default_factory=list)
    already_tracked: list[str] = field(default_factory=list)
    url_mismatch: list[dict[str, str]] = field(default_factory=list)
    verify_failed: list[dict[str, str]] = field(default_factory=list)
    added: list[dict[str, str]] = field(default_factory=list)
    dry_run: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def added_count(self) -> int:
        return len(self.added)

    @property
    def skipped_count(self) -> int:
        """Already tracked + no-accession + verify-failed (not newly added)."""
        return (
            len(self.already_tracked)
            + len(self.skipped_no_accession)
            + len(self.verify_failed)
        )


# ──────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ──────────────────────────────────────────────────────────────────────────────

def build_http_session() -> requests.Session:
    """Session with retry-on-429/503 (mirrors validate_pr.py)."""
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    retry = Retry(
        total=_HTTP_RETRY_TOTAL,
        backoff_factor=_HTTP_RETRY_BACKOFF,
        status_forcelist=_HTTP_RETRY_STATUS,
        allowed_methods=("GET", "HEAD"),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=4,
        pool_maxsize=4,
    )
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def _is_retryable_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(frag in msg for frag in _RETRYABLE_MSG_FRAGMENTS)


def _sleep_backoff(attempt: int) -> None:
    delay = min(_HTTP_RETRY_BACKOFF * (2**attempt) + random.uniform(0, 0.5), 30)
    time.sleep(delay)


def request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    *,
    attempts: int = _REQUEST_ATTEMPTS,
    headers: dict[str, str] | None = None,
    stream: bool = False,
) -> requests.Response:
    """
    Perform an HTTP request with an outer retry loop for connection drops.
    Transport-level 429/503 retries are handled by the Session adapter.
    """
    last_err: BaseException | None = None
    for attempt in range(attempts):
        try:
            if _POLITE_DELAY > 0:
                time.sleep(_POLITE_DELAY)
            resp = session.request(
                method,
                url,
                timeout=_HTTP_TIMEOUT,
                headers=headers,
                stream=stream,
                allow_redirects=True,
            )
            # Soft-retry transient server statuses not covered by the adapter
            # when the adapter exhausted its own retries (raise_on_status=False).
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                last_err = RuntimeError(f"HTTP {resp.status_code} for {url}")
                _sleep_backoff(attempt)
                continue
            return resp
        except requests.exceptions.RequestException as e:
            last_err = e
            if attempt < attempts - 1 and (
                _is_retryable_error(e) or isinstance(e, requests.exceptions.Timeout)
            ):
                print(
                    f"[toga2] transient error on {method} {url} "
                    f"(attempt {attempt + 1}/{attempts}): {e}",
                    file=sys.stderr,
                )
                _sleep_backoff(attempt)
                continue
            raise
    assert last_err is not None
    raise last_err


def fetch_text(session: requests.Session, url: str) -> str:
    resp = request_with_retry(session, "GET", url)
    resp.raise_for_status()
    return resp.text


def parse_directory_hrefs(html: str, page_url: str) -> list[str]:
    """
    Extract immediate child directory names from an Apache-style listing.
    Skips parent-directory links (absolute paths or '..').
    """
    page_path = urlparse(page_url).path.rstrip("/") + "/"
    names: list[str] = []
    seen: set[str] = set()
    for match in HREF_DIR_RE.finditer(html):
        href = match.group(1)
        if href.startswith("?") or href.startswith("#"):
            continue
        # Resolve relative hrefs against the page URL
        resolved = urljoin(page_url, href)
        resolved_path = urlparse(resolved).path
        if not resolved_path.endswith("/"):
            continue
        # Child name is the last path segment
        name = resolved_path.rstrip("/").rsplit("/", 1)[-1]
        if not name or name in (".", ".."):
            continue
        # Skip the parent directory link (absolute path shorter than / equal to parent)
        if resolved_path.rstrip("/") + "/" == page_path.rsplit("/", 2)[0] + "/":
            continue
        if href.startswith("/") and not resolved_path.startswith(page_path):
            # Absolute link pointing outside this directory (e.g. parent)
            continue
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def extract_accession(folder_name: str) -> str | None:
    matches = ACCESSION_RE.findall(folder_name)
    return matches[-1] if matches else None


def verify_gff_exists(session: requests.Session, url: str) -> tuple[bool, str]:
    """
    Confirm the GFF URL is reachable via HEAD, falling back to a Range GET.
    Returns (ok, detail).
    """
    try:
        head = request_with_retry(session, "HEAD", url)
        if head.status_code == 200:
            return True, f"HEAD {head.status_code}"
        if head.status_code in (404, 410):
            return False, f"HEAD {head.status_code}"
        # Some servers reject HEAD; fall through to Range GET for other statuses
        detail_head = f"HEAD {head.status_code}"
    except requests.exceptions.RequestException as e:
        detail_head = f"HEAD error: {e}"

    try:
        get = request_with_retry(
            session,
            "GET",
            url,
            headers={"Range": "bytes=0-0"},
            stream=True,
        )
        try:
            if get.status_code in (200, 206):
                return True, f"{detail_head}; Range-GET {get.status_code}"
            if get.status_code in (404, 410):
                return False, f"{detail_head}; Range-GET {get.status_code}"
            return False, f"{detail_head}; Range-GET {get.status_code}"
        finally:
            get.close()
    except requests.exceptions.RequestException as e:
        return False, f"{detail_head}; Range-GET error: {e}"


# ──────────────────────────────────────────────────────────────────────────────
# TSV I/O
# ──────────────────────────────────────────────────────────────────────────────

def load_existing_tsv(tsv_path: Path) -> dict[str, str]:
    """Return {assembly_accession: access_url}. Preserves first-seen on duplicates."""
    if not tsv_path.is_file():
        raise FileNotFoundError(f"TSV not found: {tsv_path}")
    text = tsv_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines:
        raise ValueError(f"Empty TSV: {tsv_path}")
    if lines[0] != REQUIRED_TSV_HEADER:
        raise ValueError(
            f"Invalid TSV header in {tsv_path}: expected {REQUIRED_TSV_HEADER!r}, "
            f"got {lines[0]!r}"
        )
    rows: dict[str, str] = {}
    for line_no, line in enumerate(lines[1:], start=2):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 2:
            raise ValueError(
                f"{tsv_path}:{line_no}: expected 2 tab-separated columns, got {len(parts)}"
            )
        accession, url = parts[0].strip(), parts[1].strip()
        if not accession or not url:
            raise ValueError(f"{tsv_path}:{line_no}: empty accession or URL")
        if accession not in rows:
            rows[accession] = url
    return rows


def atomic_append_rows(tsv_path: Path, new_rows: list[tuple[str, str]]) -> None:
    """Append rows via temp file + os.replace so a crash cannot truncate the TSV."""
    if not new_rows:
        return
    original = tsv_path.read_bytes()
    # Ensure trailing newline before appending
    if original and not original.endswith(b"\n"):
        original += b"\n"
    additions = "".join(f"{acc}\t{url}\n" for acc, url in new_rows).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(
        prefix=".toga2_annotations_",
        suffix=".tsv.tmp",
        dir=str(tsv_path.parent),
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(original)
            fh.write(additions)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, tsv_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ──────────────────────────────────────────────────────────────────────────────
# Scan logic
# ──────────────────────────────────────────────────────────────────────────────

def ensure_trailing_slash(url: str) -> str:
    return url if url.endswith("/") else url + "/"


def discover_clades(session: requests.Session, base_url: str) -> list[str]:
    html = fetch_text(session, base_url)
    return parse_directory_hrefs(html, base_url)


def list_species_folders(
    session: requests.Session, clade_url: str
) -> list[str]:
    html = fetch_text(session, clade_url)
    return parse_directory_hrefs(html, clade_url)


def scan(
    *,
    tsv_path: Path,
    base_url: str,
    dry_run: bool = False,
    session: requests.Session | None = None,
) -> ScanReport:
    base_url = ensure_trailing_slash(base_url)
    report = ScanReport(base_url=base_url, dry_run=dry_run)

    existing = load_existing_tsv(tsv_path)
    report.existing_rows = len(existing)
    print(f"[toga2] Loaded {len(existing)} existing rows from {tsv_path}")

    own_session = session is None
    session = session or build_http_session()
    try:
        print(f"[toga2] Discovering clades under {base_url}")
        try:
            clades = discover_clades(session, base_url)
        except Exception as e:
            report.errors.append(f"Failed to list clades: {e}")
            print(f"[toga2] ERROR listing clades: {e}", file=sys.stderr)
            return report

        if not clades:
            report.errors.append("No clade directories found under base URL")
            print("[toga2] ERROR: no clade directories found", file=sys.stderr)
            return report

        report.clades_scanned = clades
        print(f"[toga2] Found {len(clades)} clade(s): {', '.join(clades)}")

        candidates: list[Candidate] = []
        for clade in clades:
            clade_url = ensure_trailing_slash(urljoin(base_url, clade + "/"))
            print(f"[toga2] Listing species folders in {clade}/ ...")
            try:
                folders = list_species_folders(session, clade_url)
            except Exception as e:
                msg = f"Failed to list {clade}/: {e}"
                report.errors.append(msg)
                print(f"[toga2] ERROR: {msg}", file=sys.stderr)
                # Soft-fail: continue with other clades; existing TSV is left intact
                continue

            print(f"[toga2]   {clade}/: {len(folders)} folder(s)")
            report.folders_seen += len(folders)

            for folder in folders:
                accession = extract_accession(folder)
                if accession is None:
                    report.skipped_no_accession.append(f"{clade}/{folder}")
                    continue

                access_url = urljoin(
                    ensure_trailing_slash(urljoin(clade_url, folder + "/")),
                    GFF_FILENAME,
                )

                if accession in existing:
                    report.already_tracked.append(accession)
                    if existing[accession] != access_url:
                        report.url_mismatch.append(
                            {
                                "assembly_accession": accession,
                                "existing_url": existing[accession],
                                "server_url": access_url,
                            }
                        )
                        print(
                            f"[toga2] WARNING: URL mismatch for {accession}: "
                            f"TSV has {existing[accession]!r}, server has {access_url!r} "
                            "(keeping existing, not overwriting)",
                            file=sys.stderr,
                        )
                    continue

                candidates.append(
                    Candidate(
                        assembly_accession=accession,
                        clade=clade,
                        folder=folder,
                        access_url=access_url,
                    )
                )

        print(
            f"[toga2] Candidates to verify: {len(candidates)} "
            f"(skipped no-accession: {len(report.skipped_no_accession)}, "
            f"already tracked: {len(report.already_tracked)})"
        )

        verified: list[Candidate] = []
        for cand in candidates:
            ok, detail = verify_gff_exists(session, cand.access_url)
            if ok:
                verified.append(cand)
                print(f"[toga2]   OK  {cand.assembly_accession} ({detail})")
            else:
                report.verify_failed.append(
                    {
                        "assembly_accession": cand.assembly_accession,
                        "access_url": cand.access_url,
                        "detail": detail,
                    }
                )
                print(
                    f"[toga2]   FAIL {cand.assembly_accession}: {detail}",
                    file=sys.stderr,
                )

        # Deduplicate by accession within this scan (keep first)
        seen_new: set[str] = set()
        to_add: list[tuple[str, str]] = []
        for cand in verified:
            if cand.assembly_accession in seen_new:
                print(
                    f"[toga2] WARNING: duplicate new accession "
                    f"{cand.assembly_accession} in scan results; keeping first",
                    file=sys.stderr,
                )
                continue
            seen_new.add(cand.assembly_accession)
            to_add.append((cand.assembly_accession, cand.access_url))
            report.added.append(
                {
                    "assembly_accession": cand.assembly_accession,
                    "access_url": cand.access_url,
                    "clade": cand.clade,
                    "folder": cand.folder,
                }
            )

        if to_add and not dry_run:
            atomic_append_rows(tsv_path, to_add)
            print(f"[toga2] Appended {len(to_add)} row(s) to {tsv_path}")
        elif to_add and dry_run:
            print(f"[toga2] Dry-run: would append {len(to_add)} row(s)")
        else:
            print("[toga2] No new rows to append")

        return report
    finally:
        if own_session:
            session.close()


# ──────────────────────────────────────────────────────────────────────────────
# Reporting / GitHub Actions outputs
# ──────────────────────────────────────────────────────────────────────────────

def write_github_output(report: ScanReport) -> None:
    out_path = os.environ.get("GITHUB_OUTPUT")
    if not out_path:
        return
    with open(out_path, "a", encoding="utf-8") as fh:
        fh.write(f"added_count={report.added_count}\n")
        fh.write(f"skipped_count={report.skipped_count}\n")


def write_step_summary(report: ScanReport) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines = [
        "## TOGA2 annotation scan",
        "",
        f"- **Base URL:** `{report.base_url}`",
        f"- **Existing rows:** {report.existing_rows}",
        f"- **Clades scanned:** {', '.join(report.clades_scanned) or '(none)'}",
        f"- **Folders seen:** {report.folders_seen}",
        f"- **Added:** {report.added_count}",
        f"- **Already tracked:** {len(report.already_tracked)}",
        f"- **Skipped (no GCA/GCF in name):** {len(report.skipped_no_accession)}",
        f"- **Verify failed:** {len(report.verify_failed)}",
        f"- **URL mismatches (kept existing):** {len(report.url_mismatch)}",
        f"- **Dry run:** {report.dry_run}",
    ]
    if report.errors:
        lines += ["", "### Errors", ""]
        lines += [f"- {e}" for e in report.errors]
    if report.added:
        lines += ["", "### Added accessions", ""]
        for row in report.added:
            lines.append(
                f"- `{row['assembly_accession']}` — `{row['clade']}/{row['folder']}`"
            )
    if report.verify_failed:
        lines += ["", "### Verify failed", ""]
        for row in report.verify_failed:
            lines.append(
                f"- `{row['assembly_accession']}`: {row['detail']}"
            )
    with open(summary_path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def print_summary(report: ScanReport) -> None:
    print()
    print("=== TOGA2 scan summary ===")
    print(f"  existing_rows:          {report.existing_rows}")
    print(f"  clades_scanned:         {len(report.clades_scanned)}")
    print(f"  folders_seen:           {report.folders_seen}")
    print(f"  added:                  {report.added_count}")
    print(f"  already_tracked:        {len(report.already_tracked)}")
    print(f"  skipped_no_accession:   {len(report.skipped_no_accession)}")
    print(f"  verify_failed:          {len(report.verify_failed)}")
    print(f"  url_mismatch:           {len(report.url_mismatch)}")
    print(f"  dry_run:                {report.dry_run}")
    if report.errors:
        print(f"  errors:                 {len(report.errors)}")
        for e in report.errors:
            print(f"    - {e}")


def report_to_dict(report: ScanReport) -> dict[str, Any]:
    data = asdict(report)
    data["added_count"] = report.added_count
    data["skipped_count"] = report.skipped_count
    return data


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Scan the TOGA2 HTTPS directory listing for new GCA/GCF annotations "
            "and append them to annotations.tsv."
        )
    )
    p.add_argument(
        "--tsv",
        type=Path,
        default=Path(DEFAULT_TSV),
        help=f"Path to annotations.tsv (default: {DEFAULT_TSV})",
    )
    p.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"TOGA2 v1 base URL (default: {DEFAULT_BASE_URL})",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and report only; do not modify the TSV",
    )
    p.add_argument(
        "--report-file",
        type=Path,
        default=None,
        help="Optional path to write a JSON scan report",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    tsv_path: Path = args.tsv

    try:
        report = scan(
            tsv_path=tsv_path,
            base_url=args.base_url,
            dry_run=args.dry_run,
        )
    except Exception as e:
        print(f"[toga2] FATAL: {e}", file=sys.stderr)
        # Still emit zero counts so the workflow condition is well-defined
        out_path = os.environ.get("GITHUB_OUTPUT")
        if out_path:
            with open(out_path, "a", encoding="utf-8") as fh:
                fh.write("added_count=0\n")
                fh.write("skipped_count=0\n")
        return 1

    print_summary(report)
    write_github_output(report)
    write_step_summary(report)

    if args.report_file is not None:
        args.report_file.write_text(
            json.dumps(report_to_dict(report), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"[toga2] Wrote report to {args.report_file}")

    # Hard fail only if we could not scan any clade at all
    if report.errors and not report.clades_scanned:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
