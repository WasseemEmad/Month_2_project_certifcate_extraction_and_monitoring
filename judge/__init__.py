"""
judge package
=============
Provides LLM-as-a-Judge post-batch evaluation with stratified sampling.
"""

from .judge import (
    LLMJudge,
    sample_records_stratified,
)

__all__ = [
    "LLMJudge",
    "sample_records_stratified",
]
