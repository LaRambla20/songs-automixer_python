import threading
import numpy as np
import sounddevice as sd
from typing import Optional

from .audio_engine import SAMPLE_RATE, CHANNELS, BLOCK_SIZE


def _resample(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Cheap per-channel linear resample (np.interp, no scipy). Quality is
    irrelevant for a monitor bus — this only runs when the cue device refuses
    the engine's native 44100 Hz and we fall back to its own rate."""
    if src_rate == dst_rate or audio.shape[0] == 0:
        return audio
    n_src = audio.shape[0]
    n_dst = int(round(n_src * dst_rate / src_rate))
    if n_dst <= 0:
        return np.zeros((0, audio.shape[1]), dtype=np.float32)
    xp = np.arange(n_src)
    src_idx = np.linspace(0.0, n_src - 1, n_dst)
    out = np.empty((n_dst, audio.shape[1]), dtype=np.float32)
    for ch in range(audio.shape[1]):
        out[:, ch] = np.interp(src_idx, xp, audio[:, ch])
    return out


class CuePlayer:
    """Independent pre-listen ("PFL") output on a second device — auditioning the
    queued NEXT track in the DJ's headphones while the master AudioEngine keeps
    playing on the speakers.

    Deliberately decoupled from AudioEngine: its own stream, lock, buffer and
    playhead, no MIXING state machine, and it NEVER shares state with the engine.
    All PortAudio failures are swallowed internally (the player marks itself
    `is_dead` and goes silent) so a flaky USB-C/Bluetooth device can never crash
    or stall the master playback.

    Buffers handed to play() are at the engine's SAMPLE_RATE (44100); if the
    device only accepts another rate the buffer is resampled to `_play_rate` once
    at play() time. Position/duration are reported in seconds relative to the
    start of the supplied buffer (which the app slices at the raw cue point, so
    0.0s == the drop point)."""

    def __init__(self, device, samplerate: int = SAMPLE_RATE, channels: int = CHANNELS):
        self._lock = threading.Lock()
        self._audio: Optional[np.ndarray] = None
        self._pos: int = 0
        self._playing: bool = False
        self._dead: bool = False
        self._channels = channels
        self._volume: float = 1.0   # software gain (0.0–1.0), applied in the callback

        # Try the engine-native rate first (zero resample in the common case);
        # fall back to the device's reported default rate if PortAudio refuses.
        self._play_rate = samplerate
        try:
            self._stream = sd.OutputStream(
                device=device,
                samplerate=samplerate,
                channels=channels,
                dtype="float32",
                callback=self._callback,
                blocksize=BLOCK_SIZE,
            )
            self._stream.start()
        except sd.PortAudioError:
            dev_rate = int(round(sd.query_devices(device)["default_samplerate"]))
            self._play_rate = dev_rate
            self._stream = sd.OutputStream(
                device=device,
                samplerate=dev_rate,
                channels=channels,
                dtype="float32",
                callback=self._callback,
                blocksize=BLOCK_SIZE,
            )
            self._stream.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_playing(self) -> bool:
        return self._playing

    @property
    def is_dead(self) -> bool:
        return self._dead

    @property
    def volume(self) -> float:
        return self._volume

    def set_volume(self, v: float) -> None:
        """Set cue gain, clamped to [0.0, 1.0]."""
        with self._lock:
            self._volume = max(0.0, min(1.0, v))

    @property
    def position_seconds(self) -> float:
        return self._pos / self._play_rate

    @property
    def duration_seconds(self) -> float:
        audio = self._audio
        return len(audio) / self._play_rate if audio is not None else 0.0

    def play(self, audio: np.ndarray) -> None:
        """Start auditioning `audio` (an (N, 2) float32 array at the engine rate)
        from its first sample. Replaces anything currently cueing."""
        buf = _resample(audio, SAMPLE_RATE, self._play_rate)
        with self._lock:
            self._audio = buf
            self._pos = 0
            self._playing = True

    def stop(self) -> None:
        with self._lock:
            self._playing = False
            self._audio = None
            self._pos = 0

    def seek(self, delta_seconds: float) -> None:
        """Move the cue playhead by ±delta_seconds, clamped to [0, end]. No-op
        unless something is actively cueing."""
        with self._lock:
            if not self._playing or self._audio is None:
                return
            self._pos = int(min(max(0, self._pos + int(delta_seconds * self._play_rate)),
                                len(self._audio)))

    def close(self) -> None:
        try:
            self._stream.stop()
            self._stream.close()
        except sd.PortAudioError:
            pass

    # ------------------------------------------------------------------
    # Audio callback (real-time thread)
    # ------------------------------------------------------------------

    def _callback(self, outdata: np.ndarray, frames: int, time, status) -> None:
        try:
            with self._lock:
                if self._dead or not self._playing or self._audio is None:
                    outdata[:] = 0
                    return
                chunk = self._audio[self._pos: self._pos + frames]
                if len(chunk) < frames:
                    outdata[:len(chunk)] = chunk
                    outdata[len(chunk):] = 0
                    self._pos += len(chunk)
                    self._playing = False  # reached the end — stop silent, no loop
                else:
                    outdata[:] = chunk
                    self._pos += frames
                if self._volume != 1.0:
                    outdata *= self._volume
        except Exception:
            # Fail-soft: a device that vanished mid-set (BT drop / USB unplug)
            # must never propagate into the master engine or the UI thread.
            self._dead = True
            try:
                outdata[:] = 0
            except Exception:
                pass
