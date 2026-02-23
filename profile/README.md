# NEMAR Datasets

**NeuroElectroMagnetic Data Archive and Tools Resource**

NEMAR goes beyond traditional data archives. Every dataset here is a living, citable research object with deep metadata integration, automated quality assessment, and infrastructure designed to make neuroscience data truly FAIR (Findable, Accessible, Interoperable, Reusable) across disciplines.

## What Makes NEMAR Different

- **Rich, structured metadata** -- Every dataset is enriched with DataCite-compliant scholarly metadata: validated MeSH keywords, author ORCIDs, funding sources, related publications, and machine-readable descriptions
- **Persistent, versioned DOIs** -- Each dataset and version gets its own DOI (prefix `10.82901/NEMAR`), registered with DataCite for global discoverability and citation tracking
- **BIDS-native** -- All datasets follow the [Brain Imaging Data Structure](https://bids.neuroimaging.io/) standard with automated validation on every change
- **Git-based version control** -- Full provenance tracking through DataLad/git-annex, with immutable data blobs on S3 and metadata history on GitHub
- **Public S3 access** -- No accounts, no rate limits, no paywalls for research use

**Coming soon:** Data quality cards, citation tracking, and integration into ML pipelines, making NEMAR a resource for researchers across neuroscience, biomedical engineering, and machine learning.

## Browse Datasets

- **NEMAR Portal**: [nemar.org/dataexplorer](https://nemar.org/dataexplorer)
- **DataCite**: [Search NEMAR DOIs](https://commons.datacite.org/doi.org?query=10.82901%2Fnemar)
- **This org**: Each `nm*` repository is a dataset

## Downloading Datasets

### Using NEMAR CLI

```bash
npm install -g @nemarorg/nemar-cli

# Clone dataset (metadata only, lightweight)
nemar dataset clone <dataset-id>

# Download data files
nemar dataset get <dataset-id>
```

### Using DataLad

[DataLad](https://www.datalad.org/) provides efficient, selective access to large datasets stored across GitHub (metadata) and S3 (data files).

```bash
# Clone (metadata only)
datalad clone https://github.com/nemarDatasets/<dataset-id>.git
cd <dataset-id>

# Download specific files or everything
datalad get sub-01/emg/sub-01_task-wrist_emg.edf
datalad get .

# Free local copies when done
datalad drop .
```

### Direct S3 Access

```bash
aws s3 ls s3://nemar/<dataset-id>/ --recursive --no-sign-request
aws s3 cp s3://nemar/<dataset-id>/path/to/file.edf . --no-sign-request
```

## Contributing

**Reporting issues:** Open an issue on the dataset repository with the file path, expected vs actual behavior, and BIDS validator output if applicable.

**Proposing changes:** Fork the repository, make metadata corrections (JSON, TSV, README), and open a Pull Request. Data files are immutable; corrections are released as new versions.

**Versioning:** Datasets use semantic versioning. Each version gets a git tag, GitHub release, and DOI.

## License

Each dataset specifies its own license in `dataset_description.json`. Always check the license before use.

## Contact

- **Issues**: Repository-specific issue trackers
- **General questions**: [.github discussions](https://github.com/nemarDatasets/.github/discussions)
- **New submissions**: Visit [nemar.org](https://nemar.org)

---

*[NEMAR](https://nemar.org) -- NeuroElectroMagnetic Data Archive and Tools Resource*
