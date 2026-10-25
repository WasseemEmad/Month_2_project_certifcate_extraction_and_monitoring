"""
verifier package
================
Provides rule-based certificate verification, confidence scoring (0-100),
and status classification (Approved, Need Review, Reject).
"""

from .rules import (
    KNOWN_SITE_ISO_TYPES,
    check_rule_1_expiration_after_issued,
    check_rule_2_validity_duration,
    check_rule_3_known_site_iso_type,
    check_rule_4_pdf_pages,
    check_rule_5_fuzzy_cert_vs_iso_type,
    check_rule_6_multisite_single_page,
    check_rule_7_missing_fields,
    normalize_bool,
)
from .verifier import CertificateVerifier, VerificationRowResult

__all__ = [
    "CertificateVerifier",
    "VerificationRowResult",
    "KNOWN_SITE_ISO_TYPES",
    "normalize_bool",
    "check_rule_1_expiration_after_issued",
    "check_rule_2_validity_duration",
    "check_rule_3_known_site_iso_type",
    "check_rule_4_pdf_pages",
    "check_rule_5_fuzzy_cert_vs_iso_type",
    "check_rule_6_multisite_single_page",
    "check_rule_7_missing_fields",
]
