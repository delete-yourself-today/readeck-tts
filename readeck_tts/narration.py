"""Freeze provider-specific narration text and bounded requests for new jobs."""

import re
import textwrap

from .text import kokoro_input, narration_input


STYLE = "Thoughtful, measured narration; restrained expression, natural phrasing, clear paragraph transitions."


def render(blocks, provider):
    if provider == "kokoro":
        return kokoro_input(blocks)
    if provider == "elevenlabs":
        return "[Thoughtful, measured narration]\n\n" + narration_input(blocks, "[short pause]")
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


def speech_snapshot(blocks, provider):
    full_input = render(blocks, provider)
    if provider == "kokoro":
        return {"provider": provider, "speech_input": full_input}
    chunks = []
    current = []
    for block in blocks:
        for piece in split_block(block):
            if current and len(render(current + [piece], provider)) > 4000:
                chunks.append(render(current, provider))
                current = []
            current.append(piece)
    if current:
        chunks.append(render(current, provider))
    options = ({"stability": 0.5, "similarity_boost": 0.75} if provider == "elevenlabs"
               else {"style": STYLE})
    snapshot = {"provider": provider, "speech_input": full_input,
                "speech_chunks": chunks, "speech_options": options}
    if provider == "elevenlabs":
        snapshot["speech_seed"] = 12345
    return snapshot
