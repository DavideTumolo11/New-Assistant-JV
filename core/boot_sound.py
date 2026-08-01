"""
Synthetic boot-up chime for JARVIS.

Generates a longer sequence of clean electronic tones entirely in code
(pure sine waves) — no audio file, no copyrighted material. Played once
at startup, right before the spoken greeting.
"""
import numpy as np
import sounddevice as sd

SAMPLE_RATE = 24000


def _tone(freq: float, duration_s: float, volume: float = 0.35) -> np.ndarray:
    """One short tone with a soft fade-in/fade-out (avoids clicks)."""
    t = np.linspace(0, duration_s, int(SAMPLE_RATE * duration_s), endpoint=False)
    fade = np.minimum(1.0, np.minimum(t / 0.01, (duration_s - t) / 0.03))
    fade = np.clip(fade, 0.0, 1.0)
    wave = np.sin(2 * np.pi * freq * t) * volume * fade
    return wave.astype(np.float32)


def _chord(freqs: list[float], duration_s: float, volume: float = 0.35) -> np.ndarray:
    """Several tones played together at once, mixed into a single chord."""
    mixed = None
    for f in freqs:
        w = _tone(f, duration_s, volume=1.0)
        mixed = w if mixed is None else mixed + w
    mixed = mixed / len(freqs)          # keep total loudness in check
    return (mixed * volume).astype(np.float32)


def play_boot_sequence() -> None:
    """Plays a longer, layered synthetic startup chime. Blocking call."""
    gap      = np.zeros(int(SAMPLE_RATE * 0.05), dtype=np.float32)
    tiny_gap = np.zeros(int(SAMPLE_RATE * 0.02), dtype=np.float32)
    long_gap = np.zeros(int(SAMPLE_RATE * 0.15), dtype=np.float32)

    # Stage 1 — rising arpeggio: systems coming online, one by one
    arpeggio = np.concatenate([
        _tone(220.0, 0.09), gap,
        _tone(277.0, 0.09), gap,
        _tone(330.0, 0.09), gap,
        _tone(415.0, 0.09), gap,
        _tone(495.0, 0.09), gap,
        _tone(660.0, 0.09), gap,
        _tone(770.0, 0.09), gap,
        _tone(880.0, 0.11), gap,
    ])

    # Stage 2 — quick alternating "processing" pulses, like scanning subsystems
    processing = np.concatenate([
        _tone(660.0, 0.045), tiny_gap,
        _tone(990.0, 0.045), tiny_gap,
        _tone(660.0, 0.045), tiny_gap,
        _tone(990.0, 0.045), tiny_gap,
        _tone(660.0, 0.045), tiny_gap,
        _tone(990.0, 0.045), tiny_gap,
        _tone(660.0, 0.045), tiny_gap,
        _tone(990.0, 0.06),
    ])

    # Stage 3 — two-part closing chord: confirmation, then sustained "ready" tone
    chord_a = _chord([550.0, 660.0, 880.0], duration_s=0.30, volume=0.4)
    chord_b = _chord([660.0, 880.0, 1100.0, 1320.0], duration_s=0.65, volume=0.45)

    sequence = np.concatenate([
        arpeggio, long_gap,
        processing, long_gap,
        chord_a, gap,
        chord_b,
    ])
    sd.play(sequence, SAMPLE_RATE)
    sd.wait()