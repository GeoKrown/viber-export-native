"""Bounded access to original media files."""
from pathlib import Path
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}

def local_image(row, roots):
    """Accept original local images only, never arbitrary message paths or URLs."""
    candidates = [row.get('payload_path',''), *row.get('download_paths','').splitlines()]
    for raw in candidates:
        if not raw or '://' in raw:
            continue
        path = Path(raw).resolve()
        if path.suffix.casefold() not in IMAGE_SUFFIXES or path.name.casefold().startswith(('thumb','embedded')):
            continue
        if not any(path.is_relative_to(root.resolve()) for root in roots):
            continue
        if path.is_file() and 0 < path.stat().st_size <= 20*1024*1024:
            return path
    raise ValueError('original_image_not_downloaded')

