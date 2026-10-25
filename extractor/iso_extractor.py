"""
iso_extractor.py
================
Core extraction engine orchestrating certificate screening, field extraction,
date normalization, and scope classification using Gemini.
Tracks detailed metrics (latency, tokens, cost) matching saturday_email.py patterns.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Optional, Dict, Any, List, Tuple

from google import genai
from google.genai.types import Part

from .prompts import (
    CERT_SCREENING_PROMPT,
    CERT_EXTRACTION_PROMPT,
    CERT_SCOPE_CLASSIFY_PROMPT,
    GENERAL_TYPE_DEFS,
)
from .utils import (
    prepare_images_for_cert_url,
    normalize_date,
    clean_json_markdown,
    try_repair_json,
)
from .analytics import StepMetric, URLMetrics, estimate_cost

CORE_CERT_FIELDS = ["site_name", "address", "scope", "iso_type", "issued_date", "expiration_date"]


class ISOExtractor:
    """
    Modular ISO Certificate Extractor.
    Processes certificate URLs through:
      1. Download & image conversion (measures download latency)
      2. Dual-flag screening (is_cert, is_iso_iatf + metrics)
      3. Page-level field extraction (+ metrics)
      4. Scope manufacturing classification (+ metrics)
    """

    def __init__(self, api_key: str, model_name: str = "gemini-2.5-flash-lite"):
        if not api_key or not api_key.strip():
            raise ValueError("A valid Gemini API key must be provided.")
        self.api_key = api_key.strip()
        self.model_name = model_name
        self.client = genai.Client(api_key=self.api_key)

    def _extract_usage(self, response) -> Dict[str, int]:
        """Safely extracts token usage counts from Gemini response."""
        usage_meta = getattr(response, "usage_metadata", None)
        if usage_meta is not None:
            in_tokens = getattr(usage_meta, "prompt_token_count", 0) or 0
            out_tokens = getattr(usage_meta, "candidates_token_count", 0) or 0
            tot_tokens = getattr(usage_meta, "total_token_count", 0) or (in_tokens + out_tokens)
            return {"input": in_tokens, "output": out_tokens, "total": tot_tokens}
        return {"input": 0, "output": 0, "total": 0}

    def screen_document(self, image_path: str, mime_type: str) -> Tuple[Dict[str, Any], StepMetric]:
        """
        Screens the document image to determine:
          - is_cert: bool (Is it a certificate of any kind?)
          - is_iso_iatf: bool (Is it specifically an ISO or IATF certificate?)
          - cert_type: str | None
          - reason: str
          - error: str | None
        Returns (result_dict, StepMetric).
        """
        start = time.perf_counter()
        with open(image_path, "rb") as f:
            image_bytes = f.read()

        last_error = None
        for attempt in range(3):
            try:
                response = self.client.models.generate_content(
                    model=self.model_name,
                    contents=[
                        CERT_SCREENING_PROMPT,
                        Part.from_bytes(data=image_bytes, mime_type=mime_type),
                    ],
                    config={"temperature": 0.0},
                )
                latency_ms = (time.perf_counter() - start) * 1000
                usage = self._extract_usage(response)
                cost_usd = estimate_cost(usage["input"], usage["output"], self.model_name)

                raw = (response.text or "").strip()
                cleaned = clean_json_markdown(raw)
                try:
                    res = json.loads(cleaned)
                except json.JSONDecodeError:
                    res = try_repair_json(raw)
                    if res is None:
                        raise ValueError(f"Failed to parse screening JSON: {raw[:150]}")

                metric = StepMetric(
                    step_name="screening",
                    latency_ms=latency_ms,
                    input_tokens=usage["input"],
                    output_tokens=usage["output"],
                    cost_usd=cost_usd,
                    retry_attempts=attempt + 1,
                    status="success",
                )

                return {
                    "is_cert": bool(res.get("is_cert", False)),
                    "is_iso_iatf": bool(res.get("is_iso_iatf", False)),
                    "cert_type": res.get("cert_type"),
                    "reason": res.get("reason", ""),
                    "error": None,
                }, metric

            except Exception as e:
                err_str = str(e)
                last_error = err_str
                if "503" in err_str or "UNAVAILABLE" in err_str or "overload" in err_str.lower():
                    wait = 8 * (attempt + 1)
                    time.sleep(wait)
                else:
                    time.sleep(2)

        latency_ms = (time.perf_counter() - start) * 1000
        metric = StepMetric(
            step_name="screening",
            latency_ms=latency_ms,
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            retry_attempts=3,
            status="error",
            error=last_error,
        )

        return {
            "is_cert": False,
            "is_iso_iatf": False,
            "cert_type": None,
            "reason": "Screening failed after retries",
            "error": last_error,
        }, metric

    def extract_page_fields(self, image_path: str, mime_type: str) -> Tuple[Dict[str, Any], StepMetric]:
        """
        Extracts core fields from a single certificate image page.
        Returns (fields_dict, StepMetric).
        """
        start = time.perf_counter()
        with open(image_path, "rb") as f:
            image_bytes = f.read()

        last_error = None
        for attempt in range(3):
            try:
                response = self.client.models.generate_content(
                    model=self.model_name,
                    contents=[
                        CERT_EXTRACTION_PROMPT,
                        Part.from_bytes(data=image_bytes, mime_type=mime_type),
                    ],
                    config={"temperature": 0.0},
                )
                latency_ms = (time.perf_counter() - start) * 1000
                usage = self._extract_usage(response)
                cost_usd = estimate_cost(usage["input"], usage["output"], self.model_name)

                metric = StepMetric(
                    step_name="extraction",
                    latency_ms=latency_ms,
                    input_tokens=usage["input"],
                    output_tokens=usage["output"],
                    cost_usd=cost_usd,
                    retry_attempts=attempt + 1,
                    status="success",
                )

                if response.candidates and response.candidates[0].finish_reason == "MAX_TOKENS":
                    return {}, metric

                raw = (response.text or "").strip()
                cleaned = clean_json_markdown(raw)
                try:
                    return json.loads(cleaned), metric
                except json.JSONDecodeError:
                    repaired = try_repair_json(raw)
                    if repaired is not None:
                        return repaired, metric
                    time.sleep(1)
            except Exception as e:
                err_str = str(e)
                last_error = err_str
                if "503" in err_str or "UNAVAILABLE" in err_str or "overload" in err_str.lower():
                    time.sleep(8 * (attempt + 1))
                else:
                    time.sleep(2)

        latency_ms = (time.perf_counter() - start) * 1000
        metric = StepMetric(
            step_name="extraction",
            latency_ms=latency_ms,
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            retry_attempts=3,
            status="error",
            error=last_error,
        )
        return {}, metric

    def classify_scope(self, scope_text: Optional[str]) -> Tuple[Dict[str, Any], StepMetric]:
        """
        Classifies the certificate scope text into manufacturing site_iso_type
        and extracts iso_product.
        Returns (result_dict, StepMetric).
        """
        if not scope_text or not str(scope_text).strip():
            metric = StepMetric(
                step_name="scope_classification",
                latency_ms=0.0,
                input_tokens=0,
                output_tokens=0,
                cost_usd=0.0,
                retry_attempts=0,
                status="skipped",
            )
            return {
                "site_iso_type": None,
                "iso_product": None,
                "status": "Skipped: no scope text available",
            }, metric

        start = time.perf_counter()
        prompt = CERT_SCOPE_CLASSIFY_PROMPT.format(
            scope=scope_text,
            general_type_defs=GENERAL_TYPE_DEFS,
        )

        last_error = None
        for attempt in range(3):
            try:
                response = self.client.models.generate_content(
                    model=self.model_name,
                    contents=prompt,
                    config={"response_mime_type": "application/json", "temperature": 0.0},
                )
                latency_ms = (time.perf_counter() - start) * 1000
                usage = self._extract_usage(response)
                cost_usd = estimate_cost(usage["input"], usage["output"], self.model_name)

                metric = StepMetric(
                    step_name="scope_classification",
                    latency_ms=latency_ms,
                    input_tokens=usage["input"],
                    output_tokens=usage["output"],
                    cost_usd=cost_usd,
                    retry_attempts=attempt + 1,
                    status="success",
                )

                raw = (response.text or "").strip()
                cleaned = clean_json_markdown(raw)
                try:
                    res = json.loads(cleaned)
                except json.JSONDecodeError:
                    res = try_repair_json(raw)

                if isinstance(res, dict):
                    return {
                        "site_iso_type": res.get("site_iso_type"),
                        "iso_product": res.get("iso_product"),
                        "status": "OK",
                    }, metric
            except Exception as e:
                err_str = str(e)
                last_error = err_str
                if "503" in err_str or "UNAVAILABLE" in err_str or "overload" in err_str.lower():
                    time.sleep(8 * (attempt + 1))
                else:
                    time.sleep(2)

        latency_ms = (time.perf_counter() - start) * 1000
        metric = StepMetric(
            step_name="scope_classification",
            latency_ms=latency_ms,
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            retry_attempts=3,
            status="error",
            error=last_error,
        )
        return {
            "site_iso_type": None,
            "iso_product": None,
            "status": "Failed after 3 attempts",
        }, metric

    def _merge_page_results(self, page_results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Merges extracted fields across pages taking the first non-null value."""
        merged = {
            "site_name": None,
            "address": None,
            "scope": None,
            "iso_type": None,
            "issued_date": None,
            "issued_date_format": None,
            "expiration_date": None,
            "expiration_date_format": None,
        }
        for res in page_results:
            if not res or not isinstance(res, dict):
                continue
            for k in merged:
                if merged[k] is None and res.get(k):
                    merged[k] = res[k]
        return merged

    def process_cert_url(self, cert_url: str, temp_base_dir: str) -> Tuple[Dict[str, Any], URLMetrics]:
        """
        Executes the full pipeline for a given certificate URL:
          1. Download & convert to image(s).
          2. Screen first page:
             - Check is_cert (Flag 1)
             - Check is_iso_iatf (Flag 2)
          3. If ISO/IATF certificate -> Extract core fields from pages.
          4. Normalize issued and expiration dates.
          5. Classify scope into site_iso_type and iso_product.
        Returns (result_record, URLMetrics).
        """
        overall_start = time.perf_counter()
        url_metrics = URLMetrics(cert_url)

        result_record: Dict[str, Any] = {
            "url": cert_url,
            "status": "OK",
            "is_certificate": None,
            "is_iso_iatf": None,
            "cert_type": None,
            "screening_reason": None,
            "site_name": None,
            "address": None,
            "scope": None,
            "iso_type": None,
            "issued_date": None,
            "expiration_date": None,
            "site_iso_type": None,
            "iso_product": None,
            "site_iso_type_status": None,
            "doc_type": "unknown",
            "page_count": 0,
            "latency_ms": 0.0,
            "cost_usd": 0.0,
            "total_tokens": 0,
        }

        # ── Step 1: Download & convert ──────────────────────────────────────
        dl_start = time.perf_counter()
        images, dl_err, meta = prepare_images_for_cert_url(cert_url, temp_base_dir, return_meta=True)
        url_metrics.download_latency_ms = (time.perf_counter() - dl_start) * 1000
        result_record["doc_type"] = meta.get("doc_type", "unknown")
        result_record["page_count"] = meta.get("page_count", 0)

        if dl_err or not images:
            result_record["status"] = "did not open need review"
            tot_lat = (time.perf_counter() - overall_start) * 1000
            url_metrics.finalize(tot_lat, status="did not open need review", error=dl_err)
            result_record["latency_ms"] = url_metrics.total_latency_ms
            return result_record, url_metrics

        # ── Step 2: Screen first page ───────────────────────────────────────
        first_img_path, first_mime = images[0]
        screen_res, screen_metric = self.screen_document(first_img_path, first_mime)
        url_metrics.screening = screen_metric

        if screen_res.get("error"):
            status_err = f"Screening error ({screen_res['error'][:80]})"
            result_record["status"] = status_err
            result_record["is_certificate"] = False
            result_record["is_iso_iatf"] = False

            tot_lat = (time.perf_counter() - overall_start) * 1000
            url_metrics.finalize(tot_lat, status=status_err, error=screen_res["error"])
            result_record["latency_ms"] = url_metrics.total_latency_ms
            result_record["cost_usd"] = url_metrics.total_cost_usd
            result_record["total_tokens"] = url_metrics.total_tokens
            return result_record, url_metrics

        is_cert = screen_res.get("is_cert", False)
        is_iso_iatf = screen_res.get("is_iso_iatf", False)
        cert_type = screen_res.get("cert_type")
        reason = screen_res.get("reason", "")

        result_record["is_certificate"] = is_cert
        result_record["is_iso_iatf"] = is_iso_iatf
        result_record["cert_type"] = cert_type
        result_record["screening_reason"] = reason

        # Check if NOT a certificate
        if not is_cert:
            result_record["status"] = "Not a certificate"
            tot_lat = (time.perf_counter() - overall_start) * 1000
            url_metrics.finalize(tot_lat, status="Not a certificate")
            result_record["latency_ms"] = url_metrics.total_latency_ms
            result_record["cost_usd"] = url_metrics.total_cost_usd
            result_record["total_tokens"] = url_metrics.total_tokens
            return result_record, url_metrics

        # Check if a certificate but NOT ISO/IATF
        if not is_iso_iatf:
            result_record["status"] = "Not an ISO/IATF certificate"
            tot_lat = (time.perf_counter() - overall_start) * 1000
            url_metrics.finalize(tot_lat, status="Not an ISO/IATF certificate")
            result_record["latency_ms"] = url_metrics.total_latency_ms
            result_record["cost_usd"] = url_metrics.total_cost_usd
            result_record["total_tokens"] = url_metrics.total_tokens
            return result_record, url_metrics

        # ── Step 3: Page-by-page field extraction ────────────────────────────
        page_results = []
        extraction_metrics_list = []
        for page_num, (img_path, mime_type) in enumerate(images[:4], start=1):
            if page_results:
                running = self._merge_page_results(page_results)
                if all(running.get(f) for f in CORE_CERT_FIELDS):
                    break
            res, ext_metric = self.extract_page_fields(img_path, mime_type)
            page_results.append(res)
            extraction_metrics_list.append(ext_metric)

        merged = self._merge_page_results(page_results)

        # Retry page 1 if all core fields remained empty
        if all(not merged.get(f) for f in CORE_CERT_FIELDS) and images:
            retry_res, retry_metric = self.extract_page_fields(images[0][0], images[0][1])
            merged = self._merge_page_results([retry_res])
            extraction_metrics_list.append(retry_metric)

        # Aggregate extraction metrics
        tot_ext_lat = sum(m.latency_ms for m in extraction_metrics_list)
        tot_ext_in = sum(m.input_tokens for m in extraction_metrics_list)
        tot_ext_out = sum(m.output_tokens for m in extraction_metrics_list)
        tot_ext_cost = sum(m.cost_usd for m in extraction_metrics_list)
        url_metrics.extraction = StepMetric(
            step_name="extraction",
            latency_ms=tot_ext_lat,
            input_tokens=tot_ext_in,
            output_tokens=tot_ext_out,
            cost_usd=tot_ext_cost,
            retry_attempts=len(extraction_metrics_list),
            status="success" if any(m.status == "success" for m in extraction_metrics_list) else "error",
        )

        # ── Step 4: Normalize dates ──────────────────────────────────────────
        raw_issued = merged.get("issued_date")
        if raw_issued:
            norm, _ = normalize_date(raw_issued, merged.get("issued_date_format"))
            merged["issued_date"] = norm

        raw_expiry = merged.get("expiration_date")
        if raw_expiry:
            norm, _ = normalize_date(raw_expiry, merged.get("expiration_date_format"))
            merged["expiration_date"] = norm

        for f in ["site_name", "address", "scope", "iso_type", "issued_date", "expiration_date"]:
            result_record[f] = merged.get(f)

        # ── Step 5: Scope classification ─────────────────────────────────────
        scope_res, scope_metric = self.classify_scope(merged.get("scope"))
        url_metrics.scope_classification = scope_metric
        result_record["site_iso_type"] = scope_res.get("site_iso_type")
        result_record["iso_product"] = scope_res.get("iso_product")
        result_record["site_iso_type_status"] = scope_res.get("status")

        # ── Step 6: Determine final status ───────────────────────────────────
        missing = [f for f in CORE_CERT_FIELDS if not merged.get(f)]
        if missing:
            labels = {
                "site_name": "Site Name",
                "address": "Address",
                "scope": "Scope",
                "iso_type": "ISO Type",
                "issued_date": "Issued Date",
                "expiration_date": "Expiration Date",
            }
            result_record["status"] = "Partial: missing " + ", ".join(labels[m] for m in missing)
        else:
            result_record["status"] = "OK"

        tot_lat = (time.perf_counter() - overall_start) * 1000
        url_metrics.finalize(tot_lat, status=result_record["status"])

        result_record["latency_ms"] = url_metrics.total_latency_ms
        result_record["cost_usd"] = url_metrics.total_cost_usd
        result_record["total_tokens"] = url_metrics.total_tokens

        return result_record, url_metrics
