"""
database/db.py
==============
SQLite storage layer for certificate extraction requests, verifications,
performance metrics, and post-batch LLM judge scores.
"""

from __future__ import annotations

import datetime
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any, Dict, List, Optional
import uuid

_DB_LOCK = threading.Lock()
DEFAULT_DB_PATH = "analytics/certificate_logs.db"


def init_db(db_path: str = DEFAULT_DB_PATH) -> str:
    """
    Initializes the SQLite database schema if not already present.
    Creates analytics directory automatically.
    """
    db_file = Path(db_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)

    with _DB_LOCK:
        conn = sqlite3.connect(str(db_file))
        try:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS certificate_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT UNIQUE NOT NULL,
                    timestamp TEXT NOT NULL,
                    traffic_type TEXT DEFAULT 'production',
                    url TEXT NOT NULL,
                    doc_type TEXT,
                    page_count INTEGER,
                    image_pixel_size INTEGER,
                    status TEXT,
                    
                    -- Screening & Core Features
                    is_certificate BOOLEAN,
                    is_iso_iatf BOOLEAN,
                    cert_type TEXT,
                    screening_reason TEXT,
                    
                    -- Extracted Metadata Fields
                    site_name TEXT,
                    address TEXT,
                    scope TEXT,
                    iso_type TEXT,
                    issued_date TEXT,
                    expiration_date TEXT,
                    site_iso_type TEXT,
                    iso_product TEXT,
                    site_iso_type_status TEXT,
                    certificate_language TEXT,
                    
                    -- Verification & Verdict
                    confidence_score INTEGER,
                    verdict TEXT,
                    checks_passed_or_failed TEXT,
                    
                    -- Latencies (ms) & Cost
                    total_latency_ms REAL,
                    download_latency_ms REAL,
                    screening_latency_ms REAL,
                    extraction_latency_ms REAL,
                    scope_latency_ms REAL,
                    language_latency_ms REAL,
                    verification_latency_ms REAL,
                    
                    -- Token Usage & Cost
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    total_tokens INTEGER,
                    cost_usd REAL,
                    error TEXT,
                    
                    -- LLM Judge Fields (initially NULL / empty)
                    judge_score INTEGER DEFAULT NULL,
                    judge_reasoning TEXT DEFAULT NULL,
                    judge_timestamp TEXT DEFAULT NULL
                );
            """)

            cursor.execute("CREATE INDEX IF NOT EXISTS idx_requests_url ON certificate_requests(url);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_requests_verdict ON certificate_requests(verdict);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_requests_timestamp ON certificate_requests(timestamp);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_requests_traffic_type ON certificate_requests(traffic_type);")

            # Check and perform automated migrations for existing tables
            cursor.execute("PRAGMA table_info(certificate_requests);")
            existing_columns = {col[1] for col in cursor.fetchall()}

            migrations = [
                ("traffic_type", "TEXT DEFAULT 'production'"),
                ("image_pixel_size", "INTEGER"),
                ("certificate_language", "TEXT"),
                ("language_latency_ms", "REAL"),
            ]
            for col_name, col_def in migrations:
                if col_name not in existing_columns:
                    cursor.execute(f"ALTER TABLE certificate_requests ADD COLUMN {col_name} {col_def};")

            conn.commit()
        finally:
            conn.close()

    return str(db_file.resolve())


def log_certificate_request(
    record: Dict[str, Any],
    metrics: Optional[Any] = None,
    db_path: str = DEFAULT_DB_PATH,
) -> str:
    """
    Logs an extracted certificate request to SQLite.
    Leaves judge_score and judge_reasoning NULL for later sampling.
    Returns generated request_id.
    """
    init_db(db_path)
    request_id = record.get("request_id") or str(uuid.uuid4())
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Extract latency & token breakdowns from metrics if available
    dl_lat = getattr(metrics, "download_latency_ms", 0.0) if metrics else 0.0
    scr_metric = getattr(metrics, "screening", None) if metrics else None
    ext_metric = getattr(metrics, "extraction", None) if metrics else None
    scp_metric = getattr(metrics, "scope_classification", None) if metrics else None
    lang_metric = getattr(metrics, "language_detection", None) if metrics else None

    scr_lat = scr_metric.latency_ms if scr_metric else 0.0
    ext_lat = ext_metric.latency_ms if ext_metric else 0.0
    scp_lat = scp_metric.latency_ms if scp_metric else 0.0
    lang_lat = lang_metric.latency_ms if lang_metric else 0.0
    verif_lat = record.get("verification_latency_ms", 0.0) or 0.0

    in_tokens = getattr(metrics, "total_input_tokens", 0) if metrics else record.get("input_tokens", 0)
    out_tokens = getattr(metrics, "total_output_tokens", 0) if metrics else record.get("output_tokens", 0)
    tot_tokens = in_tokens + out_tokens if (in_tokens or out_tokens) else record.get("total_tokens", 0)

    tot_lat = record.get("latency_ms") or (getattr(metrics, "total_latency_ms", 0.0) if metrics else 0.0)
    cost = record.get("cost_usd") or (getattr(metrics, "total_cost_usd", 0.0) if metrics else 0.0)

    verdict = record.get("verification_status") or record.get("verdict")
    confidence = record.get("confidence_score")
    checks = record.get("verification_reviews") or record.get("checks_passed_or_failed")

    traffic_type = record.get("traffic_type", "production")
    cert_lang = record.get("certificate_language")
    img_pixel_size = record.get("image_pixel_size")

    with _DB_LOCK:
        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO certificate_requests (
                    request_id, timestamp, traffic_type, url, doc_type, page_count, image_pixel_size, status,
                    is_certificate, is_iso_iatf, cert_type, screening_reason,
                    site_name, address, scope, iso_type, issued_date, expiration_date,
                    site_iso_type, iso_product, site_iso_type_status, certificate_language,
                    confidence_score, verdict, checks_passed_or_failed,
                    total_latency_ms, download_latency_ms, screening_latency_ms,
                    extraction_latency_ms, scope_latency_ms, language_latency_ms, verification_latency_ms,
                    input_tokens, output_tokens, total_tokens, cost_usd, error,
                    judge_score, judge_reasoning, judge_timestamp
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    NULL, NULL, NULL
                )
            """, (
                request_id,
                ts,
                traffic_type,
                record.get("url", ""),
                record.get("doc_type"),
                record.get("page_count"),
                img_pixel_size,
                record.get("status"),
                1 if record.get("is_certificate") is True else (0 if record.get("is_certificate") is False else None),
                1 if record.get("is_iso_iatf") is True else (0 if record.get("is_iso_iatf") is False else None),
                record.get("cert_type"),
                record.get("screening_reason"),
                record.get("site_name"),
                record.get("address"),
                record.get("scope"),
                record.get("iso_type"),
                record.get("issued_date"),
                record.get("expiration_date"),
                record.get("site_iso_type"),
                record.get("iso_product"),
                record.get("site_iso_type_status"),
                cert_lang,
                confidence,
                verdict,
                checks,
                tot_lat,
                dl_lat,
                scr_lat,
                ext_lat,
                scp_lat,
                lang_lat,
                verif_lat,
                in_tokens,
                out_tokens,
                tot_tokens,
                cost,
                record.get("error"),
            ))
            conn.commit()
        finally:
            conn.close()

    return request_id


def update_judge_result(
    request_id: str,
    judge_score: int,
    judge_reasoning: str,
    db_path: str = DEFAULT_DB_PATH,
) -> bool:
    """
    Updates the judge_score (0 or 1), reasoning, and timestamp for a request_id.
    """
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _DB_LOCK:
        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE certificate_requests
                SET judge_score = ?,
                    judge_reasoning = ?,
                    judge_timestamp = ?
                WHERE request_id = ?
            """, (judge_score, judge_reasoning, now, request_id))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


def get_all_records(db_path: str = DEFAULT_DB_PATH) -> List[Dict[str, Any]]:
    """Retrieves all certificate request records as a list of dictionaries."""
    init_db(db_path)
    with _DB_LOCK:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM certificate_requests ORDER BY id ASC")
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def has_reference_data(db_path: str = DEFAULT_DB_PATH) -> bool:
    """Checks whether the database contains any baseline reference set records."""
    init_db(db_path)
    with _DB_LOCK:
        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM certificate_requests WHERE traffic_type = 'reference'")
            count = cursor.fetchone()[0]
            return count > 0
        finally:
            conn.close()


def get_reference_records(db_path: str = DEFAULT_DB_PATH) -> List[Dict[str, Any]]:
    """Retrieves all reference set records."""
    init_db(db_path)
    with _DB_LOCK:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM certificate_requests WHERE traffic_type = 'reference' ORDER BY id ASC")
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def get_recent_production_records(limit: int = 50, db_path: str = DEFAULT_DB_PATH) -> List[Dict[str, Any]]:
    """
    Retrieves the most recent `limit` production records in chronological order.
    """
    init_db(db_path)
    with _DB_LOCK:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM (
                    SELECT * FROM certificate_requests 
                    WHERE traffic_type = 'production' 
                    ORDER BY id DESC 
                    LIMIT ?
                ) ORDER BY id ASC
            """, (limit,))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def get_production_count(db_path: str = DEFAULT_DB_PATH) -> int:
    """Returns the total number of production records logged."""
    init_db(db_path)
    with _DB_LOCK:
        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM certificate_requests WHERE traffic_type = 'production'")
            count = cursor.fetchone()[0]
            return count
        finally:
            conn.close()
