# NEMAR Datasets

Research datasets in [Brain Imaging Data Structure (BIDS)](https://bids.neuroimaging.io/) format, hosted on GitHub + AWS S3 infrastructure.

## Purpose

This organization hosts BIDS-formatted datasets that cannot be hosted on public repositories (OpenNeuro, Zenodo, etc.) due to **restrictive licenses**, while remaining **freely available for research use**.

**Hosting criteria:**
- BIDS-compliant format
- Freely available for academic/research use
- Restrictive license preventing hosting on public repositories (e.g., non-commercial, research-only)

## Browse Datasets

- **NEMAR Portal**: [nemar.org/dataexplorer](https://nemar.org/dataexplorer)
- **DataCite**: [Search NEMAR DOIs](https://commons.datacite.org/doi.org?query=10.82901%2Fnemar)
- **This org**: Each `nm*` repository is a dataset

## Downloading Datasets

### Using DataLad (recommended)

[DataLad](https://www.datalad.org/) enables efficient access to large datasets stored across GitHub (metadata) and S3 (data files).

```bash
# Install DataLad (macOS)
brew install datalad

# Clone dataset (lightweight - only downloads metadata)
datalad clone https://github.com/nemarDatasets/<dataset-id>.git
cd <dataset-id>

# Download specific files
datalad get sub-01/emg/sub-01_task-wrist_emg.edf

# Download all data
datalad get .

# Remove data files (keep metadata)
datalad drop .
```

### Using NEMAR CLI

```bash
# Install
npm install -g @nemarorg/nemar-cli

# Clone dataset (metadata only)
nemar dataset clone <dataset-id>

# Download data files
nemar dataset get <dataset-id>
```

### Direct S3 Access

Large binary files are stored on S3 with public read access:

```bash
# List dataset files
aws s3 ls s3://nemar/<dataset-id>/ --recursive --no-sign-request

# Download specific file
aws s3 cp s3://nemar/<dataset-id>/path/to/file.edf . --no-sign-request
```

## Contributing

### Reporting Issues

Found incorrect metadata, missing files, or BIDS compliance issues?

1. Go to the dataset repository (e.g., `nm000107`)
2. Click **Issues** > **New Issue**
3. Describe the problem with file path, expected vs actual behavior, and BIDS validator output if applicable

### Proposing Changes

**For metadata corrections** (JSON, TSV, README):

1. Fork the dataset repository
2. Make changes to metadata files
3. Open a Pull Request with description of changes

**For data file issues**: File Issues only (data files are immutable annexes). Corrections will be released as new dataset versions.

### Dataset Versioning

Datasets use semantic versioning (`v1.0.0`, `v1.1.0`, etc.). Each version gets a git tag, GitHub release, and DOI.

## License

Each dataset has its own license specified in `dataset_description.json` and root `LICENSE` file. **Always check the dataset's license before use.**

## Technical Details

- **GitHub**: Metadata (JSON, TSV, README) + DataLad/git-annex pointers
- **AWS S3**: Binary data files (EEG/EMG recordings)
- **EZID/DataCite**: DOI registration (prefix `10.82901/NEMAR`)
- **BIDS Validation**: Automated CI on each repository

## Contact

- **Issues**: Use repository-specific issue trackers
- **General questions**: Open discussion in `.github` repository
- **New dataset submissions**: Visit [nemar.org](https://nemar.org)

---

*Hosted by [NEMAR](https://nemar.org) (NeuroElectroMagnetic Data Archive and Tools Resource)*
