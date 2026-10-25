"""
tracing/tracer.py
=================
Unified Langfuse tracing layer providing standardized spans and generations
for certificate processing, text extraction, LLM extraction, verification,
and LLM judge evaluation.
Follows saturday_email.py patterns with graceful fallback when credentials are absent.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from typing import Any, Dict, Generator, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger("cert_tracing")

# Global Langfuse client holder
_LANGFUSE_CLIENT: Any = None
_LANGFUSE_INIT_ATTEMPTED: bool = False


class _NoOpObservation:
    """Fallback observation context manager when Langfuse is not enabled/configured."""

    def __init__(self, name: str, as_type: str = "span"):
        self.name = name
        self.as_type = as_type
        self.output: Any = None
        self.metadata: Dict[str, Any] = {}

    def update(self, **kwargs):
        self.output = kwargs.get("output", self.output)
        if "metadata" in kwargs and isinstance(kwargs["metadata"], dict):
            self.metadata.update(kwargs["metadata"])
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_val is not None:
            logger.debug(f"[tracing] {self.as_type}:{self.name} failed with: {exc_val}")
        return False


def get_langfuse_client() -> Optional[Any]:
    """
    Safely retrieves or initializes the Langfuse client.
    Returns None if credentials (LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY)
    are missing or client initialization fails.
    """
    global _LANGFUSE_CLIENT, _LANGFUSE_INIT_ATTEMPTED
    if _LANGFUSE_INIT_ATTEMPTED:
        return _LANGFUSE_CLIENT

    _LANGFUSE_INIT_ATTEMPTED = True
    pub_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
    sec_key = os.environ.get("LANGFUSE_SECRET_KEY")

    if not pub_key or not sec_key:
        logger.info("[tracing] Langfuse keys not set in environment. Running in graceful no-op mode.")
        _LANGFUSE_CLIENT = None
        return None

    try:
        from langfuse import get_client
        _LANGFUSE_CLIENT = get_client()
        logger.info("[tracing] Langfuse client initialized successfully.")
    except Exception as e:
        logger.warning(f"[tracing] Langfuse initialization failed ({e}). Proceeding without remote tracing.")
        _LANGFUSE_CLIENT = None

    return _LANGFUSE_CLIENT


@contextlib.contextmanager
def trace_certificate(
    cert_url: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> Generator[Any, None, None]:
    """
    Top-level observation span representing the entire processing pipeline for one certificate.
    """
    client = get_langfuse_client()
    meta = {
        "application": "iso_certificate_extraction",
        "url": cert_url,
    }
    if metadata:
        meta.update(metadata)

    if client is not None:
        try:
            with client.start_as_current_observation(
                as_type="span",
                name="process-certificate",
                input={"url": cert_url},
                metadata=meta,
            ) as span:
                yield span
                return
        except Exception as e:
            logger.warning(f"[tracing] Error in trace_certificate span: {e}")

    # Fallback
    with _NoOpObservation(name="process-certificate", as_type="span") as noop:
        yield noop


@contextlib.contextmanager
def span_text_extraction(
    url: str,
    doc_type: str = "unknown",
    page_count: int = 1,
    metadata: Optional[Dict[str, Any]] = None,
) -> Generator[Any, None, None]:
    """
    Span tracking document download, page splitting, and image rendering.
    """
    client = get_langfuse_client()
    meta = {
        "step": "text_extraction",
        "doc_type": doc_type,
        "page_count": page_count,
    }
    if metadata:
        meta.update(metadata)

    if client is not None:
        try:
            with client.start_as_current_observation(
                as_type="span",
                name="text-extraction",
                input={"url": url, "doc_type": doc_type, "page_count": page_count},
                metadata=meta,
            ) as span:
                yield span
                return
        except Exception as e:
            logger.warning(f"[tracing] Error in span_text_extraction: {e}")

    with _NoOpObservation(name="text-extraction", as_type="span") as noop:
        yield noop


@contextlib.contextmanager
def generation_llm_extraction(
    step_name: str,
    model: str,
    prompt: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> Generator[Any, None, None]:
    """
    Generation observation tracking an individual Gemini LLM call
    (screening, page extraction, scope classification).
    """
    client = get_langfuse_client()
    meta = {
        "step": step_name,
        "model": model,
    }
    if metadata:
        meta.update(metadata)

    if client is not None:
        try:
            with client.start_as_current_observation(
                as_type="generation",
                name=f"llm-{step_name}",
                model=model,
                input=prompt,
                metadata=meta,
            ) as generation:
                yield generation
                return
        except Exception as e:
            logger.warning(f"[tracing] Error in generation_llm_extraction ({step_name}): {e}")

    with _NoOpObservation(name=f"llm-{step_name}", as_type="generation") as noop:
        yield noop


@contextlib.contextmanager
def span_verification(
    record: Dict[str, Any],
    metadata: Optional[Dict[str, Any]] = None,
) -> Generator[Any, None, None]:
    """
    Span tracking rule-based verification, rule checks, deductions, and confidence scoring.
    """
    client = get_langfuse_client()
    meta = {
        "step": "verification_and_scoring",
    }
    if metadata:
        meta.update(metadata)

    if client is not None:
        try:
            with client.start_as_current_observation(
                as_type="span",
                name="verification-and-scoring",
                input={
                    "is_certificate": record.get("is_certificate"),
                    "is_iso_iatf": record.get("is_iso_iatf"),
                    "cert_type": record.get("cert_type"),
                    "iso_type": record.get("iso_type"),
                    "issued_date": record.get("issued_date"),
                    "expiration_date": record.get("expiration_date"),
                    "site_iso_type": record.get("site_iso_type"),
                },
                metadata=meta,
            ) as span:
                yield span
                return
        except Exception as e:
            logger.warning(f"[tracing] Error in span_verification: {e}")

    with _NoOpObservation(name="verification-and-scoring", as_type="span") as noop:
        yield noop


@contextlib.contextmanager
def generation_llm_judge(
    model: str,
    url: str,
    prompt: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> Generator[Any, None, None]:
    """
    Generation observation tracking an LLM Judge evaluation call.
    """
    client = get_langfuse_client()
    meta = {
        "step": "llm_judge_evaluation",
        "url": url,
        "model": model,
    }
    if metadata:
        meta.update(metadata)

    if client is not None:
        try:
            with client.start_as_current_observation(
                as_type="generation",
                name="llm-judge-evaluation",
                model=model,
                input=prompt,
                metadata=meta,
            ) as generation:
                yield generation
                return
        except Exception as e:
            logger.warning(f"[tracing] Error in generation_llm_judge: {e}")

    with _NoOpObservation(name="llm-judge-evaluation", as_type="generation") as noop:
        yield noop


def flush_traces():
    """Flushes all buffered traces to the Langfuse backend if client is active."""
    client = get_langfuse_client()
    if client is not None:
        try:
            client.flush()
            logger.info("[tracing] Flushed traces to Langfuse.")
        except Exception as e:
            logger.warning(f"[tracing] Error flushing traces: {e}")
