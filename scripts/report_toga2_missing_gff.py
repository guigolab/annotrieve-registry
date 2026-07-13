#!/usr/bin/env python3
"""Report TOGA2 folders missing query_annotation.gff.gz across vertebrate clades."""

from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

TOGA2_BASE = "https://genome.senckenberg.de/download/TOGA2/TOGA2integration/v1"
DEFAULT_CLASSES = ("Mammalia", "Aves", "Percomorpha", "Testudines")
ACCESSION_RE = re.compile(r"(GCA_\d+\.\d+|GCF_\d+\.\d+)")
FOLDER_LINK_RE = re.compile(r'<a href="([^"]+/)"')
GFF_FILE = "query_annotation.gff.gz"


@dataclass(frozen=True)
class MissingGffFolder:
    taxonomic_class: str
    folder_name: str
    folder_url: str
    assembly_accession: str | None


def fetch_index(url: str) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": "annotrieve-registry-reporter/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def list_folders(index_url: str) -> list[str]:
    html = fetch_index(index_url)
    if html is None:
        raise SystemExit(f"could not list folders at {index_url}")
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
    if html is None:
        return False
    return f'href="{GFF_FILE}"' in html or f">{GFF_FILE}<" in html


def scan_class(taxonomic_class: str) -> list[MissingGffFolder]:
    index_url = f"{TOGA2_BASE}/{taxonomic_class}/"
    missing: list[MissingGffFolder] = []
    folders = list_folders(index_url)
    for i, folder in enumerate(folders, start=1):
        folder_url = f"{index_url}{folder}/"
        if folder_has_gff(folder_url):
            continue
        missing.append(
            MissingGffFolder(
                taxonomic_class=taxonomic_class,
                folder_name=folder,
                folder_url=folder_url,
                assembly_accession=extract_accession(folder),
            )
        )
        if i % 50 == 0:
            print(
                f"[{taxonomic_class}] checked {i}/{len(folders)} folders "
                f"({len(missing)} missing {GFF_FILE})...",
                file=sys.stderr,
            )
    print(
        f"[{taxonomic_class}] done: {len(folders)} folders, "
        f"{len(missing)} missing {GFF_FILE}",
        file=sys.stderr,
    )
    return missing


def write_tsv(path: Path, entries: list[MissingGffFolder]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["taxonomic_class\tfolder_name\tassembly_accession\tfolder_url"]
    for entry in entries:
        acc = entry.assembly_accession or ""
        lines.append(f"{entry.taxonomic_class}\t{entry.folder_name}\t{acc}\t{entry.folder_url}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_markdown(path: Path, entries: list[MissingGffFolder], classes: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    by_class: dict[str, list[MissingGffFolder]] = {cls: [] for cls in classes}
    for entry in entries:
        by_class.setdefault(entry.taxonomic_class, []).append(entry)

    with_accession = sum(1 for e in entries if e.assembly_accession)
    without_accession = len(entries) - with_accession

    lines = [
        "# TOGA2 folders missing query_annotation.gff.gz",
        "",
        f"Base URL: [{TOGA2_BASE}/]({TOGA2_BASE}/)",
        "",
        "## Summary",
        "",
        "| Clade | Total folders missing GFF | With NCBI accession | Without accession |",
        "|-------|---------------------------|---------------------|-------------------|",
    ]
    for cls in classes:
        cls_entries = by_class.get(cls, [])
        cls_with = sum(1 for e in cls_entries if e.assembly_accession)
        lines.append(
            f"| {cls} | {len(cls_entries)} | {cls_with} | {len(cls_entries) - cls_with} |"
        )
    lines.extend(
        [
            f"| **Total** | **{len(entries)}** | **{with_accession}** | **{without_accession}** |",
            "",
            "## Folders",
            "",
        ]
    )

    for cls in classes:
        cls_entries = sorted(by_class.get(cls, []), key=lambda e: e.folder_name)
        if not cls_entries:
            continue
        lines.append(f"### {cls}")
        lines.append("")
        for entry in cls_entries:
            acc_suffix = f" (`{entry.assembly_accession}`)" if entry.assembly_accession else ""
            lines.append(f"- [{entry.folder_name}]({entry.folder_url}){acc_suffix}")
        lines.append("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--classes",
        nargs="+",
        default=list(DEFAULT_CLASSES),
        help=f"TOGA2 clade subdirectories to scan (default: {' '.join(DEFAULT_CLASSES)})",
    )
    parser.add_argument(
        "--output-tsv",
        type=Path,
        default=Path("reports/toga2_missing_gff.tsv"),
        help="Output TSV path (default: reports/toga2_missing_gff.tsv)",
    )
    parser.add_argument(
        "--output-md",
        type=Path,
        default=Path("reports/toga2_missing_gff.md"),
        help="Output Markdown report path (default: reports/toga2_missing_gff.md)",
    )
    args = parser.parse_args()
    classes = tuple(args.classes)

    all_missing: list[MissingGffFolder] = []
    for taxonomic_class in classes:
        all_missing.extend(scan_class(taxonomic_class))

    all_missing.sort(key=lambda e: (e.taxonomic_class, e.folder_name))
    write_tsv(args.output_tsv, all_missing)
    write_markdown(args.output_md, all_missing, classes)

    print(f"Wrote {len(all_missing)} rows to {args.output_tsv}", file=sys.stderr)
    print(f"Wrote report to {args.output_md}", file=sys.stderr)


if __name__ == "__main__":
    main()
