"""
Microphone recorder with simple energy-based voice activity detection (VAD).

Waits silently until the user starts speaking, then records until a period
of silence is detected. Returns the captured audio as a float32 mono numpy
array at 16 kHz, ready to be passed to WhisperSTT.transcribe().

An optional should_stop() callable is checked continuously — if it returns
True (e.g. the microphone was muted from the UI), recording aborts
immediately and an empty array is returned.
"""
import queue
from typing import Callable, Optional

import numpy as np
import sounddevice as sd

SAMPLE_RATE          = 16000
CHANNELS             = 1
BLOCK_SIZE           = 1024
SILENCE_THRESHOLD    = 0.02   # RMS level below which audio is considered silence
SILENCE_DURATION_S   = 1.0    # seconds of silence that end an utterance
MAX_RECORD_S         = 30.0   # safety cap so a stuck mic can't record forever


def record_utterance(should_stop: Optional[Callable[[], bool]] = None) -> np.ndarray:
    """
    Blocking call. Waits for the user to start speaking, records until
    they stop (silence), and returns the captured audio.
    Returns an empty array if nothing was captured, or if should_stop()
    becomes True at any point (e.g. microphone was muted).
    """
    q: queue.Queue = queue.Queue()

    def callback(indata, frames, time_info, status):
        q.put(indata.copy())

    frames: list[np.ndarray] = []
    speaking = False
    silence_blocks = 0
    silence_blocks_needed = int(SILENCE_DURATION_S * SAMPLE_RATE / BLOCK_SIZE)
    max_blocks = int(MAX_RECORD_S * SAMPLE_RATE / BLOCK_SIZE)
    total_blocks = 0

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="float32",
        blocksize=BLOCK_SIZE,
        callback=callback,
    ):
        while True:
            if should_stop is not None and should_stop():
                return np.zeros(0, dtype=np.float32)

            try:
                block = q.get(timeout=0.2)
            except queue.Empty:
                continue

            rms = float(np.sqrt(np.mean(block ** 2) + 1e-12))
            total_blocks += 1

            if rms > SILENCE_THRESHOLD:
                speaking = True
                silence_blocks = 0
                frames.append(block)
            elif speaking:
                silence_blocks += 1
                frames.append(block)
                if silence_blocks >= silence_blocks_needed:
                    break
            # else: still waiting for the user to start speaking — discard

            if total_blocks >= max_blocks:
                break

    if not frames:
        return np.zeros(0, dtype=np.float32)

    return np.concatenate(frames, axis=0).flatten()