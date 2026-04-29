"""
utils/http_utils.py
HTTP probing utilities — determine file size, range-request support, and filename.

These functions isolate all network inspection from the download engine.
The download manager calls probe_url() before splitting the file into segments.
"""

import logging
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

logger = logging.getLogger(__name__)

# Connection / read timeouts for the probe request (seconds)
_PROBE_TIMEOUT = (10, 10)


def probe_url(url: str) -> dict:
    """
    Send a HEAD request (falling back to a streaming GET if HEAD is rejected)
    to retrieve metadata about the target file.

    Returns a dict:
        {
            'total_size':     int,   # 0 if Content-Length is absent
            'accepts_ranges': bool,  # True if server supports byte ranges
            'filename':       str,   # derived from headers or URL
            'content_type':   str,   # e.g. 'application/octet-stream'
        }

    Raises requests.RequestException on network failure.
    """
    logger.info("Probing URL: %s", url)
    headers = _try_head(url)
    if headers is None:
        headers = _try_get_headers(url)

    total_size      = _parse_content_length(headers)
    accepts_ranges  = _check_range_support(headers)
    content_type    = headers.get("Content-Type", "application/octet-stream").split(";")[0].strip()
    filename        = get_filename_from_headers_and_url(url, headers)

    logger.info(
        "Probe result — size=%d accepts_ranges=%s filename=%s",
        total_size, accepts_ranges, filename,
    )
    return {
        "total_size":     total_size,
        "accepts_ranges": accepts_ranges,
        "filename":       filename,
        "content_type":   content_type,
    }


def build_range_header(start: int, end: int) -> dict[str, str]:
    """Return an HTTP Range header dict for the given byte range."""
    return {"Range": f"bytes={start}-{end}"}


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _try_head(url: str) -> dict | None:
    """Attempt a HEAD request. Returns response headers dict or None on failure."""
    try:
        resp = requests.head(url, timeout=_PROBE_TIMEOUT, allow_redirects=True)
        if resp.status_code < 400:
            return dict(resp.headers)
    except requests.RequestException as exc:
        logger.debug("HEAD failed for %s: %s", url, exc)
    return None


def _try_get_headers(url: str) -> dict:
    """
    Fall back to a streaming GET request and immediately close the connection.
    Some servers (e.g. Google Drive) reject HEAD; this approach retrieves
    the same header information without downloading the body.
    """
    try:
        with requests.get(url, stream=True, timeout=_PROBE_TIMEOUT, allow_redirects=True) as resp:
            resp.raise_for_status()
            return dict(resp.headers)
    except requests.RequestException as exc:
        logger.warning("GET probe also failed for %s: %s — returning empty headers", url, exc)
        return {}


def _parse_content_length(headers: dict) -> int:
    """Extract Content-Length as an integer, returning 0 if absent or non-numeric."""
    raw = headers.get("Content-Length", "0")
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _check_range_support(headers: dict) -> bool:
    """
    Return True if the server declares byte-range support.
    Accept-Ranges: bytes is the standard signal.
    """
    return headers.get("Accept-Ranges", "").lower() == "bytes"


def get_filename_from_headers_and_url(url: str, headers: dict) -> str:
    """
    Derive a filename in priority order:
    1. Content-Disposition header  (e.g. filename="report.pdf")
    2. URL path basename           (e.g. /files/video.mp4 → video.mp4)
    3. Fallback: 'download'
    """
    # 1. Content-Disposition
    cd = headers.get("Content-Disposition", "")
    if cd:
        match = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';\r\n]+)', cd, re.IGNORECASE)
        if match:
            name = unquote(match.group(1).strip())
            if name:
                return _sanitize_filename(name)

    # 2. URL basename
    parsed = urlparse(url)
    path_name = Path(unquote(parsed.path)).name
    if path_name and "." in path_name:
        return _sanitize_filename(path_name)

    # 3. Fallback
    return "download"


def _sanitize_filename(name: str) -> str:
    """Remove characters that are invalid in Windows/Linux filenames."""
    invalid = r'\/:*?"<>|'
    for ch in invalid:
        name = name.replace(ch, "_")
    return name.strip() or "download"
