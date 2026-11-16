"""Freeze provider-specific narration text and bounded requests for new jobs."""

import re
import textwrap

from .text import kokoro_input, narration_input


STYLE = "Thoughtful, measured narration; restrained expression, natural phrasing, clear paragraph transitions."


def elevenlabs_pause(previous, current):
    if previous["type"] == "title" or (
            current["type"] == "heading" and current.get("level", 2) <= 2):
        return "[long pause]"
    if previous["type"] == current["type"] == "list_item":
        return "[short pause]"
    return "[pause]"


def render(blocks, provider, previous_block=None):
    if provider == "kokoro":
        return kokoro_input(blocks)
    if provider == "elevenlabs":
        parts = ["[Thoughtful, measured narration]"]
        for block in blocks:
            if previous_block is not None:
                parts.append(elevenlabs_pause(previous_block, block))
            parts.append(block["text"])
            previous_block = block
        return "\n\n".join(parts)
    if provider == "gemini":
        return narration_input(blocks, "<short pause>")
    raise ValueError("Unknown speech provider")


def split_block(block, limit=2800):
    """Prefer sentence boundaries; exceptionally long sentences split at words."""
    pieces = []
    current = ""
    for sentence in re.split(r"(?<=[.!?])\s+", block["text"]):
        for fragment in textwrap.wrap(sentence, width=limit, break_on_hyphens=False,
                                      replace_whitespace=False):
            candidate = (current + " " + fragment).strip()
            if len(candidate) > limit:
                pieces.append(block | {"text": current})
                current = fragment
            else:
                current = candidate
    if current:
        pieces.append(block | {"text": current})
    return pieces


def neighboring_text(blocks, *, tail=False, limit=500):
    """Use up to two source sentences, falling back to whole words at the cap."""
    text = " ".join(block["text"] for block in blocks)
    sentences = re.split(r"(?<=[.!?])\s+", text)
    excerpt = " ".join(sentences[-2:] if tail else sentences[:2])
    if len(excerpt) <= limit:
        return excerpt
    if tail:
        excerpt = excerpt[-limit:]
        return excerpt.split(" ", 1)[-1] if " " in excerpt else ""
    excerpt = excerpt[:limit]
    return excerpt.rsplit(" ", 1)[0] if " " in excerpt else ""


def speech_snapshot(blocks, provider):
    blocks = [block for block in blocks if block["text"].strip()]
    full_input = render(blocks, provider)
    if provider == "kokoro":
        return {"provider": provider, "speech_input": full_input}
    chunks = []
    chunk_blocks = []
    current = []
    previous_block = None
    for block in blocks:
        for piece in split_block(block):
            if current and len(render(current + [piece], provider, previous_block)) > 4000:
                chunks.append(render(current, provider, previous_block))
                chunk_blocks.append(current)
                previous_block = current[-1]
                current = []
            current.append(piece)
    if current:
        chunks.append(render(current, provider, previous_block))
        chunk_blocks.append(current)
    options = ({"stability": 0.5, "similarity_boost": 0.75} if provider == "elevenlabs"
               else {"style": STYLE})
    snapshot = {"provider": provider, "speech_input": full_input,
                "speech_chunks": chunks, "speech_options": options}
    if provider == "elevenlabs":
        snapshot["speech_seed"] = 12345
        contexts = []
        for index in range(len(chunk_blocks)):
            context = {}
            if index:
                context["previous_text"] = neighboring_text(chunk_blocks[index - 1], tail=True)
            if index + 1 < len(chunk_blocks):
                context["next_text"] = neighboring_text(chunk_blocks[index + 1])
            contexts.append({key: value for key, value in context.items() if value})
        snapshot["speech_contexts"] = contexts
    return snapshot
