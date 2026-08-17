"""
Document Layout (Phase 4).

Turns flat document text into a structured block list that both PDF engines
render from, so a resume looks the same whichever engine produced it.

Structure is inferred the same way the parser detects it:
- The first non-empty line is the person's name
- Lines before the first section heading are contact details
- Recognized headings (Experience, Education, …) open a section
- Runs of "- " lines become bullet lists
"""

import logging
from dataclasses import dataclass
from typing import List, Literal

from job_agent.services.document_parser import DocumentParser

logger = logging.getLogger(__name__)

BULLET_PREFIXES = ("-", "•", "*", "·", "–", "—")

BlockKind = Literal["name", "contact", "heading", "paragraph", "bullets"]


@dataclass
class Block:
    """One renderable element of a document."""

    kind: BlockKind
    text: str = ""
    items: List[str] = None  # Populated for "bullets"

    def __post_init__(self):
        if self.items is None:
            self.items = []


def build_blocks(text: str) -> List[Block]:
    """
    Convert document text into renderable blocks.

    Args:
        text: Document text

    Returns:
        Ordered blocks

    Raises:
        ValueError: If the text contains nothing renderable
    """
    if not text or not text.strip():
        raise ValueError("Cannot render an empty document")

    blocks: List[Block] = []
    bullet_buffer: List[str] = []

    name_seen = False
    in_header = True

    def flush_bullets() -> None:
        if bullet_buffer:
            blocks.append(Block(kind="bullets", items=list(bullet_buffer)))
            bullet_buffer.clear()

    for raw_line in text.split("\n"):
        line = raw_line.strip()

        if not line:
            flush_bullets()
            continue

        heading = DocumentParser.is_heading(line)

        if heading:
            flush_bullets()
            in_header = False
            blocks.append(Block(kind="heading", text=line.strip(":")))
            continue

        if line[0] in BULLET_PREFIXES:
            bullet_buffer.append(line[1:].strip())
            continue

        flush_bullets()

        if not name_seen:
            blocks.append(Block(kind="name", text=line))
            name_seen = True
            continue

        blocks.append(Block(kind="contact" if in_header else "paragraph", text=line))

    flush_bullets()

    if not blocks:
        raise ValueError("Document produced no renderable content")

    return blocks
