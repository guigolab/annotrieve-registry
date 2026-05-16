# Contributing to the registry

This guide is for **anyone** adding or changing annotation entries in Annotrieve. You can contribute directly **through GitHub in a browser** and trigger the automated validation, or **validate on your machine** using our ready-made container and then open a PR.

---

## What you are adding

Each contributors adds their **project** as a folder with two files:

| File | Purpose |
|------|--------|
| **`manifest.yaml`** | Describes the provider and pipeline (must match the [JSON schema](schema/manifest.schema.json)). |
| **`annotations.tsv`** | Lists assemblies and links to GFF3 files. First line is the header; every data line must use a **tab** between the two columns (not spaces). |

Rules that matter for everyone:

- **One row per assembly** in each `annotations.tsv` (no duplicate `assembly_accession` values in the same file).
- **One row per URL** — the same `access_url` must not appear twice in the same `annotations.tsv`.
- **One row per file content** — the same annotation file (same MD5 of the downloaded bytes) must not appear twice in the same TSV, and must not duplicate a file already listed in [`checksums/annotation_checksums.tsv`](checksums/annotation_checksums.tsv) under another project or assembly.
- Each URL must be a real **`https://`** link to a **GFF3** file that our checks can open.
- **Download size:** PR validation **downloads** each `access_url` (up to **500 MiB** of raw bytes per file). Larger files fail. **Gzip-compressed GFF3** (`.gff.gz`) is strongly recommended — smaller transfers, faster checks, and less risk of hitting the limit.
- **Must add new content** — if your GFF3 matches an existing NCBI/Ensembl annotation (same MD5 after Annotrieve’s processing), it will be skipped on import. Only submit files that add **new** annotation content. See [section below](#md5-checksum-index) for more details on md5_checksum.

---


## Contribute with a fork (works in the browser)

This is the usual way: you do **not** need direct write access to this repository.

1. **Fork** this repository on GitHub (button “Fork” on the repo page).
2. In **your fork**, create a folder for your project (or open an existing one), e.g. `my_lab_my_build/`.
3. Add or edit **`manifest.yaml`** and **`annotations.tsv`** (you can use “Add file” → “Create new file” if you like).
4. **Commit** the changes.
5. Open a **Pull request** from your fork **into the master branch** of this repository.

After you open or update the PR, **automation runs on our side** (see next section). You will see:

- A **short summary** on the PR conversation tab (pass/fail counts in simple language).
- **Hints on the “Files changed” tab** next to specific lines when something is wrong (so you know what to fix).

You can push more commits to the same PR; checks run again each time.

**Please keep each PR to edits in a single `annotations.tsv` file** (one TSV changed per PR). That keeps review and CI predictable.

When your PR is ready and all checks pass, we will review the changes and merge it. After merging, your annotation entries will be available in the next update of Annotrieve (usually within a week).

---

### What the PR check does

When you open or update a pull request, a workflow runs in a **pre-built environment** (a Docker image we publish to GitHub Container Registry). In simple terms it:

1. **Compares** your branch to the branch you are merging into, so only **new or changed rows** in `annotations.tsv` are fully re-checked (older rows are not re-downloaded unless the file changed).
2. Checks **`manifest.yaml`** for every project folder that your PR touches.
3. For each **new** TSV row, checks that:
   - the accession looks like a real NCBI assembly and **exists in NCBI** (NCBI `datasets` tool);
   - the **URL is reachable** and the downloaded data is valid **GFF3** (with `ID=` / `Parent=` in the scanned region);
   - the file can be sorted and indexed (**tabix** / **bgzip**) like the Annotrieve pipeline;
   - there are **no duplicate** `assembly_accession`, `access_url`, or file **MD5** values within the TSV;
   - the file **MD5 is not already** in `checksums/annotation_checksums.tsv` on the base branch (with the existing project path and URL cited in the review comment).

If something fails, the PR will show as failed until the data is fixed, while you always get the summary and line-level hints to guide your fixes.

**Download limit:** For each new row, the validator streams the file from your URL and stops at **500 MiB** (524,288,000 bytes) of downloaded data; larger files fail with a download error. Prefer **gzip-compressed GFF3** when you can — the limit applies to the bytes received (compressed size for `.gz`), so gzip usually keeps you under the cap and speeds up CI.

**Note:** Passing registry validation does not guarantee a new Annotrieve record if the file content is the same as an existing NCBI or Ensembl annotation (see disclaimer above).

---

### Check your TSV locally (dry run)

If you have **[Docker](https://docs.docker.com/get-docker/)** installed, you can run **the same** validator we use in CI **without** installing Python, tabix, or the NCBI CLI on your laptop.

1. **Clone** your fork (or this repo) and `cd` into it so your project folder and `.git` are present.
2. Pull the published image (same name as in `validate-pr.yml`):

   ```bash
   docker pull ghcr.io/guigolab/annotrieve-registry/registry-ci:latest
   ```

3. Run the validator inside the container, with your repo mounted at `/workspace`:

   ```bash
   docker run --rm -v "$(pwd):/workspace" -w /workspace \
     ghcr.io/guigolab/annotrieve-registry/registry-ci:latest \
     bash -lc '
       git config --global --add safe.directory /workspace
       BASE="$(git merge-base origin/master HEAD 2>/dev/null || git merge-base origin/main HEAD 2>/dev/null || git merge-base master HEAD)"
       python scripts/validate_pr.py \
         --base "$BASE" \
         --head HEAD \
         --output-summary validation-summary.md \
         --output-rdjsonl validation.rdjsonl
       echo "----"; cat validation-summary.md
     '
   ```

   Adjust `origin/master` / `origin/main` if your default remote branch has another name. The script exits with code **0** if everything passed and **1** if something failed (same as CI).

4. Optional: set an NCBI API key inside the one-off container for higher rate limits:

   ```bash
   docker run --rm -e NCBI_API_KEY="your_key_here" -v "$(pwd):/workspace" -w /workspace \
     ghcr.io/guigolab/annotrieve-registry/registry-ci:latest \
     bash -lc 'git config --global --add safe.directory /workspace && ...same python command...'
   ```

If the image is **private**, log in once with `docker login ghcr.io` using a GitHub **personal access token** that has the `read:packages` scope.

---

### Check locally without Docker (advanced)

Install **Python 3.11+**, **tabix/bgzip** (htslib), and the **[NCBI datasets CLI](https://www.ncbi.nlm.nih.gov/datasets/docs/v2/download-and-install/)**, then:

```bash
pip install -r requirements.txt
python scripts/validate_pr.py \
  --base "$(git merge-base origin/master HEAD)" \
  --head HEAD \
  --output-summary validation-summary.md \
  --output-rdjsonl validation.rdjsonl
```

Use `--datasets-binary /path/to/datasets` if `datasets` is not on your `PATH`. Replace `origin/master` with your default branch if needed.

---

### Optional tuning (for developers)

These environment variables only affect the validator when set (defaults are fine for most contributors):

| Variable | Default | Purpose |
|----------|---------|---------|
| `VALIDATE_DOWNLOAD_WORKERS` | `3` | Parallel downloads / heavy checks |
| `VALIDATE_DATASETS_BATCH_SIZE` | `2000` | Accessions per `datasets` batch |
| `VALIDATE_DATASETS_TIMEOUT` | `300` | Seconds for a `datasets` batch |
| `VALIDATE_SCAN_BYTES` | `52428800` | How much GFF3 (decompressed) to scan for `ID=` / `Parent=` |
| `VALIDATE_MAX_DOWNLOAD_BYTES` | `524288000` | Max bytes downloaded per URL |
| `VALIDATE_HTTP_RETRY_TOTAL` | `6` | Retries on slow or rate-limited URLs |
| `DATASETS_BINARY` | `datasets` | Path to the `datasets` binary if not on `PATH` |
| `NCBI_API_KEY` | — | Optional; higher NCBI rate limit when set |

Assembly checks use the **datasets** subprocess, not ad-hoc NCBI HTTP from Python. URL checks use a **single streaming GET** per row (no separate HEAD request).

---

## MD5 checksum index

The repository keeps a **repo-wide** TSV of file fingerprints:

| Column | Meaning |
|--------|---------|
| `md5_checksum` | MD5 of the **raw downloaded** GFF3 (plain or `.gz` bytes as fetched) |
| `assembly_accession` | NCBI assembly accession for that row |
| `repo_path` | Project folder (e.g. `my_lab_build`) |
| `access_url` | HTTPS link stored in `annotations.tsv` |

- **On pull requests:** new rows are downloaded and hashed during validation. Their MD5 is compared to other new rows in the PR and to the index on the **target branch**, so you get a clear error if the file was already merged elsewhere (including project path and URL in the message).
- **On merge to `master` / `main`:** [`.github/workflows/update-checksums.yml`](.github/workflows/update-checksums.yml) syncs the index for changed projects: removes entries for deleted rows (or deleted `annotations.tsv` files) and appends checksums for newly merged rows only.

You do not edit `checksums/annotation_checksums.tsv` by hand; it is maintained by automation.

---
