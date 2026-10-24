"""
rules.py
========
Rule-based validation logic for comparing ISO extractor predictions against the golden dataset.

Rules:
  - Rule 1: is_certificate == False -> Do not check other columns (they should be empty).
  - Rule 2: is_iso_iatf == False -> Do not check remaining columns except is_certificate.
  - Rule 3: cert_type, site_name, address, scope -> Normalized text, multi-value (|), rapidfuzz >= 95%.
  - Rule 4: site_iso_type -> Exact match (case-insensitive stripped).
  - Rule 5: issued_date, expiration_date -> Date format normalization (e.g. 02/25/2025 == 2/25/2025).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional, Tuple, List

import pandas as pd
from rapidfuzz import fuzz


def normalize_bool(val: Any) -> Optional[bool]:
    """Converts various boolean representations into a strict bool or None."""
    if val is None or pd.isna(val):
        return None
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if s in ("true", "1", "yes", "t", "y"):
        return True
    if s in ("false", "0", "no", "f", "n"):
        return False
    return None


def normalize_text(text: Any) -> str:
    """
    Normalizes string content:
      - Strips and lowercases.
      - Normalizes unicode dashes/hyphens to standard '-'.
      - Collapses newlines and multiple whitespace runs to single space.
    """
    if text is None or pd.isna(text):
        return ""
    s = str(text).strip()
    if s.lower() in ("nan", "none", "null", ""):
        return ""
    # Normalize unicode hyphens/dashes
    s = re.sub(r"[\u2010\u2011\u2012\u2013\u2014\u2015\u2212]", "-", s)
    # Replace newlines/tabs with space
    s = re.sub(r"[\r\n\t]+", " ", s)
    # Collapse multiple whitespace
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()


def split_multivalue(text: str) -> List[str]:
    """Splits a normalized string by '|' delimiter into separate clean items."""
    norm = normalize_text(text)
    if not norm:
        return []
    parts = [p.strip() for p in norm.split("|") if p.strip()]
    return parts if parts else [norm]


def parse_date_tuple(date_val: Any) -> Optional[Tuple[int, int, int]]:
    """
    Parses dates in various formats into a standardized (year, month, day) tuple.
    Handles:
      - '02/25/2025' or '2/25/2025'
      - '2025-02-25' or '2025-02-25 00:00:00'
      - datetime / Timestamp objects
    """
    if date_val is None or pd.isna(date_val):
        return None

    if isinstance(date_val, (datetime, pd.Timestamp)):
        return (date_val.year, date_val.month, date_val.day)

    s = str(date_val).strip()
    if not s or s.lower() in ("nan", "none", "null"):
        return None

    # Strip time part if present
    s = s.split(" ")[0].strip()

    candidate_formats = [
        "%m/%d/%Y",
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%Y/%m/%d",
        "%d.%m.%Y",
        "%m-%d-%Y",
        "%d-%m-%Y",
    ]

    for fmt in candidate_formats:
        try:
            dt = datetime.strptime(s, fmt)
            return (dt.year, dt.month, dt.day)
        except ValueError:
            continue

    # Regex for M/D/YYYY or D/M/YYYY
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m:
        # Default assume month/day/year
        return (int(m.group(3)), int(m.group(1)), int(m.group(2)))

    return None


def compare_boolean(expected: Any, predicted: Any) -> Tuple[bool, str]:
    """Compares boolean fields (is_certificate, is_iso_iatf)."""
    exp_b = normalize_bool(expected)
    pred_b = normalize_bool(predicted)

    if exp_b == pred_b:
        return True, "Match"

    return False, f"Expected {exp_b}, got {pred_b}"


def compare_text_rapidfuzz(
    expected: Any,
    predicted: Any,
    threshold: float = 95.0
) -> Tuple[bool, float, str]:
    """
    Compares text fields (cert_type, site_name, address, scope) using RapidFuzz.
    Supports normalized text, newline collapse, and '|' multi-value matching.
    Returns (is_match, similarity_score, note).
    """
    norm_exp = normalize_text(expected)
    norm_pred = normalize_text(predicted)

    # Both empty/missing
    if not norm_exp and not norm_pred:
        return True, 100.0, "Both empty"

    # One empty, other not
    if not norm_exp or not norm_pred:
        return False, 0.0, f"Mismatch: expected '{norm_exp[:50]}', got '{norm_pred[:50]}'"

    # 1. Full string direct similarity
    score_ratio = fuzz.ratio(norm_exp, norm_pred)
    score_token_sort = fuzz.token_sort_ratio(norm_exp, norm_pred)
    score_token_set = fuzz.token_set_ratio(norm_exp, norm_pred)
    best_full_score = max(score_ratio, score_token_sort, score_token_set)

    if best_full_score >= threshold:
        return True, round(best_full_score, 2), f"Full match ({best_full_score:.1f}%)"

    # 2. Multi-value (|) segmented similarity
    exp_parts = split_multivalue(norm_exp)
    pred_parts = split_multivalue(norm_pred)

    if len(exp_parts) > 1 or len(pred_parts) > 1:
        # For each expected part, find best match in predicted parts
        exp_match_scores = []
        for p_exp in exp_parts:
            best_p_score = max(
                (max(fuzz.ratio(p_exp, p_pred), fuzz.token_sort_ratio(p_exp, p_pred))
                 for p_pred in pred_parts),
                default=0.0
            )
            exp_match_scores.append(best_p_score)

        # For each predicted part, find best match in expected parts
        pred_match_scores = []
        for p_pred in pred_parts:
            best_p_score = max(
                (max(fuzz.ratio(p_exp, p_pred), fuzz.token_sort_ratio(p_exp, p_pred))
                 for p_exp in exp_parts),
                default=0.0
            )
            pred_match_scores.append(best_p_score)

        all_exp_match = all(s >= threshold for s in exp_match_scores)
        all_pred_match = all(s >= threshold for s in pred_match_scores)

        combined_avg = sum(exp_match_scores + pred_match_scores) / (len(exp_match_scores) + len(pred_match_scores))

        if all_exp_match and all_pred_match:
            return True, round(combined_avg, 2), f"Multi-part match ({combined_avg:.1f}%)"

        overall_best = max(best_full_score, combined_avg)
        return (
            overall_best >= threshold,
            round(overall_best, 2),
            f"Multi-part similarity ({overall_best:.1f}%)"
        )

    return False, round(best_full_score, 2), f"Score {best_full_score:.1f}% below threshold {threshold}%"


def compare_site_iso_type(expected: Any, predicted: Any) -> Tuple[bool, float, str]:
    """Exact string match comparison for site_iso_type."""
    norm_exp = normalize_text(expected)
    norm_pred = normalize_text(predicted)

    if not norm_exp and not norm_pred:
        return True, 100.0, "Both empty"

    if norm_exp == norm_pred:
        return True, 100.0, "Exact match"

    return False, 0.0, f"Mismatch: expected '{norm_exp}', got '{norm_pred}'"


def compare_dates(expected: Any, predicted: Any) -> Tuple[bool, float, str]:
    """
    Compares issued_date and expiration_date after normalization.
    Normalizes '02/25/2025' and '2/25/2025' to equal date tuples.
    """
    dt_exp = parse_date_tuple(expected)
    dt_pred = parse_date_tuple(predicted)

    # Both empty/missing
    if dt_exp is None and dt_pred is None:
        return True, 100.0, "Both empty"

    if dt_exp is not None and dt_pred is not None:
        if dt_exp == dt_pred:
            return True, 100.0, f"Exact date match ({dt_exp[1]}/{dt_exp[2]}/{dt_exp[0]})"
        return False, 0.0, f"Date mismatch: expected {dt_exp[1]}/{dt_exp[2]}/{dt_exp[0]}, got {dt_pred[1]}/{dt_pred[2]}/{dt_pred[0]}"

    return False, 0.0, f"Mismatch: expected {expected}, got {predicted}"
