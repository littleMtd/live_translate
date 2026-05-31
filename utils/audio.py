import wave
from pathlib import Path

import numpy as np


def rms(audio: np.ndarray) -> float:
    if audio.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(audio ** 2)))


def write_wav(path: str | Path, audio: np.ndarray, sample_rate: int) -> None:
    """Write mono float32 audio to a 16-bit PCM WAV (stdlib only, no deps).

    Used by the collection-mode STT audio dump; float samples are clipped to
    [-1, 1] before scaling so out-of-range values don't wrap around.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0)
    pcm16 = (samples * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes(pcm16.tobytes())
