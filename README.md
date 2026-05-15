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

## How it fits in the larger system

After your changes are **merged here**, the **[Genome Annotation Tracker](https://github.com/guigolab/genome-annotation-tracker)** reads this registry, turns each project’s manifest + TSV into formatted rows, and adds them to the shared **community annotation table**. Those rows are published on **[Annotrieve](https://genome.crg.eu/annotrieve)** in periodic imports.

```text
You (this repo)             Downstream                          App
─────────────────           ──────────────────────────────      ───────────
manifest.yaml        ──►    genome-annotation-tracker    ──►    Annotrieve
annotations.tsv              (community TSV)
```
