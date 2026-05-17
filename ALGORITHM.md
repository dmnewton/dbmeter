# Algorithm: Raw Audio to dBA

This document explains how `dbmeter_mqtt.py` reads audio data from the I2S microphone and converts it to a dBA (A-weighted decibel) sound pressure level.

---

## Hardware

The microphone is an **ICS-43434 MEMS I2S microphone**. It outputs 24-bit audio samples left-justified inside a 32-bit I2S word. It connects to the Raspberry Pi GPIO pins and appears as ALSA audio device index `1`.

Key datasheet spec: sensitivity of **−26 dBFS at 94 dB SPL**, which establishes the calibration offset used in the final conversion step.

---

## Audio Capture

PyAudio opens a non-blocking input stream with the following parameters (configured in `config.yaml`):

| Parameter | Value | Notes |
|---|---|---|
| Format | `paInt32` | 32-bit signed integers |
| Channels | 2 | I2S outputs stereo; only channel 0 is used |
| Sample rate | 48,000 Hz | |
| Chunk size | 12,000 frames | 0.25 seconds per chunk |
| Device index | 1 | ALSA device for the ICS-43434 |

PortAudio invokes `_audio_callback` every 0.25 seconds with a freshly captured chunk. The callback converts the raw bytes to an immutable `bytes` object and writes it into a **lock-free ring buffer** (`AudioChunkRingBuffer`) with a 16-second capacity. There is no blocking between the capture thread and the measurement thread.

---

## Processing Pipeline

Every measurement interval (default: 20 seconds), `process_buffer()` pulls the most recent 2 seconds of audio from the ring buffer and runs the following steps.

### Step 1 — Assemble a 2-second block

The 8 most recent chunks (8 × 0.25 s = 2 s) are retrieved from the ring buffer and concatenated into a single byte string of `768,000 bytes` (8 chunks × 12,000 frames × 2 channels × 4 bytes/sample).

### Step 2 — Decode and normalise

```python
decoded = np.frombuffer(block, dtype='<i4').astype(np.float32)
decoded = decoded / 2_147_483_648   # divide by 2^31
```

Each 32-bit integer is converted to a float in the range `[−1.0, +1.0]`. Because the ICS-43434 places its 24-bit value in the upper bits of the 32-bit word (left-justified), dividing by 2^31 is the physically correct full-scale normalisation.

### Step 3 — Extract mono channel

```python
mono = decoded[0::2]   # every other sample, starting at 0 → channel 0 only
```

The I2S samples are interleaved (L, R, L, R …). Striding by 2 discards channel 1, leaving **96,000 mono float32 samples**.

### Step 4 — Remove DC offset

```python
mono -= np.mean(mono)
```

The ICS-43434 has a small DC bias. Subtracting the mean removes it so it does not inflate the RMS power reading and does not cause a transient at the start of the A-weighting filter.

### Step 5 — Apply A-weighting filter

A-weighting models the human ear's reduced sensitivity to low and very high frequencies, producing a measurement that correlates with perceived loudness.

**Filter design** (`A_weighting(fs)`, computed once at startup):

The standard IEC 61672 A-weighting curve is defined by four characteristic frequencies:

| Symbol | Frequency |
|---|---|
| f1 | 20.598997 Hz |
| f2 | 107.65265 Hz |
| f3 | 737.86223 Hz |
| f4 | 12194.217 Hz |

These define a 4-pole analog IIR prototype. The prototype is converted to a discrete-time filter matched to 48,000 Hz using `scipy.signal.bilinear` (bilinear transform). A normalisation constant of `1.9997` ensures the filter has exactly 0 dB gain at 1 kHz, matching the IEC standard reference point.

**Filtering:**

```python
zi = lfilter_zi(b, a) * mono[0]   # initialise state to avoid startup transient
weighted, _ = lfilter(b, a, mono, zi=zi)
```

`lfilter_zi` computes the steady-state initial condition for a unit-step input, scaled to the actual first sample. This eliminates the filter warm-up transient that would otherwise distort the first few milliseconds of each window.

### Step 6 — Compute RMS

```python
rms = sqrt(dot(weighted, weighted) / len(weighted))
```

This is the root mean square of the A-weighted signal over the 2-second window — equivalent to `sqrt(mean(x²))` but computed via a single dot product for efficiency.

### Step 7 — Convert to dBA SPL

```python
dba = 20 * log10(rms) + 118
```

- `20 * log10(rms)` converts the normalised RMS to **dBFS** (decibels relative to full scale).
- `+ 118` converts dBFS to **dB SPL** (sound pressure level), calibrated for the ICS-43434.

**Why 118?** The microphone datasheet specifies −26 dBFS at 94 dB SPL, giving a theoretical offset of 94 + 26 = 120 dB. This was empirically adjusted by −2 dB to match reference measurements, giving a final offset of **118 dB** (set in `config.yaml` as `offset`).

---

## Summary

```
I2S mic (ICS-43434, 24-bit, 48 kHz)
  │
  ▼
PyAudio non-blocking stream (paInt32, 2ch, 12000-frame chunks)
  │
  ▼
Lock-free ring buffer (64 slots, 16 s capacity)
  │
  ▼  every measurement interval
Assemble 2-second block (8 chunks → 768,000 bytes)
  │
  ▼
Decode int32 → float32, normalise ÷ 2^31
  │
  ▼
De-interleave: channel 0 only → 96,000 mono samples
  │
  ▼
Remove DC offset (subtract mean)
  │
  ▼
A-weighting IIR filter (4-pole, bilinear-transformed, 0 dB @ 1 kHz)
  │
  ▼
RMS over 2-second window
  │
  ▼
20·log10(rms) + 118  →  dBA SPL
  │
  ▼
Publish as JSON to MQTT → Home Assistant
```
