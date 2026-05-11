# Contributing to the registry

## Workflow

1. Fork this repository and create a branch.
2. Add or update a directory `<project_name>/` containing:
   - **`manifest.yaml`** — metadata (`provider_name`, `pipeline_method`, `pipeline_version`, plus optional fields). Must conform to [`schema/manifest.schema.json`](schema/manifest.schema.json).
   - **`annotations.tsv`** — tab-separated file with header exactly:

     ```text
     assembly_accession	access_url
     ```

     Each row links one NCBI assembly accession (`GCA_*` / `GCF_*`) to an HTTPS URL for a **GFF3** file (plain or gzip-compressed).

3. Open a pull request against `main`.

## Rules

- **One row per assembly** within each project’s `annotations.tsv` (no duplicate `assembly_accession` values).
- URLs must be reachable from the internet and point at data suitable for indexing (see CI checks).
- Prefer smaller GFFs when possible: PR validation downloads each **new** row’s file and runs a full **sort → bgzip → tabix** pipeline; very large files may hit CI timeouts or size limits.

## CI validation (on every PR)

For each affected project:

- **`manifest.yaml`** is validated against the JSON Schema.
- **`annotations.tsv`** is checked for duplicate assembly accessions (full file on your branch).
- For **only newly added data rows** (compared to the merge base), CI verifies:
  - Assembly accession exists in NCBI (via `datasets` CLI).
  - URL responds (HTTP).
  - Content looks like **GFF3** (at least one feature line with `ID=` and at least one with `Parent=` in the attributes column).
  - The same **tabix** normalization used by Annotrieve succeeds (`sort`, `bgzip`, `tabix -p gff --csi`).

Results are posted as a **single comment** on your PR (summary + per-row errors).

## Local checks (optional)

Install Python 3.11+, `tabix`/`bgzip` (htslib), and the [NCBI datasets CLI](https://www.ncbi.nlm.nih.gov/datasets/docs/v2/command-line-tools/), then from the repo root:

```bash
pip install -r requirements.txt
python scripts/validate_pr.py --base "$(git merge-base origin/main HEAD)" --head HEAD
```

Adjust `--base` if your default branch is not `main`.
