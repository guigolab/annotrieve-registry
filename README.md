# Annotrieve community annotations registry

This repository lists **community-contributed genome annotations** in a structured form so they can be reviewed and, once merged, consumed by  [genome-annotation-tracker](https://github.com/guigolab/genome-annotation-tracker) and then feeded to [Annotrieve](https://genome.crg.eu/annotrieve)

## Layout

Each contribution lives under its own directory:

```text
<project_name>/
  manifest.yaml      # Provider + pipeline metadata (required fields below)
  annotations.tsv    # Tab-separated: assembly_accession + access_url (one URL per assembly)
```

See [`schema/annotations.tsv.header`](schema/annotations.tsv.header) for the exact header line and [`schema/manifest.schema.json`](schema/manifest.schema.json) for the manifest schema.

## Contributing

Read [`CONTRIBUTING.md`](CONTRIBUTING.md) for the pull-request workflow and what CI validates on each PR.

On pull requests, CI posts a **short summary** on the conversation tab and leaves **inline review comments** on each problem line under **Files changed** (via GitHub pull request reviews).

## Example

[`examples/sample_project/`](examples/sample_project/) is a template you can copy when adding a new project folder.
