"""
verifier/rules.py
=================
Implementation of verification rules and deduction logic for certificate extraction rows.

Rules:
  - Rule 1: Expiration date must be greater than Issued date (-30 pts).
  - Rule 2: Difference between Expiration and Issued date:
              >= 10 years: -20 pts
              5 <= diff < 10 years: -15 pts
              3 < diff < 5 years: -10 pts
              <= 3 years: 0 pts (Standard ISO cycle)
  - Rule 3: site_iso_type must be one of known types:
              ['Silicon Wafer', 'Fabrication', 'IC Assembly', 'Assembly', 'Factory', 'Undefined']
              If not: -40 pts
  - Rule 4: If document is PDF:
              pages > 5: -20 pts
              3 <= pages <= 5: -10 pts
              pages < 3: 0 pts
  - Rule 5: cert_type and iso_type fuzzy match (rapidfuzz) must be >= 95%.
              If < 95%: -30 pts
  - Rule 6: If site_name contains multiple entries ('|') and (page_count == 1 or doc_type == 'image'):
              -20 pts
  - Rule 7: Missing fields penalty:
              Core fields (site_name, iso_type, issued_date, expiration_date): -20 pts per missing field
              Secondary fields (address, scope): -10 pts per missing field
"""

from __future__ import annotations

import re
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from rapidfuzz import fuzz

KNOWN_SITE_ISO_TYPES = {
    "silicon wafer",
    "fabrication",
    "ic assembly",
    "assembly",
    "factory",
    "undefined",
}

CORE_FIELDS = ["site_name", "iso_type", "issued_date", "expiration_date"]
SECONDARY_FIELDS = ["address", "scope"]


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
    """Normalizes text by trimming, lowercasing, and collapsing whitespace."""
    if text is None or pd.isna(text):
        return ""
    s = str(text).strip()
    if s.lower() in ("nan", "none", "null", ""):
        return ""
    # Normalize unicode hyphens
    s = re.sub(r"[\u2010\u2011\u2012\u2013\u2014\u2015\u2212]", "-", s)
    # Collapse whitespace and newlines
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()


def parse_date_obj(date_val: Any) -> Optional[date]:
    """
    Parses a date value into a standard datetime.date object.
    Supports Timestamp, datetime, strings with common formats (MM/DD/YYYY, YYYY-MM-DD, DD/MM/YYYY, etc.).
    """
    if date_val is None or pd.isna(date_val):
        return None

    if isinstance(date_val, (datetime, pd.Timestamp)):
        return date_val.date()
    if isinstance(date_val, date):
        return date_val

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
        "%Y.%m.%d",
    ]

    for fmt in candidate_formats:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue

    return None


def calculate_year_difference(d_start: date, d_end: date) -> float:
    """
    Calculates duration in years between two dates.
    Uses days / 365.25 for fractional precision.
    """
    delta_days = (d_end - d_start).days
    return delta_days / 365.25


def check_rule_1_expiration_after_issued(
    issued_val: Any,
    expiration_val: Any
) -> Tuple[int, Optional[str]]:
    """
    Rule 1: expiration_date must be bigger than issued_date.
    If not, -30 points.
    """
    d_iss = parse_date_obj(issued_val)
    d_exp = parse_date_obj(expiration_val)

    if d_iss is None or d_exp is None:
        return -30, "Rule 1: Missing or unparseable issued/expiration date (-30 pts)"

    if d_exp <= d_iss:
        return -30, f"Rule 1: Expiration date ({d_exp}) is not after issued date ({d_iss}) (-30 pts)"

    return 0, None


def check_rule_2_validity_duration(
    issued_val: Any,
    expiration_val: Any
) -> Tuple[int, Optional[str]]:
    """
    Rule 2: Difference between expiration date and issued date:
      - >= 10 years: -20
      - between 10 and or equal 5 (5 <= diff < 10): -15
      - between 5 and not equal 3 (3 < diff < 5): -10
      - <= 3 years: 0
    (Evaluated only when both dates are valid and expiration > issued).
    """
    d_iss = parse_date_obj(issued_val)
    d_exp = parse_date_obj(expiration_val)

    if d_iss is None or d_exp is None or d_exp <= d_iss:
        return 0, None

    diff_years = calculate_year_difference(d_iss, d_exp)

    # Allow slight grace margin (up to 3.08 years / ~1125 days) for standard 3-year audits
    if diff_years >= 10.0:
        return -20, f"Rule 2: Validity duration >= 10 years ({diff_years:.1f} yrs) (-20 pts)"
    elif diff_years >= 5.0:
        return -15, f"Rule 2: Validity duration between 5 and 10 years ({diff_years:.1f} yrs) (-15 pts)"
    elif diff_years > 3.08:
        return -10, f"Rule 2: Validity duration between 3 and 5 years ({diff_years:.1f} yrs) (-10 pts)"

    return 0, None


def check_rule_3_known_site_iso_type(site_iso_type_val: Any) -> Tuple[int, Optional[str]]:
    """
    Rule 3: if site_iso_type not from the known values then -40.
    Known values: Silicon Wafer, Fabrication, IC Assembly, Assembly, Factory, Undefined.
    """
    norm = normalize_text(site_iso_type_val)
    if not norm or norm not in KNOWN_SITE_ISO_TYPES:
        display_val = str(site_iso_type_val).strip() if site_iso_type_val is not None else "None"
        return -40, f"Rule 3: site_iso_type '{display_val}' is not in known valid types (-40 pts)"
    return 0, None


def check_rule_4_pdf_pages(doc_type_val: Any, page_count_val: Any) -> Tuple[int, Optional[str]]:
    """
    Rule 4: if it is a pdf and has pages > 5 then -20;
    if pages between 5 and or equal 3 (3 <= pages <= 5) then -10.
    """
    norm_doc = normalize_text(doc_type_val)
    if norm_doc != "pdf":
        return 0, None

    try:
        pages = int(page_count_val) if page_count_val is not None else 1
    except (ValueError, TypeError):
        pages = 1

    if pages > 5:
        return -20, f"Rule 4: PDF document has {pages} pages (> 5 pages) (-20 pts)"
    elif pages >= 3:
        return -10, f"Rule 4: PDF document has {pages} pages (3-5 pages) (-10 pts)"

    return 0, None


def check_rule_5_fuzzy_cert_vs_iso_type(
    cert_type_val: Any,
    iso_type_val: Any
) -> Tuple[int, Optional[str]]:
    """
    Rule 5: if cert_type and iso_type have less than 95% match using fuzzy then -30.
    Handles multi-value iso_type (e.g. 'ISO 9001 | ISO 14001') by checking best match.
    """
    norm_cert = normalize_text(cert_type_val)
    norm_iso = normalize_text(iso_type_val)

    if not norm_cert or not norm_iso:
        return -30, "Rule 5: Missing cert_type or iso_type for fuzzy comparison (-30 pts)"

    # Base full string similarity
    score_full = max(
        fuzz.ratio(norm_cert, norm_iso),
        fuzz.token_sort_ratio(norm_cert, norm_iso),
        fuzz.token_set_ratio(norm_cert, norm_iso),
    )

    if score_full >= 95.0:
        return 0, None

    # Multi-value check if '|' is present
    iso_parts = [p.strip() for p in norm_iso.split("|") if p.strip()]
    cert_parts = [p.strip() for p in norm_cert.split("|") if p.strip()]

    best_part_score = score_full
    for c_p in cert_parts:
        for i_p in iso_parts:
            part_score = max(
                fuzz.ratio(c_p, i_p),
                fuzz.token_sort_ratio(c_p, i_p),
                fuzz.token_set_ratio(c_p, i_p),
            )
            if part_score > best_part_score:
                best_part_score = part_score

    if best_part_score >= 95.0:
        return 0, None

    return -30, (
        f"Rule 5: cert_type ('{cert_type_val}') and iso_type ('{iso_type_val}') "
        f"match is {best_part_score:.1f}% (< 95%) (-30 pts)"
    )


def check_rule_6_multisite_single_page(
    site_name_val: Any,
    page_count_val: Any,
    doc_type_val: Any
) -> Tuple[int, Optional[str]]:
    """
    Rule 6: if site_name has multiple entries separated by | and page count is 1 or it is an image then -20.
    """
    norm_site = str(site_name_val or "").strip()
    if not norm_site or norm_site.lower() in ("nan", "none", "null"):
        return 0, None

    parts = [p.strip() for p in norm_site.split("|") if p.strip()]
    if len(parts) <= 1:
        return 0, None

    try:
        pages = int(page_count_val) if page_count_val is not None else 1
    except (ValueError, TypeError):
        pages = 1

    norm_doc = normalize_text(doc_type_val)
    is_single_or_img = (pages == 1) or (norm_doc == "image")

    if is_single_or_img:
        return -20, (
            f"Rule 6: Multiple site_name entries ({len(parts)}) on a single page / image (-20 pts)"
        )

    return 0, None


def check_rule_7_missing_fields(row: Dict[str, Any]) -> Tuple[int, List[str]]:
    """
    Rule 7: Missing fields penalties:
      - Core fields (site_name, iso_type, issued_date, expiration_date): -20 pts each
      - Secondary fields (address, scope): -10 pts each.
        EXCEPTION: If either is_certificate or is_iso_iatf is False, the -10 pts
        deduction for missing secondary fields does NOT apply.
    """
    deduction = 0
    notes = []

    for field in CORE_FIELDS:
        val = row.get(field)
        if val is None or pd.isna(val) or str(val).strip() in ("", "nan", "None", "null"):
            deduction += -20
            notes.append(f"Rule 7: Missing core field '{field}' (-20 pts)")

    # Only apply -10 pts penalty for missing secondary fields if neither is_certificate nor is_iso_iatf is False
    is_cert = normalize_bool(row.get("is_certificate"))
    is_iso = normalize_bool(row.get("is_iso_iatf"))

    apply_secondary_penalty = not (is_cert is False or is_iso is False)

    if apply_secondary_penalty:
        for field in SECONDARY_FIELDS:
            val = row.get(field)
            if val is None or pd.isna(val) or str(val).strip() in ("", "nan", "None", "null"):
                deduction += -10
                notes.append(f"Rule 7: Missing field '{field}' (-10 pts)")

    return deduction, notes
