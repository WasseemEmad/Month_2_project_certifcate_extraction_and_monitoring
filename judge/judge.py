"""
judge/judge.py
==============
Post-batch LLM-as-a-Judge evaluation with stratified random sampling (~15%)
across Approved, Need Review, and Reject verdict categories.
Opens the actual certificate document (multimodal) and compares the extracted fields
and verification verdict directly against the certificate content.
Excludes failed-to-open documents from judge sampling.
Logs scores to SQLite and outputs judge metrics for analytics.json.
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import shutil
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

from google import genai
from google.genai.types import Part

from database.db import DEFAULT_DB_PATH, update_judge_result
from extractor.utils import prepare_images_for_cert_url
from tracing.tracer import generation_llm_judge

logger = logging.getLogger("cert_judge")

JUDGE_PROMPT_TEMPLATE = """You are an independent quality audit judge inspecting an ISO/IATF certificate document and evaluating an AI extraction pipeline's output.

The actual certificate document is attached as image(s).

AI Extracted Output to Audit:
- is_certificate: {is_certificate}
- is_iso_iatf: {is_iso_iatf}
- cert_type: {cert_type}
- screening_reason: {screening_reason}
- site_name: {site_name}
- address: {address}
- scope: {scope}
- iso_type: {iso_type}
- issued_date: {issued_date}
- expiration_date: {expiration_date}
- site_iso_type: {site_iso_type}

Rule-Based Verification Verdict:
- Verdict: {verdict}
- Confidence Score: {confidence_score} / 100
- Checks / Violations: {checks_passed_or_failed}

Audit Instructions:
Read and inspect the attached certificate image(s) carefully:
1. Document Identification: Does the attached document match the classification (is_certificate, is_iso_iatf)?
2. Field Verification against Document:
   - Does site_name match the certified organization visible on the certificate?
   - Does address match the site location shown?
   - Does scope accurately reflect the certified scope of operations?
   - Does iso_type / cert_type accurately match the standard (e.g. ISO 9001:2015, ISO 14001:2015, IATF 16949)?
   - Are issued_date and expiration_date accurate to the certificate?
   - Is site_iso_type a reasonable manufacturing category for this scope?
3. Verdict Assessment:
   - If the certificate is genuine and extracted accurately, is 'Approved' appropriate?
   - If the document is NOT an ISO/IATF certificate, or has severe discrepancies, does the verdict appropriately reflect 'Need Review' or 'Reject'?

Scoring Standard:
- Score 1 (Pass): The extracted fields and verdict are faithful and accurate to the attached certificate.
- Score 0 (Fail): There are significant hallucinations, wrong dates/names, misclassification, or an unjustified verdict.

Respond ONLY with a valid JSON object matching this schema:
{{
  "score": 1,
  "reasoning": "<Concise 1-2 sentence explanation of your comparison with the actual certificate>"
}}
"""


def is_record_eligible_for_judge(record: Dict[str, Any]) -> bool:
    """
    Determines whether a certificate record is eligible for LLM judge evaluation.
    Excludes cases where the certificate failed to open or has 0 pages.
    """
    status = (record.get("status") or "").lower()
    reviews = (record.get("verification_reviews") or "").lower()
    page_count = record.get("page_count") or 0
    doc_type = record.get("doc_type") or "unknown"

    if "did not open" in status or "did not open" in reviews:
        return False
    if page_count <= 0 and doc_type == "unknown":
        return False
    return True


def sample_records_stratified(
    records: List[Dict[str, Any]],
    sample_ratio: float = 0.15,
) -> List[Dict[str, Any]]:
    """
    Performs stratified random sampling covering Approved, Need Review, and Reject.
    Excludes cases where the certificate failed to open (status contains 'did not open'
    or page_count is 0), ensuring the judge only audits opened certificates.
    """
    if not records:
        return []

    # Filter out failed-to-open records
    eligible_records = [r for r in records if is_record_eligible_for_judge(r)]

    if not eligible_records:
        logger.info("[judge] No eligible opened certificates found for sampling.")
        return []

    # Group eligible records by verdict
    groups: Dict[str, List[Dict[str, Any]]] = {
        "Approved": [],
        "Need Review": [],
        "Reject": [],
        "Other": [],
    }

    for r in eligible_records:
        v = (r.get("verification_status") or r.get("verdict") or "Other").strip()
        if v in groups:
            groups[v].append(r)
        else:
            groups["Other"].append(r)

    active_groups = {k: v for k, v in groups.items() if len(v) > 0}
    total_eligible = len(eligible_records)
    # Target ~15% of total eligible records
    target_sample_size = max(len(active_groups), math.ceil(total_eligible * sample_ratio))
    target_sample_size = min(target_sample_size, total_eligible)

    # Guarantee at least 1 random pick from each active category
    remaining_budget = target_sample_size
    allocated: Dict[str, int] = {}
    for cat, items in active_groups.items():
        allocated[cat] = 1
        remaining_budget -= 1

    # Distribute remaining budget proportionally
    while remaining_budget > 0:
        added_in_round = False
        for cat, items in sorted(active_groups.items(), key=lambda x: len(x[1]), reverse=True):
            if remaining_budget <= 0:
                break
            if allocated[cat] < len(items):
                allocated[cat] += 1
                remaining_budget -= 1
                added_in_round = True
        if not added_in_round:
            break

    # Randomly pick from each category
    sampled: List[Dict[str, Any]] = []
    for cat, count in allocated.items():
        pool = active_groups[cat]
        chosen = random.sample(pool, min(count, len(pool)))
        sampled.extend(chosen)

    return sampled


class LLMJudge:
    """
    Evaluates a sample of processed certificates using Gemini as an independent auditor.
    Downloads and opens the certificate to compare the actual document visually against
    the extracted fields and rule-based verdict.
    """

    def __init__(self, api_key: str, model_name: str = "gemini-2.5-flash-lite"):
        if not api_key:
            raise ValueError("Gemini API key is required for LLM Judge.")
        self.api_key = api_key
        self.model_name = model_name
        self.client = genai.Client(api_key=self.api_key)

    def evaluate_record(self, record: Dict[str, Any]) -> Tuple[int, str, float]:
        """
        Downloads the certificate, renders pages, and passes them to Gemini
        alongside the extracted answers and verdict.
        Returns (score, reasoning, latency_ms).
        score is strictly 1 (Pass) or 0 (Fail).
        """
        url = record.get("url", "N/A")
        verdict = record.get("verification_status") or record.get("verdict") or "Unknown"

        prompt = JUDGE_PROMPT_TEMPLATE.format(
            is_certificate=record.get("is_certificate"),
            is_iso_iatf=record.get("is_iso_iatf"),
            cert_type=record.get("cert_type", "N/A"),
            screening_reason=record.get("screening_reason", "N/A"),
            site_name=record.get("site_name", "N/A"),
            address=record.get("address", "N/A"),
            scope=record.get("scope", "N/A"),
            iso_type=record.get("iso_type", "N/A"),
            issued_date=record.get("issued_date", "N/A"),
            expiration_date=record.get("expiration_date", "N/A"),
            site_iso_type=record.get("site_iso_type", "N/A"),
            verdict=verdict,
            confidence_score=record.get("confidence_score", 0),
            checks_passed_or_failed=record.get("verification_reviews") or record.get("checks_passed_or_failed", "None"),
        )

        start = time.perf_counter()
        temp_dir = tempfile.mkdtemp(prefix="judge_cert_")
        try:
            # 1. Download and render certificate pages
            images, dl_err, meta = prepare_images_for_cert_url(url, temp_dir, max_pages=3, return_meta=True)
            if dl_err or not images:
                latency_ms = (time.perf_counter() - start) * 1000
                logger.warning(f"[judge] Could not open certificate at {url}: {dl_err}")
                return 0, f"Judge failed to open certificate for verification: {dl_err}", latency_ms

            # 2. Build multimodal payload (prompt + page images)
            content_parts = [prompt]
            for img_path, mime_type in images:
                with open(img_path, "rb") as f:
                    content_parts.append(Part.from_bytes(data=f.read(), mime_type=mime_type))

            # 3. Call Gemini with multimodal inputs
            with generation_llm_judge(model=self.model_name, url=url, prompt=prompt) as gen:
                try:
                    response = self.client.models.generate_content(
                        model=self.model_name,
                        contents=content_parts,
                        config={
                            "temperature": 0.0,
                            "response_mime_type": "application/json",
                        },
                    )
                    raw_text = (response.text or "").strip()
                    parsed = json.loads(raw_text)
                    raw_score = parsed.get("score", 1)
                    score = 1 if int(raw_score) >= 1 else 0
                    reasoning = parsed.get("reasoning", "Valid extraction.")
                    latency_ms = (time.perf_counter() - start) * 1000

                    usage_meta = getattr(response, "usage_metadata", None)
                    usage = {
                        "input": getattr(usage_meta, "prompt_token_count", 0) or 0,
                        "output": getattr(usage_meta, "candidates_token_count", 0) or 0,
                    }
                    gen.update(
                        output={"score": score, "reasoning": reasoning},
                        usage_details=usage,
                        metadata={"latency_ms": latency_ms, "score": score},
                    )
                    return score, reasoning, latency_ms

                except Exception as e:
                    latency_ms = (time.perf_counter() - start) * 1000
                    logger.warning(f"[judge] LLM Judge evaluation error for {url}: {e}")
                    gen.update(level="ERROR", status_message=str(e))
                    return 0, f"Judge error during visual verification: {e}", latency_ms

        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def evaluate_batch(
        self,
        records: List[Dict[str, Any]],
        sample_ratio: float = 0.15,
        db_path: str = DEFAULT_DB_PATH,
    ) -> Dict[str, Any]:
        """
        Stratified random sampling across Approved, Need Review, and Reject (~15%),
        evaluates with Gemini by opening the certificate document, logs scores to SQLite,
        and returns summary stats.
        """
        sampled_records = sample_records_stratified(records, sample_ratio=sample_ratio)
        total_sampled = len(sampled_records)

        if total_sampled == 0:
            print("[LLM Judge] No eligible opened certificates available for sampling.")
            return {
                "total_sampled": 0,
                "sample_rate_percent": 0.0,
                "passed_count": 0,
                "failed_count": 0,
                "avg_judge_score": 1.0,
                "pass_rate_percent": 100.0,
                "category_breakdown": {},
                "evaluations": [],
            }

        print(f"\n[LLM Judge] Opening certificates & running stratified visual audit on {total_sampled}/{len(records)} records (~{round(total_sampled/max(1, len(records))*100, 1)}%)...")

        results = []
        category_counts: Dict[str, int] = {}
        category_pass: Dict[str, int] = {}

        for idx, rec in enumerate(sampled_records, start=1):
            url = rec.get("url", "")
            verdict = (rec.get("verification_status") or rec.get("verdict") or "Other").strip()
            category_counts[verdict] = category_counts.get(verdict, 0) + 1

            score, reason, lat_ms = self.evaluate_record(rec)
            if score == 1:
                category_pass[verdict] = category_pass.get(verdict, 0) + 1

            req_id = rec.get("request_id")
            if req_id:
                update_judge_result(
                    request_id=req_id,
                    judge_score=score,
                    judge_reasoning=reason,
                    db_path=db_path,
                )

            status_str = "PASS (1)" if score == 1 else "FAIL (0)"
            print(f"  [{idx}/{total_sampled}] [{verdict}] {url[:55]}... -> {status_str} ({lat_ms:.0f}ms)")
            logger.info(f"[judge] [{verdict}] score={score} reason='{reason}' url={url}")

            results.append({
                "request_id": req_id,
                "url": url,
                "verdict": verdict,
                "confidence_score": rec.get("confidence_score"),
                "judge_score": score,
                "judge_reasoning": reason,
                "latency_ms": round(lat_ms, 2),
            })

        total_pass = sum(1 for r in results if r["judge_score"] == 1)
        total_fail = total_sampled - total_pass
        avg_score = round(total_pass / total_sampled, 4) if total_sampled > 0 else 1.0

        summary = {
            "total_sampled": total_sampled,
            "sample_rate_percent": round(total_sampled / max(1, len(records)) * 100, 2),
            "passed_count": total_pass,
            "failed_count": total_fail,
            "avg_judge_score": avg_score,
            "pass_rate_percent": round(avg_score * 100, 2),
            "category_breakdown": {
                cat: {
                    "sampled": count,
                    "passed": category_pass.get(cat, 0),
                    "pass_rate_percent": round(category_pass.get(cat, 0) / count * 100, 2),
                }
                for cat, count in category_counts.items()
            },
            "evaluations": results,
        }

        print(f"[LLM Judge] Completed: {total_pass}/{total_sampled} Passed (Average Score: {avg_score:.2f} / 1.00)")
        return summary
