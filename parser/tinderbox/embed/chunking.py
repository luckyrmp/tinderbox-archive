"""Chunking strategy for messages and artifacts (stage 2 §3.2).

Approximation: mxbai-embed-large accepts up to 512 tokens. ~4 chars per English
token gives a soft target of 2048 chars per chunk. We bias slightly under
(1500-1800) to leave headroom for tokenization variance.

Messages: one embedding each. Truncate over the limit head+tail (1500 chars
head, 500 chars tail) — preserves opening context AND closing context, which
matters for short/medium messages where "what was the conclusion" is at the end.

Artifacts: two-tier per stage 2 §3.2.
  chunk_index = -1: summary embedding. Built from `title + first 1000 + last 1000`
                    chars of content. The "find this artifact" hit.
  chunk_index = 0..n-1: body chunks of 1500 chars with 200 char overlap.
                        The "find this passage within this artifact" hits.
"""

from dataclasses import dataclass


# mxbai-embed-large hard input limit is 512 tokens. At ~3 chars/token in practice
# (English prose with punctuation/markdown averages denser than the 4 chars/token
# textbook number), 1500 chars is the safe ceiling per chunk.
SOFT_INPUT_LIMIT_CHARS = 1400
MESSAGE_HEAD_CHARS = 1000
MESSAGE_TAIL_CHARS = 400
ARTIFACT_CHUNK_CHARS = 1200
ARTIFACT_CHUNK_OVERLAP_CHARS = 150
ARTIFACT_SUMMARY_HEAD_CHARS = 700
ARTIFACT_SUMMARY_TAIL_CHARS = 700


@dataclass(frozen=True)
class Chunk:
    """A piece of text destined for embedding."""
    chunk_index: int   # -1 for artifact summary; 0..n for body chunks
    chunk_text: str


def chunk_message(content: str) -> list[Chunk]:
    """One chunk per message. Empty messages are skipped (caller filters)."""
    text = (content or "").strip()
    if not text:
        return []
    if len(text) <= SOFT_INPUT_LIMIT_CHARS:
        return [Chunk(chunk_index=0, chunk_text=text)]

    # Head + ellipsis + tail.
    head = text[:MESSAGE_HEAD_CHARS]
    tail = text[-MESSAGE_TAIL_CHARS:]
    truncated = f"{head}\n\n[... truncated for embedding ...]\n\n{tail}"
    return [Chunk(chunk_index=0, chunk_text=truncated)]


def chunk_artifact(title: str | None, content: str) -> list[Chunk]:
    """Summary chunk (-1) plus body chunks (0..n) with overlap."""
    text = (content or "").strip()
    chunks: list[Chunk] = []

    # Always emit a summary chunk if we have either title or content.
    title_str = (title or "").strip()
    if title_str or text:
        head = text[:ARTIFACT_SUMMARY_HEAD_CHARS]
        tail_start = max(len(text) - ARTIFACT_SUMMARY_TAIL_CHARS,
                         ARTIFACT_SUMMARY_HEAD_CHARS)
        tail = text[tail_start:] if tail_start < len(text) else ""
        # If the head already covers the whole content, no need for a separator.
        if tail and tail != head:
            summary_body = f"{head}\n\n[...]\n\n{tail}"
        else:
            summary_body = head
        if title_str:
            summary_text = f"{title_str}\n\n{summary_body}"
        else:
            summary_text = summary_body
        chunks.append(Chunk(chunk_index=-1, chunk_text=summary_text))

    # Body chunks. Even if the artifact is small enough to fit in summary,
    # we still emit one body chunk so retrieval has both the "find this artifact"
    # signal and the "find this passage" signal at least once.
    if not text:
        return chunks

    if len(text) <= ARTIFACT_CHUNK_CHARS:
        chunks.append(Chunk(chunk_index=0, chunk_text=text))
        return chunks

    # Sliding window. start advances by (chunk - overlap).
    step = ARTIFACT_CHUNK_CHARS - ARTIFACT_CHUNK_OVERLAP_CHARS
    if step <= 0:
        raise ValueError("ARTIFACT_CHUNK_OVERLAP_CHARS must be < ARTIFACT_CHUNK_CHARS")
    body_idx = 0
    pos = 0
    while pos < len(text):
        end = pos + ARTIFACT_CHUNK_CHARS
        chunk_text = text[pos:end]
        chunks.append(Chunk(chunk_index=body_idx, chunk_text=chunk_text))
        body_idx += 1
        if end >= len(text):
            break
        pos += step

    return chunks


def message_should_skip_raw(raw_content: list[dict]) -> bool:
    """Stage 2 §3.3 + §1.2: exact-position-0 check for the truncation marker.

    Returns True if this message has truncated raw_content. We still embed
    `messages.content` (the rendered text is preserved in full), but we don't
    do anything that would require the raw blocks.

    Stage 1 ingest stores embeddings against `messages.content`, not raw_content,
    so this function exists more as a sentinel for stage 4 enrichment that may
    iterate raw blocks. The embed pass just calls chunk_message(content).
    """
    if not raw_content:
        return False
    first = raw_content[0]
    if not isinstance(first, dict):
        return False
    # Exact-position-0 only. Loose matching is forbidden.
    return "_tinderbox_truncation_metadata" in first
