# Contributing to the registry

This guide is for **anyone** adding or changing annotation entries—including people who prefer not to install developer tools. You can contribute [**through GitHub in a browser**](## Contribute with a fork (works in the browser)), or [**validate on your machine**](## Check your TSV locally (Docker, recommended for a “full” dry run)) using our ready-made container.

---

## What you are adding

Each contributors adds their **project** as a folder with two files:

| File | Purpose |
|------|--------|
| **`manifest.yaml`** | Describes the provider and pipeline (must match the [JSON schema](schema/manifest.schema.json)). |
| **`annotations.tsv`** | Lists assemblies and links to GFF3 files. First line is the header; every data line must use a **tab** between the two columns (not spaces). |

Rules that matter for everyone:

- **One row per assembly** in each `annotations.tsv` (no duplicate accessions in the same file).
- Each URL must be a real **`https://`** link to a **GFF3** file that our checks can open.

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
   - the accession looks like a real NCBI assembly and **exists in NCBI** using  NCBI `datasets` tool;
   - the **URL exists** and the downloaded data is in **GFF3** format;
   - the annotation file can be sorted and tabindexed as in the main **Annotrieve pipeline**.

If something fails, the PR will show as failed until the data is fixed, while you always get the summary and line-level hints to guide your fixes.

---

## Check your TSV locally (recommended for a “full” dry run)

If you have **[Docker](https://docs.docker.com/get-docker/)** installed, you can run **the same** validator we use in CI **without** installing Python, tabix, or the NCBI CLI on your laptop.

1. **Clone** your fork (or this repo) and `cd` into it so your project folder and `.git` are present.
2. Pull the published image (same name as in `validate-pr.yml`):

   ```bash
   docker pull ghcr.io/emiliorighi/annotrieve-registry/registry-ci:latest
   ```

3. Run the validator inside the container, with your repo mounted at `/workspace`:

   ```bash
   docker run --rm -v "$(pwd):/workspace" -w /workspace \
     ghcr.io/emiliorighi/annotrieve-registry/registry-ci:latest \
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
     ghcr.io/emiliorighi/annotrieve-registry/registry-ci:latest \
     bash -lc 'git config --global --add safe.directory /workspace && ...same python command...'
   ```

If the image is **private**, log in once with `docker login ghcr.io` using a GitHub **personal access token** that has the `read:packages` scope.

---

## Path 3 — Check locally without Docker (advanced)

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

## Optional tuning (for developers)

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
