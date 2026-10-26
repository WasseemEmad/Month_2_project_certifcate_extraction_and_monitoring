"""
tests/test_dashboard_features.py
================================
Unit tests verifying FastAPI dashboard backend endpoints and review queue workflow:
1. Dynamic baseline discovery and comparison (baseline.json vs old_baseline.json).
2. Dynamic 30% review queue calculation.
3. Decision saving (appends to reviewed/human_review.json, removes reviewed item, auto-replenishes queue).
4. Reviewed cases history retrieval.
5. FastAPI TestClient HTTP endpoint validation.
"""

import json
from pathlib import Path
import shutil
import tempfile
import unittest

from fastapi.testclient import TestClient

from dashboard import app, review_queue


class TestDashboardFeatures(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="dash_test_"))
        self.analytics_dir = self.test_dir / "analytics"
        self.reviewed_dir = self.test_dir / "reviewed"
        self.analytics_dir.mkdir(parents=True)
        self.reviewed_dir.mkdir(parents=True)

        self.db_path = Path("analytics/certificate_logs.db")
        self.reviewed_json = self.reviewed_dir / "human_review.json"

        # Initialize TestClient for FastAPI app
        self.client = TestClient(app.app)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_dynamic_baseline_discovery(self):
        # Create baseline.json and old_baseline.json in test analytics dir
        b1 = {
            "validation_timestamp": "2026-10-25",
            "golden_file": "golden.xlsx",
            "total_cases": 40,
            "passed_cases": 35,
            "pass_rate_percent": 87.5,
            "feature_scores": {"avg_site_name_score": 1.0},
            "column_breakdown": {"site_name": {"accuracy_percent": 100.0}},
        }
        b2 = {
            "validation_timestamp": "2026-10-24",
            "golden_file": "golden.xlsx",
            "total_cases": 40,
            "passed_cases": 32,
            "pass_rate_percent": 80.0,
            "feature_scores": {"avg_site_name_score": 0.9},
            "column_breakdown": {"site_name": {"accuracy_percent": 90.0}},
        }
        with open(self.analytics_dir / "baseline.json", "w", encoding="utf-8") as f:
            json.dump(b1, f)
        with open(self.analytics_dir / "old_baseline.json", "w", encoding="utf-8") as f:
            json.dump(b2, f)

        res = app.build_golden_baselines_section(self.analytics_dir, target_file="old_baseline.json")
        self.assertTrue(res["available"])
        self.assertIn("baseline.json", res["files"])
        self.assertIn("old_baseline.json", res["files"])
        self.assertEqual(res["active_file"], "baseline.json")
        self.assertEqual(res["compare_file"], "old_baseline.json")
        self.assertGreater(len(res["feature_comparison"]), 0)

    def test_review_queue_and_decision_saving(self):
        if not self.db_path.exists():
            self.skipTest("certificate_logs.db does not exist")

        # Initial queue
        q_init = review_queue.get_review_queue(db_path=self.db_path, reviewed_path=self.reviewed_json)
        init_items = q_init["items"]
        stats = q_init["stats"]
        self.assertGreater(len(init_items), 0)
        target_size = stats["target_queue_size"]
        self.assertEqual(len(init_items), target_size)

        # Pick the first case and save human decision
        first_case = init_items[0]
        req_id = first_case["request_id"]

        save_res = review_queue.save_human_decision(
            request_id=req_id,
            decision="Approved",
            reviewer_notes="Verified certificate manually.",
            reviewer_id="TEST-REV",
            corrected_fields={"site_name": "Corrected Site Name"},
            db_path=self.db_path,
            reviewed_path=self.reviewed_json,
        )
        self.assertTrue(save_res["success"])

        # Check reviewed decisions file (must exist and contain 1 record)
        reviewed_cases = review_queue.get_reviewed_cases(reviewed_path=self.reviewed_json)
        self.assertEqual(len(reviewed_cases), 1)
        self.assertEqual(reviewed_cases[0]["request_id"], str(req_id))
        self.assertEqual(reviewed_cases[0]["decision"]["reviewer_verdict"], "Approved")
        self.assertEqual(reviewed_cases[0]["decision"]["corrected_fields"]["site_name"], "Corrected Site Name")

        # Check that queue was replenished and first_case is removed from pending
        q_after = review_queue.get_review_queue(db_path=self.db_path, reviewed_path=self.reviewed_json)
        after_ids = [it["request_id"] for it in q_after["items"]]
        self.assertNotIn(req_id, after_ids)
        # Target queue size should still be maintained
        self.assertEqual(len(q_after["items"]), target_size)

    def test_fastapi_endpoints(self):
        # 1. Root index.html
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers["content-type"])

        # 2. GET /api/dashboard
        resp = self.client.get("/api/dashboard")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("overview", data)
        self.assertIn("drift", data)
        self.assertIn("quality_baselines", data)
        self.assertIn("review_queue", data)

        # 3. GET /api/baseline
        resp = self.client.get("/api/baseline?file=old_baseline.json")
        self.assertEqual(resp.status_code, 200)
        b_data = resp.json()
        self.assertTrue(b_data.get("available"))
        self.assertEqual(b_data.get("compare_file"), "old_baseline.json")

        # 4. GET /api/review-queue
        resp = self.client.get("/api/review-queue")
        self.assertEqual(resp.status_code, 200)
        rq_data = resp.json()
        self.assertIn("items", rq_data)

        # 5. GET /api/reviewed
        resp = self.client.get("/api/reviewed")
        self.assertEqual(resp.status_code, 200)
        rev_data = resp.json()
        self.assertIn("items", rev_data)


if __name__ == "__main__":
    unittest.main()
