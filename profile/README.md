# NEMAR Datasets

Research datasets in [Brain Imaging Data Structure (BIDS)](https://bids.neuroimaging.io/) format, hosted on GitHub + AWS S3 infrastructure.

## Purpose

This organization hosts BIDS-formatted datasets that cannot be hosted on public repositories (OpenNeuro, Zenodo, etc.) due to **restrictive licenses**, while remaining **freely available for research use**.

**Hosting criteria:**
- ✅ BIDS-compliant format
- ✅ Freely available for academic/research use
- ✅ Restrictive license preventing hosting on public repositories (e.g., non-commercial, research-only)

## Available Datasets

| Dataset | ID | DOI | Size | Modality | Description |
|---------|----|----|------|----------|-------------|
| [HBN-EEG NC](https://github.com/nemarDatasets/nm000103) | nm000103 | [10.5281/zenodo.17306881](https://doi.org/10.5281/zenodo.17306881) | 270 GB | EEG | Healthy Brain Network EEG, Non-commercial |
| [emg2qwerty](https://github.com/nemarDatasets/nm000104) | nm000104 | [10.5281/zenodo.17287904](https://doi.org/10.5281/zenodo.17287904) | 149 GB | EMG | Typing task sEMG dataset |
| [discrete_gestures](https://github.com/nemarDatasets/nm000105) | nm000105 | [10.5281/zenodo.17283594](https://doi.org/10.5281/zenodo.17283594) | 14 GB | EMG | Hand gesture recognition |
| [handwriting](https://github.com/nemarDatasets/nm000106) | nm000106 | [10.5281/zenodo.17283866](https://doi.org/10.5281/zenodo.17283866) | 30 GB | EMG | Handwriting sEMG dataset |
| [wrist](https://github.com/nemarDatasets/nm000107) | nm000107 | [10.5281/zenodo.17282508](https://doi.org/10.5281/zenodo.17282508) | 1.9 GB | EMG | Wrist control sEMG dataset |

## Downloading Datasets

### Using DataLad (recommended)

[DataLad](https://www.datalad.org/) enables efficient access to large datasets stored across GitHub (metadata) and S3 (data files).

```bash
# Install DataLad (macOS)
brew install datalad

# Clone dataset (lightweight - only downloads metadata)
datalad clone https://github.com/nemarDatasets/nm000107.git
cd nm000107

# Download specific files
datalad get sub-01/emg/sub-01_task-wrist_emg.edf

# Download all data
datalad get .

# Remove data files (keep metadata)
datalad drop .
```

### Using Git (metadata only)

```bash
# Clone repository (metadata only, no large files)
git clone https://github.com/nemarDatasets/nm000107.git
cd nm000107

# View S3 URLs for data files
cat .git/annex/objects/.../...
```

### Direct S3 Access

Large binary files (.edf, .bdf) are stored on S3 with public read access:

```bash
# List dataset files
aws s3 ls s3://nemar/nm000107/ --recursive --no-sign-request

# Download specific file
aws s3 cp s3://nemar/nm000107/path/to/file.edf . --no-sign-request
```

## Contributing

### Reporting Issues

Found incorrect metadata, missing files, or BIDS compliance issues?

1. Go to the dataset repository (e.g., `nm000107`)
2. Click **Issues** → **New Issue**
3. Describe the problem with:
   - File path or subject ID
   - Expected vs actual behavior
   - BIDS validator output (if applicable)

### Proposing Changes

**For metadata corrections** (JSON, TSV, README):

1. **Fork** the dataset repository
2. **Clone** your fork locally
3. Make changes to metadata files
4. **Commit** with clear message: `fix: correct participant age in participants.tsv`
5. **Push** to your fork
6. Open **Pull Request** with description of changes

**For data file issues**:
- File Issues only (data files are immutable annexes)
- Corrections will be released as new dataset versions

### Dataset Versioning

Datasets use semantic versioning (`v1.0.0`, `v1.1.0`, etc.):
- **Patch** (v1.0.1): Metadata fixes, documentation updates
- **Minor** (v1.1.0): New participants, additional sessions
- **Major** (v2.0.0): Breaking changes, restructuring

Each version gets:
- Git tag
- GitHub release
- Zenodo DOI (versioned)

### Revising a Dataset

If you're a dataset maintainer and need to create a new version:

**Step 1: Update Metadata**
```bash
# Clone dataset
datalad clone https://github.com/nemarDatasets/nm000XXX.git
cd nm000XXX

# Update dataset_description.json, README.md, or other metadata
# Make sure Authors field lists only data creators (not curators)

# Save changes
datalad save -m "fix: update metadata with complete author information"

# Push and create PR
datalad push --to origin
gh pr create --title "Update dataset metadata"
```

**Step 2: Version Bump** (after PR merged)

Minor version updates (v1.0.0 → v1.1.0):
- Metadata improvements
- Adding participants/sessions
- Non-breaking changes

This creates:
- New git tag
- New GitHub release
- New version DOI under same concept DOI

**Important Notes:**
- ✅ Only modify metadata files (*.json, *.tsv, *.md)
- ❌ Never modify data files (*.edf, *.bdf) - create new dataset instead
- ✅ Remove curator names from `Authors` field
- ✅ List only original data creators in `Authors`
- ⚠️ DOIs are permanent - review carefully before publication

For detailed workflow, see repository documentation.

## License

Each dataset has its own license specified in `dataset_description.json` and root `LICENSE` file. Common restrictions:
- ✅ Academic/research use
- ❌ Commercial use
- ❌ Redistribution without attribution
- ❌ Public repository hosting (e.g., OpenNeuro)

**Always check the dataset's `LICENSE` file before use.**

## Technical Details

**Infrastructure:**
- **GitHub**: Metadata (JSON, TSV, README) + DataLad/git-annex pointers
- **AWS S3**: Binary data files (EMG recordings)
- **Zenodo**: DOI registration + archived releases

**BIDS Validation:**
- Datasets pass basic BIDS checks (required files, structure)
- Full validator compliance is work in progress

**Data Access:**
- S3 public read access (no AWS account needed)
- No rate limiting on downloads
- Free egress for research use

## Contact

- **Issues**: Use repository-specific issue trackers
- **General questions**: Open discussion in `.github` repository
- **New dataset submissions**: Contact dataset maintainers

---

*Hosted by NEMAR (NeuroElectroMagnetic Archive) infrastructure*
