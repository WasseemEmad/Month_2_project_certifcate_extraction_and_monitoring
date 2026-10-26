"""
database package
================
Provides SQLite persistence for certificate requests, verifications,
and post-batch LLM judge evaluations.
"""

from .db import (
    DEFAULT_DB_PATH,
    init_db,
    log_certificate_request,
    update_judge_result,
    get_all_records,
    has_reference_data,
    get_reference_records,
    get_recent_production_records,
    get_production_count,
)

__all__ = [
    "DEFAULT_DB_PATH",
    "init_db",
    "log_certificate_request",
    "update_judge_result",
    "get_all_records",
    "has_reference_data",
    "get_reference_records",
    "get_recent_production_records",
    "get_production_count",
]
