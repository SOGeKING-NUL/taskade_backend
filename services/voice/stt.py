"""
Sarvam AI Speech-to-Text service — SDK implementation.
"""

import asyncio
import base64
import io
import wave
import logging
from sarvamai import AsyncSarvamAI

from core.config import settings

logger = logging.getLogger(__name__)

def _pcm_to_wav_base64(pcm_bytes: bytes, sample_rate: int = 16000) -> str:
    """Wrap raw PCM-16 bytes in a WAV container and base64 encode."""
    with io.BytesIO() as wav_io:
        with wave.open(wav_io, 'wb') as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)  # 16-bit
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm_bytes)
        wav_io.seek(0)
        return base64.b64encode(wav_io.read()).decode('utf-8')


class SarvamStreamingSTT:
    """Fast STT using Sarvam AI SDK."""

    def __init__(self) -> None:
        self.api_key = settings.SARVAM_API_KEY
        self.model = settings.SARVAM_STT_MODEL
        self.language = settings.SARVAM_STT_LANGUAGE
        self.mode = settings.SARVAM_STT_MODE
        self._client: AsyncSarvamAI | None = None
        self._audio_buffer = bytearray()

    async def connect(self) -> None:
        """Initialize the SDK client."""
        if self._client is None:
            self._client = AsyncSarvamAI(api_subscription_key=self.api_key)
            logger.info("Sarvam SDK initialized")

    async def close(self) -> None:
        """Close the SDK client."""
        self._client = None
        logger.info("Sarvam SDK closed")

    async def stream_audio(self, pcm_chunk: bytes) -> None:
        """Buffer incoming PCM-16 audio frames."""
        self._audio_buffer.extend(pcm_chunk)

    async def end_utterance(self) -> str:
        """
        Send the buffered audio to Sarvam using the SDK for immediate transcription.
        """
        if self._client is None:
            await self.connect()

        if not self._audio_buffer:
            return ""

        audio_bytes = bytes(self._audio_buffer)
        self._audio_buffer.clear()

        # Convert to WAV base64
        audio_data = _pcm_to_wav_base64(audio_bytes, sample_rate=16000)

        try:
            async with self._client.speech_to_text_streaming.connect(
                model=self.model,
                mode=self.mode,
                language_code=self.language,
                high_vad_sensitivity=True
            ) as ws:
                await ws.transcribe(audio=audio_data)
                response = await ws.recv()

                # Extract transcript based on SDK response format
                transcript = ""
                if hasattr(response, "data") and hasattr(response.data, "transcript"):
                    transcript = response.data.transcript
                elif hasattr(response, "transcript"):
                    transcript = response.transcript
                elif isinstance(response, dict):
                    if "data" in response and isinstance(response["data"], dict):
                        transcript = response["data"].get("transcript", "")
                    else:
                        transcript = response.get("transcript", "")
                else:
                    transcript = str(response)

                transcript = transcript.strip() if transcript else ""
                logger.info("STT → %s", transcript)
                return transcript

        except Exception as exc:
            logger.error("Sarvam SDK STT failed: %s", exc)
            return ""
