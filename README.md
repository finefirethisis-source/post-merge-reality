### Do These Violent Delights Have Violent Ends? Measuring the Post-Merge Fate of Agentic Code

<p align="center">
    <a href="https://arxiv.org/pdf/2607.09902"><img src="https://img.shields.io/badge/📃-Arxiv-b31b1b?style=for-the-badge"></a>
    <a href="https://post-merge-reality.github.io"><img src="https://img.shields.io/badge/🌐-Webpage-3d85c6?style=for-the-badge"></a>
    <a href="https://github.com/post-merge-reality/post-merge-reality/blob/master/LICENSE"><img src="https://img.shields.io/badge/Apache-LICENSE-b0aeaf?style=for-the-badge"></a>
</p>
  
<p align="center">
    <big><a href="#-setup">⚡Setup</a></big> |
    <big><a href="#-data-collection">🔢Data Collection</a></big> |
    <big><a href="#-analysis">📊Analysis</a></big> |
    <big><a href="#-citation">📝Citation</a></big> 
</p>

This repository contains the source code for our paper <i> "Do These Violent Delights Have Violent Ends? Measuring the Post-Merge Fate of Agentic Code" </i>

## ⚡ Setup

Setup your python environment and install dependencies:
```sh
uv venv 
uv pip install -r requirements.txt

source .venv/bin/activate
export PYTHONPATH=$PYTHONPATH:$(pwd)
```

## 🔢 Data Collection

> [!Important]
> The raw data for the full sample are very large.
> Collecting them end-to-end took roughly two weeks in our environment.

We provide the list of the 182 projects used in the study in `filtered_full_sample_repos.csv`. 

The commands below can also be run on a subset by setting `MAX_REPOS`, `START_AT`, or by passing a smaller CSV with the same `repo_slug` and `primary_language` columns.

All raw per-repository outputs are written under:

```text
src/webscrape/output/<owner>__<repo>/
```

##### Raw Data Shortcut

You can skip the raw-data collection steps below. 
Download `csv_full.tar.gz` from [here](temp) and extract it at the repository root:

```sh
tar -xzf csv_full.tar.gz
```

##### 1. Commit scraping, classification, and lifecycle analysis

The scraper clones each repository, collects commit metadata and file-level diffs, and reconstructs line lifecycles with `git blame`. This step writes `all_commits.jsonl`, `observation_commits.jsonl`, `commit_file_changes.jsonl`, `line_lifecycle.jsonl`, `summary.json`, and `summary.csv` for each repository.

```sh
CSV_PATH=filtered_full_sample_repos.csv \
REPO_WORKERS=1 \
BLAME_WORKERS=4 \
./scripts/run_initial_sample_scrape.sh
```

The heuristic contribution classifier then labels commits and changed files as human-authored, AI-coauthored, or bot/unknown where applicable. It writes `commit_contribution_classifications.jsonl` and `commit_file_contribution_classifications.jsonl`.

```sh
CSV_PATH=filtered_full_sample_repos.csv \
REPO_WORKERS=4 \
./scripts/run_initial_sample_classification.sh
```

##### 2. Commit maintenance intent classification

Maintenance intent labels are produced by the LLM classification pipeline. This step reads the heuristic contribution classifications and writes `llm_commit_contribution_classifications.jsonl` for each repository. 
By default, the wrapper uses OpenRouter, so set `OPENROUTER_API_KEY` before running it or override `PROVIDER`, `CLIENT`, and `MODEL` as needed.

```sh
export OPENROUTER_API_KEY=<your_api_key>

CSV_PATH=filtered_full_sample_repos.csv \
REPO_WORKERS=1 \
PROVIDER=openrouter \
MODEL=minimax/minimax-m2.7 \
./scripts/run_initial_sample_llm_classification.sh
```

After generating the LLM labels, apply them back onto the commit and file classification outputs:

```sh
./.venv/bin/python src/llm/apply_llm_classifications.py \
  --input-dir src/webscrape/output
```

##### 3. Static analysis

The static-analysis pipeline has three independent parts. 

The Semgrep wrapper analyzes each changed version of each commit and writes `commit_static_findings.jsonl` and `commit_static_analysis_summary.jsonl`.

```sh
CSV_PATH=filtered_full_sample_repos.csv \
REPO_WORKERS=1 \
COMMIT_WORKERS=1 \
SEMGREP_BIN=semgrep \
./scripts/run_initial_sample_static_analysis.sh
```

The supply-chain wrapper runs OSV Scanner and writes `commit_supply_chain_findings.jsonl` and `commit_supply_chain_summary.jsonl`.

```sh
CSV_PATH=filtered_full_sample_repos.csv \
REPO_WORKERS=1 \
COMMIT_WORKERS=1 \
OSV_BIN=osv-scanner \
./scripts/run_initial_sample_supply_chain_analysis.sh
```

The SonarQube snapshot wrapper collects repository-level static-quality metrics over time and writes `sonarqube_snapshot_scans.jsonl`. This step requires a running SonarQube instance, `sonar-scanner`, and a start date or explicit snapshot date.

```sh
SINCE=<YYYY-MM-DD> \
CSV_PATH=filtered_full_sample_repos.csv \
REPO_WORKERS=1 \
SONAR_HOST_URL=http://localhost:9000 \
SONAR_TOKEN=<your_sonar_token> \
./scripts/run_initial_sample_sonarqube_snapshot_analysis.sh
```

#### 4. Agentic code share

The code-share pipeline measures the share of living code attributed to AI-coauthored commits at periodic snapshots. It writes `codebase_agent_share_snapshots.jsonl` and `codebase_agent_share_files.jsonl`. This step uses the scraped commit data and the contribution labels from the previous steps.

```sh
SINCE=<YYYY-MM-DD> \
CSV_PATH=filtered_full_sample_repos.csv \
REPO_WORKERS=1 \
BLAME_WORKERS=4 \
./scripts/run_initial_sample_codebase_agent_share.sh
```

After the raw JSONL outputs have been collected, export the analysis CSVs used by the R notebooks and helper scripts:

```sh
./.venv/bin/python src/webscrape/export_scrape_tables.py \
  --input-dir src/webscrape/output \
  --output-dir csv_full \
  --repo-file filtered_full_sample_repos.csv \
  --csv-profile compact
```

## 📊 Analysis

After collecting or downloading the raw results, place the exported CSV files in `csv_full/`. The analysis notebooks use repo-relative paths, so they can be run from either the repository root or the `R_code/` directory.

The full line lifecycle CSV is large. If `csv_full/line_lifecycle.parquet` and `csv_full/line_weighted.rds` are already present, use them directly. If they are not present, open `R_code/Data_Analysis_rq1.Rmd` and run the setup/data-loading chunks that convert `line_lifecycle.csv` to Parquet and build `line_weighted.rds`.

The primary statistical analyses are in the R notebooks:

```text
R_code/Data_Analysis_rq1.Rmd
R_code/Data_Analysis_rq2.Rmd
R_code/Data_Analysis_rq3.Rmd
```

Run them in this order:

1. `R_code/Data_Analysis_rq1.Rmd` for RQ1a, RQ1b, and RQ1c.
2. `R_code/Data_Analysis_rq2.Rmd` for RQ2. This notebook reads `csv_full/rq2_repo_week_panel.csv`.
3. `R_code/Data_Analysis_rq3.Rmd` for RQ3. This notebook reuses the weighted line table produced by the RQ1 workflow when available.

The notebooks are intended to be run interactively in RStudio or another R notebook environment so expensive setup chunks can be skipped when cached artifacts already exist.

##### RQ1 visualizations

The RQ1a, RQ1b, and RQ1c helper scripts can rebuild their compact CSVs from the per-repository scraper output in `src/webscrape/output/`. 

To regenerate from raw per-repo outputs, omit the `--use-compact-csvs` or `--use-compact-csv` flags.

To reproduce the included figures from the compact CSVs:

```sh
uv run python dev/analysis/explore_visualization_rq1a.py \
  --repo-file dev/analysis/full_sample_repos.txt \
  --workers 10 \
  --use-compact-csvs

uv run python dev/analysis/explore_visualization_rq1b.py \
  --repo-file dev/analysis/full_sample_repos.txt \
  --workers 10 \
  --use-compact-csvs

uv run python dev/analysis/explore_visualization_rq1c.py \
  --repo-file dev/analysis/full_sample_repos.txt \
  --workers 10 \
  --use-compact-csv
```

These commands write figures and compact CSVs under:

```text
dev/analysis/output_rq1a_explore/
dev/analysis/output_rq1b_explore/
dev/analysis/output_rq1c_explore/
```

##### RQ1 Fine-Gray models

The Fine-Gray pipeline has two stages. The Python stage prepares compact line-survival CSVs from per-repository scraper output:

```sh
uv run python dev/analysis/fine_gray_rq1.py \
  --repo-file dev/analysis/full_sample_repos.txt \
  --workers 10
```

In the packaged repository, the large Fine-Gray line-data CSVs are stored as `.csv.gz` files to keep the archive GitHub-friendly. 

If you are using the packaged outputs instead of regenerating them with the Python stage, expand them before running the R stage:

```sh
gzip -dk dev/analysis/output_rq1/rq1_fine_gray_maintenance_class_line_data.csv.gz
gzip -dk dev/analysis/output_rq1/rq1_fine_gray_operational_intent_line_data.csv.gz
```

The R stage fits the Fine-Gray models and writes CIF/model outputs:

```sh
Rscript dev/analysis/fine_gray_rq1.R --event-taxonomy maintenance_class
Rscript dev/analysis/fine_gray_rq1.R --event-taxonomy operational_intent
```

Outputs are written to `dev/analysis/output_rq1/`.

##### RQ1c static and supply-chain figures

The static-analysis helper script can reuse the compact CSVs already in
`dev/analysis/output_static_rq1c/`:

```sh
uv run python dev/analysis/static_analysis_rq1c.py \
  --repo-file dev/analysis/full_sample_repos.txt \
  --workers 10 \
  --use-compact-csvs
```

This writes the Semgrep and OSV severity figures to
`dev/analysis/output_static_rq1c/`.


##### RQ2

The RQ2 notebook reads `csv_full/rq2_repo_week_panel.csv`. If you need to rebuild that panel from per-repository scraper output, run:

```sh
uv run python dev/analysis/ai_code_proportion_rq2.py \
  --repo-file dev/analysis/full_sample_repos.txt \
  --workers 10 \
  --panel-csv csv_full/rq2_repo_week_panel.csv \
  --output-dir dev/analysis/output_rq2
```

Then run `R_code/Data_Analysis_rq2.Rmd` for the mixed-effects RQ2 models.


After the RQ2 model prediction CSV is available in `dev/analysis/output_rq2/`, you can regenerate the figure from the existing panel with:

```sh
uv run python dev/analysis/ai_code_proportion_rq2.py \
  --use-panel-csv csv_full/rq2_repo_week_panel.csv \
  --output-dir dev/analysis/output_rq2
```

##### RQ3

Run `R_code/Data_Analysis_rq3.Rmd` after the RQ1 weighted line table exists. The notebook builds per-repository Cox estimates, constructs the repo-level covariate table, and fits the RQ3 meta-regression models.


## 📝 Citation

```bibtex
@article{postmerge26,
  title   = {Do These Violent Delights Have Violent Ends? 
             Measuring the Post-Merge Fate of Agentic Code},
  author  = {Xia, Chunqiu Steven and Miller, Courtney},
  year    = {2026},
  journal = {arXiv preprint arXiv:2607.09902},
}
```
