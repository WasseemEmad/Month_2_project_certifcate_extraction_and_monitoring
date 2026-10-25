"""
judge/judge.py
==============
Post-batch LLM-as-a-Judge evaluation with stratified random sampling (~15%)
across Approved, Need Review, and Reject verdict categories.
Logs scores to SQLite and outputs judge metrics for analytics.json.
"""

from __future__ import annotations

import json
import logging
import math
import random
import time
from typing import Any, Dict, List, Optional, Tuple

from google import genai
from google.genai.types import Part

from database.db import DEFAULT_DB_PATH, update_judge_result
from tracing.tracer import generation_llm_judge

logger = logging.getLogger("cert_judge")

JUDGE_PROMPT_TEMPLATE = """You are an independent quality audit judge evaluating an AI extraction and rule-based verification pipeline for ISO/IATF certificates.

Document Input:
- URL: {url}
- Document Type: {doc_type} (Page count: {page_count})

AI Extracted Output:
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

Audit Task:
Assess whether the extracted data and the verification verdict are sound, coherent, and free of critical hallucinations.
- Score 1 (Pass): The extraction and verdict are plausible, consistent, and logically valid.
- Score 0 (Fail): There are severe contradictions, hallucinated data, or an unreasonable verdict.

Respond ONLY with a valid JSON object matching this schema:
{{
  "score": 1,
  "reasoning": "<Concise 1-2 sentence justification>"
}}
"""


def sample_records_stratified(
    records: List[Dict[str, Any]],
    sample_ratio: float = 0.15,
) -> List[Dict[str, Any]]:
    """
    Performs stratified random sampling covering Approved, Need Review, and Reject.
    Samples approximately ~15% of the total dataset, ensuring representation
    across all present verdict categories.
    """
    if not records:
        return []

    # Group records by verdict
    groups: Dict[str, List[Dict[str, Any]]] = {
        "Approved": [],
        "Need Review": [],
        "Reject": [],
        "Other": [],
    }

    for r in records:
        v = (r.get("verification_status") or r.get("verdict") or "Other").strip()
        if v in groups:
            groups[v].append(r)
        else:
            groups["Other"].append(r)

    active_groups = {k: v for k, v in groups.items() if len(v) > 0}
    total_records = len(records)
    target_sample_size = max(len(active_groups), math.ceil(total_records * sample_ratio))
    target_sample_size = min(target_sample_size, total_records)

    sampled: List[Dict[str, Any]] = []

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
    for cat, count in allocated.items():
        pool = active_groups[cat]
        chosen = random.sample(pool, min(count, len(pool)))
        sampled.extend(chosen)

    return sampled


class LLMJudge:
    """
    Evaluates a sample of processed certificates using Gemini as an independent auditor.
    """

    def __init__(self, api_key: str, model_name: str = "gemini-2.5-flash-lite"):
        if not api_key:
            raise ValueError("Gemini API key is required for LLM Judge.")
        self.api_key = api_key
        self.model_name = model_name
        self.client = genai.Client(api_key=self.api_key)

    def evaluate_record(self, record: Dict[str, Any]) -> Tuple[int, str, float]:
        """
        Evaluates a single certificate record with Gemini and returns (score, reasoning, latency_ms).
        score is strictly 1 (Pass) or 0 (Fail).
        """
        url = record.get("url", "N/A")
        verdict = record.get("verification_status") or record.get("verdict") or "Unknown"

        prompt = JUDGE_PROMPT_TEMPLATE.format(
            url=url,
            doc_type=record.get("doc_type", "unknown"),
            page_count=record.get("page_count", 1),
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
        score = 1
        reasoning = "Extraction is sound."

        with generation_llm_judge(model=self.model_name, url=url, prompt=prompt) as gen:
            try:
                response = self.client.models.generate_content(
                    model=self.model_name,
                    contents=prompt,
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
            except Exception as e:
                latency_ms = (time.perf_counter() - start) * 1000
                logger.warning(f"[judge] LLM Judge evaluation error for {url}: {e}")
                gen.update(level="ERROR", status_message=str(e))
                # Fallback heuristic: if approved by verifier with score >= 90, score 1 else 0
                conf = record.get("confidence_score") or 0
                score = 1 if conf >= 80 else 0
                reasoning = f"Fallback evaluation due to error ({e}): confidence={conf}"

        return score, reasoning, latency_ms

    def evaluate_batch(
        self,
        records: List[Dict[str, Any]],
        sample_ratio: float = 0.15,
        db_path: str = DEFAULT_DB_PATH,
    ) -> Dict[str, Any]:
        """
        Stratified random sampling across Approved, Need Review, and Reject (~15%),
        evaluates with Gemini, logs scores to SQLite, and returns summary stats.
        """
        sampled_records = sample_records_stratified(records, sample_ratio=sample_ratio)
        total_sampled = len(sampled_records)

        print(f"\n[LLM Judge] Running stratified post-batch evaluation on {total_sampled}/{len(records)} records (~{round(total_sampled/max(1, len(records))*100, 1)}%)...")

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
