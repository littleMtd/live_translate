import wave

import numpy as np

from utils.audio import write_wav


def test_write_wav_roundtrip(tmp_path):
    audio = np.array([0.0, 0.5, -0.5, 1.0, -1.0], dtype=np.float32)
    path = tmp_path / "x.wav"
    write_wav(path, audio, 16000)

    with wave.open(str(path), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 16000
        assert handle.getnframes() == 5
        decoded = np.frombuffer(handle.readframes(5), dtype="<i2")

    assert decoded[0] == 0
    assert decoded[3] == 32767   # +1.0 -> full scale
    assert decoded[4] == -32767  # -1.0 -> full scale


def test_write_wav_clips_out_of_range(tmp_path):
    path = tmp_path / "y.wav"
    write_wav(path, np.array([2.0, -2.0], dtype=np.float32), 16000)

    with wave.open(str(path), "rb") as handle:
        decoded = np.frombuffer(handle.readframes(2), dtype="<i2")

    assert decoded[0] == 32767
    assert decoded[1] == -32767


def test_write_wav_creates_parent_dirs(tmp_path):
    path = tmp_path / "sub" / "dir" / "z.wav"
    write_wav(path, np.zeros(3, dtype=np.float32), 16000)
    assert path.exists()
