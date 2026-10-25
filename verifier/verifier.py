"""
verifier/verifier.py
====================
Main verifier engine that applies scoring rules to extraction outputs.
Calculates confidence score (0-100) and assigns status:
  - Approved: score >= 90
  - Need Review: 50 <= score < 90
  - Reject: score < 50
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from .rules import (
    check_rule_1_expiration_after_issued,
    check_rule_2_validity_duration,
    check_rule_3_known_site_iso_type,
    check_rule_4_pdf_pages,
    check_rule_5_fuzzy_cert_vs_iso_type,
    check_rule_6_multisite_single_page,
    check_rule_7_missing_fields,
    normalize_text,
    normalize_bool,
)


@dataclass
class VerificationRowResult:
    score: int
    status: str
    reviews: str
    deductions: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "confidence_score": self.score,
            "verification_status": self.status,
            "verification_reviews": self.reviews,
        }


class CertificateVerifier:
    """
    Evaluates extracted certificate rows against business verification rules.
    Initial score: 100 points per row.
    Score clamped between 0 and 100.
    """

    def __init__(self, initial_score: int = 100):
        self.initial_score = initial_score

    def infer_doc_type_and_pages(self, row: Dict[str, Any]) -> Tuple[str, int]:
        """Infers doc_type and page_count if not already explicitly present in row."""
        doc_type = str(row.get("doc_type") or "").strip().lower()
        page_count = row.get("page_count")

        if not doc_type or doc_type == "unknown":
            url = str(row.get("url") or "").split("?")[0].lower()
            if url.endswith(".pdf"):
                doc_type = "pdf"
            elif any(url.endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".tif", ".tiff"]):
                doc_type = "image"
            else:
                doc_type = "unknown"

        try:
            pages = int(page_count) if page_count is not None and not pd.isna(page_count) else 1
            if pages < 1:
                pages = 1
        except (ValueError, TypeError):
            pages = 1

        return doc_type, pages

    def verify_row(self, row: Dict[str, Any]) -> VerificationRowResult:
        """
        Runs verification rules on a single extracted certificate row.
        - If document failed to open / screening error -> Need Review (Score: 50).
        - If is_certificate == False -> Approved (Score: 100), field rules skipped.
        - If is_iso_iatf == False -> Approved (Score: 100), field rules skipped.
        - Otherwise -> Standard evaluation (Rules 1-7).
        """
        status_val = str(row.get("status") or "").strip().lower()

        # ── 1. Document did not open -> Need Review with score = 50 ────────────
        if "did not open" in status_val or "screening error" in status_val:
            return VerificationRowResult(
                score=50,
                status="Need Review",
                reviews="Document did not open; flagged for manual review",
                deductions=["Status: Document did not open or screening failed (Score: 50)"],
            )

        # ── 2. Screened as Not a Certificate or Not an ISO/IATF Certificate ────
        # Non-ISO documents correctly have empty extraction fields; skip field-level rules.
        is_cert = normalize_bool(row.get("is_certificate"))
        is_iso = normalize_bool(row.get("is_iso_iatf"))

        if is_cert is False or status_val == "not a certificate":
            return VerificationRowResult(
                score=100,
                status="Approved",
                reviews="Screening confirmed: Not a certificate",
                deductions=[],
            )

        if is_iso is False or status_val == "not an iso/iatf certificate":
            return VerificationRowResult(
                score=100,
                status="Approved",
                reviews="Screening confirmed: Not an ISO/IATF certificate",
                deductions=[],
            )

        score = self.initial_score
        deductions: List[str] = []

        # Infer doc_type and page_count if necessary
        doc_type, page_count = self.infer_doc_type_and_pages(row)

        issued_val = row.get("issued_date")
        expiration_val = row.get("expiration_date")
        site_iso_type_val = row.get("site_iso_type")
        cert_type_val = row.get("cert_type")
        iso_type_val = row.get("iso_type")
        site_name_val = row.get("site_name")

        # ── Rule 1: Expiration date > Issued date (-30 pts) ────────────────────
        r1_deduct, r1_note = check_rule_1_expiration_after_issued(issued_val, expiration_val)
        if r1_deduct != 0 and r1_note:
            score += r1_deduct
            deductions.append(r1_note)

        # ── Rule 2: Validity duration limits (-10, -15, -20 pts) ──────────────
        r2_deduct, r2_note = check_rule_2_validity_duration(issued_val, expiration_val)
        if r2_deduct != 0 and r2_note:
            score += r2_deduct
            deductions.append(r2_note)

        # ── Rule 3: Known site_iso_type (-40 pts) ─────────────────────────────
        r3_deduct, r3_note = check_rule_3_known_site_iso_type(site_iso_type_val)
        if r3_deduct != 0 and r3_note:
            score += r3_deduct
            deductions.append(r3_note)

        # ── Rule 4: PDF pages > 5 (-20 pts), 3-5 pages (-10 pts) ──────────────
        r4_deduct, r4_note = check_rule_4_pdf_pages(doc_type, page_count)
        if r4_deduct != 0 and r4_note:
            score += r4_deduct
            deductions.append(r4_note)

        # ── Rule 5: cert_type vs iso_type fuzzy match < 95% (-30 pts) ─────────
        r5_deduct, r5_note = check_rule_5_fuzzy_cert_vs_iso_type(cert_type_val, iso_type_val)
        if r5_deduct != 0 and r5_note:
            score += r5_deduct
            deductions.append(r5_note)

        # ── Rule 6: Multi site_name on single page / image (-20 pts) ──────────
        r6_deduct, r6_note = check_rule_6_multisite_single_page(site_name_val, page_count, doc_type)
        if r6_deduct != 0 and r6_note:
            score += r6_deduct
            deductions.append(r6_note)

        # ── Rule 7: Missing fields penalty (-20 core, -10 secondary) ──────────
        r7_deduct, r7_notes = check_rule_7_missing_fields(row)
        if r7_deduct != 0:
            score += r7_deduct
            deductions.extend(r7_notes)

        # ── Clamp score between 0 and 100 ─────────────────────────────────────
        final_score = max(0, min(100, score))

        # ── Status classification ─────────────────────────────────────────────
        if final_score >= 90:
            status = "Approved"
        elif final_score >= 50:
            status = "Need Review"
        else:
            status = "Reject"

        reviews_str = "; ".join(deductions) if deductions else "Passed all verification rules"

        return VerificationRowResult(
            score=final_score,
            status=status,
            reviews=reviews_str,
            deductions=deductions,
        )

    def verify_dataframe(self, df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, Any]]:
        """
        Applies verification rules to all rows of a DataFrame.
        Returns (augmented_dataframe, summary_statistics_dict).
        """
        if df.empty:
            empty_summary = {
                "total_cases": 0,
                "approved_cases": 0,
                "need_review_cases": 0,
                "rejected_cases": 0,
                "approval_rate_percent": 0.0,
                "avg_score": 0.0,
                "rule_violation_counts": {},
            }
            return df.copy(), empty_summary

        df_out = df.copy()

        scores = []
        statuses = []
        reviews = []
        violation_counts: Dict[str, int] = {}

        for _, row in df_out.iterrows():
            row_dict = row.to_dict()
            res = self.verify_row(row_dict)
            scores.append(res.score)
            statuses.append(res.status)
            reviews.append(res.reviews)

            for d in res.deductions:
                rule_prefix = d.split(":")[0].strip() if ":" in d else "Other"
                violation_counts[rule_prefix] = violation_counts.get(rule_prefix, 0) + 1

        df_out["confidence_score"] = scores
        df_out["verification_status"] = statuses
        df_out["verification_reviews"] = reviews

        total_cases = len(df_out)
        approved_count = sum(1 for s in statuses if s == "Approved")
        review_count = sum(1 for s in statuses if s == "Need Review")
        rejected_count = sum(1 for s in statuses if s == "Reject")
        avg_score = round(sum(scores) / total_cases, 2) if total_cases > 0 else 0.0
        approval_rate = round(approved_count / total_cases * 100.0, 2) if total_cases > 0 else 0.0

        summary = {
            "total_cases": total_cases,
            "approved_cases": approved_count,
            "need_review_cases": review_count,
            "rejected_cases": rejected_count,
            "approval_rate_percent": approval_rate,
            "avg_score": avg_score,
            "rule_violation_counts": violation_counts,
        }

        return df_out, summary
