import threading
import numpy as np
import sounddevice as sd
from enum import Enum
from typing import Optional

from .fx import MasterFx

SAMPLE_RATE = 44100
CHANNELS = 2
BLOCK_SIZE = 2048
# Master gain ceiling — allows boosting quiet tracks above unity (+6 dB). Output is
# hard-limited to ±1.0 in the callback so a boosted signal never exceeds full scale
# at the device (speaker safety).
MAX_GAIN = 2.0


class State(Enum):
    IDLE = "idle"
    PLAYING = "playing"
    MIXING = "mixing"


class AudioEngine:
    def __init__(self, device=None):
        self.state = State.IDLE
        self._lock = threading.Lock()

        self._now_audio: Optional[np.ndarray] = None   # (N, 2) float32
        self._next_audio: Optional[np.ndarray] = None  # (N, 2) float32, pre-stretched

        self._position: int = 0       # sample index in _now_audio
        self._mix_pos: int = 0        # sample index in _next_audio during crossfade
        self._fade_samples: int = 0
        self._paused: bool = False
        # Master software gain (0.0–1.0), applied to every callback's output. Lets
        # the keyboard control the level even when the master is pinned to a non-
        # default device (Windows volume keys only affect the OS default device).
        self._volume: float = 1.0
        # Master performance FX (HPF / LPF / tempo-synced Trans gate). Mutated only via
        # the set_fx_* wrappers below (which hold _lock); the callback reads it under the
        # same lock. Holds its own filter delay state + gate phase across blocks.
        self._fx = MasterFx(SAMPLE_RATE)
        # When set, the callback stays in PLAYING until _position reaches this sample,
        # then transitions to MIXING in the same audio frame. Used for downbeat-aligned
        # mix starts; None for immediate mixing.
        self._pending_mix_at: Optional[int] = None

        # device=None follows the OS default output; an explicit index pins the
        # master to a chosen device (e.g. the speakers / a mixer via the AUX jack),
        # independent of the flaky Windows default.
        self._stream = sd.OutputStream(
            device=device,
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            callback=self._callback,
            blocksize=BLOCK_SIZE,
        )
        self._stream.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def position(self) -> int:
        return self._position

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def volume(self) -> float:
        return self._volume

    def set_volume(self, v: float) -> None:
        """Set master gain, clamped to [0.0, MAX_GAIN]. Above 1.0 boosts quiet
        material; the callback hard-limits the result to ±1.0 (speaker safety)."""
        with self._lock:
            self._volume = max(0.0, min(MAX_GAIN, v))

    # --- Master FX (lock-guarded passthroughs to the MasterFx processor) ---------

    def set_fx_enabled(self, on: bool) -> None:
        with self._lock:
            self._fx.set_enabled(on)

    def set_fx_type(self, fx_type: str) -> None:
        with self._lock:
            self._fx.set_type(fx_type)

    def adjust_fx(self, direction: int) -> None:
        with self._lock:
            self._fx.adjust(direction)

    def set_fx_tempo(self, bpm: float) -> None:
        with self._lock:
            self._fx.set_tempo(bpm)

    def fx_state(self) -> tuple:
        """(enabled, label) for the UI indicator, e.g. (True, 'HPF 65%')."""
        with self._lock:
            return self._fx.enabled, self._fx.describe()

    @property
    def duration(self) -> int:
        audio = self._now_audio
        return len(audio) if audio is not None else 0

    @property
    def mix_position(self) -> int:
        """Sample index into the incoming track (_next_audio) during a crossfade.
        Only meaningful while state == MIXING; 0 otherwise."""
        return self._mix_pos

    @property
    def mix_duration(self) -> int:
        """Length of the incoming (pre-rendered) buffer during a crossfade, 0 if none."""
        audio = self._next_audio
        return len(audio) if audio is not None else 0

    def load_audio(self, path: str) -> np.ndarray:
        """Decode any ffmpeg-supported file. Returns (N, 2) float32 array."""
        from pydub import AudioSegment
        seg = AudioSegment.from_file(path)
        seg = seg.set_channels(CHANNELS).set_frame_rate(SAMPLE_RATE)
        raw = np.array(seg.get_array_of_samples(), dtype=np.float32)
        return raw.reshape(-1, CHANNELS) / 32768.0

    def play(self, audio: np.ndarray):
        with self._lock:
            self._now_audio = audio
            self._position = 0
            self._paused = False
            self.state = State.PLAYING
            # Drop filter ringing / gate phase from any previous track.
            self._fx.reset()

    def pause(self):
        with self._lock:
            self._paused = not self._paused

    def stop(self):
        with self._lock:
            self.state = State.IDLE
            self._now_audio = None
            self._next_audio = None
            self._position = 0
            self._paused = False
            self._pending_mix_at = None
            self._fx.reset()

    def start_mix(
        self,
        next_audio: np.ndarray,
        fade_seconds: float,
        scheduled_start_sample: Optional[int] = None,
    ):
        """Crossfade from _now_audio into next_audio over fade_seconds.

        If `scheduled_start_sample` is given, the crossfade is deferred until the
        callback's `_position` reaches that sample (sample-accurate, no UI tick
        polling). Used for downbeat-aligned mix starts. Pass None for the legacy
        immediate-start behaviour.
        """
        with self._lock:
            if self.state != State.PLAYING:
                return
            self._next_audio = next_audio
            self._fade_samples = int(fade_seconds * SAMPLE_RATE)
            self._mix_pos = 0
            if scheduled_start_sample is None or scheduled_start_sample <= self._position:
                self._pending_mix_at = None
                self.state = State.MIXING
            else:
                self._pending_mix_at = int(scheduled_start_sample)
                # state stays PLAYING; the callback flips to MIXING when _position arrives.

    def close(self):
        self._stream.stop()
        self._stream.close()

    # ------------------------------------------------------------------
    # Audio callback (real-time thread — no Python I/O, minimal work)
    # ------------------------------------------------------------------

    def _callback(self, outdata: np.ndarray, frames: int, time, status):
        with self._lock:
            if self._paused or self.state == State.IDLE:
                outdata[:] = 0
                return
            if self.state == State.PLAYING:
                self._fill_playing(outdata, frames)
            elif self.state == State.MIXING:
                self._fill_mixing(outdata, frames)
            # Master performance FX (filter / gate) on the program material, before the
            # master gain trim. No-op when disabled.
            self._fx.process(outdata, frames)
            # Master gain, applied once over the filled buffer (covers both the
            # PLAYING and MIXING fills; the IDLE/paused branch already returned).
            if self._volume != 1.0:
                outdata *= self._volume
                # Boost (>1.0) can push past full scale; hard-limit to ±1.0 so the
                # device never receives a beyond-full-scale signal (speaker safety).
                if self._volume > 1.0:
                    np.clip(outdata, -1.0, 1.0, out=outdata)

    def _fill_playing(self, outdata: np.ndarray, frames: int):
        # If a scheduled mix is armed and the trigger lies within this callback's
        # span, split the chunk: fill the pre-trigger samples from _now_audio,
        # transition to MIXING, then let _fill_mixing handle the remainder so
        # there's no silence gap at the seam.
        if (
            self._pending_mix_at is not None
            and self._pending_mix_at < self._position + frames
        ):
            trigger = max(self._pending_mix_at, self._position)
            frames_before = trigger - self._position
            if frames_before > 0:
                chunk = self._get_chunk(self._now_audio, self._position, frames_before)
                outdata[:frames_before] = chunk
                self._position += frames_before
            self.state = State.MIXING
            self._mix_pos = 0
            self._pending_mix_at = None
            frames_after = frames - frames_before
            if frames_after > 0:
                self._fill_mixing(outdata[frames_before:], frames_after)
            return

        chunk = self._get_chunk(self._now_audio, self._position, frames)
        outdata[:] = chunk
        self._position += frames
        if self._position >= len(self._now_audio):
            self.state = State.IDLE

    def _fill_mixing(self, outdata: np.ndarray, frames: int):
        now_chunk = self._get_chunk(self._now_audio, self._position, frames)
        nxt_chunk = self._get_chunk(self._next_audio, self._mix_pos, frames)

        t0 = min(self._mix_pos / self._fade_samples, 1.0)
        t1 = min((self._mix_pos + frames) / self._fade_samples, 1.0)
        fade_in = np.linspace(t0, t1, frames, dtype=np.float32).reshape(-1, 1)
        fade_out = 1.0 - fade_in

        outdata[:] = now_chunk * fade_out + nxt_chunk * fade_in

        self._position += frames
        self._mix_pos += frames

        if self._mix_pos >= self._fade_samples:
            self._now_audio = self._next_audio
            self._position = self._mix_pos
            self._next_audio = None
            self._mix_pos = 0
            self.state = State.PLAYING

    @staticmethod
    def _get_chunk(audio: Optional[np.ndarray], start: int, frames: int) -> np.ndarray:
        if audio is None:
            return np.zeros((frames, CHANNELS), dtype=np.float32)
        chunk = audio[start: start + frames]
        if len(chunk) < frames:
            pad = np.zeros((frames - len(chunk), CHANNELS), dtype=np.float32)
            chunk = np.concatenate([chunk, pad], axis=0)
        return chunk
