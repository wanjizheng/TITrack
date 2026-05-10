"""Icon proxy routes - fetches icons from CDN with proper headers."""

import hashlib
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import Response as FastAPIResponse

from titrack.config.paths import get_data_dir
from titrack.db.repository import Repository


def get_repository() -> Repository:
    """Dependency injection for repository - set by app factory."""
    raise NotImplementedError("Repository not configured")

router = APIRouter(prefix="/api/icons", tags=["icons"])

# In-memory cache (icon_url -> bytes) backed by an on-disk cache so icons
# survive restarts and don't have to be re-downloaded on every cold launch.
_icon_cache: dict[str, bytes] = {}
# Map url -> (timestamp, is_permanent). Transient failures expire; 404s are
# cached permanently for the process lifetime.
_failed_urls: dict[str, tuple[float, bool]] = {}
_TRANSIENT_FAIL_TTL = 60.0  # seconds


def _disk_cache_dir() -> Path:
    """Return (and create) the on-disk icon cache directory."""
    try:
        d = get_data_dir() / "icon_cache"
    except Exception:
        # Fallback to temp if data dir unavailable for any reason
        import tempfile
        d = Path(tempfile.gettempdir()) / "titrack_icon_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _disk_cache_path(url: str) -> Path:
    """Stable filename for a given icon URL."""
    suffix = ""
    lower = url.lower()
    for ext in (".webp", ".png", ".jpg", ".jpeg", ".gif"):
        if lower.endswith(ext):
            suffix = ext
            break
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return _disk_cache_dir() / f"{h}{suffix}"


# CDN request headers
CDN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://tlidb.com/",
    "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
}


def fetch_icon(url: str) -> Optional[bytes]:
    """Fetch icon: memory cache → disk cache → CDN."""
    cached = _icon_cache.get(url)
    if cached is not None:
        return cached

    # Try on-disk cache
    disk_path = _disk_cache_path(url)
    if disk_path.exists():
        try:
            data = disk_path.read_bytes()
            if data:
                _icon_cache[url] = data
                return data
        except OSError:
            pass

    failure = _failed_urls.get(url)
    if failure is not None:
        ts, permanent = failure
        if permanent or (time.monotonic() - ts) < _TRANSIENT_FAIL_TTL:
            return None
        _failed_urls.pop(url, None)

    try:
        req = urllib.request.Request(url, headers=CDN_HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
            if not data:
                # Treat empty response as a transient failure
                _failed_urls[url] = (time.monotonic(), False)
                return None
            _icon_cache[url] = data
            # Write atomically via tmp + rename so a process kill mid-write
            # never leaves a truncated/corrupt cache file behind.
            try:
                tmp_path = disk_path.with_suffix(disk_path.suffix + ".tmp")
                tmp_path.write_bytes(data)
                tmp_path.replace(disk_path)
            except OSError:
                pass
            return data
    except urllib.error.HTTPError as e:
        permanent = 400 <= e.code < 500 and e.code not in (408, 429)
        _failed_urls[url] = (time.monotonic(), permanent)
        return None
    except (urllib.error.URLError, TimeoutError):
        _failed_urls[url] = (time.monotonic(), False)
        return None


@router.get("/{config_base_id}")
def get_icon(config_base_id: int, repo: Repository = Depends(get_repository)) -> Response:
    """
    Proxy icon for an item.

    Fetches the icon from the CDN with proper headers and caches it.
    Returns 404 if no icon URL exists or the CDN returns an error.
    """
    # Look up item to get icon URL
    item = repo.get_item(config_base_id)
    if not item or not item.icon_url:
        raise HTTPException(status_code=404, detail="No icon available")

    # Fetch from CDN (with caching)
    icon_data = fetch_icon(item.icon_url)
    if icon_data is None:
        raise HTTPException(status_code=404, detail="Icon not available from CDN")

    # Determine content type from URL
    content_type = "image/webp"
    if item.icon_url.endswith(".png"):
        content_type = "image/png"
    elif item.icon_url.endswith(".jpg") or item.icon_url.endswith(".jpeg"):
        content_type = "image/jpeg"

    return FastAPIResponse(
        content=icon_data,
        media_type=content_type,
        headers={
            "Cache-Control": "public, max-age=86400",  # Cache for 24 hours
        },
    )
