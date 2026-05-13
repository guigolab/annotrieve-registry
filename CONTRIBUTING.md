# Contributing to the registry

## Workflow

1. Fork this repository.
2. Add or update a directory `<project_name>/` containing:
   - **`manifest.yaml`** — metadata (`provider_name`, `pipeline_method`, `pipeline_version`, plus optional fields). Must conform to [`schema/manifest.schema.json`](schema/manifest.schema.json).
   - **`annotations.tsv`** — tab-separated file with header exactly:

     ```text
     assembly_accession	access_url
     ```

     Each row links one NCBI assembly accession (`GCA_*` / `GCF_*`) to an HTTPS URL for a **GFF3** file (plain or gzip-compressed).

3. Open a pull request against `master`.

## Rules

- **One row per assembly** within each project's `annotations.tsv` (no duplicate `assembly_accession` values).
- Rows must be strictly tab-separated — exactly two columns, no spaces as delimiters.
- URLs must be reachable from the internet and point at data suitable for indexing (see CI checks).

## CI validation (on every PR)

PR validation runs in a **GHCR container** (`ghcr.io/<lowercase-owner>/<lowercase-repo>/registry-ci:latest`) built from [`docker/ci-validator/Dockerfile`](docker/ci-validator/Dockerfile). It includes Python 3.11, `tabix`/`bgzip`, a pinned [NCBI `datasets` CLI](https://github.com/ncbi/datasets/releases), pip deps from [`requirements.txt`](requirements.txt), and [reviewdog](https://github.com/reviewdog/reviewdog).

**First-time setup (maintainers):** merge the image workflow, then run **Actions → Publish CI validator image** (or push to `main`/`master` changing `docker/ci-validator/**` or `requirements.txt`) so `:latest` exists before PR checks can pull the image. To bump tools, adjust `DATASETS_RELEASE` / `REVIEWDOG_SEMVER` in the Dockerfile or the publish workflow’s `workflow_dispatch` inputs, then publish again.

For each touched project directory:

1. **`manifest.yaml`** is validated against the JSON Schema (required fields: `provider_name`, `pipeline_method`, `pipeline_version`).
2. **`annotations.tsv`** is checked for duplicate `assembly_accession` values across the whole file on your branch.
3. For **newly added rows only** (compared to the PR merge-base), CI verifies:
   - Assembly accession matches `GCA_*/GCF_*` format and exists in NCBI — checked in bulk using the **NCBI datasets CLI** with `--inputfile` in batches of up to 2,000 accessions per call (no individual NCBI HTTP traffic).
   - URL is reachable and the content is valid **GFF3** — checked in a **single streaming GET**: the HTTP status is verified from the response headers before any body is read (no separate HEAD request), then the body is decompressed on-the-fly while scanning the first 50 MB (decompressed) for at least one `ID=` and one `Parent=` in column 9.
   - The **tabix** normalization used by Annotrieve succeeds: comments-first sort, `bgzip`, `tabix -p gff --csi`. This step is skipped if the GFF3 check already failed (saves bandwidth).

Results appear as:
- A **short summary** posted (and kept updated) as a PR conversation comment.
- **Inline annotations** via [reviewdog](https://github.com/reviewdog/reviewdog) on each problem line under **Files changed**.

## Local checks

Install Python 3.11+, `tabix`/`bgzip` (htslib), and the [NCBI datasets CLI](https://www.ncbi.nlm.nih.gov/datasets/docs/v2/download-and-install/), then from the repo root:

```bash
pip install -r requirements.txt
python scripts/validate_pr.py \
  --base "$(git merge-base origin/master HEAD)" \
  --head HEAD \
  --output-summary validation-summary.md \
  --output-rdjsonl validation.rdjsonl
```

Pass `--datasets-binary /path/to/datasets` if the binary is not on `PATH`.
Adjust `--base` if your default branch differs.

## Tuning (optional)

All knobs are environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `VALIDATE_DOWNLOAD_WORKERS` | `3` | Parallel GFF download + GFF3 check + tabix pipeline jobs |
| `VALIDATE_DATASETS_BATCH_SIZE` | `2000` | Accessions per `datasets` CLI call |
| `VALIDATE_DATASETS_TIMEOUT` | `300` | Seconds before a `datasets` batch call times out |
| `VALIDATE_SCAN_BYTES` | `52428800` | Decompressed bytes to scan for GFF3 attributes (50 MB) |
| `VALIDATE_MAX_DOWNLOAD_BYTES` | `524288000` | Max download size per annotation file (500 MB) |
| `VALIDATE_HTTP_RETRY_TOTAL` | `6` | Max retries per URL/download request |
| `VALIDATE_HTTP_RETRY_BACKOFF` | `2` | urllib3 backoff factor (`Retry-After` honored on 429) |
| `VALIDATE_HTTP_RETRY_STATUS` | `429,503` | HTTP status codes that trigger a retry |
| `VALIDATE_HTTP_USER_AGENT` | (bundled string) | `User-Agent` for HTTP requests |
| `DATASETS_BINARY` | `datasets` | Path or name of the NCBI datasets CLI binary |
| `NCBI_API_KEY` | — | NCBI API key; raises NCBI rate limit from 3 → 10 req/s |

GFF downloads use a **separate `requests.Session` per thread-pool worker** (thread-safe; retries 429/503 with exponential backoff). URL reachability is verified implicitly at the start of each streaming download — no separate HEAD request. Assembly validation runs through the datasets CLI subprocess — no direct NCBI HTTP traffic from Python.
