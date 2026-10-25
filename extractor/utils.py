"""
utils.py
========
Helper utilities for downloading files, inspecting mime types, converting PDFs
to images with PyMuPDF, parsing/normalizing dates, and repairing raw JSON responses.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime as _dt
from typing import Optional, Tuple, List, Dict, Any
from urllib.parse import urlparse

import fitz  # PyMuPDF
import requests

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif", ".gif"}
_PDF_EXTENSIONS = {".pdf"}

_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
    "oct": 10, "nov": 11, "dec": 12,
}


def get_mime_type(file_path: str) -> str:
    """Returns MIME type string based on file extension."""
    ext = os.path.splitext(file_path)[1].lower()
    mapping = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".gif": "image/gif",
        ".tiff": "image/tiff",
        ".tif": "image/tiff",
        ".pdf": "application/pdf",
    }
    return mapping.get(ext, "image/png")


def get_url_type(url: str) -> str:
    """
    Detects whether a URL points to an image or a PDF.
    First inspects URL path extension; if ambiguous, checks Content-Type header.
    """
    cleaned_url = url.split("?")[0].split("#")[0].lower()
    ext = os.path.splitext(cleaned_url)[1]
    if ext in _IMAGE_EXTENSIONS:
        return "image"
    if ext in _PDF_EXTENSIONS:
        return "pdf"

    # Fallback: lightweight HEAD / GET check for Content-Type
    try:
        resp = requests.head(url, headers=REQUEST_HEADERS, timeout=10, allow_redirects=True)
        ct = resp.headers.get("Content-Type", "").lower()
        if "application/pdf" in ct:
            return "pdf"
        if "image/" in ct:
            return "image"
    except Exception:
        pass

    return "unknown"


def download_cert_file(url: str, dest_path: str, timeouts: tuple = (30, 60)) -> bool:
    """
    Downloads a certificate file with streaming and timeout retries.
    Returns True if downloaded successfully, False otherwise.
    """
    for timeout in timeouts:
        try:
            resp = requests.get(url, headers=REQUEST_HEADERS, timeout=timeout, stream=True)
            resp.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=16384):
                    if chunk:
                        f.write(chunk)
            if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
                return True
        except Exception:
            continue
    return False


def pdf_to_images(pdf_path: str, output_dir: str, dpi: int = 200, max_pages: int = 4) -> List[str]:
    """
    Renders PDF pages to PNG images using PyMuPDF (fitz).
    Returns list of paths to rendered images.
    """
    doc = fitz.open(pdf_path)
    image_paths = []
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    try:
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img_path = os.path.join(output_dir, f"page_{i+1:03d}.png")
            pix.save(img_path)
            image_paths.append(img_path)
    finally:
        doc.close()
    return image_paths


def prepare_images_for_cert_url(
    url: str,
    work_dir: str,
    dpi: int = 200,
    max_pages: int = 4,
    return_meta: bool = False,
) -> Any:
    """
    Downloads URL and converts it into a list of (image_path, mime_type) tuples.
    If return_meta=False (default): returns (images_list, error_reason).
    If return_meta=True: returns (images_list, error_reason, meta_dict).
    meta_dict contains: {"doc_type": "pdf"|"image"|"unknown", "page_count": int}
    """
    meta: Dict[str, Any] = {"doc_type": "unknown", "page_count": 0}

    def _ret(images_list: List[Tuple[str, str]], err: Optional[str]):
        if return_meta:
            return images_list, err, meta
        return images_list, err

    if not url or not str(url).strip():
        return _ret([], "Empty URL")

    url_type = get_url_type(url)
    url_hash = hashlib.md5(url.encode("utf-8")).hexdigest()[:10]
    cert_dir = os.path.join(work_dir, f"cert_{url_hash}")
    os.makedirs(cert_dir, exist_ok=True)

    # 1) If detected as image
    if url_type == "image":
        meta["doc_type"] = "image"
        meta["page_count"] = 1
        ext = os.path.splitext(url.split("?")[0].lower())[1] or ".jpg"
        img_path = os.path.join(cert_dir, f"direct_image{ext}")
        if not download_cert_file(url, img_path):
            return _ret([], "did not open need review")
        return _ret([(img_path, get_mime_type(img_path))], None)

    # 2) If detected as PDF
    elif url_type == "pdf":
        meta["doc_type"] = "pdf"
        pdf_path = os.path.join(cert_dir, "cert.pdf")
        if not download_cert_file(url, pdf_path):
            return _ret([], "did not open need review")
        try:
            doc = fitz.open(pdf_path)
            meta["page_count"] = len(doc)
            doc.close()

            img_paths = pdf_to_images(pdf_path, cert_dir, dpi=dpi, max_pages=max_pages)
            if not img_paths:
                return _ret([], "did not open need review")
            return _ret([(p, "image/png") for p in img_paths], None)
        except Exception:
            return _ret([], "did not open need review")

    # 3) Unknown extension: try downloading as generic file and inspect bytes
    else:
        generic_path = os.path.join(cert_dir, "downloaded_file.bin")
        if not download_cert_file(url, generic_path):
            return _ret([], "did not open need review")

        # Check magic bytes for PDF (%PDF)
        with open(generic_path, "rb") as f:
            header = f.read(10)
        if header.startswith(b"%PDF"):
            meta["doc_type"] = "pdf"
            pdf_path = os.path.join(cert_dir, "cert.pdf")
            os.replace(generic_path, pdf_path)
            try:
                doc = fitz.open(pdf_path)
                meta["page_count"] = len(doc)
                doc.close()

                img_paths = pdf_to_images(pdf_path, cert_dir, dpi=dpi, max_pages=max_pages)
                return _ret([(p, "image/png") for p in img_paths], None)
            except Exception:
                return _ret([], "did not open need review")

        # Check if it's an image
        if header.startswith(b"\xff\xd8\xff") or header.startswith(b"\x89PNG") or header.startswith(b"GIF"):
            meta["doc_type"] = "image"
            meta["page_count"] = 1
            ext = ".jpg" if header.startswith(b"\xff\xd8\xff") else ".png"
            img_path = os.path.join(cert_dir, f"direct_image{ext}")
            os.replace(generic_path, img_path)
            return _ret([(img_path, get_mime_type(img_path))], None)

        return _ret([], "did not open need review")


def clean_json_markdown(raw: str) -> str:
    """Strips Markdown fences like ```json ... ```."""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return raw.strip()


def try_repair_json(raw: str) -> Optional[dict]:
    """
    Fallback for common LLM JSON issues like unescaped newlines inside strings.
    """
    cleaned = clean_json_markdown(raw)
    repaired = re.sub(r'(?<!\\)\n', '\\n', cleaned)
    try:
        result = json.loads(repaired)
    except json.JSONDecodeError:
        return None

    if isinstance(result, dict):
        for key, value in result.items():
            if isinstance(value, str):
                result[key] = re.sub(r'(\\n\s*){2,}', ' ', value).strip()
        return result
    return None


def normalize_date(date_str: str, fmt_hint: str = None) -> Tuple[Optional[str], bool]:
    """
    Normalizes candidate date strings into MM/DD/YYYY format.
    Returns (normalized_date_string, success_bool).
    """
    if not date_str or str(date_str).strip() in ("", "nan", "None"):
        return None, False

    s = str(date_str).strip()

    # Already MM/DD/YYYY
    if re.fullmatch(r'\d{1,2}/\d{1,2}/\d{4}', s):
        try:
            dt = _dt.strptime(s, "%m/%d/%Y")
            return dt.strftime("%m/%d/%Y"), True
        except ValueError:
            pass

    candidate_formats = [
        "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y", "%d-%m-%Y",
        "%Y/%m/%d", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y",
        "%B %d %Y", "%b %d %Y", "%d. %B %Y", "%d. %b %Y", "%Y.%m.%d",
        "%m-%d-%Y", "%d/%m/%y", "%m/%d/%y", "%Y-%m", "%m/%Y",
        "%b %Y", "%B %Y", "%Y",
        "%d-%b-%Y", "%d-%B-%Y",
    ]

    for fmt in candidate_formats:
        try:
            dt = _dt.strptime(s, fmt)
            return dt.strftime("%m/%d/%Y"), True
        except ValueError:
            continue

    # Regex pattern: "25th June, 2024" or "25 June 2024"
    m = re.match(r'(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+),?\s+(\d{4})', s)
    if m:
        day, mon_str, year = m.group(1), m.group(2).lower(), m.group(3)
        mon = _MONTH_NAMES.get(mon_str)
        if mon:
            return f"{mon:02d}/{int(day):02d}/{year}", True

    # Regex pattern: "June 25, 2024"
    m = re.match(r'([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})', s)
    if m:
        mon_str, day, year = m.group(1).lower(), m.group(2), m.group(3)
        mon = _MONTH_NAMES.get(mon_str)
        if mon:
            return f"{mon:02d}/{int(day):02d}/{year}", True

    # Only year: "2024" -> "01/01/2024"
    if re.fullmatch(r'\d{4}', s):
        return f"01/01/{s}", True

    return s, False
