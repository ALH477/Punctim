# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (c) 2026 DeMoD LLC.
"""In-process HydraModem codec via ctypes — drives libhydramodem's hydra_modem_tx/rx +
hydra_wav_read/write directly (no subprocess). Faster than the frame_tx/frame_rx tools
and the path FDMA's concurrent decoders want. Discover the shared library via
$HYDRAMODEM_LIB or hydramodem/build/libhydramodem.so*.

A struct self-check (sample_rate/baud after hydra_profile_default) guards against any
hydra_profile layout drift between this binding and the C header."""
import ctypes as C
import os

HYDRA_DCF_BYTES = 17
_FEC = {"none": 0, "rep3": 1, "conv": 2}


class _Profile(C.Structure):
    # Must match hydramodem/src/hydra_profile.h exactly (user fields + derived fields).
    _fields_ = [
        ("sample_rate", C.c_double), ("baud", C.c_double), ("n_tones", C.c_int),
        ("base_freq", C.c_double), ("tone_spacing", C.c_double),
        ("preamble_syms", C.c_int), ("sync_word", C.c_uint16),
        ("fec_mode", C.c_int), ("interleave", C.c_int), ("tx_gain", C.c_double),
        ("bits_per_symbol", C.c_int), ("samples_per_symbol", C.c_int),
        ("lp_cut", C.c_double), ("data_bits", C.c_size_t), ("coded_bits", C.c_size_t),
        ("interleave_stride", C.c_int), ("sync_syms", C.c_size_t),
        ("data_syms", C.c_size_t), ("total_syms", C.c_size_t),
        # musical tone map (appended in hydra_profile.h; HYDRA_MUSIC_MAX_TONES=16,
        # HYDRA_MUSIC_MAX_DRONES=2). The C constructors write these, so the binding
        # must reserve them even when only linear profiles are used.
        ("tone_mult", C.c_int * 16), ("drone_mult", C.c_int * 2),
        ("drone_gain", C.c_double), ("ramp_ms", C.c_double),
    ]


# The musical tone-table profiles (hydra_profile_music presets, hydramodem/docs/MUSIC.md).
MUSIC_PROFILES = ("melody", "chime", "nocturne", "bass")
PROFILES = ("default", "aux") + MUSIC_PROFILES


def _find_lib():
    p = os.environ.get("HYDRAMODEM_LIB")
    if p and os.path.exists(p):
        return p
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in ("libhydramodem.so", "libhydramodem.so.2", "libhydramodem.so.2.0.1"):
        f = os.path.abspath(os.path.join(here, "..", "..", "hydramodem", "build", cand))
        if os.path.exists(f):
            return f
    return "libhydramodem.so"        # let the dynamic loader try


_lib = None
_libc = None


def _load():
    global _lib, _libc
    if _lib is not None:
        return _lib
    lib = C.CDLL(_find_lib())
    P = C.POINTER(_Profile)
    lib.hydra_profile_default.argtypes = [P]
    lib.hydra_profile_init.argtypes = [P]; lib.hydra_profile_init.restype = C.c_int
    lib.hydra_modem_tx.argtypes = [P, C.POINTER(C.c_uint8),
                                   C.POINTER(C.POINTER(C.c_float)), C.POINTER(C.c_size_t)]
    lib.hydra_modem_tx.restype = C.c_int
    lib.hydra_modem_rx.argtypes = [P, C.POINTER(C.c_float), C.c_size_t,
                                   C.POINTER(C.c_uint8)]
    lib.hydra_modem_rx.restype = C.c_int
    lib.hydra_wav_write.argtypes = [C.c_char_p, C.POINTER(C.c_float), C.c_size_t, C.c_int]
    lib.hydra_wav_write.restype = C.c_int
    lib.hydra_wav_read.argtypes = [C.c_char_p, C.POINTER(C.POINTER(C.c_float)),
                                   C.POINTER(C.c_size_t), C.POINTER(C.c_int)]
    lib.hydra_wav_read.restype = C.c_int
    libc = C.CDLL(None)
    libc.free.argtypes = [C.c_void_p]
    # struct self-check
    pr = _Profile()
    lib.hydra_profile_default(C.byref(pr))
    if abs(pr.sample_rate - 48000.0) > 1.0 or abs(pr.baud - 1000.0) > 1.0:
        raise RuntimeError("hydra_profile struct mismatch (sample_rate=%r baud=%r) — "
                           "binding is out of sync with hydra_profile.h"
                           % (pr.sample_rate, pr.baud))
    _lib, _libc = lib, libc
    return _lib


def available():
    """True iff libhydramodem loads and the struct self-check passes."""
    try:
        _load()
        return True
    except Exception:
        return False


class HydraModem:
    """In-process HydraModem codec: encode_wav(frame, path) / decode_wav(path)."""

    def __init__(self, fec="conv", base_freq=None, tone_spacing=None, baud=None,
                 n_tones=None, profile="default", interleave=None, preamble_syms=None):
        lib = _load()
        p = _Profile()
        if profile == "aux":
            # hydra_profile_aux_cable(): 1200 baud, tones 1200/2400 Hz, 16-symbol preamble
            fn = getattr(lib, "hydra_profile_aux_cable", None)
            if fn is not None:
                fn.argtypes = [C.POINTER(_Profile)]
                fn(C.byref(p))
            else:                                   # older library: same values by hand
                lib.hydra_profile_default(C.byref(p))
                p.baud, p.base_freq, p.tone_spacing, p.preamble_syms = 1200.0, 1200.0, 1200.0, 16
        elif profile == "default":
            lib.hydra_profile_default(C.byref(p))
        elif profile in MUSIC_PROFILES:
            fn = getattr(lib, "hydra_profile_" + profile, None)
            if fn is None:
                raise ValueError(f"libhydramodem predates the {profile!r} profile; rebuild it")
            fn.argtypes = [C.POINTER(_Profile)]
            fn(C.byref(p))
            if base_freq is not None or tone_spacing is not None or n_tones is not None:
                raise ValueError("base_freq/tone_spacing/n_tones do not apply to a musical "
                                 "profile (its pitches come from the tone table)")
        else:
            raise ValueError(f"unknown HydraModem profile {profile!r} ({'|'.join(PROFILES)})")
        p.fec_mode = _FEC.get(fec, 2)
        if interleave is not None: p.interleave = 1 if int(interleave) else 0
        if preamble_syms is not None: p.preamble_syms = int(preamble_syms)
        if base_freq is not None: p.base_freq = float(base_freq)
        if tone_spacing is not None: p.tone_spacing = float(tone_spacing)
        if baud is not None: p.baud = float(baud)
        if n_tones is not None: p.n_tones = int(n_tones)
        if lib.hydra_profile_init(C.byref(p)) != 0:
            raise ValueError("bad HydraModem profile")
        self._p = p

    def encode_wav(self, frame, path):
        lib = _load()
        buf = (C.c_uint8 * HYDRA_DCF_BYTES)(*bytes(frame))
        audio = C.POINTER(C.c_float)(); n = C.c_size_t(0)
        if lib.hydra_modem_tx(C.byref(self._p), buf, C.byref(audio), C.byref(n)) != 0:
            raise RuntimeError("hydra_modem_tx failed")
        try:
            if lib.hydra_wav_write(path.encode(), audio, n, int(self._p.sample_rate)) != 0:
                raise RuntimeError("hydra_wav_write failed")
        finally:
            _libc.free(C.cast(audio, C.c_void_p))

    def decode_wav(self, path):
        lib = _load()
        audio = C.POINTER(C.c_float)(); n = C.c_size_t(0); sr = C.c_int(0)
        if lib.hydra_wav_read(path.encode(), C.byref(audio), C.byref(n), C.byref(sr)) != 0:
            return None
        try:
            out = (C.c_uint8 * HYDRA_DCF_BYTES)()
            rc = lib.hydra_modem_rx(C.byref(self._p), audio, n, out)
        finally:
            _libc.free(C.cast(audio, C.c_void_p))
        return bytes(out) if rc == 0 else None


class HydraDuet:
    """Two frames in ONE WAV, one per voice of the polyphonic `duet` profile: frame A
    on the melody voice (600-1500 Hz), frame B on the bass voice (75-225 Hz), sounding
    together -- the acoustic counterpart of SuperPack's frame pair in one datagram.
    Each voice decodes independently (hydra_modem_rx_poly), so decode_pair_wav returns
    (a, b) with None for a voice that was lost. hydramodem/docs/MUSIC.md, "Polyphony"."""

    def __init__(self):
        lib = _load()
        for name in ("hydra_profile_duet", "hydra_modem_tx_poly", "hydra_modem_rx_poly",
                     "hydra_profile_bass", "hydra_profile_melody"):
            if getattr(lib, name, None) is None:
                raise ValueError(f"libhydramodem predates polyphony ({name}); rebuild it")
        V = _Profile * 2
        lib.hydra_profile_duet.argtypes = [C.POINTER(_Profile)]
        lib.hydra_profile_melody.argtypes = [C.POINTER(_Profile)]
        lib.hydra_profile_bass.argtypes = [C.POINTER(_Profile)]
        lib.hydra_modem_tx_poly.argtypes = [C.POINTER(_Profile), C.c_int,
                                            C.POINTER(C.c_uint8 * HYDRA_DCF_BYTES),
                                            C.POINTER(C.POINTER(C.c_float)), C.POINTER(C.c_size_t)]
        lib.hydra_modem_tx_poly.restype = C.c_int
        lib.hydra_modem_rx_poly.argtypes = [C.POINTER(_Profile), C.c_int, C.POINTER(C.c_float),
                                            C.c_size_t, C.POINTER(C.c_uint8 * HYDRA_DCF_BYTES),
                                            C.POINTER(C.c_int)]
        lib.hydra_modem_rx_poly.restype = C.c_int
        self._tx = V(); lib.hydra_profile_duet(self._tx)
        self._rx = V(); lib.hydra_profile_melody(C.byref(self._rx[0]))
        lib.hydra_profile_bass(C.byref(self._rx[1]))
        for p in (self._tx[0], self._tx[1], self._rx[0], self._rx[1]):
            if lib.hydra_profile_init(C.byref(p)) != 0:
                raise ValueError("bad duet profile")

    def encode_pair_wav(self, frame_a, frame_b, path):
        """frame_b=None sends frame_a alone on the melody voice (a lone frame)."""
        lib = _load()
        pl = ((C.c_uint8 * HYDRA_DCF_BYTES) * 2)()
        pl[0][:] = bytes(frame_a)
        if frame_b is not None:
            pl[1][:] = bytes(frame_b)
        nv = 1 if frame_b is None else 2
        audio = C.POINTER(C.c_float)(); n = C.c_size_t(0)
        if lib.hydra_modem_tx_poly(self._tx, nv, pl, C.byref(audio), C.byref(n)) != 0:
            raise RuntimeError("hydra_modem_tx_poly failed")
        try:
            if lib.hydra_wav_write(path.encode(), audio, n, int(self._tx[0].sample_rate)) != 0:
                raise RuntimeError("hydra_wav_write failed")
        finally:
            _libc.free(C.cast(audio, C.c_void_p))

    def decode_pair_wav(self, path):
        lib = _load()
        audio = C.POINTER(C.c_float)(); n = C.c_size_t(0); sr = C.c_int(0)
        if lib.hydra_wav_read(path.encode(), C.byref(audio), C.byref(n), C.byref(sr)) != 0:
            return (None, None)
        out = ((C.c_uint8 * HYDRA_DCF_BYTES) * 2)(); st = (C.c_int * 2)()
        try:
            lib.hydra_modem_rx_poly(self._rx, 2, audio, n, out, st)
        finally:
            _libc.free(C.cast(audio, C.c_void_p))
        return tuple(bytes(out[v]) if st[v] == 0 else None for v in range(2))
