"""
dashboard/app.py
================
Monitoring, Drift, Golden Dataset Baselines & Human Review Dashboard
for ISO / IATF Certificate Extraction Pipeline (FastAPI Application).

Endpoints:
  - GET  /                           : Serves interactive HTML dashboard
  - GET  /api/dashboard              : Aggregated dashboard payload (Overview, Drift, Baselines, Alerts, Queue)
  - GET  /api/baseline?file=...      : Dynamic golden dataset baseline details & comparisons
  - GET  /api/review-queue           : Dynamic 30% prioritized pending review queue
  - GET  /api/review-queue/next      : Single case review payload (prioritized or by request_id)
  - GET  /api/reviewed               : All reviewed decisions and field corrections from reviewed/human_review.json
  - POST /api/review-queue/decision  : Save human review verdict and corrections, auto-promoting next candidate
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import uvicorn

DASHBOARD_DIR = Path(__file__).resolve().parent
PROJECT_DIR = DASHBOARD_DIR.parent
STATIC_DIR = DASHBOARD_DIR / "static"

if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))
if str(PROJECT_DIR) not in sys.path:
    sys.path.append(PROJECT_DIR)

import review_queue

# ==============================================================================
# Configuration & Paths
# ==============================================================================

DEFAULT_DB_PATH = PROJECT_DIR / "analytics" / "certificate_logs.db"
DEFAULT_DRIFT_JSON = PROJECT_DIR / "analytics" / "drift_test.json"
DEFAULT_ALARMS_JSON = PROJECT_DIR / "analytics" / "alarms.json"
ANALYTICS_DIR = PROJECT_DIR / "analytics"
REVIEWED_JSON_PATH = PROJECT_DIR / "reviewed" / "human_review.json"

ROLLING_LATENCY_WINDOW = 30
ROLLING_LATENCY_MIN = 10
P95_LATENCY_THRESHOLD_MS = 25000.0  # 25 seconds for document vision extraction
ERROR_RATE_THRESHOLD = 0.10

# Dynamic DB Path holder (allows CLI override)
_ACTIVE_DB_PATH = DEFAULT_DB_PATH


# ==============================================================================
# Ingestion & Loading Helpers
# ==============================================================================

def load_json_file(file_path: Path) -> Any:
    if not file_path.exists():
        return None
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def nearest_rank(values: List[float], q: float) -> Optional[float]:
    """Nearest-rank percentile calculation."""
    clean = [v for v in values if v is not None and not math.isnan(v)]
    if not clean:
        return None
    s = sorted(clean)
    idx = int(math.ceil(q * len(s))) - 1
    return round(s[max(0, min(idx, len(s) - 1))], 1)


def load_certificate_records(db_path: Path = DEFAULT_DB_PATH) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Returns (production_records, reference_records) from certificate_logs.db.
    """
    if not db_path.exists():
        return [], []

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM certificate_requests ORDER BY id ASC")
        rows = cursor.fetchall()
        all_records = [dict(r) for r in rows]
    finally:
        conn.close()

    ref_records = [r for r in all_records if str(r.get("traffic_type", "")).lower() == "reference"]
    prod_records = [r for r in all_records if str(r.get("traffic_type", "")).lower() != "reference"]

    for pos, r in enumerate(prod_records, start=1):
        r["request_number"] = pos

    return prod_records, ref_records


# ==============================================================================
# Dashboard Section Builders
# ==============================================================================

def build_overview_section(
    prod_records: List[Dict[str, Any]],
    ref_records: List[Dict[str, Any]],
    drift_tests: List[Dict[str, Any]],
    alarms: List[Dict[str, Any]],
) -> Dict[str, Any]:
    n_prod = len(prod_records)
    n_ref = len(ref_records)

    # Verdicts breakdown
    approved = sum(1 for r in prod_records if str(r.get("verdict", "")).lower() == "approved")
    need_review = sum(1 for r in prod_records if str(r.get("verdict", "")).lower() == "need review")
    rejected = sum(1 for r in prod_records if str(r.get("verdict", "")).lower() == "reject")

    # Error count (failed download / could not open document or explicit runtime error)
    def is_err(r: Dict[str, Any]) -> bool:
        st = str(r.get("status") or "").lower()
        err = r.get("error")
        checks = str(r.get("checks_passed_or_failed") or "").lower()
        doc_type = str(r.get("doc_type") or "").lower()
        page_count = r.get("page_count")

        if err and str(err).strip():
            return True
        if "did not open" in st or "did not open" in checks:
            return True
        if "failed" in st or "error" in st:
            return True
        if doc_type == "pdf" and page_count is not None and page_count == 0:
            return True
        return False

    errors = sum(1 for r in prod_records if is_err(r))
    error_rate_pct = round(errors / n_prod * 100.0, 2) if n_prod > 0 else 0.0

    # Latencies & Costs
    latencies = [float(r["total_latency_ms"]) for r in prod_records if r.get("total_latency_ms") is not None]
    costs = [float(r["cost_usd"]) for r in prod_records if r.get("cost_usd") is not None]
    conf_scores = [float(r["confidence_score"]) for r in prod_records if r.get("confidence_score") is not None]

    p50_lat = nearest_rank(latencies, 0.50)
    p95_lat = nearest_rank(latencies, 0.95)
    avg_cost = round(sum(costs) / len(costs), 6) if costs else 0.0
    tot_cost = round(sum(costs), 4) if costs else 0.0
    avg_conf = round(sum(conf_scores) / len(conf_scores), 1) if conf_scores else 0.0

    # Rolling p95 latency
    rolling_p95 = []
    for i in range(len(latencies)):
        win = latencies[max(0, i - ROLLING_LATENCY_WINDOW + 1): i + 1]
        rolling_p95.append(nearest_rank(win, 0.95) if len(win) >= ROLLING_LATENCY_MIN else None)

    # Windows summary from drift tests
    windows = []
    if ref_records:
        ref_lat = [float(r["total_latency_ms"]) for r in ref_records if r.get("total_latency_ms") is not None]
        ref_cost = [float(r["cost_usd"]) for r in ref_records if r.get("cost_usd") is not None]
        windows.append({
            "label": f"Reference Set ({n_ref} baseline)",
            "kind": "reference",
            "request_range": f"1–{n_ref}",
            "count": n_ref,
            "error_rate_pct": round(sum(1 for r in ref_records if is_err(r)) / n_ref * 100.0, 1),
            "p50_latency_ms": nearest_rank(ref_lat, 0.50),
            "p95_latency_ms": nearest_rank(ref_lat, 0.95),
            "avg_cost_usd": round(sum(ref_cost) / len(ref_cost), 6) if ref_cost else 0.0,
            "status": "BASELINE",
        })

    for idx, t in enumerate(drift_tests, start=1):
        from_idx = t.get("from_index")
        to_idx = t.get("to_index")
        range_str = f"#{from_idx}–#{to_idx}" if from_idx is not None else f"Window #{idx}"
        windows.append({
            "label": f"Test #{idx} ({range_str})",
            "kind": "recent",
            "request_range": range_str,
            "count": t.get("recent_sample_count", 50),
            "error_rate_pct": t.get("quality_drift", {}).get("error_rate", {}).get("recent_rate_pct", 0.0),
            "status": t.get("status", "STABLE"),
            "input_drift": t.get("input_drift", {}).get("input_drift_detected", False),
            "output_drift": t.get("output_drift", {}).get("output_drift_detected", False),
            "quality_drift": t.get("quality_drift", {}).get("quality_drift_detected", False),
            "alarm_count": t.get("alarm_count", 0),
        })

    return {
        "kpis": {
            "total_production_requests": n_prod,
            "total_reference_requests": n_ref,
            "approved_count": approved,
            "approved_pct": round(approved / n_prod * 100.0, 1) if n_prod else 0.0,
            "need_review_count": need_review,
            "need_review_pct": round(need_review / n_prod * 100.0, 1) if n_prod else 0.0,
            "reject_count": rejected,
            "reject_pct": round(rejected / n_prod * 100.0, 1) if n_prod else 0.0,
            "error_count": errors,
            "error_rate_pct": error_rate_pct,
            "avg_confidence_score": avg_conf,
            "p50_latency_ms": p50_lat,
            "p95_latency_ms": p95_lat,
            "avg_cost_usd": avg_cost,
            "total_cost_usd": tot_cost,
            "active_alarms_count": len(alarms),
            "critical_alarms_count": sum(1 for a in alarms if str(a.get("severity")).upper() == "CRITICAL"),
        },
        "overall": {
            "count": n_prod,
        },
        "windows": windows,
        "latency_series": {
            "request_numbers": [r["request_number"] for r in prod_records],
            "latency_ms": [round(x, 1) for x in latencies],
            "rolling_p95_ms": rolling_p95,
        },
        "p95_threshold_ms": P95_LATENCY_THRESHOLD_MS,
    }


def build_drift_section(drift_tests: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Formats all drift tests recorded in analytics/drift_test.json."""
    if not drift_tests:
        return {"tests": [], "has_drift": False, "total_tests": 0}

    formatted_tests = []
    for idx, t in enumerate(drift_tests, start=1):
        from_idx = t.get("from_index")
        to_idx = t.get("to_index")
        range_label = f"Samples #{from_idx} to #{to_idx}" if from_idx is not None else f"Window #{idx}"

        formatted_tests.append({
            "test_id": t.get("test_id"),
            "index": idx,
            "timestamp": t.get("timestamp"),
            "range_label": range_label,
            "from_index": from_idx,
            "to_index": to_idx,
            "status": t.get("status", "STABLE"),
            "overall_drift": t.get("overall_drift_detected", False),
            "input_drift": t.get("input_drift", {}),
            "output_drift": t.get("output_drift", {}),
            "quality_drift": t.get("quality_drift", {}),
            "alarm_count": t.get("alarm_count", 0),
        })

    any_drift = any(t["overall_drift"] for t in formatted_tests)
    return {
        "tests": formatted_tests,
        "has_drift": any_drift,
        "total_tests": len(formatted_tests),
        "latest_test": formatted_tests[-1] if formatted_tests else None,
    }


def build_golden_baselines_section(analytics_dir: Path = ANALYTICS_DIR, target_file: Optional[str] = None) -> Dict[str, Any]:
    """
    Finds all JSON files matching *baseline*.json in analytics/.
    Default is 'baseline.json' (the latest/active baseline).
    Loads and provides side-by-side comparison with older versions (e.g. old_baseline.json).
    """
    if not analytics_dir.exists():
        return {"available": False, "files": [], "active_baseline": None}

    baseline_files = sorted(
        [f.name for f in analytics_dir.glob("*baseline*.json")],
        key=lambda x: (0 if x == "baseline.json" else 1, x),
    )

    if not baseline_files:
        return {"available": False, "files": [], "active_baseline": None}

    # Load active baseline (baseline.json)
    active_path = analytics_dir / "baseline.json"
    if not active_path.exists() and baseline_files:
        active_path = analytics_dir / baseline_files[0]
    active_data = load_json_file(active_path)

    # Load comparison baseline
    compare_filename = target_file if target_file in baseline_files else (
        "old_baseline.json" if "old_baseline.json" in baseline_files and active_path.name != "old_baseline.json" else active_path.name
    )
    compare_path = analytics_dir / compare_filename
    compare_data = load_json_file(compare_path) if compare_path.exists() else active_data

    # Summary metric comparison rows (Pass Rate, False Positive Rate, Total Score)
    feature_rows = []

    act_pass = (active_data or {}).get("pass_rate_percent")
    cmp_pass = (compare_data or {}).get("pass_rate_percent")
    diff_pass = round(act_pass - cmp_pass, 2) if act_pass is not None and cmp_pass is not None else 0.0
    feature_rows.append({
        "key": "overall_pass_rate",
        "label": "Overall Golden Pass Rate (%)",
        "active_score": act_pass,
        "active_accuracy_pct": act_pass,
        "compare_score": cmp_pass,
        "compare_accuracy_pct": cmp_pass,
        "delta_score": diff_pass,
        "is_pct": True,
        "improved": diff_pass > 0,
        "regressed": diff_pass < 0,
    })

    act_fp = (active_data or {}).get("false_pass_rate_total_percent")
    cmp_fp = (compare_data or {}).get("false_pass_rate_total_percent")
    diff_fp = round(act_fp - cmp_fp, 2) if act_fp is not None and cmp_fp is not None else 0.0
    feature_rows.append({
        "key": "false_positive_rate",
        "label": "False Positive / False Pass Rate (%)",
        "active_score": act_fp,
        "active_accuracy_pct": act_fp,
        "compare_score": cmp_fp,
        "compare_accuracy_pct": cmp_fp,
        "delta_score": diff_fp,
        "is_pct": True,
        "is_fp": True,
        "improved": diff_fp < 0,  # lower false positive rate is better!
        "regressed": diff_fp > 0,
    })

    act_fpc = (active_data or {}).get("false_pass_cases")
    cmp_fpc = (compare_data or {}).get("false_pass_cases")
    diff_fpc = (act_fpc - cmp_fpc) if act_fpc is not None and cmp_fpc is not None else 0
    feature_rows.append({
        "key": "false_positive_cases",
        "label": "False Positive Cases (Count)",
        "active_score": act_fpc,
        "active_accuracy_pct": act_fpc,
        "compare_score": cmp_fpc,
        "compare_accuracy_pct": cmp_fpc,
        "delta_score": diff_fpc,
        "is_count": True,
        "is_fp": True,
        "improved": diff_fpc < 0,  # fewer false positive cases is better!
        "regressed": diff_fpc > 0,
    })

    act_tot = (active_data or {}).get("average_total_score")
    cmp_tot = (compare_data or {}).get("average_total_score")
    diff_tot = round(act_tot - cmp_tot, 2) if act_tot is not None and cmp_tot is not None else 0.0
    feature_rows.append({
        "key": "average_total_score",
        "label": "Average Total Score (/10)",
        "active_score": act_tot,
        "active_accuracy_pct": round(act_tot * 10, 1) if act_tot is not None else None,
        "compare_score": cmp_tot,
        "compare_accuracy_pct": round(cmp_tot * 10, 1) if cmp_tot is not None else None,
        "delta_score": diff_tot,
        "improved": diff_tot > 0,
        "regressed": diff_tot < 0,
    })

    act_feats = (active_data or {}).get("feature_scores", {})
    cmp_feats = (compare_data or {}).get("feature_scores", {})
    act_cols = (active_data or {}).get("column_breakdown", {})
    cmp_cols = (compare_data or {}).get("column_breakdown", {})

    features = [
        ("is_certificate", "Is Certificate", "avg_is_certificate_score"),
        ("is_iso_iatf", "Is ISO/IATF", "avg_is_iso_iatf_score"),
        ("cert_type", "Certificate Type", "avg_cert_type_score"),
        ("site_name", "Site Name", "avg_site_name_score"),
        ("address", "Company Address", "avg_address_score"),
        ("scope", "Certificate Scope", "avg_scope_score"),
        ("iso_type", "ISO Standard Type", "avg_iso_type_score"),
        ("issued_date", "Issued Date", "avg_issued_date_score"),
        ("expiration_date", "Expiration Date", "avg_expiration_date_score"),
        ("site_iso_type", "Site ISO Classification", "avg_site_iso_type_score"),
    ]

    for key, label, score_k in features:
        act_score = act_feats.get(score_k)
        cmp_score = cmp_feats.get(score_k)

        act_col_info = act_cols.get(key, {})
        cmp_col_info = cmp_cols.get(key, {})

        act_acc = act_col_info.get("accuracy_percent")
        cmp_acc = cmp_col_info.get("accuracy_percent")

        diff = round(act_score - cmp_score, 4) if act_score is not None and cmp_score is not None else 0.0

        feature_rows.append({
            "key": key,
            "label": label,
            "active_score": act_score,
            "active_accuracy_pct": act_acc,
            "compare_score": cmp_score,
            "compare_accuracy_pct": cmp_acc,
            "delta_score": diff,
            "improved": diff > 0,
            "regressed": diff < 0,
        })

    return {
        "available": True,
        "files": baseline_files,
        "active_file": active_path.name,
        "compare_file": compare_filename,
        "active": active_data,
        "compare": compare_data,
        "feature_comparison": feature_rows,
    }


def build_alerts_section(alarms: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Formats alarms from analytics/alarms.json with severity and sample ranges."""
    formatted = []
    for a in alarms:
        from_idx = a.get("from_index")
        to_idx = a.get("to_index")
        range_str = f"Samples #{from_idx}–#{to_idx}" if from_idx is not None else "Production Window"

        formatted.append({
            "alarm_id": a.get("alarm_id"),
            "timestamp": a.get("timestamp"),
            "range_str": range_str,
            "from_index": from_idx,
            "to_index": to_idx,
            "severity": str(a.get("severity", "WARNING")).lower(),
            "category": a.get("category", "DRIFT"),
            "metric": a.get("metric", ""),
            "message": a.get("message", ""),
            "reference_value": a.get("reference_value"),
            "recent_value": a.get("recent_value"),
            "delta": a.get("delta"),
            "threshold": a.get("threshold"),
        })

    return {
        "total": len(formatted),
        "critical_count": sum(1 for a in formatted if a["severity"] == "critical"),
        "warning_count": sum(1 for a in formatted if a["severity"] == "warning"),
        "alarms": list(reversed(formatted)),  # newest first
    }


# ==============================================================================
# Master Data Aggregator
# ==============================================================================

def build_dashboard_data(
    db_path: Path = DEFAULT_DB_PATH,
    drift_path: Path = DEFAULT_DRIFT_JSON,
    alarms_path: Path = DEFAULT_ALARMS_JSON,
    baseline_filename: Optional[str] = None,
) -> Dict[str, Any]:
    """Builds the comprehensive payload served to the frontend."""
    prod_records, ref_records = load_certificate_records(db_path)
    drift_tests = load_json_file(drift_path) or []
    if isinstance(drift_tests, dict):
        drift_tests = list(drift_tests.values())
    alarms = load_json_file(alarms_path) or []

    overview = build_overview_section(prod_records, ref_records, drift_tests, alarms)
    drift_sec = build_drift_section(drift_tests)
    golden_sec = build_golden_baselines_section(ANALYTICS_DIR, target_file=baseline_filename)
    alerts_sec = build_alerts_section(alarms)
    queue_sec = review_queue.get_review_queue(db_path=db_path, reviewed_path=REVIEWED_JSON_PATH)
    reviewed_cases = review_queue.get_reviewed_cases(reviewed_path=REVIEWED_JSON_PATH)

    return {
        "app_title": "ISO / IATF Certificate Extraction Monitor",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "overview": overview,
        "drift": drift_sec,
        "quality_baselines": golden_sec,
        "alerts": alerts_sec,
        "review_queue": queue_sec,
        "reviewed_cases": reviewed_cases,
        "generated_from": {
            "db": str(db_path),
            "drift_tests": str(drift_path),
            "reference_file": "reference_set.xlsx",
        },
    }


# ==============================================================================
# FastAPI Application & Endpoints
# ==============================================================================

app = FastAPI(
    title="ISO / IATF Certificate Monitor Dashboard",
    description="FastAPI Backend for Certificate Extraction Pipeline Monitoring, Drift Testing, Baselines & Human Review",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_no_cache_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


class DecisionPayload(BaseModel):
    request_id: str
    decision: str
    reviewer_notes: Optional[str] = None
    reason: Optional[str] = None
    reviewer_id: str = "REV-01"
    corrected_fields: Optional[Dict[str, Any]] = None
    corrected_values: Optional[Dict[str, Any]] = None


@app.get("/api/dashboard")
async def get_dashboard(baseline: Optional[str] = Query(None)):
    """Returns aggregated dashboard payload."""
    data = build_dashboard_data(
        db_path=_ACTIVE_DB_PATH,
        drift_path=DEFAULT_DRIFT_JSON,
        alarms_path=DEFAULT_ALARMS_JSON,
        baseline_filename=baseline,
    )
    return data


@app.get("/api/baseline")
async def get_baseline(file: str = Query("baseline.json")):
    """Returns golden baseline details for specified baseline file."""
    sec = build_golden_baselines_section(ANALYTICS_DIR, target_file=file)
    return sec


@app.get("/api/review-queue")
async def get_review_queue_endpoint():
    """Returns the prioritized 30% pending review queue."""
    res = review_queue.get_review_queue(db_path=_ACTIVE_DB_PATH, reviewed_path=REVIEWED_JSON_PATH)
    return res


@app.get("/api/review-queue/next")
async def get_next_case(request_id: Optional[str] = Query(None)):
    """Returns the next pending review case or the case matching request_id."""
    res = review_queue.get_next_pending_case(
        request_id=request_id,
        db_path=_ACTIVE_DB_PATH,
        reviewed_path=REVIEWED_JSON_PATH,
    )
    return res


@app.get("/api/reviewed")
async def get_reviewed_cases_endpoint():
    """Returns all saved human decisions and field corrections from reviewed/human_review.json."""
    res = review_queue.get_reviewed_cases(reviewed_path=REVIEWED_JSON_PATH)
    return {"total": len(res), "items": res}


@app.post("/api/review-queue/decision")
async def post_review_decision(payload: DecisionPayload):
    """
    Saves a human review verdict and field corrections into reviewed/human_review.json.
    Automatically replenishes the 30% queue with the next unreviewed candidate.
    """
    try:
        notes = payload.reviewer_notes or payload.reason or ""
        corrections = payload.corrected_fields or payload.corrected_values or {}
        res = review_queue.save_human_decision(
            request_id=payload.request_id,
            decision=payload.decision,
            reviewer_notes=notes,
            reviewer_id=payload.reviewer_id,
            corrected_fields=corrections,
            db_path=_ACTIVE_DB_PATH,
            reviewed_path=REVIEWED_JSON_PATH,
        )
        return res
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/")
async def root():
    """Serves the main dashboard single-page application."""
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    return FileResponse(index_path)


# Mount static assets for /styles.css, /app.js, etc.
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


# ==============================================================================
# CLI Entrypoint
# ==============================================================================

def main():
    global _ACTIVE_DB_PATH
    parser = argparse.ArgumentParser(description="Certificate Extraction Monitoring Dashboard (FastAPI)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8050)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to certificate_logs.db")
    parser.add_argument("--dump", action="store_true", help="Print dashboard JSON and exit")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload for development")
    args = parser.parse_args()

    _ACTIVE_DB_PATH = Path(args.db)

    if args.dump:
        print(json.dumps(build_dashboard_data(Path(args.db)), indent=2, default=str))
        return

    print(f"\n==================================================================")
    print(f"       CERTIFICATE EXTRACTION & MONITORING DASHBOARD (FastAPI)")
    print(f"==================================================================")
    print(f"Dashboard URL : http://{args.host}:{args.port}")
    print(f"Database      : {_ACTIVE_DB_PATH.resolve()}")
    print(f"Reviewed JSON : {REVIEWED_JSON_PATH.resolve()}")
    print(f"API Docs      : http://{args.host}:{args.port}/docs")
    print(f"==================================================================\n")

    if args.reload:
        uvicorn.run("dashboard.app:app", host=args.host, port=args.port, reload=True, app_dir=str(PROJECT_DIR))
    else:
        uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
