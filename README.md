# ISO / IATF Certificate Extraction, Quality Monitoring & Human Review Pipeline

An enterprise-grade, multimodal AI pipeline for automated extraction, rule-based verification, drift monitoring, LLM-as-a-judge evaluation, and human-in-the-loop review of ISO and IATF certificates.

---

## Architecture Overview

```
                                      [ Input URLs / Documents ]
                                                 │
                                                 ▼
                                     [ Multimodal Extractor ]
                                   (Gemini 2.5 Flash Lite + PyMuPDF)
                                                 │
                        ┌────────────────────────┴────────────────────────┐
                        ▼                                                 ▼
             [ Language Classifier ]                          [ Rule-Based Verifier ]
             (Full Document Language)                         - Confidence Score (0–100)
                        │                                     - Verdict (Approved / Review / Reject)
                        │                                                 │
                        └────────────────────────┬────────────────────────┘
                                                 ▼
                                     [ Observability & Storage ]
                                     - Langfuse Distributed Tracing
                                     - SQLite Database (certificate_logs.db)
                                                 │
                        ┌────────────────────────┴────────────────────────┐
                        ▼                                                 ▼
          [ Stratified LLM-as-a-Judge ]                      [ Drift & Quality Engine ]
          - Online 15% sample evaluation                     - Input Drift (PDF pages, image pixels)
          - Ground truth reasoning                           - Output Drift (Languages, missing fields)
                                                             - Quality Drift (Error rate, Judge pass)
                                                             - Alarms & Deduplication (alarms.json)
                                                                          │
                                                                          ▼
                                                       [ FastAPI Monitoring Dashboard ]
                                                       - Production Health KPIs & Latency
                                                       - Drift Visualizations vs. Frozen Reference
                                                       - Golden Dataset Baseline Comparison
                                                       - 30% Dynamic Prioritized Human Review Queue
```

---

## Key Features

### 1. Multimodal Extraction (`extractor/`)
- Extracts structured metadata from both **PDFs** (via PyMuPDF page rendering) and **Images** (`.png`, `.jpg`, `.jpeg`, `.tiff`, `.webp`).
- Utilizes **Gemini 2.5 Flash Lite** with strict Pydantic structured output:
  - Screening: `is_certificate`, `is_iso_iatf`, `cert_type`, `screening_reason`
  - Core Metadata: `site_name`, `address`, `scope`, `iso_type`, `issued_date`, `expiration_date`, `site_iso_type`
  - Full-document certificate language detection.

### 2. Rule-Based Verification & Scoring (`verifier/`)
- Strict deterministic rules:
  - Date sanity checks (issued date $\le$ expiration date, non-expired validity, ISO format validation).
  - Scope coverage and standard validation (ISO 9001, ISO 14001, ISO 45001, ISO 27001, IATF 16949, etc.).
  - Address and site name consistency checks.
- Generates a weighted **Confidence Score (0–100)** and a business verdict:
  - `Approved`: High confidence, all validation rules passed.
  - `Need Review`: Missing optional fields, unreadable/open errors, or moderate confidence.
  - `Reject`: Non-certificate documents, non-ISO standards, or contradictory dates.

### 3. Observability & SQLite Logging (`tracing/`, `database/`)
- Distributed tracing integrated via **Langfuse** (tracks latencies, token consumption, and cost in USD).
- Persistent SQLite logging (`analytics/certificate_logs.db`) tracking production vs. reference runs.
- Excel export utility with conditional formatting and styling:
  ```bash
  python export_db_to_excel.py
  ```

### 4. Stratified Online LLM-as-a-Judge (`judge/`)
- Evaluates a random stratified sample (~15%) of production extractions against the original document.
- Assigns binary scoring (`1` = Pass, `0` = Fail) with explanatory reasoning, persisting evaluation verdicts directly into the database.

### 5. Automated Drift & Quality Monitoring (`drift/`)
- Evaluates rolling production windows against a frozen reference baseline:
  - **Input Drift:** PDF page count shifts and image pixel size changes using Cohen's $d$ and percentage change.
  - **Output Drift:** Total Variation Distance (TVD) for language distribution, novel language alerting, average missing core fields per valid certificate, and `site_iso_type` undefined ratios.
  - **Quality Drift:** Technical pipeline error rate monitoring and online judge pass rate collapse detection.
- Generates deduplicated actionable alarms in `analytics/alarms.json` with sample window tracking (`from_index` to `to_index`).

### 6. Interactive FastAPI Monitoring & Human Review Dashboard (`dashboard/`)
- **Overview:** Real-time production throughput, latency percentiles ($p_{50}$ / $p_{95}$), technical error rate, and sample window health.
- **Drift Tab:** Interactive time-series visual charts comparing production windows against frozen reference baselines.
- **Golden Dataset Tab:** Multi-baseline comparisons (Pass Rate %, Accuracy by Field, Average Score, and False Positive / False Pass Rate % with regression badges).
- **Alerts Tab:** History of CRITICAL and WARNING alerts with sample window ranges.
- **Review Queue:** Dynamic 30% prioritized review queue (surfacing low-confidence records, unreadable files, judge failures, and random audits) with:
  - External URL viewer button.
  - Interactive decision controls (`Approved`, `Need Review`, `Reject`) with field-level correction inputs.
  - Automatic persistence to `reviewed/human_review.json` and automatic replenishment of the queue.
- **Reviewed Cases:** Chronological audit trail of all completed human reviews and field corrections.

---

## Directory Structure

```
├── analytics/                      # Databases, drift reports, alarms, and baseline files
│   ├── baseline.json               # Active golden dataset baseline
│   ├── certificate_logs.db         # SQLite database logging all extractions
│   ├── drift_test.json             # Rolling window drift test results
│   └── alarms.json                 # Generated drift and quality alarms
├── dashboard/                      # FastAPI Monitoring & Review application
│   ├── app.py                      # FastAPI server & API routes
│   ├── review_queue.py             # 30% priority queue sampling logic
│   └── static/                     # Vanilla CSS & JS frontend
│       ├── index.html              # Dashboard layout
│       ├── app.js                  # Frontend state, charts, and API client
│       └── styles.css              # Custom dark-mode design system
├── database/                       # Database schema and SQLite query helpers
├── dataset_validation/             # Golden dataset evaluation & baseline generation
├── drift/                          # Input, output, and quality drift detection engine
├── extractor/                      # Multimodal Gemini 2.5 Flash Lite extractor
├── judge/                          # LLM-as-a-judge evaluation module
├── reviewed/                       # Human review audit logs
│   └── human_review.json           # Persisted human verdicts and field corrections
├── tests/                          # Automated unit and integration test suite
├── tracing/                        # Langfuse observability integration
├── export_db_to_excel.py           # Database to styled Excel export script
├── main.py                         # Unified CLI entry point
└── requirements.txt                # Python project dependencies
```

---

## Installation & Setup

### 1. Clone Repository & Create Virtual Environment
```bash
git clone https://github.com/WasseemEmad/Month_2_project_certifcate_extraction_and_monitoring.git
cd Month_2_project_certifcate_extraction_and_monitoring

python -m venv venv
# Linux / macOS:
source venv/bin/activate
# Windows:
.\venv\Scripts\Activate.ps1
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure Environment Variables
Create a `.env` file (or provide `gemini.txt`):
```env
GEMINI_API_KEY=your_gemini_api_key_here
LANGFUSE_PUBLIC_KEY=your_langfuse_public_key_optional
LANGFUSE_SECRET_KEY=your_langfuse_secret_key_optional
LANGFUSE_HOST=https://cloud.langfuse.com
```

---

## Usage

### Unified CLI (`main.py`)

Run the pipeline interactively or supply arguments:

```bash
python main.py
```

Select a mode:
- **`[1] Run Extraction`**: Extract certificates from an Excel file containing URLs.
  ```bash
  python main.py 1 --input input_production.xlsx --workers 4
  ```
- **`[2] Dataset Validation`**: Run golden dataset validation against `reference_set.xlsx` and compare against `analytics/baseline.json`.
  ```bash
  python main.py 2
  ```
- **`[3] Production Pipeline`**: Full production execution including multimodal extraction, rule-based verification, Langfuse tracing, SQLite logging, ~15% online judge evaluation, and rolling drift testing.
  ```bash
  python main.py 3 --input input_production.xlsx --workers 4
  ```

---

### Starting the Dashboard

Launch the FastAPI dashboard:
```bash
python dashboard/app.py --port 8050
```
Open your browser at:
- **Dashboard:** [http://127.0.0.1:8050](http://127.0.0.1:8050)
- **Interactive Swagger API Docs:** [http://127.0.0.1:8050/docs](http://127.0.0.1:8050/docs)

---

## Running Automated Tests

Run the test suite covering extraction, rule verification, baseline evaluation, drift persistence, deduplication, and dashboard features:

```bash
python -m unittest discover tests
```

---

## Continuous Integration (CI)

The repository includes a GitHub Actions workflow (`.github/workflows/prompt-eval.yml`) that runs on every push and pull request to `main`:
- Executes golden dataset regression validation (Mode 2).
- Validates prompt performance and checks for feature regressions against `analytics/baseline.json`.
- Automatically posts a detailed Markdown step summary in the GitHub Actions run.
