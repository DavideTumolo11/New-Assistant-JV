"""
Persistent-stream voice listener: wake-word detection + utterance recording
sharing a single always-open microphone stream.

Keeping ONE microphone stream open for the whole session (instead of
closing/reopening it between "waiting for the wake word" and "recording the
command") removes the open/close latency that caused two problems:
  1. the first words of a command being lost right after "hey jarvis"
  2. a noticeable delay before JARVIS started listening for "hey jarvis" again
     after finishing a reply.

openWakeWord needs int16 audio at 16 kHz in 1280-sample (80 ms) chunks, so the
stream is opened in that format. For Whisper, recorded int16 audio is converted
to float32 before being returned.
"""
import queue
from typing import Callable, Optional

import numpy as np
import sounddevice as sd

SAMPLE_RATE        = 16000
CHANNELS           = 1
FRAME_SIZE         = 1280     # 80 ms at 16 kHz — openWakeWord's expected chunk
_QUEUE_MAX         = 100      # ~8 s of audio; oldest dropped when full

SILENCE_THRESHOLD  = 0.02     # RMS (float32 scale) below which audio is silence
SILENCE_DURATION_S = 1.0      # seconds of silence that end an utterance
MAX_RECORD_S       = 30.0     # safety cap


class VoiceListener:
    """Always-on mic: one persistent stream shared by wake detection + recording."""

    def __init__(self, wake_model: str = "hey_jarvis", threshold: float = 0.5):
        from openwakeword.model import Model
        self._model = Model(
            wakeword_models=[wake_model],
            inference_framework="onnx",
        )
        self._threshold = threshold

        self._q: "queue.Queue" = queue.Queue(maxsize=_QUEUE_MAX)
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=FRAME_SIZE,
            callback=self._callback,
        )
        self._stream.start()

    def _callback(self, indata, frames, time_info, status):
        try:
            self._q.put_nowait(indata.copy())
        except queue.Full:
            try:
                self._q.get_nowait()          # drop oldest frame
                self._q.put_nowait(indata.copy())
            except (queue.Empty, queue.Full):
                pass

    def _drain(self) -> None:
        """Discard buffered audio (e.g. JARVIS's own voice from the last reply)."""
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break

    def wait_for_wake(self, should_stop: Optional[Callable[[], bool]] = None) -> bool:
        """
        Blocking. Discards stale audio, then listens on the live stream until the
        wake word is heard. Returns True when heard, or False if should_stop()
        becomes True first.
        """
        try:
            self._model.reset()
        except Exception:
            pass
        self._drain()

        while True:
            if should_stop is not None and should_stop():
                return False
            try:
                block = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            audio = block.flatten()                 # int16, 1-D
            try:
                prediction = self._model.predict(audio)
            except Exception:
                continue
            score = max(prediction.values()) if prediction else 0.0
            if score >= self._threshold:
                return True

    def record_utterance(
        self,
        should_stop:  Optional[Callable[[], bool]] = None,
        start_timeout_s: Optional[float] = None,
        drain_first: bool = False,
    ) -> np.ndarray:
        """
        Records one spoken utterance from the live stream.

        start_timeout_s:
            If set, and the user does not START speaking within this many
            seconds, return an empty array (used for the "keep listening for a
            few seconds after a reply" window). If None, wait indefinitely for
            speech to begin (used right after the wake word).
        drain_first:
            If True, discard any buffered audio before listening — used for the
            follow-up window so JARVIS's own just-finished reply isn't recorded.
            Right after the wake word this is False, so the command already
            flowing in is not lost.

        Returns float32 mono 16 kHz audio for Whisper, or an empty array if
        nothing was captured (silence / timeout / stopped).
        """
        if drain_first:
            self._drain()

        frames: list[np.ndarray] = []
        speaking = False
        silence_blocks = 0
        silence_needed = int(SILENCE_DURATION_S * SAMPLE_RATE / FRAME_SIZE)
        max_blocks = int(MAX_RECORD_S * SAMPLE_RATE / FRAME_SIZE)
        total_blocks = 0

        start_deadline = None
        if start_timeout_s is not None:
            import time as _time
            start_deadline = _time.monotonic() + start_timeout_s

        while True:
            if should_stop is not None and should_stop():
                return np.zeros(0, dtype=np.float32)

            # If waiting for speech to begin and the start window elapses, give up.
            if start_deadline is not None and not speaking:
                import time as _time
                if _time.monotonic() > start_deadline:
                    return np.zeros(0, dtype=np.float32)

            try:
                block = self._q.get(timeout=0.2)
            except queue.Empty:
                continue

            f32 = block.flatten().astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(f32 ** 2) + 1e-12))
            total_blocks += 1

            if rms > SILENCE_THRESHOLD:
                speaking = True
                silence_blocks = 0
                frames.append(f32)
            elif speaking:
                silence_blocks += 1
                frames.append(f32)
                if silence_blocks >= silence_needed:
                    break
            # else: still waiting for speech to begin — discard

            if total_blocks >= max_blocks:
                break

        if not frames:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(frames, axis=0).flatten()

    def close(self) -> None:
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass