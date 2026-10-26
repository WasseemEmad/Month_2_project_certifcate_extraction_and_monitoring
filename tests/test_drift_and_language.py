"""
tests/test_drift_and_language.py
================================
Unit tests verifying:
1. SQLite schema migration and persistence of traffic_type, certificate_language, image_pixel_size.
2. Drift detection: Input drift (PDF pages, image pixels), Output drift (languages, missing fields, verdicts, false flags), Quality drift (errors, judge score).
3. Alarm generation and persistence into analytics/drift_test.json and analytics/alarms.json.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.db import (
    init_db,
    log_certificate_request,
    has_reference_data,
    get_reference_records,
    get_recent_production_records,
    get_production_count,
)
from drift.detector import (
    compute_numeric_stats,
    compute_cohens_d,
    check_input_drift,
    check_output_drift,
    check_quality_drift,
    generate_alarms,
    execute_drift_test,
)


class TestDatabaseExtensions(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_logs.db")

    def tearDown(self):
        try:
            import shutil
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        except Exception:
            pass

    def test_schema_and_traffic_partitioning(self):
        # 1. DB initially empty
        self.assertFalse(has_reference_data(self.db_path))
        self.assertEqual(get_production_count(self.db_path), 0)

        # 2. Insert reference records
        for i in range(10):
            log_certificate_request(
                record={
                    "request_id": f"ref_{i}",
                    "traffic_type": "reference",
                    "url": f"https://example.com/ref_{i}.pdf",
                    "doc_type": "pdf",
                    "page_count": 1,
                    "image_pixel_size": None,
                    "status": "OK",
                    "is_certificate": True,
                    "is_iso_iatf": True,
                    "certificate_language": "English",
                    "site_name": "Ref Site",
                    "address": "Ref Addr",
                    "scope": "Ref Scope",
                    "iso_type": "ISO 9001",
                    "issued_date": "2020-01-01",
                    "expiration_date": "2023-01-01",
                    "site_iso_type": "Main Site",
                    "confidence_score": 100,
                    "verdict": "Approved",
                },
                db_path=self.db_path,
            )

        self.assertTrue(has_reference_data(self.db_path))
        ref_records = get_reference_records(self.db_path)
        self.assertEqual(len(ref_records), 10)
        self.assertEqual(ref_records[0]["traffic_type"], "reference")
        self.assertEqual(ref_records[0]["certificate_language"], "English")

        # 3. Insert production records
        for i in range(15):
            log_certificate_request(
                record={
                    "request_id": f"prod_{i}",
                    "traffic_type": "production",
                    "url": f"https://example.com/prod_{i}.png",
                    "doc_type": "image",
                    "page_count": 1,
                    "image_pixel_size": 2500000,
                    "status": "OK",
                    "is_certificate": True,
                    "is_iso_iatf": True,
                    "certificate_language": "German" if i >= 10 else "English",
                    "confidence_score": 85,
                    "verdict": "Need Review" if i >= 10 else "Approved",
                },
                db_path=self.db_path,
            )

        self.assertEqual(get_production_count(self.db_path), 15)
        recent_5 = get_recent_production_records(limit=5, db_path=self.db_path)
        self.assertEqual(len(recent_5), 5)
        # Verify chronological order of the recent 5
        self.assertEqual(recent_5[-1]["request_id"], "prod_14")
        self.assertEqual(recent_5[-1]["image_pixel_size"], 2500000)


class TestDriftDetection(unittest.TestCase):
    def setUp(self):
        # Baseline reference set: 40 standard certificates
        self.ref_records = []
        for i in range(40):
            self.ref_records.append({
                "request_id": f"ref_{i}",
                "doc_type": "pdf" if i % 2 == 0 else "image",
                "page_count": 1 if i % 2 == 0 else 1,
                "image_pixel_size": 1500000 if i % 2 != 0 else None,
                "status": "OK",
                "is_certificate": True,
                "is_iso_iatf": True,
                "certificate_language": "English",
                "site_name": f"Company {i}",
                "address": f"Address {i}",
                "scope": "Manufacturing",
                "iso_type": "ISO 9001:2015",
                "issued_date": "2021-01-01",
                "expiration_date": "2024-01-01",
                "site_iso_type": "Main Site",
                "confidence_score": 95,
                "verdict": "Approved",
                "judge_score": 1,
                "checks_passed_or_failed": "Passed all verification rules",
            })

    def test_stable_batch_no_drift(self):
        # A recent batch with identical characteristics
        recent_records = []
        for i in range(20):
            recent_records.append({
                "request_id": f"rec_{i}",
                "doc_type": "pdf" if i % 2 == 0 else "image",
                "page_count": 1 if i % 2 == 0 else 1,
                "image_pixel_size": 1500000 if i % 2 != 0 else None,
                "status": "OK",
                "is_certificate": True,
                "is_iso_iatf": True,
                "certificate_language": "English",
                "site_name": f"Company {i}",
                "address": f"Address {i}",
                "scope": "Manufacturing",
                "iso_type": "ISO 9001:2015",
                "issued_date": "2021-01-01",
                "expiration_date": "2024-01-01",
                "site_iso_type": "Main Site",
                "confidence_score": 95,
                "verdict": "Approved",
                "judge_score": 1,
                "checks_passed_or_failed": "Passed all verification rules",
            })

        inp = check_input_drift(self.ref_records, recent_records)
        out = check_output_drift(self.ref_records, recent_records)
        qua = check_quality_drift(self.ref_records, recent_records)

        self.assertFalse(inp["input_drift_detected"])
        self.assertFalse(out["output_drift_detected"])
        self.assertFalse(qua["quality_drift_detected"])

        report = {
            "input_drift": inp,
            "output_drift": out,
            "quality_drift": qua,
        }
        alarms = generate_alarms(report)
        self.assertEqual(len(alarms), 0)

    def test_input_drift_page_count_spike(self):
        # Recent batch where PDFs have massive 15-page manuals
        recent_records = []
        for i in range(20):
            recent_records.append({
                "doc_type": "pdf",
                "page_count": 15,
                "image_pixel_size": None,
                "status": "OK",
                "is_certificate": True,
                "is_iso_iatf": True,
                "certificate_language": "English",
                "confidence_score": 95,
                "verdict": "Approved",
            })

        inp = check_input_drift(self.ref_records, recent_records)
        self.assertTrue(inp["input_drift_detected"])
        self.assertTrue(inp["pdf_pages"]["drift_detected"])

    def test_output_drift_novel_language(self):
        # Recent batch with new languages (Japanese, French)
        recent_records = []
        for i in range(20):
            recent_records.append({
                "doc_type": "pdf",
                "page_count": 1,
                "status": "OK",
                "is_certificate": True,
                "is_iso_iatf": True,
                "certificate_language": "Japanese" if i < 10 else "French",
                "confidence_score": 90,
                "verdict": "Approved",
            })

        out = check_output_drift(self.ref_records, recent_records)
        self.assertTrue(out["output_drift_detected"])
        self.assertIn("Japanese", out["language_distribution"]["novel_languages_detected"])
        self.assertIn("French", out["language_distribution"]["novel_languages_detected"])

    def test_output_drift_site_iso_type_undefined(self):
        # Baseline reference has 0 Undefined site_iso_type (all "Main Site")
        # Recent batch has 15 out of 20 as "Undefined" (75% vs 0%)
        recent_records = []
        for i in range(20):
            recent_records.append({
                "doc_type": "pdf",
                "page_count": 1,
                "status": "OK",
                "is_certificate": True,
                "is_iso_iatf": True,
                "certificate_language": "English",
                "site_iso_type": "Undefined" if i < 15 else "Factory",
                "confidence_score": 90,
                "verdict": "Approved",
            })

        out = check_output_drift(self.ref_records, recent_records)
        self.assertTrue(out["output_drift_detected"])
        self.assertTrue(out["site_iso_type_undefined"]["drift_detected"])
        self.assertEqual(out["site_iso_type_undefined"]["ref_count"], 0)
        self.assertEqual(out["site_iso_type_undefined"]["recent_count"], 15)
        self.assertEqual(out["site_iso_type_undefined"]["recent_rate_pct"], 75.0)
        self.assertEqual(out["site_iso_type_undefined"]["pp_change"], 75.0)

        report = {
            "input_drift": {},
            "output_drift": out,
            "quality_drift": {},
        }
        alarms = generate_alarms(report)
        alarm_metrics = [a["metric"] for a in alarms]
        self.assertIn("site_iso_type_undefined_ratio", alarm_metrics)

    def test_quality_drift_error_rate_and_judge(self):
        # Recent batch where LLM or network crashed on half the requests
        recent_records = []
        for i in range(20):
            is_err = i >= 10
            recent_records.append({
                "doc_type": "pdf",
                "page_count": 0 if is_err else 1,
                "status": "Failed" if is_err else "OK",
                "error": "Connection timeout" if is_err else None,
                "is_certificate": None if is_err else True,
                "is_iso_iatf": None if is_err else True,
                "certificate_language": "Unknown" if is_err else "English",
                "confidence_score": 50 if is_err else 95,
                "verdict": "Need Review" if is_err else "Approved",
                "judge_score": 0 if is_err else 1,
                "checks_passed_or_failed": "Document did not open" if is_err else "Passed all verification rules",
            })

        qua = check_quality_drift(self.ref_records, recent_records)
        self.assertTrue(qua["quality_drift_detected"])
        self.assertTrue(qua["error_rate"]["drift_detected"])
        self.assertTrue(qua["judge_pass_rate"]["drift_detected"])

    def test_execute_drift_test_persistence(self):
        temp_dir = tempfile.mkdtemp()
        drift_json = os.path.join(temp_dir, "drift_test.json")
        alarms_json = os.path.join(temp_dir, "alarms.json")

        try:
            # Create a drifted batch
            recent_records = []
            for i in range(20):
                recent_records.append({
                    "doc_type": "pdf",
                    "page_count": 10,  # page count drift
                    "status": "OK",
                    "is_certificate": True,
                    "is_iso_iatf": True,
                    "certificate_language": "Chinese",  # novel language
                    "site_name": None,  # missing fields
                    "address": None,
                    "confidence_score": 55,  # confidence score drop
                    "verdict": "Need Review",  # verdict shift
                    "judge_score": 0,  # judge drop
                })

            report, alarms = execute_drift_test(
                ref_records=self.ref_records,
                recent_records=recent_records,
                batch_index=50,
                from_index=1,
                to_index=50,
                save_reports=True,
                drift_report_path=drift_json,
                alarms_path=alarms_json,
            )

            self.assertTrue(report["overall_drift_detected"])
            self.assertEqual(report["from_index"], 1)
            self.assertEqual(report["to_index"], 50)
            self.assertGreater(len(alarms), 0)
            self.assertEqual(alarms[0]["from_index"], 1)
            self.assertEqual(alarms[0]["to_index"], 50)

            # Verify files were written
            self.assertTrue(os.path.exists(drift_json))
            self.assertTrue(os.path.exists(alarms_json))

            with open(drift_json, "r", encoding="utf-8") as f:
                d_list = json.load(f)
                self.assertEqual(len(d_list), 1)
                self.assertEqual(d_list[0]["batch_sample_count"], 50)
                self.assertEqual(d_list[0]["from_index"], 1)
                self.assertEqual(d_list[0]["to_index"], 50)

            with open(alarms_json, "r", encoding="utf-8") as f:
                a_list = json.load(f)
                self.assertGreater(len(a_list), 0)
                self.assertEqual(a_list[0]["from_index"], 1)
                self.assertEqual(a_list[0]["to_index"], 50)

            # Test appending a second run (51 to 100)
            execute_drift_test(
                ref_records=self.ref_records,
                recent_records=recent_records,
                batch_index=100,
                from_index=51,
                to_index=100,
                save_reports=True,
                drift_report_path=drift_json,
                alarms_path=alarms_json,
            )

            with open(drift_json, "r", encoding="utf-8") as f:
                d_list_2 = json.load(f)
                self.assertEqual(len(d_list_2), 2)
                self.assertEqual(d_list_2[1]["batch_sample_count"], 100)
                self.assertEqual(d_list_2[1]["from_index"], 51)
                self.assertEqual(d_list_2[1]["to_index"], 100)

        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
