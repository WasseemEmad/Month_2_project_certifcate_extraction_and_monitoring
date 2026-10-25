"""
analytics.py
============
Metrics and analytics tracking module for ISO certificate extraction.
Tracks latency, token usage, retry attempts, and cost estimation (matching saturday_email.py patterns).
Saves cumulative run history to analytics/analytics.json with runs labeled "run 1", "run 2", etc.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

# ── Pricing Configuration (per 1 Million Tokens) ──────────────────────────────
# Matches the cost structure used in saturday_email.py:
# Can be calibrated per model or overridden.
DEFAULT_COST_PER_1M_INPUT = 1.0   # $1.00 per 1M input tokens
DEFAULT_COST_PER_1M_OUTPUT = 2.5  # $2.50 per 1M output tokens

MODEL_PRICING = {
    # Gemini 2.5 Flash Lite official pricing ($0.075 / 1M input, $0.30 / 1M output)
    "gemini-2.5-flash-lite": {"input": 0.075, "output": 0.30},
    "gemini-1.5-flash": {"input": 0.075, "output": 0.30},
    "gemini-1.5-pro": {"input": 1.25, "output": 5.00},
}


def estimate_cost(
    input_tokens: int,
    output_tokens: int,
    model_name: Optional[str] = None
) -> float:
    """
    Estimates USD cost based on token counts and model pricing.
    Falls back to saturday_email.py standard pricing ($1.00 / $2.50) if model not in table.
    """
    pricing = MODEL_PRICING.get(model_name or "")
    if pricing:
        rate_in = pricing["input"]
        rate_out = pricing["output"]
    else:
        rate_in = DEFAULT_COST_PER_1M_INPUT
        rate_out = DEFAULT_COST_PER_1M_OUTPUT

    in_tokens = max(0, input_tokens or 0)
    out_tokens = max(0, output_tokens or 0)
    return (in_tokens / 1e6) * rate_in + (out_tokens / 1e6) * rate_out


class StepMetric:
    """Stores metrics for a single LLM operation step."""

    def __init__(
        self,
        step_name: str,
        latency_ms: float = 0.0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        retry_attempts: int = 1,
        status: str = "success",
        error: Optional[str] = None,
    ):
        self.step_name = step_name
        self.latency_ms = round(latency_ms, 2)
        self.input_tokens = input_tokens or 0
        self.output_tokens = output_tokens or 0
        self.total_tokens = self.input_tokens + self.output_tokens
        self.cost_usd = round(cost_usd, 6)
        self.retry_attempts = retry_attempts
        self.status = status
        self.error = error

    def to_dict(self) -> Dict[str, Any]:
        return {
            "latency_ms": self.latency_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "retry_attempts": self.retry_attempts,
            "status": self.status,
            "error": self.error,
        }


class URLMetrics:
    """Stores end-to-end metrics for processing a single certificate URL."""

    def __init__(self, url: str):
        self.url = url
        self.status = "pending"
        self.total_latency_ms: float = 0.0
        self.download_latency_ms: float = 0.0
        self.screening: Optional[StepMetric] = None
        self.extraction: Optional[StepMetric] = None
        self.scope_classification: Optional[StepMetric] = None
        self.overhead_ms: float = 0.0
        self.error: Optional[str] = None

    def finalize(self, total_latency_ms: float, status: str, error: Optional[str] = None):
        self.total_latency_ms = round(total_latency_ms, 2)
        self.status = status
        self.error = error

        step_latencies = sum(
            s.latency_ms for s in [self.screening, self.extraction, self.scope_classification]
            if s is not None
        ) + self.download_latency_ms
        self.overhead_ms = round(max(0.0, self.total_latency_ms - step_latencies), 2)

    @property
    def total_input_tokens(self) -> int:
        return sum(
            s.input_tokens for s in [self.screening, self.extraction, self.scope_classification]
            if s is not None
        )

    @property
    def total_output_tokens(self) -> int:
        return sum(
            s.output_tokens for s in [self.screening, self.extraction, self.scope_classification]
            if s is not None
        )

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    @property
    def total_cost_usd(self) -> float:
        return round(
            sum(
                s.cost_usd for s in [self.screening, self.extraction, self.scope_classification]
                if s is not None
            ),
            6
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "status": self.status,
            "total_latency_ms": self.total_latency_ms,
            "download_latency_ms": round(self.download_latency_ms, 2),
            "overhead_ms": self.overhead_ms,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_tokens": self.total_tokens,
            "total_cost_usd": self.total_cost_usd,
            "error": self.error,
            "steps": {
                "screening": self.screening.to_dict() if self.screening else None,
                "extraction": self.extraction.to_dict() if self.extraction else None,
                "scope_classification": (
                    self.scope_classification.to_dict() if self.scope_classification else None
                ),
            },
        }


class RunAnalytics:
    """
    Thread-safe collector for overall run analytics and persistence into analytics/analytics.json.
    """

    def __init__(self, model_name: str = "gemini-2.5-flash-lite"):
        self.model_name = model_name
        self.start_time = datetime.now()
        self._url_records: List[URLMetrics] = []
        self._lock = threading.Lock()

    def record_url_metrics(self, metric: URLMetrics):
        with self._lock:
            self._url_records.append(metric)

    def generate_summary(self) -> Dict[str, Any]:
        with self._lock:
            records = list(self._url_records)

        total_urls = len(records)
        successful_urls = sum(
            1 for r in records if r.status in ("OK", "Partial") or r.status.startswith("OK")
        )
        failed_urls = total_urls - successful_urls

        total_latency_ms = sum(r.total_latency_ms for r in records)
        avg_latency_ms = (total_latency_ms / total_urls) if total_urls > 0 else 0.0

        total_input_tokens = sum(r.total_input_tokens for r in records)
        total_output_tokens = sum(r.total_output_tokens for r in records)
        total_tokens = total_input_tokens + total_output_tokens
        total_cost_usd = round(sum(r.total_cost_usd for r in records), 6)

        # Step breakdowns
        def summarize_step(step_getter):
            step_list = [step_getter(r) for r in records if step_getter(r) is not None]
            count = len(step_list)
            if count == 0:
                return {
                    "count": 0,
                    "total_latency_ms": 0.0,
                    "avg_latency_ms": 0.0,
                    "total_tokens": 0,
                    "total_cost_usd": 0.0,
                }
            tot_lat = sum(s.latency_ms for s in step_list)
            tot_in = sum(s.input_tokens for s in step_list)
            tot_out = sum(s.output_tokens for s in step_list)
            tot_cost = sum(s.cost_usd for s in step_list)
            return {
                "count": count,
                "total_latency_ms": round(tot_lat, 2),
                "avg_latency_ms": round(tot_lat / count, 2),
                "total_input_tokens": tot_in,
                "total_output_tokens": tot_out,
                "total_tokens": tot_in + tot_out,
                "total_cost_usd": round(tot_cost, 6),
            }

        dl_latencies = [r.download_latency_ms for r in records if r.download_latency_ms > 0]
        dl_summary = {
            "count": len(dl_latencies),
            "total_latency_ms": round(sum(dl_latencies), 2),
            "avg_latency_ms": round(sum(dl_latencies) / len(dl_latencies), 2) if dl_latencies else 0.0,
        }

        return {
            "run_timestamp": self.start_time.strftime("%Y-%m-%d %H:%M:%S"),
            "model": self.model_name,
            "summary": {
                "total_urls": total_urls,
                "successful_urls": successful_urls,
                "failed_urls": failed_urls,
                "total_latency_ms": round(total_latency_ms, 2),
                "total_latency_seconds": round(total_latency_ms / 1000, 2),
                "avg_latency_per_url_ms": round(avg_latency_ms, 2),
                "total_input_tokens": total_input_tokens,
                "total_output_tokens": total_output_tokens,
                "total_tokens": total_tokens,
                "total_cost_usd": total_cost_usd,
                "steps_breakdown": {
                    "download": dl_summary,
                    "screening": summarize_step(lambda r: r.screening),
                    "extraction": summarize_step(lambda r: r.extraction),
                    "scope_classification": summarize_step(lambda r: r.scope_classification),
                },
            },
            "per_url_details": [r.to_dict() for r in records],
        }

    def save_to_json(
        self,
        analytics_dir: str = "analytics",
        filename: str = "analytics.json",
        aggregated_filename: str = "analytics_aggregated.json",
        verification_summary: Optional[Dict[str, Any]] = None,
        judge_summary: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, str]:
        """
        Saves the run analytics to a JSON file inside analytics_dir.
        Finds existing runs ('run 1', 'run 2', ...) and appends the new run.
        Also automatically maintains and updates analytics_aggregated.json.
        Returns the absolute path to the detailed analytics file and run key name.
        """
        out_dir = Path(analytics_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        file_path = out_dir / filename

        data: Dict[str, Any] = {}
        if file_path.exists():
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        data = json.loads(content)
            except Exception:
                data = {}

        # Determine next run label ("run 1", "run 2", ...)
        existing_numbers = []
        for key in data.keys():
            m = re.match(r"^run\s+(\d+)$", key.strip(), flags=re.IGNORECASE)
            if m:
                existing_numbers.append(int(m.group(1)))

        next_run_num = (max(existing_numbers) + 1) if existing_numbers else 1
        run_key = f"run {next_run_num}"

        run_summary = self.generate_summary()
        if verification_summary is not None:
            run_summary["verification_summary"] = verification_summary
        if judge_summary is not None:
            run_summary["judge_summary"] = judge_summary

        data[run_key] = run_summary

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        # Update aggregated analytics file
        self._update_aggregated_json(
            out_dir=out_dir,
            aggregated_filename=aggregated_filename,
            run_key=run_key,
            run_summary=run_summary,
            verification_summary=verification_summary,
            judge_summary=judge_summary,
        )

        return str(file_path.resolve()), run_key

    def _update_aggregated_json(
        self,
        out_dir: Path,
        aggregated_filename: str,
        run_key: str,
        run_summary: Dict[str, Any],
        verification_summary: Optional[Dict[str, Any]] = None,
        judge_summary: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Maintains an aggregated runs summary file (analytics_aggregated.json)
        recording key metrics per run (run 1, 2, ...) and overall cumulative totals.
        """
        agg_path = out_dir / aggregated_filename
        agg_data: Dict[str, Any] = {"runs": {}, "overall_aggregated": {}}

        if agg_path.exists():
            try:
                with open(agg_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        loaded = json.loads(content)
                        if isinstance(loaded, dict) and "runs" in loaded:
                            agg_data = loaded
            except Exception:
                pass

        summary_metrics = run_summary.get("summary", {})
        agg_data["runs"][run_key] = {
            "timestamp": run_summary.get("run_timestamp"),
            "model": run_summary.get("model"),
            "total_urls": summary_metrics.get("total_urls", 0),
            "successful_urls": summary_metrics.get("successful_urls", 0),
            "failed_urls": summary_metrics.get("failed_urls", 0),
            "total_latency_seconds": summary_metrics.get("total_latency_seconds", 0.0),
            "avg_latency_per_url_ms": summary_metrics.get("avg_latency_per_url_ms", 0.0),
            "total_tokens": summary_metrics.get("total_tokens", 0),
            "total_input_tokens": summary_metrics.get("total_input_tokens", 0),
            "total_output_tokens": summary_metrics.get("total_output_tokens", 0),
            "total_cost_usd": summary_metrics.get("total_cost_usd", 0.0),
            "verification": verification_summary,
            "judge": judge_summary,
        }

        # Calculate cumulative totals across all recorded runs
        runs_dict = agg_data["runs"]
        total_runs_count = len(runs_dict)
        tot_urls = sum(r.get("total_urls", 0) for r in runs_dict.values())
        tot_success = sum(r.get("successful_urls", 0) for r in runs_dict.values())
        tot_failed = sum(r.get("failed_urls", 0) for r in runs_dict.values())
        tot_latency_sec = round(sum(r.get("total_latency_seconds", 0.0) for r in runs_dict.values()), 2)
        tot_tokens = sum(r.get("total_tokens", 0) for r in runs_dict.values())
        tot_in_tokens = sum(r.get("total_input_tokens", 0) for r in runs_dict.values())
        tot_out_tokens = sum(r.get("total_output_tokens", 0) for r in runs_dict.values())
        tot_cost = round(sum(r.get("total_cost_usd", 0.0) for r in runs_dict.values()), 6)
        avg_lat_ms = round((tot_latency_sec * 1000.0 / tot_urls), 2) if tot_urls > 0 else 0.0

        # Verification aggregation if present
        v_runs = [r["verification"] for r in runs_dict.values() if r.get("verification")]
        overall_verif = None
        if v_runs:
            tot_eval = sum(v.get("total_cases", 0) for v in v_runs)
            tot_approved = sum(v.get("approved_cases", 0) for v in v_runs)
            tot_review = sum(v.get("need_review_cases", 0) for v in v_runs)
            tot_rejected = sum(v.get("rejected_cases", 0) for v in v_runs)
            pass_rate = round((tot_approved / tot_eval * 100.0), 2) if tot_eval > 0 else 0.0
            sum_scores = sum(v.get("avg_score", 0.0) * v.get("total_cases", 0) for v in v_runs)
            avg_score = round((sum_scores / tot_eval), 2) if tot_eval > 0 else 0.0
            overall_verif = {
                "runs_with_verification": len(v_runs),
                "total_evaluated": tot_eval,
                "approved_cases": tot_approved,
                "need_review_cases": tot_review,
                "rejected_cases": tot_rejected,
                "approval_rate_percent": pass_rate,
                "overall_avg_score": avg_score,
            }

        # Judge aggregation if present
        j_runs = [r["judge"] for r in runs_dict.values() if r.get("judge")]
        overall_judge = None
        if j_runs:
            tot_sampled = sum(j.get("total_sampled", 0) for j in j_runs)
            tot_passed = sum(j.get("passed_count", 0) for j in j_runs)
            tot_failed = sum(j.get("failed_count", 0) for j in j_runs)
            avg_judge_score = round((tot_passed / tot_sampled), 4) if tot_sampled > 0 else 1.0
            overall_judge = {
                "runs_with_judge": len(j_runs),
                "total_sampled": tot_sampled,
                "passed_count": tot_passed,
                "failed_count": tot_failed,
                "avg_judge_score": avg_judge_score,
                "pass_rate_percent": round(avg_judge_score * 100, 2),
            }

        agg_data["overall_aggregated"] = {
            "total_runs": total_runs_count,
            "total_urls_processed": tot_urls,
            "total_successful_urls": tot_success,
            "total_failed_urls": tot_failed,
            "overall_latency_seconds": tot_latency_sec,
            "overall_avg_latency_per_url_ms": avg_lat_ms,
            "overall_total_tokens": tot_tokens,
            "overall_total_input_tokens": tot_in_tokens,
            "overall_total_output_tokens": tot_out_tokens,
            "overall_total_cost_usd": tot_cost,
            "overall_verification": overall_verif,
            "overall_judge": overall_judge,
        }

        with open(agg_path, "w", encoding="utf-8") as f:
            json.dump(agg_data, f, indent=2, ensure_ascii=False)

        return str(agg_path.resolve())
