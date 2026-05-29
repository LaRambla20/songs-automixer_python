import threading
import numpy as np
import sounddevice as sd
from enum import Enum
from typing import Optional

SAMPLE_RATE = 44100
CHANNELS = 2
BLOCK_SIZE = 2048


class State(Enum):
    IDLE = "idle"
    PLAYING = "playing"
    MIXING = "mixing"


class AudioEngine:
    def __init__(self):
        self.state = State.IDLE
        self._lock = threading.Lock()

        self._now_audio: Optional[np.ndarray] = None   # (N, 2) float32
        self._next_audio: Optional[np.ndarray] = None  # (N, 2) float32, pre-stretched

        self._position: int = 0       # sample index in _now_audio
        self._mix_pos: int = 0        # sample index in _next_audio during crossfade
        self._fade_samples: int = 0
        self._paused: bool = False
        # When set, the callback stays in PLAYING until _position reaches this sample,
        # then transitions to MIXING in the same audio frame. Used for downbeat-aligned
        # mix starts; None for immediate mixing.
        self._pending_mix_at: Optional[int] = None

        self._stream = sd.OutputStream(
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
    def duration(self) -> int:
        audio = self._now_audio
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
