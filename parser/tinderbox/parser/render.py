"""Render content blocks into human-readable text for FTS indexing.

Rule: concatenate text blocks and voice_note blocks (their text field) in source order.
Separator: double newline. Everything else is skipped (preserved in raw_content).
"""


def render_blocks(blocks: list[dict]) -> str:
    parts = []
    for block in blocks:
        btype = block.get("type")
        if btype == "text":
            text = block.get("text", "")
            if text:
                parts.append(text)
        elif btype == "voice_note":
            text = block.get("text", "")
            if text:
                parts.append(text)
    return "\n\n".join(parts)
