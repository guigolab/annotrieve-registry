# Annotrieve community annotations registry

This repository is the **community registry** for genome annotation entries that power [Annotrieve](https://genome.crg.es/annotrieve/).

## What this repo is for

Contributors add **project folders**. Each folder contains:

- A **`manifest.yaml`** file — who produced the annotation and how (provider, pipeline, version).
- An **`annotations.tsv`** file — one row per assembly: NCBI accession (`GCA_…` / `GCF_…`) and a stable **HTTPS link** to a **GFF3** file (plain or gzipped).

Together, these files describe “this assembly, this annotation file,” in a form that can be checked automatically.

### Files you add or edit when you contribute

```text
<project_name>/
  manifest.yaml      # Required metadata (see schema)
  annotations.tsv    # Header + one row per assembly (tab-separated)
```

- Exact TSV header: [`schema/annotations.tsv.header`](schema/annotations.tsv.header)
- Manifest rules: [`schema/manifest.schema.json`](schema/manifest.schema.json)
- Copy-paste starter: [`examples/sample_project/`](examples/sample_project/)

See **[`CONTRIBUTING.md`](CONTRIBUTING.md)** for a step-by-step flow (fork → edit → pull request).

### Repo-wide checksum index

After entries are merged to the default branch, automation maintains a shared index:

```text
checksums/annotation_checksums.tsv
```

Each row records the **MD5 of the downloaded annotation file** (raw bytes as fetched from `access_url`), plus the assembly accession, project path, and URL. Pull-request validation uses this index to reject new rows whose file content is already registered under another project or assembly.

- Index header: [`schema/annotation_checksums.header`](schema/annotation_checksums.header)
- Updated on push to `master` / `main` by [`.github/workflows/update-checksums.yml`](.github/workflows/update-checksums.yml)

## How it fits in the larger system

After your changes are **merged here**, the **[Genome Annotation Tracker](https://github.com/guigolab/genome-annotation-tracker)** reads this registry, turns each project’s manifest + TSV into formatted rows, and adds them to the shared **community annotation table**. Those rows are published on **[Annotrieve](https://genome.crg.eu/annotrieve)** in periodic imports.

```text
You (this repo)          Downstream                         App
─────────────────        ───────────────────────────────   ───────────
manifest.yaml    ──┐
annotations.tsv  ──┼──►  genome-annotation-tracker   ──►   community TSV
(project folders)  │     (merges + normalizes rows)        ──►   Annotrieve
checksums/       ──┘     github.com/guigolab/
annotation_              genome-annotation-tracker
checksums.tsv
```

## Import into Annotrieve

> **Disclaimer — duplicate file content**  
> Annotrieve identifies each annotation by an **MD5 checksum of the sorted, uncompressed GFF3** (the same content identity used for NCBI and Ensembl entries in the database).  
> **Community submissions whose file content matches an annotation already imported from NCBI or Ensembl (same MD5) are skipped during import** and will not appear as a separate community record, even if your registry PR passed validation.  
> Submit **distinct** annotation files (different assemblies and genuinely different GFF3 content). Re-hosting the same file under another URL or project folder does not create a second Annotrieve entry.

Registry CI checks **downloaded file bytes**; Annotrieve’s import deduplication uses the **processed** checksum after sort/bgzip. In practice, identical biological content that is already in NCBI/Ensembl will be treated as a duplicate at import time.
