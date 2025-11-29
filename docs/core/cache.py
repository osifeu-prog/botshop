from functools import lru_cache
from pathlib import Path
from typing import Optional

from .logging import logger

BASE_DIR = Path(__file__).resolve().parent.parent
MESSAGES_FILE = BASE_DIR / "messages" / "messages.md"


def load_message_block(block_name: str, fallback: str = "") -> str:
    """Load a block from messages.md between [block:name] ... [/block]."""
    if not MESSAGES_FILE.exists():
        logger.warning("messages.md not found, using fallback for %s", block_name)
        return fallback

    text = MESSAGES_FILE.read_text(encoding="utf-8")
    start_tag = f"[block:{block_name}]"
    end_tag = "[/block]"
    start = text.find(start_tag)
    if start == -1:
        return fallback
    start += len(start_tag)
    end = text.find(end_tag, start)
    if end == -1:
        end = len(text)
    block = text[start:end].strip()
    return block or fallback


@lru_cache(maxsize=100)
def load_message_block_cached(block_name: str, file_mtime: float) -> Optional[str]:
    """Cached wrapper for message loading keyed by file mtime."""
    return load_message_block(block_name, "")


def get_cached_message(block_name: str, fallback: str = "") -> str:
    try:
        mtime = MESSAGES_FILE.stat().st_mtime if MESSAGES_FILE.exists() else 0
        result = load_message_block_cached(block_name, mtime)
        return result or fallback
    except Exception as e:  # pragma: no cover - defensive
        logger.error("Cache error for %s: %s", block_name, e)
        return load_message_block(block_name, fallback)
