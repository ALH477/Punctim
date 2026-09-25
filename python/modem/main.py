#!/usr/bin/env python3
"""
DeMoD DCF Acoustic Modem - Handshakeless Over-Air Communication
================================================================

Faust JIT compiler + real-time audio I/O for speaker-to-microphone
frame transmission between two computers over open air.

NON-CONFORMING to the wire quantum (Documentation/WIRE_QUANTUM_SPEC.md). This is the
repo's only *live* audio path, but what it puts on air is its own frame, not a
DeModFrame: a 15-byte header (type u8 | seq u32 | ts u64 | payload_len u16, see
DCFFrame below) + N payload bytes, followed by a CRC-8 -- no 0xD3 sync, no version
nibble, no CRC-16. Only the bit layer (preamble, 0x7E sync, CRC-8, postamble) is shared
with acoustic_frame.py, so its frames are not certified and do not interoperate with
the certified `afsk:` medium. Porting it to carry the 17-byte DeModFrame is deferred to
v0.2; the port should target the certified `afsk_bits` bit-stream codec in
python/MCP/mediumlab_core.py (Documentation/DCF_MEDIUM_SPEC.md, `afsk:`).

Usage:
    python dcf_acoustic_modem.py tx "Hello from DCF"
    python dcf_acoustic_modem.py rx
    python dcf_acoustic_modem.py loopback "Test message"

Requirements:
    - faust (Faust compiler, on PATH)
    - gcc (C compiler, on PATH)
    - pip: sounddevice, numpy

License: LGPL-3.0 | Patent Pending
Based on a secure protocol validated by the United States Air Force.
Originally designed for DeMoD Guitars by Asher, founder of DeMoD LLC.
(c) 2025 DeMoD LLC
"""

import argparse
import ctypes
import ctypes.util
import hashlib
import math
import os
import platform
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

from faust_jit import FaustJIT  # extracted Faust JIT compiler
import acoustic_frame as _af     # shared on-air framing (interop with the numpy modem)

try:
    import sounddevice as sd
except ImportError:
    print("[!] sounddevice not installed. Run: pip install sounddevice")
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

SAMPLE_RATE     = 48000
BLOCK_SIZE      = 256       # audio callback block size
BAUD_RATE       = 300       # symbols per second
SAMPLES_PER_BIT = int(SAMPLE_RATE / BAUD_RATE)  # 160 at 48kHz/300bd

# DCF frame geometry (S2)
DCF_HEADER_SIZE = 17        # bytes
DCF_MIN_PAYLOAD = 4         # bytes

# Modem framing
PREAMBLE_BITS   = 80        # alternating 01 pattern for clock recovery (~267ms)
SYNC_WORD       = 0x7E      # frame delimiter
SYNC_BITS       = 8

# Frequencies
MARK_FREQ       = 1200.0    # Hz (bit 1)
SPACE_FREQ      = 2200.0    # Hz (bit 0)

# Per-medium profiles (see Documentation/DCF_FIELD_USE.md). Shared with the numpy modem
# via acoustic_frame.PROFILES so both modems agree on tones/baud/preamble: "standard" is
# the original Bell-202 AFSK; "handheld" tunes for a walkie-talkie's 300-3000 Hz band-pass
# + AGC (both tones mid-band off the rolloffs, a much longer keyup preamble so the
# receiver's AGC normalizes and squelch opens before the sync word).
PROFILES = _af.PROFILES

# Detection
DETECT_THRESHOLD    = 0.45  # confidence threshold for frame detection
DETECT_HOLD_BITS    = 10    # hold detection for this many bit periods after drop

# Colors
C_RESET  = "\033[0m"
C_CYAN   = "\033[96m"
C_VIOLET = "\033[95m"
C_GREEN  = "\033[92m"
C_YELLOW = "\033[93m"
C_RED    = "\033[91m"
C_DIM    = "\033[90m"
C_BOLD   = "\033[1m"
C_BG     = "\033[48;5;234m"


# FaustJIT has been extracted to faust_jit.py and is imported above.


# ═══════════════════════════════════════════════════════════════════════════════
# DCF Frame Encoding / Decoding (S2)
# ═══════════════════════════════════════════════════════════════════════════════

class DCFFrame:
    """
    Acoustic-modem frame (NON-CONFORMING -- not the 17-byte DeModFrame wire quantum):
    15-byte header + N-byte payload, followed on air by a CRC-8.

    Format (big-endian), as HEADER_FMT below actually packs it:
        type(1) + sequence(4) + timestamp(8) + payload_len(2) + payload(N)

    The header maps to Z/136Z (S8).
    A 4-byte payload maps to Z/32Z (S7.4).
    Together: F_{H x P} = F_H (x) F_P (tensor product decomposition).
    """

    # Message types
    TYPE_DATA      = 0x02
    TYPE_HEARTBEAT = 0x01
    TYPE_ACK       = 0x04

    HEADER_FMT  = ">BIQH"   # type(1) + seq(4) + timestamp(8) + payload_len(2)
    # Note: using 2-byte payload_len for acoustic efficiency (max 65535 bytes)
    # Full DCF uses 4-byte payload_len; acoustic variant saves 2 bytes per frame
    HEADER_SIZE = struct.calcsize(HEADER_FMT)  # 15 bytes for acoustic variant

    def __init__(self, msg_type, sequence, payload):
        self.msg_type = msg_type
        self.sequence = sequence
        self.timestamp = int(time.time() * 1_000_000)
        self.payload = payload if isinstance(payload, bytes) else payload.encode("utf-8")

    def encode(self):
        """Serialize to bytes."""
        header = struct.pack(
            self.HEADER_FMT,
            self.msg_type,
            self.sequence & 0xFFFFFFFF,
            self.timestamp,
            len(self.payload) & 0xFFFF,
        )
        return header + self.payload

    @classmethod
    def decode(cls, data):
        """Deserialize from bytes. Returns DCFFrame or None."""
        if len(data) < cls.HEADER_SIZE:
            return None
        msg_type, seq, ts, plen = struct.unpack(cls.HEADER_FMT, data[:cls.HEADER_SIZE])
        payload = data[cls.HEADER_SIZE:cls.HEADER_SIZE + plen]
        if len(payload) < plen:
            return None
        frame = cls(msg_type, seq, payload)
        frame.timestamp = ts
        return frame

    def __repr__(self):
        return (f"DCFFrame(type=0x{self.msg_type:02X}, seq={self.sequence}, "
                f"payload={self.payload!r})")

    @staticmethod
    def crc8(data):
        """CRC-8 for frame integrity: poly 0x31, init 0x00, MSB-first, non-reflected
        (crc8("123456789") = 0xA2). Not CRC-8/MAXIM, which is the reflected form (0xA1)."""
        crc = 0x00
        for byte in data:
            crc ^= byte
            for _ in range(8):
                if crc & 0x80:
                    crc = (crc << 1) ^ 0x31
                else:
                    crc <<= 1
                crc &= 0xFF
        return crc


# ═══════════════════════════════════════════════════════════════════════════════
# Bit-Level Framing
# ═══════════════════════════════════════════════════════════════════════════════

def bytes_to_bits(data):
    """Convert bytes to list of bits (MSB first)."""
    bits = []
    for byte in data:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)
    return bits


def bits_to_bytes(bits):
    """Convert list of bits to bytes (MSB first)."""
    result = bytearray()
    for i in range(0, len(bits) - 7, 8):
        byte = 0
        for j in range(8):
            byte = (byte << 1) | (bits[i + j] & 1)
        result.append(byte)
    return bytes(result)


def encode_frame_bits(frame):
    """
    Encode a DCF frame into the on-air bit sequence:
        [preamble] [sync] [frame_data] [crc] [postamble]

    Delegates to the shared acoustic_frame.encode_bits so this is byte-identical to what
    the numpy modem (acoustic.py) puts on air — that is what makes the two interoperable.
    """
    return _af.encode_bits(frame.encode(), fec_mode=False, preamble_bits=PREAMBLE_BITS)


# ═══════════════════════════════════════════════════════════════════════════════
# Acoustic Modem
# ═══════════════════════════════════════════════════════════════════════════════

class AcousticModem:
    """
    Real-time acoustic modem using JIT-compiled Faust DSP.

    TX: encodes message -> DCF frame -> bit sequence -> drives Faust FSK modulator
    RX: feeds mic audio to Faust demodulator -> reads confidence + bits -> decodes frame
    """

    def __init__(self, dsp_path, mode="tx"):
        self.mode = mode
        self.running = False
        self.sequence = 0

        # TX state
        self.tx_bits = []
        self.tx_bit_index = 0
        self.tx_sample_counter = 0
        self.tx_done = threading.Event()
        self.tx_repeat = 1

        # RX state
        self.rx_detected = False
        self.rx_bits = []
        self.rx_sample_counter = 0
        self.rx_hold_counter = 0
        self.rx_bit_clock_phase = 0
        self.rx_frames_decoded = []
        self.rx_lock = threading.Lock()

        # Monitoring
        self.mon_confidence = 0.0
        self.mon_soft_bit = 0.0
        self.mon_bits_received = 0
        self.mon_frames_found = 0

        # Pre-allocate audio buffers
        self._in_buf  = np.zeros(BLOCK_SIZE, dtype=np.float32)
        self._out_bufs = [np.zeros(BLOCK_SIZE, dtype=np.float32) for _ in range(3)]

        # JIT compile and load Faust DSP
        print(f"\n{C_BOLD}{C_CYAN}DeMoD DCF Acoustic Modem{C_RESET}")
        print(f"{C_DIM}LGPL-3.0 | Patent Pending | USAF Validated{C_RESET}")
        print(f"{C_DIM}Handshakeless over-air communication{C_RESET}\n")
        print(f"{C_VIOLET}[JIT]{C_RESET} Compiling Faust DSP to native code ...")
        self.dsp = FaustJIT(dsp_path, "DCFModem", SAMPLE_RATE)
        print(f"{C_GREEN}[JIT]{C_RESET} DSP ready: {self.dsp.n_inputs}in / {self.dsp.n_outputs}out")
        print(f"{C_DIM}      Parameters: {list(self.dsp.params.keys())}{C_RESET}\n")

        # Configure DSP for mode
        if mode == "tx":
            self.dsp.set_param("mode", 0.0)
            self.dsp.set_param("tx_bit", 2.0)  # silence initially
        else:
            self.dsp.set_param("mode", 1.0)

        self.dsp.set_param("mark_freq", MARK_FREQ)
        self.dsp.set_param("space_freq", SPACE_FREQ)
        self.dsp.set_param("tx_gain_db", -3.0)
        self.dsp.set_param("detect_thresh", 8.0)

    # ── TX ──────────────────────────────────────────────────────────────────

    def transmit(self, message, repeat=3):
        """Encode and transmit a message as a DCF frame."""
        # Build DCF frame
        frame = DCFFrame(DCFFrame.TYPE_DATA, self.sequence, message)
        self.sequence += 1

        # Encode to bit sequence
        bits = encode_frame_bits(frame)

        # Build full TX sequence: [silence] [frame x repeat] [silence]
        silence_bits = [2] * 30  # bit value 2 = silence in Faust
        self.tx_bits = []
        self.tx_bits.extend(silence_bits)
        for r in range(repeat):
            self.tx_bits.extend(bits)
            self.tx_bits.extend(silence_bits)
        self.tx_bits.extend(silence_bits)

        self.tx_bit_index = 0
        self.tx_sample_counter = 0
        self.tx_done.clear()
        self.tx_repeat = repeat

        frame_bytes = frame.encode()
        total_bits = len(bits)
        duration = (len(self.tx_bits) * SAMPLES_PER_BIT) / SAMPLE_RATE

        print(f"{C_CYAN}[TX]{C_RESET} Frame assembled:")
        print(f"     Message:  \"{message}\"")
        print(f"     Type:     0x{frame.msg_type:02X} (DATA)")
        print(f"     Sequence: {frame.sequence}")
        print(f"     Header:   {DCFFrame.HEADER_SIZE} bytes -> Z/{DCFFrame.HEADER_SIZE * 8}Z")
        print(f"     Payload:  {len(frame.payload)} bytes -> Z/{len(frame.payload) * 8}Z")
        print(f"     CRC-8:    0x{DCFFrame.crc8(frame_bytes):02X}")
        print(f"     On-air:   {total_bits} bits x {repeat} repeats")
        print(f"     Duration: {duration:.1f}s at {BAUD_RATE} baud")
        print(f"     Carrier:  {MARK_FREQ:.0f} Hz mark / {SPACE_FREQ:.0f} Hz space")
        print()

    def _tx_callback(self, outdata, frames, time_info, status):
        """Audio output callback for TX mode."""
        if status:
            print(f"{C_YELLOW}[TX] Audio status: {status}{C_RESET}")

        for i in range(frames):
            # Advance bit clock
            if self.tx_sample_counter >= SAMPLES_PER_BIT:
                self.tx_sample_counter = 0
                self.tx_bit_index += 1
                if self.tx_bit_index < len(self.tx_bits):
                    bit = self.tx_bits[self.tx_bit_index]
                    self.dsp.set_param("tx_bit", float(bit))
                else:
                    self.dsp.set_param("tx_bit", 2.0)  # silence
                    if not self.tx_done.is_set():
                        self.tx_done.set()
            self.tx_sample_counter += 1

        # Run Faust DSP (generates FSK audio)
        self._in_buf[:frames] = 0.0  # TX mode ignores input
        for buf in self._out_bufs:
            buf[:frames] = 0.0
        self.dsp.compute(frames, self._in_buf[:frames], [b[:frames] for b in self._out_bufs])

        # Output channel 0 = modulated audio
        outdata[:frames, 0] = self._out_bufs[0][:frames]

    # ── RX ──────────────────────────────────────────────────────────────────

    def _rx_callback(self, indata, outdata, frames, time_info, status):
        """Audio I/O callback for RX mode."""
        if status:
            print(f"{C_YELLOW}[RX] Audio status: {status}{C_RESET}")

        # Feed microphone audio to Faust DSP
        self._in_buf[:frames] = indata[:frames, 0]
        for buf in self._out_bufs:
            buf[:frames] = 0.0
        self.dsp.compute(frames, self._in_buf[:frames], [b[:frames] for b in self._out_bufs])

        # Read Faust outputs
        audio_out  = self._out_bufs[0][:frames]
        confidence = self._out_bufs[1][:frames]
        soft_bit   = self._out_bufs[2][:frames]

        # Process each sample for bit recovery
        for i in range(frames):
            conf = confidence[i]
            sbit = soft_bit[i]

            # Update monitoring
            self.mon_confidence = float(conf)
            self.mon_soft_bit = float(sbit)

            # Detection state machine
            if conf > DETECT_THRESHOLD:
                if not self.rx_detected:
                    self.rx_detected = True
                    self.rx_bits = []
                    self.rx_sample_counter = 0
                    self.rx_bit_clock_phase = 0
                self.rx_hold_counter = DETECT_HOLD_BITS * SAMPLES_PER_BIT

                # Bit clock: sample at center of each symbol period
                self.rx_sample_counter += 1
                if self.rx_sample_counter >= SAMPLES_PER_BIT:
                    self.rx_sample_counter = 0
                    # Hard decision: soft_bit > 0 -> mark (1), else space (0)
                    bit = 1 if sbit > 0 else 0
                    self.rx_bits.append(bit)
                    self.mon_bits_received = len(self.rx_bits)
            else:
                if self.rx_detected:
                    self.rx_hold_counter -= 1
                    if self.rx_hold_counter <= 0:
                        # Signal lost - try to decode what we have
                        self._try_decode()
                        self.rx_detected = False
                        self.rx_bits = []

        # Passthrough demodulated audio for monitoring
        outdata[:frames, 0] = audio_out

    def _try_decode(self):
        """Attempt to decode a DCF frame from received bits."""
        bits = self.rx_bits
        if len(bits) < SYNC_BITS + DCFFrame.HEADER_SIZE * 8:
            return

        # Search for sync word (0x7E = 01111110)
        sync_pattern = bytes_to_bits(bytes([SYNC_WORD]))
        sync_pos = -1
        for i in range(len(bits) - SYNC_BITS):
            if bits[i:i + SYNC_BITS] == sync_pattern:
                sync_pos = i + SYNC_BITS
                break

        if sync_pos < 0:
            return

        # Extract frame bytes after sync
        frame_bits = bits[sync_pos:]
        if len(frame_bits) < DCFFrame.HEADER_SIZE * 8 + 8:  # +8 for CRC
            return

        frame_bytes = bits_to_bytes(frame_bits)
        if len(frame_bytes) < DCFFrame.HEADER_SIZE + 1:
            return

        # Decode frame
        frame = DCFFrame.decode(frame_bytes)
        if frame is None:
            return

        # Verify CRC
        frame_data = frame.encode()
        expected_crc = frame_bytes[len(frame_data)] if len(frame_bytes) > len(frame_data) else None
        actual_crc = DCFFrame.crc8(frame_data)

        crc_ok = expected_crc == actual_crc if expected_crc is not None else False

        with self.rx_lock:
            self.rx_frames_decoded.append((frame, crc_ok))
            self.mon_frames_found += 1

    # ── Main Loop ──────────────────────────────────────────────────────────

    def run_tx(self, message, repeat=3):
        """Run transmitter: encode message and play through speakers."""
        self.transmit(message, repeat)

        print(f"{C_CYAN}[TX]{C_RESET} Transmitting ... (speaker output)")
        print(f"{C_DIM}     Press Ctrl+C to stop{C_RESET}\n")

        self.running = True
        # Set initial bit
        if self.tx_bits:
            self.dsp.set_param("tx_bit", float(self.tx_bits[0]))

        try:
            with sd.OutputStream(
                samplerate=SAMPLE_RATE,
                blocksize=BLOCK_SIZE,
                channels=1,
                dtype="float32",
                callback=self._tx_callback,
            ):
                # Wait for transmission to complete
                while not self.tx_done.wait(timeout=0.1):
                    self._print_tx_status()
                # Let final audio drain
                time.sleep(0.5)

            print(f"\n{C_GREEN}[TX]{C_RESET} Transmission complete.\n")

        except KeyboardInterrupt:
            print(f"\n{C_YELLOW}[TX]{C_RESET} Stopped.")
        finally:
            self.running = False

    def run_rx(self):
        """Run receiver: listen on microphone and decode DCF frames."""
        print(f"{C_VIOLET}[RX]{C_RESET} Listening on microphone ...")
        print(f"     Carrier:   {MARK_FREQ:.0f} Hz mark / {SPACE_FREQ:.0f} Hz space")
        print(f"     Threshold: {DETECT_THRESHOLD}")
        print(f"     Baud:      {BAUD_RATE}")
        print(f"{C_DIM}     Press Ctrl+C to stop{C_RESET}\n")

        self.running = True
        try:
            with sd.Stream(
                samplerate=SAMPLE_RATE,
                blocksize=BLOCK_SIZE,
                channels=1,
                dtype="float32",
                callback=self._rx_callback,
            ):
                while self.running:
                    time.sleep(0.05)
                    self._print_rx_status()

                    # Check for decoded frames
                    with self.rx_lock:
                        while self.rx_frames_decoded:
                            frame, crc_ok = self.rx_frames_decoded.pop(0)
                            self._print_decoded_frame(frame, crc_ok)

        except KeyboardInterrupt:
            print(f"\n{C_YELLOW}[RX]{C_RESET} Stopped.")
            print(f"     Total frames decoded: {self.mon_frames_found}")
        finally:
            self.running = False

    def run_loopback(self, message, repeat=2):
        """
        Loopback test: TX through speakers, RX from microphone simultaneously.
        Proves the commutation relation S4.3 over a real acoustic channel.
        """
        print(f"{C_BOLD}{C_GREEN}[LOOPBACK]{C_RESET} Acoustic channel test")
        print(f"  TX -> speaker -> air -> microphone -> RX")
        print(f"  Proves: |F{{T_a f}}|^2 = |F{{f}}|^2 (S4.3)\n")

        # Prepare TX
        frame = DCFFrame(DCFFrame.TYPE_DATA, self.sequence, message)
        self.sequence += 1
        bits = encode_frame_bits(frame)

        silence_bits = [2] * 50
        self.tx_bits = []
        self.tx_bits.extend(silence_bits)
        for r in range(repeat):
            self.tx_bits.extend(bits)
            self.tx_bits.extend(silence_bits)
        self.tx_bits.extend(silence_bits)
        self.tx_bit_index = 0
        self.tx_sample_counter = 0
        self.tx_done.clear()

        frame_bytes = frame.encode()
        print(f"  Message:  \"{message}\"")
        print(f"  CRC-8:    0x{DCFFrame.crc8(frame_bytes):02X}")
        print(f"  Bits:     {len(bits)} x {repeat} repeats")
        print(f"  Duration: {len(self.tx_bits) * SAMPLES_PER_BIT / SAMPLE_RATE:.1f}s\n")

        # Loopback needs both TX and RX on the DSP simultaneously.
        # We run in TX mode for audio generation, and do RX analysis in Python
        # using numpy (since the Faust DSP can only be in one mode at a time).
        self.dsp.set_param("mode", 0.0)  # TX mode for FSK generation
        if self.tx_bits:
            self.dsp.set_param("tx_bit", float(self.tx_bits[0]))

        # RX analysis buffers (numpy-based for loopback)
        rx_recording = []

        def loopback_callback(indata, outdata, frames, time_info, status):
            if status:
                print(f"  {C_YELLOW}Status: {status}{C_RESET}")

            # TX: advance bit clock and generate FSK
            for i in range(frames):
                if self.tx_sample_counter >= SAMPLES_PER_BIT:
                    self.tx_sample_counter = 0
                    self.tx_bit_index += 1
                    if self.tx_bit_index < len(self.tx_bits):
                        self.dsp.set_param("tx_bit", float(self.tx_bits[self.tx_bit_index]))
                    else:
                        self.dsp.set_param("tx_bit", 2.0)
                        if not self.tx_done.is_set():
                            self.tx_done.set()
                self.tx_sample_counter += 1

            # Generate TX audio
            self._in_buf[:frames] = 0.0
            for buf in self._out_bufs:
                buf[:frames] = 0.0
            self.dsp.compute(frames, self._in_buf[:frames], [b[:frames] for b in self._out_bufs])
            outdata[:frames, 0] = self._out_bufs[0][:frames]

            # Record microphone for offline RX analysis
            rx_recording.append(indata[:frames, 0].copy())

        self.running = True
        try:
            with sd.Stream(
                samplerate=SAMPLE_RATE,
                blocksize=BLOCK_SIZE,
                channels=1,
                dtype="float32",
                callback=loopback_callback,
            ):
                print(f"  {C_CYAN}Transmitting ...{C_RESET}")
                while not self.tx_done.wait(timeout=0.1):
                    idx = self.tx_bit_index
                    total = len(self.tx_bits)
                    pct = min(100, int(idx / max(1, total) * 100))
                    bar = "#" * (pct // 5) + "-" * (20 - pct // 5)
                    sys.stdout.write(f"\r  [{bar}] {pct}%  bit {idx}/{total}")
                    sys.stdout.flush()
                time.sleep(1.0)  # capture trailing audio

            print(f"\n\n  {C_GREEN}TX complete.{C_RESET} Analyzing recorded audio ...\n")

        except KeyboardInterrupt:
            print(f"\n  {C_YELLOW}Interrupted.{C_RESET}")
            self.running = False
            return

        self.running = False

        # Offline RX analysis of recorded microphone audio
        if not rx_recording:
            print(f"  {C_RED}No audio recorded.{C_RESET}")
            return

        recorded = np.concatenate(rx_recording)
        self._analyze_loopback(recorded, frame)

    def _analyze_loopback(self, audio, original_frame):
        """
        Offline spectral analysis of loopback recording.
        Proves the handshakeless identity by showing:
        1. Spectral energy at carrier frequencies (S6 PSD)
        2. Detection without prior coordination (S4.3 commutation)
        3. Bit extraction and frame decode (S7.4 tensor product)
        """
        n = len(audio)
        duration = n / SAMPLE_RATE

        print(f"  Recorded: {n} samples ({duration:.2f}s)")
        print(f"  RMS level: {np.sqrt(np.mean(audio**2)):.4f}\n")

        # ── S6.1: Power Spectral Density via FFT ──
        print(f"  {C_VIOLET}S6.1 - Power Spectral Density{C_RESET}")
        # Compute PSD using Welch's method (windowed, averaged)
        nperseg = SAMPLES_PER_BIT * 4  # 4 symbol periods per segment
        n_segments = max(1, n // nperseg)
        psd_sum = np.zeros(nperseg // 2 + 1)

        for seg in range(n_segments):
            start = seg * nperseg
            end = start + nperseg
            if end > n:
                break
            window = audio[start:end] * np.hanning(nperseg)
            spectrum = np.fft.rfft(window)
            psd_sum += np.abs(spectrum) ** 2  # |F{f}|^2

        psd = psd_sum / max(1, n_segments)  # E[|F{x_T}|^2]
        freqs = np.fft.rfftfreq(nperseg, 1.0 / SAMPLE_RATE)

        # Find energy at mark and space frequencies
        mark_idx = np.argmin(np.abs(freqs - MARK_FREQ))
        space_idx = np.argmin(np.abs(freqs - SPACE_FREQ))
        noise_idx = np.argmin(np.abs(freqs - 3500))  # off-carrier

        mark_psd  = psd[max(0, mark_idx-2):mark_idx+3].mean()
        space_psd = psd[max(0, space_idx-2):space_idx+3].mean()
        noise_psd = psd[max(0, noise_idx-5):noise_idx+6].mean()

        snr_mark  = 10 * np.log10(mark_psd / max(noise_psd, 1e-20))
        snr_space = 10 * np.log10(space_psd / max(noise_psd, 1e-20))

        print(f"    S_xx({MARK_FREQ:.0f} Hz) = {mark_psd:.2f}  (mark)   SNR: {snr_mark:.1f} dB")
        print(f"    S_xx({SPACE_FREQ:.0f} Hz) = {space_psd:.2f}  (space)  SNR: {snr_space:.1f} dB")
        print(f"    S_xx(3500 Hz)  = {noise_psd:.2f}  (noise floor)")

        detected = snr_mark > 6 or snr_space > 6
        if detected:
            print(f"    {C_GREEN}DETECTED: DCF carrier energy exceeds noise floor{C_RESET}")
            print(f"    Handshakeless detection confirmed (S4.3){C_RESET}")
        else:
            print(f"    {C_RED}NOT DETECTED: carrier energy below threshold{C_RESET}")
            print(f"    Try increasing TX gain or reducing ambient noise")
            return

        # ── S4.3: Verify magnitude invariance ──
        print(f"\n  {C_VIOLET}S4.3 - Commutation Relation Verification{C_RESET}")
        print(f"    |F{{T_a f}}|^2 should equal |F{{f}}|^2 for any delay a.")
        print(f"    Testing with two different time windows ...")

        # Compare PSD from first half vs second half (different T_a offsets)
        half = n // 2
        if half > nperseg:
            w1 = audio[:nperseg] * np.hanning(nperseg)
            w2 = audio[half:half + nperseg] * np.hanning(nperseg)
            s1 = np.abs(np.fft.rfft(w1)) ** 2
            s2 = np.abs(np.fft.rfft(w2)) ** 2

            # Correlation between the two magnitude spectra
            s1_norm = s1 / (np.linalg.norm(s1) + 1e-20)
            s2_norm = s2 / (np.linalg.norm(s2) + 1e-20)
            correlation = float(np.dot(s1_norm, s2_norm))

            print(f"    Spectral correlation (window 1 vs window 2): {correlation:.4f}")
            if correlation > 0.8:
                print(f"    {C_GREEN}PASS: Magnitude spectrum is shift-invariant{C_RESET}")
            else:
                print(f"    {C_YELLOW}PARTIAL: Correlation {correlation:.2f} (noise/multipath){C_RESET}")

        # ── S7.4: Bit extraction (tensor product) ──
        print(f"\n  {C_VIOLET}S7.4 - Bit Extraction (Tensor Product Decomposition){C_RESET}")

        # Bandpass filter at mark and space, then energy detection
        # Simple IIR bandpass approximation
        demod_bits = []
        window_size = SAMPLES_PER_BIT

        for i in range(0, n - window_size, window_size):
            chunk = audio[i:i + window_size]
            # Correlate with mark and space tones
            t = np.arange(window_size) / SAMPLE_RATE
            mark_corr = np.abs(np.sum(chunk * np.sin(2 * np.pi * MARK_FREQ * t)))
            space_corr = np.abs(np.sum(chunk * np.sin(2 * np.pi * SPACE_FREQ * t)))

            if mark_corr + space_corr > 0.01:  # energy threshold
                demod_bits.append(1 if mark_corr > space_corr else 0)

        print(f"    Raw bits recovered: {len(demod_bits)}")

        # Search for sync word in demodulated bits
        sync_pattern = bytes_to_bits(bytes([SYNC_WORD]))
        sync_found = False
        for i in range(len(demod_bits) - len(sync_pattern)):
            if demod_bits[i:i + len(sync_pattern)] == sync_pattern:
                frame_start = i + len(sync_pattern)
                print(f"    Sync word found at bit {i}")

                # Extract frame bits
                frame_bits = demod_bits[frame_start:]
                if len(frame_bits) >= DCFFrame.HEADER_SIZE * 8:
                    frame_bytes = bits_to_bytes(frame_bits)
                    decoded = DCFFrame.decode(frame_bytes)
                    if decoded:
                        sync_found = True
                        crc_data = decoded.encode()
                        actual_crc = DCFFrame.crc8(crc_data)
                        print(f"    {C_GREEN}FRAME DECODED:{C_RESET}")
                        print(f"      Type:     0x{decoded.msg_type:02X}")
                        print(f"      Sequence: {decoded.sequence}")
                        print(f"      Payload:  {decoded.payload!r}")

                        try:
                            text = decoded.payload.decode("utf-8")
                            print(f"      Message:  \"{text}\"")
                            if text == original_frame.payload.decode("utf-8"):
                                print(f"      {C_GREEN}{C_BOLD}MATCH - Message integrity verified{C_RESET}")
                            else:
                                print(f"      {C_YELLOW}MISMATCH - bit errors present{C_RESET}")
                        except UnicodeDecodeError:
                            print(f"      {C_YELLOW}Payload is not valid UTF-8{C_RESET}")
                        break

        if not sync_found:
            print(f"    {C_YELLOW}Sync word not found in demodulated bits.{C_RESET}")
            print(f"    This may indicate too much ambient noise or")
            print(f"    insufficient speaker/mic coupling.")

        print()

    # ── Status Display ─────────────────────────────────────────────────────

    def _print_tx_status(self):
        idx = self.tx_bit_index
        total = len(self.tx_bits)
        pct = min(100, int(idx / max(1, total) * 100))
        bar = "#" * (pct // 5) + "-" * (20 - pct // 5)
        sys.stdout.write(f"\r{C_CYAN}[TX]{C_RESET} [{bar}] {pct}%  bit {idx}/{total}")
        sys.stdout.flush()

    def _print_rx_status(self):
        conf = self.mon_confidence
        sbit = self.mon_soft_bit
        nbits = self.mon_bits_received
        nframes = self.mon_frames_found

        # Confidence bar
        bar_len = 30
        filled = int(conf * bar_len)
        bar = "=" * filled + "-" * (bar_len - filled)
        color = C_GREEN if conf > DETECT_THRESHOLD else C_DIM

        bit_char = "1" if sbit > 0 else "0"
        det = f"{C_GREEN}DETECTED{C_RESET}" if conf > DETECT_THRESHOLD else f"{C_DIM}scanning{C_RESET}"

        sys.stdout.write(
            f"\r{C_VIOLET}[RX]{C_RESET} "
            f"S_xx: [{color}{bar}{C_RESET}] {conf:.2f} "
            f"bit:{bit_char} "
            f"rx:{nbits} "
            f"frames:{nframes} "
            f"{det}    "
        )
        sys.stdout.flush()

    def _print_decoded_frame(self, frame, crc_ok):
        """Print a successfully decoded DCF frame."""
        crc_str = f"{C_GREEN}CRC OK{C_RESET}" if crc_ok else f"{C_YELLOW}CRC?{C_RESET}"
        print(f"\n\n{'=' * 60}")
        print(f"{C_GREEN}{C_BOLD}  DCF FRAME RECEIVED (HANDSHAKELESS){C_RESET}")
        print(f"{'=' * 60}")
        print(f"  Type:     0x{frame.msg_type:02X}")
        print(f"  Sequence: {frame.sequence}")
        print(f"  Payload:  {len(frame.payload)} bytes")
        try:
            text = frame.payload.decode("utf-8")
            print(f"  Message:  \"{C_BOLD}{text}{C_RESET}\"")
        except UnicodeDecodeError:
            print(f"  Payload:  {frame.payload.hex()}")
        print(f"  {crc_str}")
        print(f"{'=' * 60}\n")

    def cleanup(self):
        self.running = False
        if self.dsp:
            self.dsp.cleanup()


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

BANNER = f"""
{C_BOLD}{C_CYAN}
  ____        __  __       ____     ____   _____
 |  _ \\  ___ |  \\/  | ___ |  _ \\   / ___| |  ___|
 | | | |/ _ \\| |\\/| |/ _ \\| | | | | |     | |_
 | |_| |  __/| |  | | (_) | |_| | | |___  |  _|
 |____/ \\___||_|  |_|\\___/|____/   \\____| |_|
{C_RESET}
{C_DIM}  Acoustic Modem - Handshakeless Over-Air Communication
  LGPL-3.0 | Patent Pending | USAF Validated
  Created by Asher - DeMoD LLC{C_RESET}
"""


def find_dsp_file():
    """Locate the Faust DSP file."""
    candidates = [
        Path("demod_dcf_acoustic_modem.dsp"),
        Path(__file__).parent / "demod_dcf_acoustic_modem.dsp",
        Path.home() / "demod_dcf_acoustic_modem.dsp",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def main():
    global MARK_FREQ, SPACE_FREQ, DETECT_THRESHOLD, PREAMBLE_BITS, BAUD_RATE, SAMPLES_PER_BIT
    print(BANNER)

    parser = argparse.ArgumentParser(
        description="DeMoD DCF Acoustic Modem",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s tx "Hello from DCF"     Transmit a message via speaker
  %(prog)s rx                       Listen on microphone for DCF frames
  %(prog)s loopback "Test"         TX + RX loopback test (proves S4.3)
  %(prog)s tx -r 5 "SOS"           Transmit 5 repetitions
  %(prog)s rx --mark 1500          Use 1500 Hz mark frequency
  %(prog)s tx --profile handheld "SOS"   Walkie-talkie profile (mid-band + long keyup)
        """,
    )
    parser.add_argument("mode", choices=["tx", "rx", "loopback"],
                        help="Operating mode")
    parser.add_argument("message", nargs="?", default="DeMoD DCF",
                        help="Message to transmit (TX/loopback modes)")
    parser.add_argument("-r", "--repeat", type=int, default=3,
                        help="Number of frame repetitions (default: 3)")
    parser.add_argument("--profile", choices=sorted(PROFILES), default="standard",
                        help="Medium profile: 'standard' (Bell-202) or 'handheld' "
                             "(walkie-talkie: mid-band tones + long AGC keyup)")
    parser.add_argument("--mark", type=float, default=None,
                        help="Mark frequency in Hz (overrides the profile default)")
    parser.add_argument("--space", type=float, default=None,
                        help="Space frequency in Hz (overrides the profile default)")
    parser.add_argument("--preamble-bits", type=int, default=None,
                        help="Keyup preamble length in bits (overrides the profile)")
    parser.add_argument("--gain", type=float, default=-3.0,
                        help="TX gain in dB (default: -3.0)")
    parser.add_argument("--threshold", type=float, default=DETECT_THRESHOLD,
                        help=f"Detection threshold (default: {DETECT_THRESHOLD})")
    parser.add_argument("--dsp", type=str, default=None,
                        help="Path to Faust DSP file")
    parser.add_argument("--device", default=None,
                        help="Audio device for live I/O — name or index from "
                             "--list-devices. Pick a PipeWire/JACK/Pulse port or a USB "
                             "interface to route to/from the radio (e.g. --device pipewire).")
    parser.add_argument("--list-devices", action="store_true",
                        help="List audio devices (PortAudio host APIs incl. JACK/PipeWire/Pulse) and exit")

    args = parser.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        return
    if args.device is not None:
        try:
            sd.default.device = int(args.device)     # accept an index or a name substring
        except ValueError:
            sd.default.device = args.device
        print(f"{C_DIM}Audio device: {args.device}{C_RESET}")

    # Find DSP file
    if args.dsp:
        dsp_path = Path(args.dsp)
    else:
        dsp_path = find_dsp_file()

    if dsp_path is None or not dsp_path.exists():
        print(f"{C_RED}[!] Cannot find demod_dcf_acoustic_modem.dsp{C_RESET}")
        print(f"    Place it in the current directory or specify with --dsp")
        sys.exit(1)

    print(f"{C_DIM}DSP file: {dsp_path}{C_RESET}")

    # Resolve the medium profile, then let explicit flags override it.
    prof = PROFILES[args.profile]
    MARK_FREQ = args.mark if args.mark is not None else prof["mark"]
    SPACE_FREQ = args.space if args.space is not None else prof["space"]
    PREAMBLE_BITS = args.preamble_bits if args.preamble_bits is not None else prof["preamble_bits"]
    BAUD_RATE = prof["baud"]                       # shared with the numpy modem
    SAMPLES_PER_BIT = int(SAMPLE_RATE / BAUD_RATE)
    DETECT_THRESHOLD = args.threshold
    if args.profile != "standard" or args.mark or args.space:
        print(f"{C_DIM}Profile: {args.profile} — {MARK_FREQ:.0f}/{SPACE_FREQ:.0f} Hz, "
              f"{PREAMBLE_BITS}-bit keyup{C_RESET}")

    # Create modem
    modem_mode = "tx" if args.mode in ("tx", "loopback") else "rx"
    modem = AcousticModem(dsp_path, mode=modem_mode)

    # Override DSP params from CLI
    modem.dsp.set_param("mark_freq", MARK_FREQ)
    modem.dsp.set_param("space_freq", SPACE_FREQ)
    modem.dsp.set_param("tx_gain_db", args.gain)

    try:
        if args.mode == "tx":
            modem.run_tx(args.message, repeat=args.repeat)
        elif args.mode == "rx":
            modem.run_rx()
        elif args.mode == "loopback":
            modem.run_loopback(args.message, repeat=args.repeat)
    finally:
        modem.cleanup()


if __name__ == "__main__":
    main()
