#!/usr/bin/env python3
"""
Generate GitHub Actions Step Summary markdown from dataset validation summary.
"""
import json
import os
from pathlib import Path


def main():
    summary_path = Path("dataset_validation/validation_summary.json")
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")

    if not summary_path.exists():
        print(f"[INFO] Validation summary not found at: {summary_path}")
        return

    data = json.loads(summary_path.read_text(encoding="utf-8"))
    s = data.get("summary", {})

    lines = [
        "# 🏆 ISO Golden Dataset Regression Summary",
        "",
        "| Metric | Result |",
        "| :--- | :--- |",
        f"| **Total Cases** | {s.get('total_cases', 'N/A')} |",
        f"| **Passed Cases** | {s.get('passed_cases', 'N/A')} / {s.get('total_cases', 'N/A')} |",
        f"| **Failed Cases** | {s.get('failed_cases', 'N/A')} |",
        f"| **Pass Rate** | {s.get('pass_rate_percent', 0)}% |",
        f"| **Average Score** | {s.get('average_total_score', 0)} / 10 |",
        f"| **False Pass Cases** | {s.get('false_pass_cases', 0)} |",
        f"| **False Pass Rate (Total)** | {s.get('false_pass_rate_total_percent', 0.0)}% |",
        f"| **False Pass Rate (Failed)** | {s.get('false_pass_rate_failed_percent', 0.0)}% |",
        "",
        "### 📊 Feature Accuracy Breakdown",
        "",
        "| Feature Name | Avg Score | Accuracy (%) |",
        "| :--- | :--- | :--- |",
    ]

    for col, stats in s.get("column_breakdown", {}).items():
        lines.append(f"| `{col}` | {stats.get('avg_score', 0):.4f} | {stats.get('accuracy_percent', 0)}% |")

    if s.get("false_pass_urls"):
        lines.extend([
            "",
            "### ⚠️ False Pass Detections (Verifier Approved vs Validator Failed)",
            "",
            "| Row # | Confidence Score | URL |",
            "| :--- | :--- | :--- |",
        ])
        for fp in s.get("false_pass_urls", []):
            lines.append(f"| #{fp.get('index')} | {fp.get('confidence_score')} | {fp.get('url')} |")

    baseline_file = Path("analytics/new_baseline.json")
    if baseline_file.exists():
        lines.extend([
            "",
            "### 🎉 Improvement Detected!",
            "A candidate baseline with positive improvement was generated at `analytics/new_baseline.json`.",
        ])

    output_text = "\n".join(lines) + "\n"

    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write(output_text)
        print(f"[INFO] Successfully appended step summary to {step_summary}")
    else:
        print("[INFO] GITHUB_STEP_SUMMARY not set. Summary generated successfully:")
        try:
            print(output_text)
        except UnicodeEncodeError:
            print(output_text.encode("ascii", errors="replace").decode("ascii"))


if __name__ == "__main__":
    main()
