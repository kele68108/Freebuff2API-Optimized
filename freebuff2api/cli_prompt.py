"""Extract the Buffy system prompt embedded in the installed Freebuff CLI binary.

The upstream free_mode_cli_required gate fingerprints chat requests on the
CLI's system prompt. This mirrors the Go implementation in
freebuff-go/watchdog.go: it streams the large (~124MB) Bun bundle in 1MiB
chunks so the prompt is located without loading the whole binary into memory.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_CLI_PROMPT_MARKER = b"You are Buffy, the strategic coding assistant"
_CLI_PROMPT_END_MARK = b"See freebuff.com for more information about the product."
_CHUNK_SIZE = 1 << 20  # 1 MiB
_REGION_SIZE = 64 * 1024  # enough to reach the end marker after the start marker


def _find_prompt_offset(path: Path) -> int:
    """Return the byte offset of the prompt start marker, or -1 if not found."""
    size = path.stat().st_size
    overlap = len(_CLI_PROMPT_MARKER) + 32
    window = bytearray()
    window_start = 0
    position = 0
    with path.open("rb") as handle:
        while position < size:
            chunk = handle.read(_CHUNK_SIZE)
            if not chunk:
                break
            window.extend(chunk)
            index = window.find(_CLI_PROMPT_MARKER)
            if index >= 0:
                return window_start + index
            if len(window) > overlap:
                tail = window[-overlap:]
                window_start += len(window) - len(tail)
                window = bytearray(tail)
            position += len(chunk)
    return -1


def extract_cli_system_prompt(cli_path: str | None) -> str:
    """Extract the full Buffy system prompt from the CLI binary.

    Returns "" if the binary is missing or the prompt cannot be located or
    fails sanity checks (callers fall back to a minimal Buffy system line).
    """
    if not cli_path:
        return ""
    path = Path(cli_path)
    if not path.is_file():
        return ""
    try:
        offset = _find_prompt_offset(path)
        if offset < 0:
            return ""
        with path.open("rb") as handle:
            handle.seek(offset)
            region = handle.read(_REGION_SIZE)
        end = region.find(_CLI_PROMPT_END_MARK)
        if end < 0:
            return ""
        prompt = region[: end + len(_CLI_PROMPT_END_MARK)].decode(
            "utf-8", errors="replace"
        )
        if not (800 <= len(prompt) <= 12000):
            return ""
        if "You are Buffy" not in prompt or "freebuff.com" not in prompt:
            return ""
        return prompt
    except OSError:
        return ""


@lru_cache(maxsize=1)
def cli_system_prompt(cli_path: str | None) -> str:
    """Cached extraction — avoids re-reading the 124MB binary per chat request."""
    return extract_cli_system_prompt(cli_path)
