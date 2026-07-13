#!/usr/bin/env python3
"""Generate annotrieve-registry TOGA2/annotations.tsv from Senckenberg directory listings."""

from __future__ import annotations

import argparse
import re
import sys
import urllib.request
from pathlib import Path

TOGA2_BASE = "https://genome.senckenberg.de/download/TOGA2/TOGA2integration/v1"
TSV_HEADER = "assembly_accession\taccess_url"
ACCESSION_RE = re.compile(r"(GCA_\d+\.\d+|GCF_\d+\.\d+)")
FOLDER_LINK_RE = re.compile(r'<a href="([^"]+/)"')
GFF_FILE = "query_annotation.gff.gz"


def fetch_index(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "annotrieve-registry-generator/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read().decode("utf-8", errors="replace")


def list_folders(index_url: str) -> list[str]:
    html = fetch_index(index_url)
    folders: list[str] = []
    seen: set[str] = set()
    for match in FOLDER_LINK_RE.finditer(html):
        href = match.group(1)
        if href in ("../", "./") or href.startswith("/"):
            continue
        name = href.rstrip("/")
        if name and name not in seen:
            seen.add(name)
            folders.append(name)
    return folders


def extract_accession(folder_name: str) -> str | None:
    match = ACCESSION_RE.search(folder_name)
    return match.group(1) if match else None


def folder_has_gff(folder_url: str) -> bool:
    html = fetch_index(folder_url)
    return f'href="{GFF_FILE}"' in html or f">{GFF_FILE}<" in html


def build_rows(taxonomic_class: str, *, verify_gff: bool = True) -> list[tuple[str, str]]:
    index_url = f"{TOGA2_BASE}/{taxonomic_class}/"
    rows: list[tuple[str, str]] = []
    skipped: list[str] = []
    folders = list_folders(index_url)
    for i, folder in enumerate(folders, start=1):
        accession = extract_accession(folder)
        if not accession:
            continue
        url = f"{index_url}{folder}/{GFF_FILE}"
        if verify_gff:
            folder_url = f"{index_url}{folder}/"
            if not folder_has_gff(folder_url):
                skipped.append(f"{accession} ({folder})")
                continue
            if i % 50 == 0:
                print(f"verified {i}/{len(folders)} folders...", file=sys.stderr)
        rows.append((accession, url))
    if skipped:
        print(f"skipped {len(skipped)} folders without {GFF_FILE}:", file=sys.stderr)
        for entry in skipped:
            print(f"  - {entry}", file=sys.stderr)
    return rows


def write_tsv(path: Path, rows: list[tuple[str, str]]) -> None:
    accessions = [acc for acc, _ in rows]
    urls = [url for _, url in rows]

    dup_acc = sorted({a for a in accessions if accessions.count(a) > 1})
    dup_url = sorted({u for u in urls if urls.count(u) > 1})
    if dup_acc:
        raise SystemExit(f"duplicate assembly_accession values: {dup_acc[:5]}")
    if dup_url:
        raise SystemExit(f"duplicate access_url values: {dup_url[:5]}")

    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [TSV_HEADER] + [f"{acc}\t{url}" for acc, url in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--class",
        dest="taxonomic_class",
        default="Mammalia",
        help="TOGA2 taxonomic class subdirectory (default: Mammalia)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("TOGA2/annotations.tsv"),
        help="Output TSV path relative to repo root (default: TOGA2/annotations.tsv)",
    )
    parser.add_argument(
        "--no-verify-gff",
        action="store_true",
        help="Skip per-folder check that query_annotation.gff.gz exists",
    )
    args = parser.parse_args()

    rows = build_rows(args.taxonomic_class, verify_gff=not args.no_verify_gff)
    rows.sort(key=lambda row: row[0])
    write_tsv(args.output, rows)
    print(f"Wrote {len(rows)} rows to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
