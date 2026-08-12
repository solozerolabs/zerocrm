"""Transcript-source seam. Google Meet arrives already transcribed (structured
entries from the Meet API), so the only real source this slice is 'google'.
Audio-file transcription (Google Voice recordings) is deferred to an OSS Whisper
adapter — the seam is explicit so the later swap is not a silent empty path."""

from __future__ import annotations


def transcribe_audio(uri: str) -> str:  # noqa: ARG001
    # ponytail: the Whisper adapter lands with the phone (source=voice) path.
    raise NotImplementedError(
        "audio transcription (Whisper) not built yet; Meet arrives pre-transcribed"
    )
