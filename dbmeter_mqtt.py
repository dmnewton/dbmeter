# Import required libraries
import time
import json
import uuid
import math
import threading

import pyaudio  # For audio input processing
#import audioop  # For audio operations like RMS calculation

from statistics import median


import yaml  # For config file parsing
#import datetime
import numpy as np
from numpy.polynomial import Polynomial
from scipy.signal import bilinear, lfilter, lfilter_zi  # For signal processing

# Setup logging configuration
import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger()

import paho.mqtt.client as mqtt  # For MQTT communication

# Load configuration from YAML file
with open('config.yaml') as f:
    config_data = yaml.safe_load(f)

# Load secrets from .env file
import os
from dotenv import load_dotenv
load_dotenv()
MQTT_PASSWORD = os.environ['MQTT_PASSWORD']


# Home Assistant MQTT Discovery configuration
DISCOVERY_PREFIX = "homeassistant"
DEVICE_NAME = config_data['homeassistant'].get('device_name', 'Sound Meter')
DEVICE_ID = config_data['homeassistant'].get('device_id', 'soundmeter2')
MANUFACTURER = "Custom"
MODEL = "Sound Level Meter"
LEVEL_TOPIC = f"sound/{DEVICE_ID}"
FREQUENCY_TOPIC = f"{LEVEL_TOPIC}/frequency"


# MQTT callback when connection is established
def connect_callback(client, userdata, connect_flags, reason_code, properties):
    publish_discovery_message(client)
    publish_frequency_discovery_message(client)
    result = client.subscribe(f"{FREQUENCY_TOPIC}/set")
    logger.info(f"Subscribe result: {result}")
    # Do NOT publish measurement_interval here — the broker will deliver the
    # retained value previously set by Home Assistant, which on_frequency_message
    # will pick up. Publishing here would overwrite the HA-controlled value.
    logger.info(f"Connected with result code {reason_code}")

# MQTT callback when disconnection occurs
def disconnect_callback(client, userdata, disconnect_flags, reason_code, properties):
    client.connected_flag=False
    logger.info(f"disconnected {reason_code}")

def on_subscribe(client, userdata, mid, reason_codes, properties):
    """Callback when subscription is confirmed"""
    logger.info(f"Subscribed! Mid: {mid}, Reason codes: {reason_codes}")


def publish_discovery_message(client):
    """Publish the Home Assistant MQTT discovery configuration for automatic device setup"""
    discovery_topic = f"{DISCOVERY_PREFIX}/sensor/{DEVICE_ID}/config"
    
    payload = {
        "name": DEVICE_NAME,
        "unique_id": f"{DEVICE_ID}_sound_level",
        "device": {
            "identifiers": [DEVICE_ID],
            "name": DEVICE_NAME,
            "manufacturer": MANUFACTURER,
            "model": MODEL
        },
        "state_topic": LEVEL_TOPIC,
        "unit_of_measurement": "dBA",
        "value_template": "{{ value_json.dba | round(2) }}",
        "device_class": "sound_pressure",
        "state_class": "measurement"
    }
    
    client.publish(discovery_topic, json.dumps(payload), retain=True)
    logger.info("Published Home Assistant discovery configuration")

def publish_frequency_discovery_message(client):
    """Publish the Home Assistant MQTT discovery configuration for measurement frequency control"""
    discovery_topic = f"{DISCOVERY_PREFIX}/number/{DEVICE_ID}_frequency/config"
    
    payload = {
        "name": f"{DEVICE_NAME} Measurement Frequency",
        "unique_id": f"{DEVICE_ID}_frequency",
        "device": {
            "identifiers": [DEVICE_ID],
            "name": DEVICE_NAME,
            "manufacturer": MANUFACTURER,
            "model": MODEL
        },
        "command_topic": f"{FREQUENCY_TOPIC}/set",
        "state_topic": FREQUENCY_TOPIC,
        "min": 5,
        "max": 60,
        "step": 1,
        "unit_of_measurement": "seconds",
        "icon": "mdi:timer-outline",
    }
    
    client.publish(discovery_topic, json.dumps(payload), retain=True)
    logger.info(f"Published frequency control discovery configuration to {discovery_topic}")

# Audio configuration parameters
# Load from config file with fallback defaults
SPEAKER_RATE = config_data['audio'].get('sample_rate', 48000)  # Sample rate in Hz
CHUNK = config_data['audio'].get('chunk_size', 4096)  # Number of frames per buffer
RECORD_SECONDS = config_data['audio'].get('record_seconds', 2)  # Recording duration

# Load audio format from config
FORMAT_MAP = {
    'paInt16': pyaudio.paInt16,
    'paInt32': pyaudio.paInt32,
    'paFloat32': pyaudio.paFloat32
}

FORMAT = FORMAT_MAP.get(config_data['audio'].get('format', 'paInt32'), pyaudio.paInt32)
CHANNELS = config_data['audio'].get('channels', 2)

# Full-scale reference level for normalisation.
# ICS-43434 is 24-bit left-justified in a 32-bit I2S word: full-scale = 2^31 = 2147483648.
AUDIO_LEVEL = config_data['audio'].get('level', 2147483648)

def A_weighting(fs: float):
    """
    Calculate A-weighting filter coefficients for given sampling frequency.
    A-weighting approximates human ear frequency sensitivity.
    """
    f1 = 20.598997
    f2 = 107.65265
    f3 = 737.86223
    f4 = 12194.217
    a1000 = 1.9997

    nums = Polynomial(((2*np.pi * f4)**2 * 10**(a1000 / 20), 0,0,0,0))
    dens = (
        Polynomial((1, 4*np.pi * f4, (2*np.pi * f4)**2)) *
        Polynomial((1, 4*np.pi * f1, (2*np.pi * f1)**2)) *
        Polynomial((1, 2*np.pi * f3)) *
        Polynomial((1, 2*np.pi * f2))
    )
    return bilinear(nums.coef, dens.coef, fs)

def rms_flat(a: np.ndarray):
    """Calculate Root Mean Square (RMS) of array"""
    return np.sqrt(a.dot(a) / len(a))

class AudioChunkRingBuffer:
    """
    Lock-free ring buffer optimized for storing audio chunk references.
    Designed for single writer (audio callback) and single reader (dBA calculation).
    """
    def __init__(self, buffer_seconds=16, chunk_rate=None):
        if chunk_rate is None:
            chunk_rate = SPEAKER_RATE // CHUNK
        
        self.buffer_size = int(chunk_rate * buffer_seconds)
        self.buffer = [None] * self.buffer_size
        self.write_index = 0
        self.count = 0
        
    def append_chunk(self, audio_bytes):
        """Add audio chunk - lock-free for single writer"""
        self.buffer[self.write_index] = audio_bytes
        self.write_index = (self.write_index + 1) % self.buffer_size
        if self.count < self.buffer_size:
            self.count += 1
    
    def get_recent_chunks(self, chunks_needed):
        """Get most recent N chunks - lock-free read"""
        chunks_needed = min(chunks_needed, self.count)
        if chunks_needed == 0:
            return []
        
        # Calculate starting position
        start_pos = (self.write_index - chunks_needed) % self.buffer_size
        
        result = []
        for i in range(chunks_needed):
            pos = (start_pos + i) % self.buffer_size
            chunk = self.buffer[pos]
            if chunk is not None:  # Safety check
                result.append(chunk)
        
        return result
    
    def is_empty(self):
        return self.count == 0
    
    def current_size(self):
        return self.count
    
    def __len__(self):
        return self.count

class Meter:
    """
    Audio meter class that handles audio input and processing
    with A-weighting filter applied. Continuously captures audio
    data into a ring buffer.
    """
    def __init__(self, buffer_seconds=16):
        self.pa = pyaudio.PyAudio()
        self.numerator, self.denominator = A_weighting(SPEAKER_RATE)
        
        # Initialize filter state for continuous processing
        self.filter_zi = lfilter_zi(self.numerator, self.denominator)
        
        # Initialize lock-free ring buffer
        self.ring_buffer = AudioChunkRingBuffer(buffer_seconds)
        
        self._chunks_needed = int((SPEAKER_RATE / CHUNK) * RECORD_SECONDS)
        self.stream = None
        self.is_running = False
        self.capture_thread = None
        
        # Track audio issues
        self.overflow_count = 0
        self.underflow_count = 0

    def __enter__(self):
        self.start_capture()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop_capture()
        try:
            self.pa.terminate()
        except Exception as e:
            logger.error(f"Error terminating PyAudio: {e}")

    def _audio_callback(self, in_data, frame_count, time_info, status):
        """Callback function for audio stream - runs in separate thread"""
        if status:
            if status & pyaudio.paInputOverflow:
                self.overflow_count += 1
                logger.warning(f"Audio input overflow detected! (Total: {self.overflow_count})")
            if status & pyaudio.paInputUnderflow:
                self.underflow_count += 1
                logger.warning(f"Audio input underflow detected! (Total: {self.underflow_count})")

        # Lock-free append to ring buffer
        self.ring_buffer.append_chunk(bytes(in_data))
        
        return (None, pyaudio.paContinue)

    def start_capture(self):
        """Start continuous audio capture into ring buffer"""
        if self.is_running:
            logger.warning("Capture already running")
            return
        
        self.stream = self.pa.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=SPEAKER_RATE,
            input=True,
            frames_per_buffer=CHUNK,
            input_device_index=1,
            stream_callback=self._audio_callback
        )
        
        self.is_running = True
        self.stream.start_stream()
        logger.info("Started continuous audio capture")

    def stop_capture(self):
        """Stop continuous audio capture"""
        if not self.is_running:
            return
        
        self.is_running = False
        
        if self.stream:
            try:
                if self.stream.is_active():
                    self.stream.stop_stream()
                self.stream.close()
            except Exception as e:
                logger.error(f"Error closing stream: {e}")
            finally:
                self.stream = None
        
        logger.info("Stopped audio capture")

    def reset_filter_state(self):
        """Reset filter state - useful when starting a new measurement session"""
        self.filter_zi = lfilter_zi(self.numerator, self.denominator)

    def process_buffer(self, offset: int):
        """
        Process the current contents of the ring buffer
        Returns the calculated dBA value
        """
        # Lock-free read from ring buffer
        if self.ring_buffer.is_empty():
            logger.warning("Ring buffer is empty")
            return None
        
        # Get the most recent chunks needed for calculation
        frames = self.ring_buffer.get_recent_chunks(self._chunks_needed)
        if not frames:
            logger.warning("No frames available in ring buffer")
            return None
        
        # Combine chunks into single block
        block = b''.join(frames)
        
        # Decode and process audio data
        decoded_block = np.frombuffer(block, dtype='<i4').astype(np.float32)
        decoded_block = decoded_block / AUDIO_LEVEL

        # Extract channel 0 from interleaved multi-channel audio before filtering.
        # Filtering across interleaved channels distorts the frequency response
        # since the filter sees samples from alternating channels as a sequence.
        if CHANNELS > 1:
            decoded_block = decoded_block[0::CHANNELS]

        # Remove DC offset before filtering. The mic has a DC bias which would
        # dominate the RMS if left in, and also cause the A-weighting filter
        # initial state (lfilter_zi assumes a unit-step input) to produce a
        # large transient at the start of each block.
        decoded_block -= np.mean(decoded_block)

        logger.debug(f"Signal stats — samples: {len(decoded_block)}, "
                     f"mean: {np.mean(decoded_block):.6f}, "
                     f"std: {np.std(decoded_block):.6f}, "
                     f"peak: {np.max(np.abs(decoded_block)):.6f}, "
                     f"rms_raw: {rms_flat(decoded_block):.6f}")

        # Scale the filter initial state to the first sample value to avoid a
        # transient at the start of each block (lfilter_zi is for a unit step).
        fresh_zi = lfilter_zi(self.numerator, self.denominator) * decoded_block[0]
        y, _ = lfilter(self.numerator, self.denominator, decoded_block, zi=fresh_zi)

        logger.debug(f"Post A-weight stats — rms: {rms_flat(y):.6f}, "
                     f"peak: {np.max(np.abs(y)):.6f}")
        rms = rms_flat(y)
        if rms == 0:
            logger.warning("RMS is zero (silence or no signal), skipping measurement")
            return None
        new_decibel = 20*np.log10(rms) + offset
        
        return new_decibel

def sleep_gen(period):
    """
    Generator function to maintain consistent sampling intervals
    Returns required sleep duration to maintain timing
    """
    num = 1  # Start from 1 to ensure first sleep is a full period
    start_time = time.time()
    while True:
        sleeplength = start_time + (period * num) - time.time()
        sleeplength = max(sleeplength, 0)
        yield sleeplength
        num += 1

def decibel_a(rms):
    """Convert RMS value to decibels"""
    db = 20 * math.log10(rms)
    return db

# Load measurement interval — prefer the persisted value from a previous run
# so that intervals set via Home Assistant survive device restarts.
_default_interval = config_data['mqtt'].get('measurement_interval', 20)
try:
    with open('measurement_interval.txt') as f:
        measurement_interval = int(f.read().strip())
    logger.info(f"Loaded persisted measurement interval: {measurement_interval}s")
except (FileNotFoundError, ValueError):
    measurement_interval = _default_interval
    logger.info(f"Using default measurement interval: {measurement_interval}s")

def on_frequency_message(client, userdata, message):
    """Handle frequency update messages"""
    global sleeplength, measurement_interval
    logger.info(f"Frequency message received on topic: {message.topic}")
    logger.info(f"Payload: {message.payload.decode()}")
    try:
        new_interval = int(message.payload.decode())
        measurement_interval = new_interval
        sleeplength = sleep_gen(measurement_interval)
        # Publish state back so HA UI stays in sync
        client.publish(FREQUENCY_TOPIC, str(new_interval), retain=True)
        # Persist to file so the interval survives device restarts without
        # relying on a retained message on the command topic (which would loop).
        with open('measurement_interval.txt', 'w') as f:
            f.write(str(new_interval))
        logger.info(f"Updated measurement interval to {new_interval} seconds")
    except ValueError:
        logger.error("Invalid frequency value received")

def on_message(client, userdata, message):
    """General message callback for debugging"""
    logger.info(f"Message received on topic: {message.topic}")
    logger.info(f"Payload: {message.payload.decode()}")

if __name__ == '__main__':
    logging.info("started")
    
    mqtt_enabled = config_data['mqtt'].get('enabled', True)
    logger.info(f"MQTT {'enabled' if mqtt_enabled else 'disabled'}, measurement interval: {measurement_interval}s")

    try:
        with Meter() as meter:
            client = None
            if mqtt_enabled:
                # Setup MQTT client with unique ID
                my_UUID = uuid.uuid4()
                client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=str(my_UUID))
                client.on_connect = connect_callback
                client.on_disconnect = disconnect_callback
                client.on_subscribe = on_subscribe
                client.on_message = on_message  # General message handler for debugging
                client.username_pw_set(config_data['mqtt']['username'], MQTT_PASSWORD)
                logger.info(f"Registering callback for topic: {FREQUENCY_TOPIC}/set")
                client.message_callback_add(f"{FREQUENCY_TOPIC}/set", on_frequency_message)

                # Connect to MQTT broker and start loop
                client.connect(config_data['mqtt']['host'], 1883)
                client.loop_start()

            # Update sleep generator to use the configured interval
            sleeplength = sleep_gen(measurement_interval)
            msg_info = None

            # Continuous measurement loop
            while True:
                time.sleep(next(sleeplength))  # Maintain consistent timing

                dba = meter.process_buffer(config_data['homeassistant'].get('offset', 0))

                if dba is None:
                    logger.warning("Skipping measurement - no data available")
                    continue

                data = {
                    "time": int(time.time() * 1000),
                    "dba": dba
                }

                if mqtt_enabled:
                    if client.is_connected():
                        try:
                            logging.debug("publishing")
                            msg_info = client.publish(LEVEL_TOPIC, json.dumps(data))
                            logging.info(f"dba {dba} result {msg_info}")
                        except Exception as e:
                            logging.info(f"publish went wrong: {e}")
                    else:
                        logging.info("waiting until client connected")
                else:
                    logging.info(f"dba {dba}")

    except KeyboardInterrupt:
        logger.info("Shutting down gracefully...")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
    finally:
        logger.info("Cleanup complete")
