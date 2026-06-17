"""
Audio format utilities — PCM ↔ WAV conversion.
"""

import struct


def pcm16_to_wav(
    pcm_data: bytes,
    sample_rate: int = 16000,
    channels: int = 1,
    bits_per_sample: int = 16,
) -> bytes:
    """Prepend a standard 44-byte WAV/RIFF header to raw PCM16 data."""
    data_size = len(pcm_data)
    byte_rate = sample_rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8

    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        data_size + 36,       # ChunkSize  (file size − 8)
        b"WAVE",
        b"fmt ",
        16,                   # Subchunk1Size (PCM)
        1,                    # AudioFormat   (1 = PCM)
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        b"data",
        data_size,            # Subchunk2Size
    )
    return header + pcm_data
