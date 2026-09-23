"""Result artifact metadata: legacy pass-through and enriched fail-closed validation.

The Controller never stores artifact files. It transports metadata and, for
enriched text artifacts, a bounded ``inline_text`` copy of the content.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

MAX_INLINE_TEXT_BYTES = 32 * 1024
ENRICHED_KEYS = frozenset({"sha256", "bytes", "media_type", "inline_text"})
_ALLOWED_KEYS = ENRICHED_KEYS | {"path"}
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MEDIA_TYPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}")
_DRIVE = re.compile(r"[A-Za-z]:")


def is_enriched(artifact: Any) -> bool:
    """An artifact opts into strict validation by declaring any enriched key."""
    return isinstance(artifact, dict) and not ENRICHED_KEYS.isdisjoint(artifact)


def _validate_path(path: Any) -> None:
    if not isinstance(path, str) or not path or "\x00" in path:
        raise ValueError("artifact path must be a non-empty string")
    if path.startswith(("/", "\\")) or _DRIVE.match(path):
        raise ValueError(f"artifact path must be relative: {path!r}")
    if "\\" in path:
        raise ValueError(f"artifact path must not contain backslashes: {path!r}")
    segments = path.split("/")
    if ".." in segments:
        raise ValueError(f"artifact path must not contain '..': {path!r}")
    if any(segment in {"", "."} for segment in segments):
        raise ValueError(f"artifact path has empty or '.' segments: {path!r}")


def _validate_enriched(artifact: dict[str, Any], hashes: dict[str, Any]) -> None:
    unknown = set(artifact) - _ALLOWED_KEYS
    if unknown:
        raise ValueError(f"enriched artifact has unknown fields: {sorted(unknown)}")
    path = artifact.get("path")
    _validate_path(path)
    digest = artifact.get("sha256")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise ValueError(f"artifact {path!r} sha256 must be 64 lowercase hex characters")
    size = artifact.get("bytes")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise ValueError(f"artifact {path!r} bytes must be an integer >= 0")
    if "media_type" in artifact:
        media_type = artifact["media_type"]
        if not isinstance(media_type, str) or not _MEDIA_TYPE.fullmatch(media_type):
            raise ValueError(f"artifact {path!r} media_type is invalid")
    if "inline_text" in artifact:
        text = artifact["inline_text"]
        if not isinstance(text, str):
            raise ValueError(f"artifact {path!r} inline_text must be a string")
        try:
            encoded = text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError(f"artifact {path!r} inline_text is not valid UTF-8") from exc
        if len(encoded) > MAX_INLINE_TEXT_BYTES:
            raise ValueError(f"artifact {path!r} inline_text exceeds {MAX_INLINE_TEXT_BYTES} bytes")
        # inline_text must be exactly the artifact, never a truncated copy.
        if size != len(encoded):
            raise ValueError(f"artifact {path!r} bytes does not match inline_text")
        if digest != hashlib.sha256(encoded).hexdigest():
            raise ValueError(f"artifact {path!r} sha256 does not match inline_text")
    if hashes.get(path) != digest:
        raise ValueError(f"artifact {path!r} sha256 does not match hashes[path]")


def validate_artifacts(artifacts: Any, hashes: Any) -> None:
    """Validate enriched artifacts; legacy artifacts and hash entries pass unchanged."""
    if not isinstance(artifacts, list) or not isinstance(hashes, dict):
        raise ValueError("artifacts must be a list and hashes an object")
    seen: set[str] = set()
    for artifact in artifacts:
        if not is_enriched(artifact):
            continue
        _validate_enriched(artifact, hashes)
        if artifact["path"] in seen:
            raise ValueError(f"duplicate enriched artifact path: {artifact['path']!r}")
        seen.add(artifact["path"])


def text_artifact(path: str, text: str, *, media_type: str | None = None, inline: bool = False) -> dict[str, Any]:
    """Build an enriched artifact describing ``text``; ``inline`` embeds it verbatim."""
    encoded = text.encode("utf-8")
    artifact: dict[str, Any] = {"path": path, "sha256": hashlib.sha256(encoded).hexdigest(), "bytes": len(encoded)}
    if media_type is not None:
        artifact["media_type"] = media_type
    if inline:
        if len(encoded) > MAX_INLINE_TEXT_BYTES:
            raise ValueError(f"inline_text exceeds {MAX_INLINE_TEXT_BYTES} bytes; publish a separate summary artifact")
        artifact["inline_text"] = text
    _validate_enriched(artifact, {path: artifact["sha256"]})
    return artifact
