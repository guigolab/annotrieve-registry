"""Repo-wide annotation file MD5 index (checksums/annotation_checksums.tsv)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

CHECKSUM_INDEX_REL = "checksums/annotation_checksums.tsv"
CHECKSUM_HEADER = "md5_checksum\tassembly_accession\trepo_path\taccess_url"
CHECKSUM_COLUMNS = CHECKSUM_HEADER.split("\t")

RowKey = tuple[str, str, str]  # (repo_path, assembly_accession, access_url)


def normalize_repo_path(repo_path: str) -> str:
    return repo_path.replace("\\", "/").strip("/") or "."


@dataclass(frozen=True)
class ChecksumIndexEntry:
    md5_checksum: str
    assembly_accession: str
    repo_path: str
    access_url: str

    def row_key(self) -> RowKey:
        """Logical row identity within the registry."""
        return (
            normalize_repo_path(self.repo_path),
            self.assembly_accession,
            self.access_url,
        )

    def format_location(self) -> str:
        tsv = f"{self.repo_path}/annotations.tsv"
        return (
            f"`{tsv}` (assembly `{self.assembly_accession}`, "
            f"URL `{self.access_url}`)"
        )


def parse_checksum_index(content: str | None) -> tuple[list[ChecksumIndexEntry], str | None]:
    if content is None:
        return [], None
    lines = [ln.rstrip("\n\r") for ln in content.splitlines()]
    if not lines:
        return [], None
    if lines[0].strip() != CHECKSUM_HEADER:
        return [], f"invalid header; expected: {CHECKSUM_HEADER!r}"
    entries: list[ChecksumIndexEntry] = []
    for i, ln in enumerate(lines[1:], start=2):
        raw = ln.strip()
        if not raw or raw.startswith("#"):
            continue
        parts = raw.split("\t")
        if len(parts) != 4:
            return [], f"line {i}: expected 4 tab-separated columns, got {len(parts)}"
        md5, acc, repo_path, url = (p.strip() for p in parts)
        if not md5 or not acc or not repo_path or not url:
            return [], f"line {i}: empty md5_checksum, assembly_accession, repo_path, or access_url"
        entries.append(
            ChecksumIndexEntry(
                md5_checksum=md5.lower(),
                assembly_accession=acc,
                repo_path=normalize_repo_path(repo_path),
                access_url=url,
            )
        )
    return entries, None


def load_checksum_index(path: Path) -> tuple[list[ChecksumIndexEntry], str | None]:
    if not path.is_file():
        return [], None
    return parse_checksum_index(path.read_text(encoding="utf-8"))


def index_by_md5(
    entries: list[ChecksumIndexEntry],
) -> dict[str, list[ChecksumIndexEntry]]:
    out: dict[str, list[ChecksumIndexEntry]] = defaultdict(list)
    for e in entries:
        out[e.md5_checksum].append(e)
    return dict(out)


def format_index_collision(existing: ChecksumIndexEntry) -> str:
    return (
        f"identical file already registered at {existing.format_location()} "
        f"(MD5 `{existing.md5_checksum}`)"
    )


def find_index_md5_collisions(
    md5: str,
    repo_path: str,
    assembly_accession: str,
    access_url: str,
    index_by_md5_map: dict[str, list[ChecksumIndexEntry]],
) -> list[ChecksumIndexEntry]:
    """Index entries with the same MD5 but a different logical row."""
    current = (normalize_repo_path(repo_path), assembly_accession, access_url)
    hits = index_by_md5_map.get(md5.lower(), [])
    return [e for e in hits if e.row_key() != current]


def prune_index_for_repo_path(
    entries: list[ChecksumIndexEntry],
    repo_path: str,
    valid_row_keys: set[RowKey],
) -> list[ChecksumIndexEntry]:
    """Drop index rows for `repo_path` that are not in `valid_row_keys` (current TSV)."""
    rp = normalize_repo_path(repo_path)
    return [e for e in entries if e.repo_path != rp or e.row_key() in valid_row_keys]


def drop_index_for_repo_path(
    entries: list[ChecksumIndexEntry],
    repo_path: str,
) -> list[ChecksumIndexEntry]:
    """Remove every index row for a project path (e.g. deleted annotations.tsv)."""
    rp = normalize_repo_path(repo_path)
    return [e for e in entries if e.repo_path != rp]


def entries_for_new_rows(
    existing: list[ChecksumIndexEntry],
    additions: list[ChecksumIndexEntry],
) -> list[ChecksumIndexEntry]:
    """Return additions not already present (same row_key or same md5+row)."""
    seen_keys = {e.row_key() for e in existing}
    seen_full = {(e.md5_checksum, *e.row_key()) for e in existing}
    out: list[ChecksumIndexEntry] = []
    for e in additions:
        rk = e.row_key()
        full = (e.md5_checksum, *rk)
        if rk in seen_keys or full in seen_full:
            continue
        out.append(e)
        seen_keys.add(rk)
        seen_full.add(full)
    return out


def render_checksum_index(entries: list[ChecksumIndexEntry]) -> str:
    lines = [CHECKSUM_HEADER]
    for e in entries:
        lines.append(
            "\t".join([e.md5_checksum, e.assembly_accession, e.repo_path, e.access_url])
        )
    return "\n".join(lines) + "\n"
