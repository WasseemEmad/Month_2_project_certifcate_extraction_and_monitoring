"""
drift/detector.py
=================
Automated Drift Detection Pipeline for ISO/IATF Certificate Extraction.

Monitors and detects drift across three tiers between a Reference Window and a Recent Window:
1. Input Drift Check:
   - PDF page count (for doc_type == 'pdf')
   - Image pixel size (for doc_type == 'image')
2. Output Drift Check:
   - Certificate language distribution (shares, TVD, novel language discovery)
   - Missing core fields per row (for rows where is_certificate=True AND is_iso_iatf=True)
   - Verdict distributions ('Approved', 'Need Review', 'Reject') shares & TVD
   - 
   - False flAverage confidence scoreag rates: is_certificate=False rate, is_iso_iatf=False rate
3. Quality Drift Check:
   - Operational error rate (network failures, LLM extraction crashes, "did not open")
   - LLM Judge pass rate (average judge score)

Generates structured alarms and appends historical drift records to analytics/drift_test.json
and analytics/alarms.json.
"""

from __future__ import annotations

import datetime
import json
import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
import uuid

# Attempt importing scipy for 2-sample Kolmogorov-Smirnov test; fallback gracefully if absent
try:
    from scipy import stats as scipy_stats
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

DEFAULT_DRIFT_REPORT_PATH = "analytics/drift_test.json"
DEFAULT_ALARMS_PATH = "analytics/alarms.json"

CORE_METADATA_FIELDS = [
    "site_name",
    "address",
    "scope",
    "iso_type",
    "issued_date",
    "expiration_date",
    "site_iso_type",
]


# ==============================================================================
# 1. Statistical & Numeric Utilities
# ==============================================================================

def compute_numeric_stats(values: List[Union[int, float]]) -> Dict[str, Any]:
    """Calculates descriptive summary statistics for a numeric sample."""
    clean_vals = [float(v) for v in values if v is not None and not math.isnan(float(v))]
    n = len(clean_vals)
    if n == 0:
        return {
            "count": 0, "mean": 0.0, "std": 0.0, "median": 0.0,
            "min": 0.0, "max": 0.0, "p25": 0.0, "p75": 0.0, "iqr": 0.0,
        }

    sorted_vals = sorted(clean_vals)
    mean_val = sum(sorted_vals) / n
    variance = sum((x - mean_val) ** 2 for x in sorted_vals) / (n - 1) if n > 1 else 0.0
    std_val = math.sqrt(variance)

    def get_percentile(p: float) -> float:
        idx = (n - 1) * p
        low = int(math.floor(idx))
        high = int(math.ceil(idx))
        weight = idx - low
        return sorted_vals[low] * (1.0 - weight) + sorted_vals[high] * weight

    median_val = get_percentile(0.50)
    p25 = get_percentile(0.25)
    p75 = get_percentile(0.75)
    iqr = p75 - p25

    return {
        "count": n,
        "mean": round(mean_val, 2),
        "std": round(std_val, 2),
        "median": round(median_val, 2),
        "min": round(sorted_vals[0], 2),
        "max": round(sorted_vals[-1], 2),
        "p25": round(p25, 2),
        "p75": round(p75, 2),
        "iqr": round(iqr, 2),
    }


def compute_cohens_d(
    mean1: float, std1: float, n1: int,
    mean2: float, std2: float, n2: int,
) -> float:
    """Calculates Cohen's d effect size between two samples."""
    if n1 <= 1 or n2 <= 1:
        return 0.0
    pooled_variance = (((n1 - 1) * (std1 ** 2)) + ((n2 - 1) * (std2 ** 2))) / (n1 + n2 - 2)
    pooled_std = math.sqrt(pooled_variance)
    if pooled_std == 0:
        return 0.0
    return round((mean2 - mean1) / pooled_std, 3)


# ==============================================================================
# 2. Input Drift Check
# ==============================================================================

def check_input_drift(
    ref_records: List[Dict[str, Any]],
    recent_records: List[Dict[str, Any]],
    relative_drift_threshold_pct: float = 20.0,
    cohens_d_threshold: float = 0.50,
) -> Dict[str, Any]:
    """
    Monitors input characteristics:
    - Number of pages for PDF documents (doc_type == 'pdf')
    - Image pixel size for image documents (doc_type == 'image')
    """
    # 1. PDF Page Counts
    ref_pdf_pages = [
        r.get("page_count", 0) for r in ref_records 
        if str(r.get("doc_type", "")).lower() == "pdf" and r.get("page_count") is not None and r.get("page_count", 0) > 0
    ]
    recent_pdf_pages = [
        r.get("page_count", 0) for r in recent_records 
        if str(r.get("doc_type", "")).lower() == "pdf" and r.get("page_count") is not None and r.get("page_count", 0) > 0
    ]

    pdf_stats_ref = compute_numeric_stats(ref_pdf_pages)
    pdf_stats_recent = compute_numeric_stats(recent_pdf_pages)

    pdf_rel_diff = round(((pdf_stats_recent["mean"] - pdf_stats_ref["mean"]) / pdf_stats_ref["mean"] * 100.0), 1) if pdf_stats_ref["mean"] > 0 else 0.0
    pdf_cohens_d = compute_cohens_d(
        pdf_stats_ref["mean"], pdf_stats_ref["std"], pdf_stats_ref["count"],
        pdf_stats_recent["mean"], pdf_stats_recent["std"], pdf_stats_recent["count"],
    )
    pdf_ks_stat, pdf_ks_pval = None, None
    if HAS_SCIPY and len(ref_pdf_pages) >= 5 and len(recent_pdf_pages) >= 5:
        try:
            ks_res = scipy_stats.ks_2samp(ref_pdf_pages, recent_pdf_pages)
            pdf_ks_stat = round(float(ks_res.statistic), 4)
            pdf_ks_pval = float(ks_res.pvalue)
        except Exception:
            pass

    pdf_drift = (abs(pdf_rel_diff) >= relative_drift_threshold_pct) or (abs(pdf_cohens_d) >= cohens_d_threshold)

    # 2. Image Pixel Sizes
    ref_img_pixels = [
        r.get("image_pixel_size") for r in ref_records 
        if str(r.get("doc_type", "")).lower() == "image" and r.get("image_pixel_size") is not None and r.get("image_pixel_size", 0) > 0
    ]
    recent_img_pixels = [
        r.get("image_pixel_size") for r in recent_records 
        if str(r.get("doc_type", "")).lower() == "image" and r.get("image_pixel_size") is not None and r.get("image_pixel_size", 0) > 0
    ]

    img_stats_ref = compute_numeric_stats(ref_img_pixels)
    img_stats_recent = compute_numeric_stats(recent_img_pixels)

    img_rel_diff = round(((img_stats_recent["mean"] - img_stats_ref["mean"]) / img_stats_ref["mean"] * 100.0), 1) if img_stats_ref["mean"] > 0 else 0.0
    img_cohens_d = compute_cohens_d(
        img_stats_ref["mean"], img_stats_ref["std"], img_stats_ref["count"],
        img_stats_recent["mean"], img_stats_recent["std"], img_stats_recent["count"],
    )
    img_ks_stat, img_ks_pval = None, None
    if HAS_SCIPY and len(ref_img_pixels) >= 5 and len(recent_img_pixels) >= 5:
        try:
            ks_res = scipy_stats.ks_2samp(ref_img_pixels, recent_img_pixels)
            img_ks_stat = round(float(ks_res.statistic), 4)
            img_ks_pval = float(ks_res.pvalue)
        except Exception:
            pass

    img_drift = False
    if img_stats_ref["count"] > 0 and img_stats_recent["count"] > 0:
        img_drift = (abs(img_rel_diff) >= relative_drift_threshold_pct) or (abs(img_cohens_d) >= cohens_d_threshold)

    has_input_drift = pdf_drift or img_drift

    return {
        "pdf_pages": {
            "reference": pdf_stats_ref,
            "recent": pdf_stats_recent,
            "relative_change_pct": pdf_rel_diff,
            "cohens_d": pdf_cohens_d,
            "ks_statistic": pdf_ks_stat,
            "ks_pvalue": pdf_ks_pval,
            "drift_detected": pdf_drift,
        },
        "image_pixel_size": {
            "reference": img_stats_ref,
            "recent": img_stats_recent,
            "relative_change_pct": img_rel_diff,
            "cohens_d": img_cohens_d,
            "ks_statistic": img_ks_stat,
            "ks_pvalue": img_ks_pval,
            "drift_detected": img_drift,
        },
        "input_drift_detected": has_input_drift,
    }


# ==============================================================================
# 3. Output Drift Check
# ==============================================================================

def check_output_drift(
    ref_records: List[Dict[str, Any]],
    recent_records: List[Dict[str, Any]],
    pp_shift_threshold: float = 15.0,
    missing_fields_increase_threshold: float = 1.0,
    confidence_drop_threshold: float = 10.0,
) -> Dict[str, Any]:
    """
    Monitors model output characteristics:
    1. Language distribution & novel language detection.
    2. Missing core fields per row (for is_certificate=True AND is_iso_iatf=True).
    3. Verdict distributions ('Approved', 'Need Review', 'Reject') shares & TVD.
    4. Average confidence score.
    5. False flag rates: is_certificate=False rate, is_iso_iatf=False rate.
    """
    n_ref = len(ref_records)
    n_recent = len(recent_records)

    # --------------------------------------------------------------------------
    # A. Language Distribution
    # --------------------------------------------------------------------------
    ref_langs: Dict[str, int] = {}
    for r in ref_records:
        lang = str(r.get("certificate_language") or "Unknown").strip().capitalize()
        ref_langs[lang] = ref_langs.get(lang, 0) + 1

    recent_langs: Dict[str, int] = {}
    for r in recent_records:
        lang = str(r.get("certificate_language") or "Unknown").strip().capitalize()
        recent_langs[lang] = recent_langs.get(lang, 0) + 1

    all_langs = sorted(set(ref_langs.keys()).union(set(recent_langs.keys())))
    lang_details: Dict[str, Any] = {}
    novel_languages: List[str] = []
    total_lang_tvd = 0.0

    for lang in all_langs:
        c_ref = ref_langs.get(lang, 0)
        c_recent = recent_langs.get(lang, 0)
        sh_ref = round((c_ref / n_ref * 100.0), 2) if n_ref > 0 else 0.0
        sh_recent = round((c_recent / n_recent * 100.0), 2) if n_recent > 0 else 0.0
        pp_ch = round(sh_recent - sh_ref, 2)
        total_lang_tvd += abs(sh_recent - sh_ref)

        if c_ref == 0 and c_recent > 0 and lang != "Unknown":
            novel_languages.append(lang)

        lang_details[lang] = {
            "ref_count": c_ref,
            "ref_share_pct": sh_ref,
            "recent_count": c_recent,
            "recent_share_pct": sh_recent,
            "pp_change": pp_ch,
        }

    lang_tvd_pct = round(total_lang_tvd / 2.0, 2)
    language_drift = (len(novel_languages) > 0) or (lang_tvd_pct >= pp_shift_threshold)

    # --------------------------------------------------------------------------
    # B. Missing Core Fields per Valid Certificate Row (is_cert=True & is_iso=True)
    # --------------------------------------------------------------------------
    def calculate_missing_counts(records: List[Dict[str, Any]]) -> List[int]:
        counts = []
        for r in records:
            is_cert = bool(r.get("is_certificate"))
            is_iso = bool(r.get("is_iso_iatf"))
            if is_cert and is_iso:
                missing = 0
                for field in CORE_METADATA_FIELDS:
                    val = r.get(field)
                    if val is None or str(val).strip() == "" or str(val).strip().lower() in ("none", "null"):
                        missing += 1
                counts.append(missing)
        return counts

    ref_missing = calculate_missing_counts(ref_records)
    recent_missing = calculate_missing_counts(recent_records)

    ref_missing_stats = compute_numeric_stats(ref_missing)
    recent_missing_stats = compute_numeric_stats(recent_missing)
    delta_missing_avg = round(recent_missing_stats["mean"] - ref_missing_stats["mean"], 2)
    missing_fields_drift = (delta_missing_avg >= missing_fields_increase_threshold)

    # --------------------------------------------------------------------------
    # C. Verdict Distribution ('Approved', 'Need Review', 'Reject')
    # --------------------------------------------------------------------------
    verdicts = ["Approved", "Need Review", "Reject"]
    verdict_details: Dict[str, Any] = {}
    total_verdict_tvd = 0.0
    max_verdict_pp = 0.0

    for v in verdicts:
        c_ref = sum(1 for r in ref_records if str(r.get("verdict", "")).strip().lower() == v.lower())
        c_recent = sum(1 for r in recent_records if str(r.get("verdict", "")).strip().lower() == v.lower())
        sh_ref = round((c_ref / n_ref * 100.0), 2) if n_ref > 0 else 0.0
        sh_recent = round((c_recent / n_recent * 100.0), 2) if n_recent > 0 else 0.0
        pp_ch = round(sh_recent - sh_ref, 2)
        total_verdict_tvd += abs(sh_recent - sh_ref)
        if abs(pp_ch) > max_verdict_pp:
            max_verdict_pp = abs(pp_ch)

        verdict_details[v] = {
            "ref_count": c_ref,
            "ref_share_pct": sh_ref,
            "recent_count": c_recent,
            "recent_share_pct": sh_recent,
            "pp_change": pp_ch,
        }

    verdict_tvd_pct = round(total_verdict_tvd / 2.0, 2)
    verdict_drift = (max_verdict_pp >= pp_shift_threshold)

    # --------------------------------------------------------------------------
    # D. Average Confidence Score
    # --------------------------------------------------------------------------
    ref_conf = [r.get("confidence_score") for r in ref_records if r.get("confidence_score") is not None]
    recent_conf = [r.get("confidence_score") for r in recent_records if r.get("confidence_score") is not None]

    ref_conf_stats = compute_numeric_stats(ref_conf)
    recent_conf_stats = compute_numeric_stats(recent_conf)
    delta_conf_avg = round(recent_conf_stats["mean"] - ref_conf_stats["mean"], 2)
    conf_drop_drift = (delta_conf_avg <= -confidence_drop_threshold)

    # --------------------------------------------------------------------------
    # E. False Flag Rates (is_certificate=False, is_iso_iatf=False)
    # --------------------------------------------------------------------------
    def compute_false_rate(records: List[Dict[str, Any]], field: str) -> Tuple[int, float]:
        n = len(records)
        if n == 0:
            return 0, 0.0
        false_count = sum(1 for r in records if r.get(field) is False or r.get(field) == 0)
        rate_pct = round((false_count / n * 100.0), 2)
        return false_count, rate_pct

    ref_cert_false_count, ref_cert_false_pct = compute_false_rate(ref_records, "is_certificate")
    recent_cert_false_count, recent_cert_false_pct = compute_false_rate(recent_records, "is_certificate")
    delta_cert_false_pp = round(recent_cert_false_pct - ref_cert_false_pct, 2)

    ref_iso_false_count, ref_iso_false_pct = compute_false_rate(ref_records, "is_iso_iatf")
    recent_iso_false_count, recent_iso_false_pct = compute_false_rate(recent_records, "is_iso_iatf")
    delta_iso_false_pp = round(recent_iso_false_pct - ref_iso_false_pct, 2)

    flags_drift = (abs(delta_cert_false_pp) >= pp_shift_threshold) or (abs(delta_iso_false_pp) >= pp_shift_threshold)

    # --------------------------------------------------------------------------
    # F. Ratio of "Undefined" in site_iso_type
    # --------------------------------------------------------------------------
    def is_site_iso_type_undefined(val: Any) -> bool:
        if val is None:
            return False
        return str(val).strip().lower() == "undefined"

    ref_undefined_count = sum(1 for r in ref_records if is_site_iso_type_undefined(r.get("site_iso_type")))
    recent_undefined_count = sum(1 for r in recent_records if is_site_iso_type_undefined(r.get("site_iso_type")))

    ref_undefined_ratio = round(ref_undefined_count / n_ref, 4) if n_ref > 0 else 0.0
    recent_undefined_ratio = round(recent_undefined_count / n_recent, 4) if n_recent > 0 else 0.0

    ref_undefined_pct = round(ref_undefined_ratio * 100.0, 2)
    recent_undefined_pct = round(recent_undefined_ratio * 100.0, 2)
    delta_undefined_pp = round(recent_undefined_pct - ref_undefined_pct, 2)

    # Also calculate ratio specifically among valid ISO/IATF certificates
    ref_iso_certs = [r for r in ref_records if bool(r.get("is_certificate")) and bool(r.get("is_iso_iatf"))]
    recent_iso_certs = [r for r in recent_records if bool(r.get("is_certificate")) and bool(r.get("is_iso_iatf"))]

    ref_iso_undef_count = sum(1 for r in ref_iso_certs if is_site_iso_type_undefined(r.get("site_iso_type")))
    recent_iso_undef_count = sum(1 for r in recent_iso_certs if is_site_iso_type_undefined(r.get("site_iso_type")))

    ref_iso_undef_ratio = round(ref_iso_undef_count / len(ref_iso_certs), 4) if ref_iso_certs else 0.0
    recent_iso_undef_ratio = round(recent_iso_undef_count / len(recent_iso_certs), 4) if recent_iso_certs else 0.0

    site_iso_type_undefined_drift = (abs(delta_undefined_pp) >= pp_shift_threshold)

    has_output_drift = (
        language_drift or
        missing_fields_drift or
        verdict_drift or
        conf_drop_drift or
        flags_drift or
        site_iso_type_undefined_drift
    )

    return {
        "language_distribution": {
            "categories": lang_details,
            "novel_languages_detected": novel_languages,
            "total_variation_distance_pct": lang_tvd_pct,
            "drift_detected": language_drift,
        },
        "missing_fields_per_valid_row": {
            "reference": ref_missing_stats,
            "recent": recent_missing_stats,
            "delta_average": delta_missing_avg,
            "drift_detected": missing_fields_drift,
        },
        "verdict_distribution": {
            "categories": verdict_details,
            "max_abs_pp_change": max_verdict_pp,
            "total_variation_distance_pct": verdict_tvd_pct,
            "drift_detected": verdict_drift,
        },
        "confidence_score": {
            "reference": ref_conf_stats,
            "recent": recent_conf_stats,
            "delta_average": delta_conf_avg,
            "drift_detected": conf_drop_drift,
        },
        "false_flags": {
            "is_certificate_false": {
                "ref_count": ref_cert_false_count,
                "ref_rate_pct": ref_cert_false_pct,
                "recent_count": recent_cert_false_count,
                "recent_rate_pct": recent_cert_false_pct,
                "pp_change": delta_cert_false_pp,
            },
            "is_iso_iatf_false": {
                "ref_count": ref_iso_false_count,
                "ref_rate_pct": ref_iso_false_pct,
                "recent_count": recent_iso_false_count,
                "recent_rate_pct": recent_iso_false_pct,
                "pp_change": delta_iso_false_pp,
            },
            "drift_detected": flags_drift,
        },
        "site_iso_type_undefined": {
            "ref_count": ref_undefined_count,
            "ref_ratio": ref_undefined_ratio,
            "ref_rate_pct": ref_undefined_pct,
            "recent_count": recent_undefined_count,
            "recent_ratio": recent_undefined_ratio,
            "recent_rate_pct": recent_undefined_pct,
            "pp_change": delta_undefined_pp,
            "among_valid_iso": {
                "ref_count": ref_iso_undef_count,
                "ref_total_iso": len(ref_iso_certs),
                "ref_ratio": ref_iso_undef_ratio,
                "recent_count": recent_iso_undef_count,
                "recent_total_iso": len(recent_iso_certs),
                "recent_ratio": recent_iso_undef_ratio,
            },
            "drift_detected": site_iso_type_undefined_drift,
        },
        "output_drift_detected": has_output_drift,
    }


# ==============================================================================
# 4. Quality Drift Check
# ==============================================================================

def check_quality_drift(
    ref_records: List[Dict[str, Any]],
    recent_records: List[Dict[str, Any]],
    error_increase_threshold_pp: float = 10.0,
    judge_drop_threshold_pp: float = 15.0,
) -> Dict[str, Any]:
    """
    Monitors operational pipeline quality:
    - Error rate: LLM/network failures, status != 'OK', error non-null, or "did not open".
    - LLM Judge score: Average score among sampled records.
    """
    n_ref = len(ref_records)
    n_recent = len(recent_records)

    # 1. Error Rate Check
    def count_errors(records: List[Dict[str, Any]]) -> int:
        errs = 0
        for r in records:
            status = str(r.get("status") or "").upper()
            err_msg = r.get("error")
            checks = str(r.get("checks_passed_or_failed") or "").lower()
            page_count = r.get("page_count")

            is_err = False
            if status not in ("", "OK"):
                is_err = True
            elif err_msg and str(err_msg).strip() != "":
                is_err = True
            elif "did not open" in checks or (page_count is not None and page_count == 0):
                is_err = True

            if is_err:
                errs += 1
        return errs

    ref_err_count = count_errors(ref_records)
    recent_err_count = count_errors(recent_records)

    ref_err_rate_pct = round((ref_err_count / n_ref * 100.0), 2) if n_ref > 0 else 0.0
    recent_err_rate_pct = round((recent_err_count / n_recent * 100.0), 2) if n_recent > 0 else 0.0
    delta_err_pp = round(recent_err_rate_pct - ref_err_rate_pct, 2)
    error_drift = (delta_err_pp >= error_increase_threshold_pp)

    # 2. LLM Judge Score Check
    ref_judged = [r.get("judge_score") for r in ref_records if r.get("judge_score") is not None]
    recent_judged = [r.get("judge_score") for r in recent_records if r.get("judge_score") is not None]

    ref_judge_pass_rate = round(sum(ref_judged) / len(ref_judged) * 100.0, 2) if ref_judged else None
    recent_judge_pass_rate = round(sum(recent_judged) / len(recent_judged) * 100.0, 2) if recent_judged else None

    delta_judge_pp = None
    judge_drift = False
    if ref_judge_pass_rate is not None and recent_judge_pass_rate is not None:
        delta_judge_pp = round(recent_judge_pass_rate - ref_judge_pass_rate, 2)
        judge_drift = (delta_judge_pp <= -judge_drop_threshold_pp)

    has_quality_drift = error_drift or judge_drift

    return {
        "error_rate": {
            "ref_count": ref_err_count,
            "ref_rate_pct": ref_err_rate_pct,
            "recent_count": recent_err_count,
            "recent_rate_pct": recent_err_rate_pct,
            "delta_pp": delta_err_pp,
            "drift_detected": error_drift,
        },
        "judge_pass_rate": {
            "ref_judged_count": len(ref_judged),
            "ref_pass_rate_pct": ref_judge_pass_rate,
            "recent_judged_count": len(recent_judged),
            "recent_pass_rate_pct": recent_judge_pass_rate,
            "delta_pp": delta_judge_pp,
            "drift_detected": judge_drift,
        },
        "quality_drift_detected": has_quality_drift,
    }


# ==============================================================================
# 5. Alarms Generation
# ==============================================================================

def generate_alarms(
    drift_report: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Evaluates drift report results and generates actionable alarm objects.
    Severity:
    - CRITICAL: Quality degradation (error spike, judge score collapse), or severe output drift.
    - WARNING: Input shift or novel language discovered.
    """
    alarms: List[Dict[str, Any]] = []
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    from_index = drift_report.get("from_index")
    to_index = drift_report.get("to_index")

    def make_alarm(severity: str, category: str, metric: str, message: str, reference_value: Any, recent_value: Any, delta: Any, threshold: str) -> Dict[str, Any]:
        return {
            "alarm_id": str(uuid.uuid4()),
            "timestamp": ts,
            "from_index": from_index,
            "to_index": to_index,
            "severity": severity,
            "category": category,
            "metric": metric,
            "message": message,
            "reference_value": reference_value,
            "recent_value": recent_value,
            "delta": delta,
            "threshold": threshold,
        }

    inp = drift_report.get("input_drift", {})
    out = drift_report.get("output_drift", {})
    qua = drift_report.get("quality_drift", {})

    # Input Alarms
    pdf_info = inp.get("pdf_pages", {})
    if pdf_info.get("drift_detected"):
        alarms.append(make_alarm(
            severity="WARNING",
            category="INPUT_DRIFT",
            metric="pdf_page_count",
            message=f"Significant shift in PDF page count: {pdf_info.get('relative_change_pct'):+.1f}% change (Cohen's d: {pdf_info.get('cohens_d')})",
            reference_value=pdf_info.get("reference", {}).get("mean"),
            recent_value=pdf_info.get("recent", {}).get("mean"),
            delta=pdf_info.get("relative_change_pct"),
            threshold="relative_change >= 20% or |cohens_d| >= 0.50",
        ))

    img_info = inp.get("image_pixel_size", {})
    if img_info.get("drift_detected"):
        alarms.append(make_alarm(
            severity="WARNING",
            category="INPUT_DRIFT",
            metric="image_pixel_size",
            message=f"Significant shift in Image pixel resolution: {img_info.get('relative_change_pct'):+.1f}% change (Cohen's d: {img_info.get('cohens_d')})",
            reference_value=img_info.get("reference", {}).get("mean"),
            recent_value=img_info.get("recent", {}).get("mean"),
            delta=img_info.get("relative_change_pct"),
            threshold="relative_change >= 20% or |cohens_d| >= 0.50",
        ))

    # Output Alarms - Novel Languages
    lang_info = out.get("language_distribution", {})
    novel_langs = lang_info.get("novel_languages_detected", [])
    if novel_langs:
        alarms.append(make_alarm(
            severity="WARNING",
            category="NOVEL_LANGUAGE",
            metric="certificate_language",
            message=f"Novel certificate language(s) observed in production: {novel_langs}",
            reference_value="Not in reference baseline",
            recent_value=novel_langs,
            delta=len(novel_langs),
            threshold="len(novel_languages) > 0",
        ))

    if lang_info.get("drift_detected") and not novel_langs:
        alarms.append(make_alarm(
            severity="WARNING",
            category="OUTPUT_DRIFT",
            metric="language_distribution",
            message=f"Language distribution TVD exceeded threshold: {lang_info.get('total_variation_distance_pct'):.1f}%",
            reference_value=None,
            recent_value=None,
            delta=lang_info.get("total_variation_distance_pct"),
            threshold="TVD >= 15.0%",
        ))

    # Output Alarms - Missing Core Fields
    mf_info = out.get("missing_fields_per_valid_row", {})
    if mf_info.get("drift_detected"):
        alarms.append(make_alarm(
            severity="CRITICAL",
            category="OUTPUT_DRIFT",
            metric="missing_core_fields_per_valid_row",
            message=f"Increase in missing core fields for valid certificates: +{mf_info.get('delta_average')} fields/row on average",
            reference_value=mf_info.get("reference", {}).get("mean"),
            recent_value=mf_info.get("recent", {}).get("mean"),
            delta=mf_info.get("delta_average"),
            threshold="delta >= 1.0 field",
        ))

    # Output Alarms - Verdict Shift
    vd_info = out.get("verdict_distribution", {})
    if vd_info.get("drift_detected"):
        alarms.append(make_alarm(
            severity="WARNING",
            category="OUTPUT_DRIFT",
            metric="verdict_distribution",
            message=f"Verdict distribution shifted significantly (Max shift: {vd_info.get('max_abs_pp_change'):.1f} pp, TVD: {vd_info.get('total_variation_distance_pct'):.1f}%)",
            reference_value=None,
            recent_value=None,
            delta=vd_info.get("max_abs_pp_change"),
            threshold="max_abs_pp_change >= 15.0 pp",
        ))

    # Output Alarms - Confidence Score Drop
    conf_info = out.get("confidence_score", {})
    if conf_info.get("drift_detected"):
        alarms.append(make_alarm(
            severity="CRITICAL",
            category="OUTPUT_DRIFT",
            metric="confidence_score",
            message=f"Confidence score dropped by {conf_info.get('delta_average')} points on average",
            reference_value=conf_info.get("reference", {}).get("mean"),
            recent_value=conf_info.get("recent", {}).get("mean"),
            delta=conf_info.get("delta_average"),
            threshold="delta <= -10.0 points",
        ))

    # Output Alarms - False Flags Shift
    ff_info = out.get("false_flags", {})
    if ff_info.get("drift_detected"):
        cert_f = ff_info.get("is_certificate_false", {})
        iso_f = ff_info.get("is_iso_iatf_false", {})
        alarms.append(make_alarm(
            severity="WARNING",
            category="OUTPUT_DRIFT",
            metric="screening_false_flags",
            message=f"Shift in false rates: is_certificate=False changed by {cert_f.get('pp_change'):+.1f} pp, is_iso_iatf=False changed by {iso_f.get('pp_change'):+.1f} pp",
            reference_value={"is_certificate_false_pct": cert_f.get("ref_rate_pct"), "is_iso_false_pct": iso_f.get("ref_rate_pct")},
            recent_value={"is_certificate_false_pct": cert_f.get("recent_rate_pct"), "is_iso_false_pct": iso_f.get("recent_rate_pct")},
            delta=max(abs(cert_f.get("pp_change", 0.0)), abs(iso_f.get("pp_change", 0.0))),
            threshold="|pp_change| >= 15.0 pp",
        ))

    # Output Alarms - site_iso_type Undefined Ratio Shift
    si_info = out.get("site_iso_type_undefined", {})
    if si_info.get("drift_detected"):
        alarms.append(make_alarm(
            severity="WARNING",
            category="OUTPUT_DRIFT",
            metric="site_iso_type_undefined_ratio",
            message=f"Significant shift in site_iso_type 'Undefined' ratio: {si_info.get('pp_change'):+.1f} pp shift (Ref: {si_info.get('ref_rate_pct')}%, Rec: {si_info.get('recent_rate_pct')}%)",
            reference_value=si_info.get("ref_ratio"),
            recent_value=si_info.get("recent_ratio"),
            delta=si_info.get("pp_change"),
            threshold="|pp_change| >= 15.0 pp",
        ))

    # Quality Alarms - Error Rate
    err_info = qua.get("error_rate", {})
    if err_info.get("drift_detected"):
        alarms.append(make_alarm(
            severity="CRITICAL",
            category="QUALITY_DRIFT",
            metric="error_rate",
            message=f"Operational pipeline error rate increased by {err_info.get('delta_pp'):+.1f} pp (from {err_info.get('ref_rate_pct')}% to {err_info.get('recent_rate_pct')}%)",
            reference_value=err_info.get("ref_rate_pct"),
            recent_value=err_info.get("recent_rate_pct"),
            delta=err_info.get("delta_pp"),
            threshold="delta_pp >= 10.0 pp",
        ))

    # Quality Alarms - Judge Pass Rate
    judge_info = qua.get("judge_pass_rate", {})
    if judge_info.get("drift_detected"):
        alarms.append(make_alarm(
            severity="CRITICAL",
            category="QUALITY_DRIFT",
            metric="judge_pass_rate",
            message=f"LLM Judge score pass rate dropped by {judge_info.get('delta_pp'):+.1f} pp (from {judge_info.get('ref_pass_rate_pct')}% to {judge_info.get('recent_pass_rate_pct')}%)",
            reference_value=judge_info.get("ref_pass_rate_pct"),
            recent_value=judge_info.get("recent_pass_rate_pct"),
            delta=judge_info.get("delta_pp"),
            threshold="delta_pp <= -15.0 pp",
        ))

    return alarms


# ==============================================================================
# 6. Report & Alarms Persistence (Append to JSON)
# ==============================================================================

def append_to_json_file(file_path: Union[str, Path], item_or_items: Union[Dict[str, Any], List[Dict[str, Any]]]) -> None:
    """Safely reads existing JSON array from file, appends new item(s), and writes back."""
    target_path = Path(file_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    items_to_append = item_or_items if isinstance(item_or_items, list) else [item_or_items]
    if not items_to_append:
        return

    data: List[Dict[str, Any]] = []
    if target_path.exists() and target_path.stat().st_size > 0:
        try:
            with open(target_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, list):
                    data = loaded
                elif isinstance(loaded, dict):
                    data = [loaded]
        except Exception:
            data = []

    data.extend(items_to_append)

    with open(target_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ==============================================================================
# 7. Drift Master Execution & Console Dashboard
# ==============================================================================

def execute_drift_test(
    ref_records: List[Dict[str, Any]],
    recent_records: List[Dict[str, Any]],
    batch_index: Optional[int] = None,
    from_index: Optional[int] = None,
    to_index: Optional[int] = None,
    save_reports: bool = True,
    drift_report_path: str = DEFAULT_DRIFT_REPORT_PATH,
    alarms_path: str = DEFAULT_ALARMS_PATH,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Executes a complete drift test comparison between ref_records and recent_records.
    Returns (drift_report, alarms).
    """
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if to_index is None and batch_index is not None:
        to_index = batch_index
    if from_index is None and to_index is not None:
        from_index = max(1, to_index - len(recent_records) + 1)

    input_results = check_input_drift(ref_records, recent_records)
    output_results = check_output_drift(ref_records, recent_records)
    quality_results = check_quality_drift(ref_records, recent_records)

    has_drift = (
        input_results["input_drift_detected"] or
        output_results["output_drift_detected"] or
        quality_results["quality_drift_detected"]
    )

    report = {
        "test_id": str(uuid.uuid4()),
        "timestamp": ts,
        "batch_sample_count": batch_index,
        "from_index": from_index,
        "to_index": to_index,
        "reference_sample_count": len(ref_records),
        "recent_sample_count": len(recent_records),
        "status": "DRIFT_ALERT" if has_drift else "STABLE",
        "overall_drift_detected": has_drift,
        "input_drift": input_results,
        "output_drift": output_results,
        "quality_drift": quality_results,
    }

    alarms = generate_alarms(report)
    report["alarm_count"] = len(alarms)

    if save_reports:
        append_to_json_file(drift_report_path, report)
        if alarms:
            append_to_json_file(alarms_path, alarms)

    return report, alarms


def print_drift_dashboard(report: Dict[str, Any], alarms: List[Dict[str, Any]]) -> None:
    """Prints a clear console dashboard summarizing the drift test results."""
    print("\n" + "=" * 80)
    print(f"       ISO CERTIFICATE EXTRACTION - DRIFT MONITORING REPORT ({report.get('timestamp')})")
    print("=" * 80)
    idx_str = f" (Sample #{report.get('from_index')} to #{report.get('to_index')})" if report.get('from_index') is not None else ""
    print(f"Reference Window: {report.get('reference_sample_count')} samples | Recent Window: {report.get('recent_sample_count')} samples{idx_str}")
    print(f"Overall Status  : [{'ALERT - DRIFT DETECTED' if report.get('overall_drift_detected') else 'STABLE - NORMAL'}]")

    # 1. Input Drift
    inp = report.get("input_drift", {})
    pdf = inp.get("pdf_pages", {})
    img = inp.get("image_pixel_size", {})
    print(f"\n1. INPUT DRIFT [{'DRIFT' if inp.get('input_drift_detected') else 'STABLE'}]")
    print(f"   - PDF Pages Mean: Ref={pdf.get('reference', {}).get('mean')} | Rec={pdf.get('recent', {}).get('mean')} | Rel: {pdf.get('relative_change_pct', 0.0):+.1f}% | Cohen d: {pdf.get('cohens_d', 0.0):+.2f} [{'DRIFT' if pdf.get('drift_detected') else 'OK'}]")
    print(f"   - Image Pixels Mean: Ref={img.get('reference', {}).get('mean')} | Rec={img.get('recent', {}).get('mean')} | Rel: {img.get('relative_change_pct', 0.0):+.1f}% | Cohen d: {img.get('cohens_d', 0.0):+.2f} [{'DRIFT' if img.get('drift_detected') else 'OK'}]")

    # 2. Output Drift
    out = report.get("output_drift", {})
    lang = out.get("language_distribution", {})
    mf = out.get("missing_fields_per_valid_row", {})
    vd = out.get("verdict_distribution", {})
    cf = out.get("confidence_score", {})
    ff = out.get("false_flags", {})
    si = out.get("site_iso_type_undefined", {})
    print(f"\n2. OUTPUT DRIFT [{'DRIFT' if out.get('output_drift_detected') else 'STABLE'}]")
    if lang.get("novel_languages_detected"):
        print(f"   - [!] Novel Languages Detected: {lang.get('novel_languages_detected')}")
    print(f"   - Language TVD: {lang.get('total_variation_distance_pct')}%")
    print(f"   - site_iso_type 'Undefined' Ratio: Ref={si.get('ref_ratio', 0.0):.3f} ({si.get('ref_rate_pct', 0.0)}%) | Rec={si.get('recent_ratio', 0.0):.3f} ({si.get('recent_rate_pct', 0.0)}%) | pp Change: {si.get('pp_change', 0.0):+.1f} pp [{'DRIFT' if si.get('drift_detected') else 'OK'}]")
    print(f"   - Missing Core Fields / Valid Cert: Ref={mf.get('reference', {}).get('mean')} | Rec={mf.get('recent', {}).get('mean')} | Delta={mf.get('delta_average'):+.2f} [{'DRIFT' if mf.get('drift_detected') else 'OK'}]")
    print(f"   - Verdict Shift Max: {vd.get('max_abs_pp_change')} pp | TVD: {vd.get('total_variation_distance_pct')}% [{'DRIFT' if vd.get('drift_detected') else 'OK'}]")
    print(f"   - Confidence Score Mean: Ref={cf.get('reference', {}).get('mean')} | Rec={cf.get('recent', {}).get('mean')} | Delta={cf.get('delta_average'):+.1f} pts [{'DRIFT' if cf.get('drift_detected') else 'OK'}]")
    print(f"   - False Screening Flags: is_cert False={ff.get('is_certificate_false', {}).get('pp_change'):+.1f} pp | is_iso False={ff.get('is_iso_iatf_false', {}).get('pp_change'):+.1f} pp")

    # 3. Quality Drift
    qua = report.get("quality_drift", {})
    err = qua.get("error_rate", {})
    jd = qua.get("judge_pass_rate", {})
    print(f"\n3. QUALITY DRIFT [{'QUALITY DEGRADED' if qua.get('quality_drift_detected') else 'HEALTHY'}]")
    print(f"   - Pipeline Error Rate: Ref={err.get('ref_rate_pct')}% | Rec={err.get('recent_rate_pct')}% | Delta={err.get('delta_pp'):+.1f} pp [{'DEGRADED' if err.get('drift_detected') else 'OK'}]")
    ref_jp = f"{jd.get('ref_pass_rate_pct')}%" if jd.get('ref_pass_rate_pct') is not None else "N/A"
    rec_jp = f"{jd.get('recent_pass_rate_pct')}%" if jd.get('recent_pass_rate_pct') is not None else "N/A"
    delta_jp = f"{jd.get('delta_pp'):+.1f} pp" if jd.get('delta_pp') is not None else "N/A"
    print(f"   - LLM Judge Pass Rate: Ref={ref_jp} | Rec={rec_jp} | Delta={delta_jp} [{'DEGRADED' if jd.get('drift_detected') else 'OK'}]")

    # 4. Alarms
    if alarms:
        print(f"\n4. ALARMS TRIGGERED ({len(alarms)} ALARM(S))")
        for idx, a in enumerate(alarms, 1):
            print(f"   [{idx}] [{a['severity']}] [{a['category']}] {a['message']}")
    else:
        print("\n4. ALARMS: None (All metrics within safe thresholds)")
    print("=" * 80 + "\n")
