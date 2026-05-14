# Annotrieve community annotations registry

This repository is the **community registry** for genome annotation entries that power **Annotrieve**.

## What this repo is for

Contributors add **small project folders**. Each folder contains:

- A **`manifest.yaml`** file — who produced the annotation and how (provider, pipeline, version).
- An **`annotations.tsv`** file — one row per assembly: NCBI accession (`GCA_…` / `GCF_…`) and a stable **HTTPS link** to a **GFF3** file (plain or gzipped).

Together, these files describe “this assembly, this annotation file,” in a form that can be checked automatically.

## How it fits in the larger system

```text
You (this repo)          Downstream                         App
─────────────────        ───────────────────────────────   ───────────
manifest.yaml    ──┐
annotations.tsv  ──┼──►  genome-annotation-tracker   ──►   community TSV
(project folders)  │     (merges + normalizes rows)        ──►   Annotrieve
                   └     github.com/guigolab/
                         genome-annotation-tracker
```

After your changes are **merged here**, the **[Genome Annotation Tracker](https://github.com/guigolab/genome-annotation-tracker)** reads this registry, turns each project’s manifest + TSV into **formatted rows** in a shared **community annotation table**, and that table is what **[Annotrieve](https://genome.crg.eu/annotrieve)** uses.

So: **this repo = curated source of truth**; the tracker = **batch merger / formatter**; Annotrieve = **what researchers use in the browser**.

## Repository layout

```text
<project_name>/
  manifest.yaml      # Required metadata (see schema)
  annotations.tsv    # Header + one row per assembly (tab-separated)
```

- Exact TSV header: [`schema/annotations.tsv.header`](schema/annotations.tsv.header)
- Manifest rules: [`schema/manifest.schema.json`](schema/manifest.schema.json)
- Copy-paste starter: [`examples/sample_project/`](examples/sample_project/)

## Contributing

See **[`CONTRIBUTING.md`](CONTRIBUTING.md)** for a step-by-step flow (fork → edit → pull request), what the automated checks do in plain language, and how to run the same checks **on your computer** (including using the **published Docker image** so you do not have to install Python or NCBI tools yourself).
