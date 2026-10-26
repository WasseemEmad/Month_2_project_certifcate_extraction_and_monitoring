"""
dashboard/review_queue.py
=========================
Human Review Routing & Dynamic 30% Queue Management for ISO Certificate Extraction.

Rules:
1. Dynamic 30% Queue:
   - Queue size is strictly 30% of total production records in certificate_logs.db
     (excluding traffic_type == 'reference').
   - Prioritized by operational risk:
     * Priority 1: Verdict == 'Reject'
     * Priority 2: Verdict == 'Need Review'
     * Priority 3: Lowest confidence_score
     * Priority 4: Low-sample 'Approved' cases
2. Persistence:
   - When a human decision is saved, it is removed from the active queue.
   - Appended to 'reviewed/human_review.json' (always appends, never creates fresh/overwrites).
   - The queue automatically promotes the next eligible unreviewed production record
     to maintain the 30% queue target.
3. Certificate inspection:
   - Provides certificate URL, document type, extracted fields, rule-based verification reviews,
     confidence score, and LLM judge score & reasoning.
"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any, Dict, List, Optional, Tuple
import uuid

_QUEUE_LOCK = threading.RLock()

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "analytics" / "certificate_logs.db"
DEFAULT_REVIEWED_JSON = Path(__file__).resolve().parent.parent / "reviewed" / "human_review.json"


# ==============================================================================
# Helper Functions & Reviewed JSON Persistence
# ==============================================================================

def load_reviewed_decisions(reviewed_path: Path = DEFAULT_REVIEWED_JSON) -> List[Dict[str, Any]]:
    """Loads all human review decisions saved in reviewed/human_review.json."""
    if not reviewed_path.exists():
        return []
    with _QUEUE_LOCK:
        try:
            with open(reviewed_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            return []


def append_reviewed_decision(
    decision_record: Dict[str, Any],
    reviewed_path: Path = DEFAULT_REVIEWED_JSON,
) -> None:
    """Appends a new human review decision to reviewed/human_review.json."""
    reviewed_path.parent.mkdir(parents=True, exist_ok=True)
    with _QUEUE_LOCK:
        existing = load_reviewed_decisions(reviewed_path)
        existing.append(decision_record)
        with open(reviewed_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)


# ==============================================================================
# Queue Population & Ranking Logic (Dynamic 30%)
# ==============================================================================

def assign_priority_tier(record: Dict[str, Any]) -> Tuple[int, str, List[str]]:
    """
    Ranks production cases for human review prioritization:
      Tier 1: Verdict == 'Reject'
      Tier 2: Verdict == 'Need Review'
      Tier 3: Low confidence score (< 80)
      Tier 4: Status / Document opening warnings or Judge Failures
      Tier 5: General sample (Approved cases)
    """
    verdict = str(record.get("verdict") or "").strip()
    score = record.get("confidence_score")
    judge_score = record.get("judge_score")
    status = str(record.get("status") or "")
    checks = str(record.get("checks_passed_or_failed") or "")

    reasons = []

    if verdict.lower() == "reject":
        reasons.append("Verdict: Rejected by rule-based verification")
        return 1, "P1 - Rejected", reasons

    if verdict.lower() == "need review":
        reasons.append(f"Verdict: Flagged for manual review ({checks[:60]}...)")
        return 2, "P2 - Need Review", reasons

    if score is not None and score < 80:
        reasons.append(f"Confidence score degraded to {score}/100")
        return 3, "P3 - Low Confidence", reasons

    if judge_score is not None and judge_score == 0:
        reasons.append("LLM Judge evaluated extraction as failed (Score: 0)")
        return 4, "P4 - Judge Failure", reasons

    if "did not open" in status.lower() or "error" in status.lower():
        reasons.append(f"Operational warning: {status}")
        return 4, "P4 - Pipeline Warning", reasons

    reasons.append("Random audit calibration sample")
    return 5, "P5 - Quality Audit", reasons


def build_active_review_queue(
    db_path: Path = DEFAULT_DB_PATH,
    reviewed_path: Path = DEFAULT_REVIEWED_JSON,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Constructs the active review queue from SQLite:
    - Excludes reference set records (traffic_type == 'reference').
    - Excludes all requests already present in reviewed/human_review.json.
    - Capped at exactly 30% of total production records.
    - Prioritized by Tier 1-5.
    """
    if not db_path.exists():
        return [], {
            "total_production": 0,
            "target_queue_size": 0,
            "pending_count": 0,
            "reviewed_count": 0,
            "queue_percent": 30.0,
        }

    # 1. Fetch already reviewed IDs
    reviewed_list = load_reviewed_decisions(reviewed_path)
    reviewed_ids = {str(item.get("request_id")) for item in reviewed_list if item.get("request_id")}

    # 2. Fetch all production records from SQLite
    with _QUEUE_LOCK:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM certificate_requests 
                WHERE traffic_type = 'production' 
                ORDER BY id ASC
            """)
            raw_rows = cursor.fetchall()
            production_records = [dict(r) for r in raw_rows]
        finally:
            conn.close()

    total_prod = len(production_records)
    if total_prod == 0:
        return [], {
            "total_production": 0,
            "target_queue_size": 0,
            "pending_count": 0,
            "reviewed_count": len(reviewed_list),
            "queue_percent": 30.0,
        }

    # 30% target queue size (at least 1 if production records exist)
    target_queue_size = max(1, int(round(total_prod * 0.30)))

    # Filter out already reviewed cases
    unreviewed = [r for r in production_records if str(r.get("request_id")) not in reviewed_ids]

    # Rank unreviewed candidates by priority
    def sort_key(r: Dict[str, Any]):
        tier, _, _ = assign_priority_tier(r)
        conf = r.get("confidence_score")
        conf_val = float(conf) if conf is not None else 100.0
        return (tier, conf_val, r.get("id", 0))

    ranked_unreviewed = sorted(unreviewed, key=sort_key)

    # Take top N to maintain exactly 30%
    active_queue = ranked_unreviewed[:target_queue_size]

    # Attach queue metadata to each active item
    queue_items = []
    for pos, item in enumerate(active_queue, start=1):
        tier, label, reasons = assign_priority_tier(item)
        item_copy = dict(item)
        item_copy["queue_position"] = pos
        item_copy["priority_tier"] = tier
        item_copy["priority_label"] = label
        item_copy["reasons"] = reasons
        item_copy["status"] = "pending"
        queue_items.append(item_copy)

    stats = {
        "total_production": total_prod,
        "target_queue_size": target_queue_size,
        "pending_count": len(queue_items),
        "reviewed_count": len(reviewed_list),
        "unreviewed_pool_remaining": max(0, len(unreviewed) - len(queue_items)),
        "queue_percent": 30.0,
    }

    return queue_items, stats


# ==============================================================================
# Public API Endpoints Handlers
# ==============================================================================

def get_review_queue(
    db_path: Path = DEFAULT_DB_PATH,
    reviewed_path: Path = DEFAULT_REVIEWED_JSON,
) -> Dict[str, Any]:
    """Returns the current prioritized 30% queue and queue statistics."""
    items, stats = build_active_review_queue(db_path=db_path, reviewed_path=reviewed_path)
    return {
        "stats": stats,
        "items": items,
    }


def get_next_pending_case(
    request_id: Optional[str] = None,
    db_path: Path = DEFAULT_DB_PATH,
    reviewed_path: Path = DEFAULT_REVIEWED_JSON,
) -> Dict[str, Any]:
    """
    Returns full details for a single case review.
    If request_id is provided, fetches that specific item from the active queue.
    Otherwise, returns the highest-priority pending case.
    """
    items, stats = build_active_review_queue(db_path=db_path, reviewed_path=reviewed_path)
    if not items:
        return {
            "has_case": False,
            "message": "No pending cases in the review queue. All 30% target cases have been reviewed!",
            "stats": stats,
            "case": None,
        }

    selected_case = None
    if request_id:
        for it in items:
            if str(it.get("request_id")) == str(request_id):
                selected_case = it
                break

    if not selected_case:
        selected_case = items[0]

    return {
        "has_case": True,
        "stats": stats,
        "case": selected_case,
    }


def save_human_decision(
    request_id: str,
    decision: str,
    reviewer_notes: str = "",
    reviewer_id: str = "REV-01",
    corrected_fields: Optional[Dict[str, Any]] = None,
    db_path: Path = DEFAULT_DB_PATH,
    reviewed_path: Path = DEFAULT_REVIEWED_JSON,
) -> Dict[str, Any]:
    """
    Saves human review verdict & field corrections:
    1. Looks up original record in certificate_requests.
    2. Builds structured decision object and appends to reviewed/human_review.json.
    3. Automatically replenishes the queue with the next unreviewed production case.
    """
    if not request_id:
        raise ValueError("request_id is required to save human review decision.")

    # 1. Fetch original record from SQLite
    original_record = None
    with _QUEUE_LOCK:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM certificate_requests WHERE request_id = ?", (str(request_id),))
            row = cursor.fetchone()
            if row:
                original_record = dict(row)
        finally:
            conn.close()

    if not original_record:
        # Fallback to empty if not found
        original_record = {"request_id": str(request_id)}

    now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Clean corrected fields (only retain non-empty corrections)
    cleaned_corrections: Dict[str, Any] = {}
    if isinstance(corrected_fields, dict):
        for k, v in corrected_fields.items():
            if v is not None and str(v).strip() != "":
                cleaned_corrections[k] = str(v).strip()

    decision_entry = {
        "review_id": str(uuid.uuid4()),
        "request_id": str(request_id),
        "timestamp": now_ts,
        "url": original_record.get("url", ""),
        "doc_type": original_record.get("doc_type"),
        "page_count": original_record.get("page_count"),
        "image_pixel_size": original_record.get("image_pixel_size"),
        "original_data": {
            "verdict": original_record.get("verdict"),
            "confidence_score": original_record.get("confidence_score"),
            "checks_passed_or_failed": original_record.get("checks_passed_or_failed"),
            "site_name": original_record.get("site_name"),
            "address": original_record.get("address"),
            "scope": original_record.get("scope"),
            "iso_type": original_record.get("iso_type"),
            "issued_date": original_record.get("issued_date"),
            "expiration_date": original_record.get("expiration_date"),
            "site_iso_type": original_record.get("site_iso_type"),
            "certificate_language": original_record.get("certificate_language"),
        },
        "decision": {
            "reviewer_verdict": decision,
            "reviewer_notes": reviewer_notes,
            "reviewer_id": reviewer_id,
            "corrected_fields": cleaned_corrections,
        },
    }

    # 2. Append to reviewed/human_review.json
    append_reviewed_decision(decision_entry, reviewed_path=reviewed_path)

    # 3. Automatically promote next case and return updated queue
    next_info = get_next_pending_case(db_path=db_path, reviewed_path=reviewed_path)

    return {
        "success": True,
        "message": f"Decision for Request #{request_id} successfully saved to {reviewed_path}.",
        "saved_decision": decision_entry,
        "stats": next_info.get("stats"),
        "next_case": next_info.get("case"),
    }


def get_reviewed_cases(reviewed_path: Path = DEFAULT_REVIEWED_JSON) -> List[Dict[str, Any]]:
    """Returns all human review decisions saved in reviewed/human_review.json, newest first."""
    decisions = load_reviewed_decisions(reviewed_path=reviewed_path)
    return list(reversed(decisions))
