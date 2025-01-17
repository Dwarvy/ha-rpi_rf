"""Support for a switch using a 433MHz module via GPIO on a Raspberry Pi."""
from __future__ import annotations

import importlib
import logging
from threading import RLock
import time
from collections import namedtuple

from RPi import GPIO

MAX_CHANGES = 67
import voluptuous as vol

from homeassistant.components.switch import PLATFORM_SCHEMA, SwitchEntity
from homeassistant.const import (
    CONF_NAME,
    CONF_UNIQUE_ID,
    CONF_PROTOCOL,
    CONF_SWITCHES,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

_LOGGER = logging.getLogger(__name__)

###############################################################

Protocol = namedtuple('Protocol',
                      ['pulselength',
                       'sync_high', 'sync_low',
                       'zero_high', 'zero_low',
                       'one_high', 'one_low'])
PROTOCOLS = (None,
             Protocol(350, 1, 31, 1, 3, 3, 1),
             Protocol(650, 1, 10, 1, 2, 2, 1),
             Protocol(100, 30, 71, 4, 11, 9, 6),
             Protocol(380, 1, 6, 1, 3, 3, 1),
             Protocol(500, 6, 14, 1, 2, 2, 1),
             Protocol(200, 1, 10, 1, 5, 1, 1),
             Protocol(180, 0, 0, 0, 1, 1, 0))

def kaku_encode(to_encode):
    print(f"To Encode: {to_encode}")
    to_encode = str(bin(int(to_encode)))[2:]
    to_encode = to_encode.replace("1", "3").replace("0", "1")
    encoded1 = ""
    for i in range(len(to_encode)-1):
        if to_encode[i] == to_encode[i+1]:
            encoded1 += "2"
        else:
            encoded1 += to_encode[i]
    encoded1 += to_encode[-1]
    encoded = "1000000000010100000100000"

    print(f"To Encode: {encoded1}")

    for nr in encoded1:
        bit = ""
        for x in range(int(nr)):
            bit += "10"
        bit += "0000"
        encoded += bit
    encoded += "0000000000000000000000000000000000000000"
    return encoded

class RFDevice:
    """Representation of a GPIO RF device."""

    # pylint: disable=too-many-instance-attributes,too-many-arguments
    def __init__(self, gpio,
                 tx_proto=1, tx_pulselength=None, tx_repeat=10, tx_length=24, rx_tolerance=80):
        """Initialize the RF device."""
        self.gpio = gpio
        self.tx_enabled = False
        self.tx_proto = tx_proto
        if tx_pulselength:
            self.tx_pulselength = tx_pulselength
        else:
            self.tx_pulselength = PROTOCOLS[tx_proto].pulselength
        self.tx_repeat = tx_repeat
        self.tx_length = tx_length
        self.rx_enabled = False
        self.rx_tolerance = rx_tolerance
        # internal values
        self._rx_timings = [0] * (MAX_CHANGES + 1)
        self._rx_last_timestamp = 0
        self._rx_change_count = 0
        self._rx_repeat_count = 0
        # successful RX values
        self.rx_code = None
        self.rx_code_timestamp = None
        self.rx_proto = None
        self.rx_bitlength = None
        self.rx_pulselength = None

        GPIO.setmode(GPIO.BCM)
        _LOGGER.debug("Using GPIO " + str(gpio))

    def cleanup(self):
        """Disable TX and RX and clean up GPIO."""
        if self.tx_enabled:
            self.disable_tx()
        if self.rx_enabled:
            self.disable_rx()
        _LOGGER.debug("Cleanup")
        GPIO.cleanup()

    def enable_tx(self):
        """Enable TX, set up GPIO."""
        if self.rx_enabled:
            _LOGGER.error("RX is enabled, not enabling TX")
            return False
        if not self.tx_enabled:
            self.tx_enabled = True
            GPIO.setup(self.gpio, GPIO.OUT)
            _LOGGER.debug("TX enabled")
        return True

    def disable_tx(self):
        """Disable TX, reset GPIO."""
        if self.tx_enabled:
            # set up GPIO pin as input for safety
            GPIO.setup(self.gpio, GPIO.IN)
            self.tx_enabled = False
            _LOGGER.debug("TX disabled")
        return True

    def tx_code(self, code, tx_proto=None, tx_pulselength=None, tx_length=None):
        """
        Send a decimal code.

        Optionally set protocol, pulselength and code length.
        When none given reset to default protocol, default pulselength and set code length to 24 bits.
        """
        if tx_proto:
            self.tx_proto = tx_proto
        else:
            self.tx_proto = 1
        if tx_pulselength:
            self.tx_pulselength = tx_pulselength
        elif not self.tx_pulselength:
            self.tx_pulselength = PROTOCOLS[self.tx_proto].pulselength
        if tx_length:
            self.tx_length = tx_length
        elif self.tx_proto == 6:
            self.tx_length = 32
        elif (code > 16777216):
            self.tx_length = 32
        else:
            self.tx_length = 24
        rawcode = format(code, '#0{}b'.format(self.tx_length + 2))[2:]
        if self.tx_proto == 6:
            nexacode = ""
            for b in rawcode:
                if b == '0':
                    nexacode = nexacode + "01"
                if b == '1':
                    nexacode = nexacode + "10"
            rawcode = nexacode
            self.tx_length = 64
        if self.tx_proto == 7:
            rawcode = kaku_encode(code)
            self.tx_length = len(rawcode)
        _LOGGER.debug("TX code: " + str(code))
        return self.tx_bin(rawcode)

    def tx_bin(self, rawcode):
        """Send a binary code."""
        _LOGGER.debug("TX bin: " + str(rawcode))
        if self.tx_proto != 7:
            for _ in range(0, self.tx_repeat):
                if self.tx_proto == 6:
                    if not self.tx_sync():
                        return False
                for byte in range(0, self.tx_length):
                    if rawcode[byte] == '0':
                        if not self.tx_l0():
                            return False
                    else:
                        if not self.tx_l1():
                            return False
                if not self.tx_sync():
                    return False
        else:
            for _ in range(0, self.tx_repeat):
                if self.tx_proto == 6:
                    if not self.tx_sync():
                        return False
                for byte in range(0, self.tx_length):
                    if rawcode[byte] == '0':
                        if not self.tx_l0_kaku():
                            return False
                    else:
                        if not self.tx_l1_kaku():
                            return False

        return True

    def tx_l0_kaku(self):
        """Send a '0' bit."""
        if not 0 < self.tx_proto < len(PROTOCOLS):
            _LOGGER.error("Unknown TX protocol")
            return False
        lowpulses = PROTOCOLS[self.tx_proto].zero_low
        """Send basic waveform."""
        if not self.tx_enabled:
            _LOGGER.error("TX is not enabled, not sending data")
            return False
        GPIO.output(self.gpio, GPIO.LOW)
        self._sleep((lowpulses * self.tx_pulselength) / 1000000)
        return True

    def tx_l1_kaku(self):
        """Send a '1' bit."""
        if not 0 < self.tx_proto < len(PROTOCOLS):
            _LOGGER.error("Unknown TX protocol")
            return False
        highpulses = PROTOCOLS[self.tx_proto].one_high
        """Send basic waveform."""
        if not self.tx_enabled:
            _LOGGER.error("TX is not enabled, not sending data")
            return False
        GPIO.output(self.gpio, GPIO.HIGH)
        self._sleep((highpulses * self.tx_pulselength) / 1000000)
        return True

    def tx_waveform_kaku(self, highpulses, lowpulses):
        """Send basic waveform."""
        if not self.tx_enabled:
            _LOGGER.error("TX is not enabled, not sending data")
            return False
        GPIO.output(self.gpio, GPIO.HIGH)
        self._sleep((highpulses * self.tx_pulselength) / 1000000)
        GPIO.output(self.gpio, GPIO.LOW)
        self._sleep((lowpulses * self.tx_pulselength) / 1000000)
        return True

    def tx_l0(self):
        """Send a '0' bit."""
        if not 0 < self.tx_proto < len(PROTOCOLS):
            _LOGGER.error("Unknown TX protocol")
            return False
        return self.tx_waveform(PROTOCOLS[self.tx_proto].zero_high,
                                PROTOCOLS[self.tx_proto].zero_low)

    def tx_l1(self):
        """Send a '1' bit."""
        if not 0 < self.tx_proto < len(PROTOCOLS):
            _LOGGER.error("Unknown TX protocol")
            return False
        return self.tx_waveform(PROTOCOLS[self.tx_proto].one_high,
                                PROTOCOLS[self.tx_proto].one_low)

    def tx_sync(self):
        """Send a sync."""
        if not 0 < self.tx_proto < len(PROTOCOLS):
            _LOGGER.error("Unknown TX protocol")
            return False
        return self.tx_waveform(PROTOCOLS[self.tx_proto].sync_high,
                                PROTOCOLS[self.tx_proto].sync_low)



    def tx_waveform(self, highpulses, lowpulses):
        """Send basic waveform."""
        if not self.tx_enabled:
            _LOGGER.error("TX is not enabled, not sending data")
            return False
        GPIO.output(self.gpio, GPIO.HIGH)
        self._sleep((highpulses * self.tx_pulselength) / 1000000)
        GPIO.output(self.gpio, GPIO.LOW)
        self._sleep((lowpulses * self.tx_pulselength) / 1000000)
        return True

    def enable_rx(self):
        """Enable RX, set up GPIO and add event detection."""
        if self.tx_enabled:
            _LOGGER.error("TX is enabled, not enabling RX")
            return False
        if not self.rx_enabled:
            self.rx_enabled = True
            GPIO.setup(self.gpio, GPIO.IN)
            GPIO.add_event_detect(self.gpio, GPIO.BOTH)
            GPIO.add_event_callback(self.gpio, self.rx_callback)
            _LOGGER.debug("RX enabled")
        return True

    def disable_rx(self):
        """Disable RX, remove GPIO event detection."""
        if self.rx_enabled:
            GPIO.remove_event_detect(self.gpio)
            self.rx_enabled = False
            _LOGGER.debug("RX disabled")
        return True

    # pylint: disable=unused-argument
    def rx_callback(self, gpio):
        """RX callback for GPIO event detection. Handle basic signal detection."""
        timestamp = int(time.perf_counter() * 1000000)
        duration = timestamp - self._rx_last_timestamp

        if duration > 5000:
            if abs(duration - self._rx_timings[0]) < 200:
                self._rx_repeat_count += 1
                self._rx_change_count -= 1
                if self._rx_repeat_count == 2:
                    for pnum in range(1, len(PROTOCOLS)):
                        if self._rx_waveform(pnum, self._rx_change_count, timestamp):
                            _LOGGER.debug("RX code " + str(self.rx_code))
                            break
                    self._rx_repeat_count = 0
            self._rx_change_count = 0

        if self._rx_change_count >= MAX_CHANGES:
            self._rx_change_count = 0
            self._rx_repeat_count = 0
        self._rx_timings[self._rx_change_count] = duration
        self._rx_change_count += 1
        self._rx_last_timestamp = timestamp

    def _rx_waveform(self, pnum, change_count, timestamp):
        """Detect waveform and format code."""
        code = 0
        delay = int(self._rx_timings[0] / PROTOCOLS[pnum].sync_low)
        delay_tolerance = delay * self.rx_tolerance / 100

        for i in range(1, change_count, 2):
            if (abs(self._rx_timings[i] - delay * PROTOCOLS[pnum].zero_high) < delay_tolerance and
                abs(self._rx_timings[i+1] - delay * PROTOCOLS[pnum].zero_low) < delay_tolerance):
                code <<= 1
            elif (abs(self._rx_timings[i] - delay * PROTOCOLS[pnum].one_high) < delay_tolerance and
                  abs(self._rx_timings[i+1] - delay * PROTOCOLS[pnum].one_low) < delay_tolerance):
                code <<= 1
                code |= 1
            else:
                return False

        if self._rx_change_count > 6 and code != 0:
            self.rx_code = code
            self.rx_code_timestamp = timestamp
            self.rx_bitlength = int(change_count / 2)
            self.rx_pulselength = delay
            self.rx_proto = pnum
            return True

        return False
           
    def _sleep(self, delay):      
        _delay = delay / 100
        end = time.time() + delay - _delay
        while time.time() < end:
            time.sleep(_delay)

###############################################################
CONF_CODE_OFF = "code_off"
CONF_CODE_ON = "code_on"
CONF_GPIO = "gpio"
CONF_PULSELENGTH = "pulselength"
CONF_SIGNAL_REPETITIONS = "signal_repetitions"
CONF_LENGTH = "length"

DEFAULT_PROTOCOL = 1
DEFAULT_SIGNAL_REPETITIONS = 10
DEFAULT_LENGTH = 24

SWITCH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CODE_OFF): vol.All(cv.ensure_list_csv, [cv.positive_int]),
        vol.Required(CONF_CODE_ON): vol.All(cv.ensure_list_csv, [cv.positive_int]),
        vol.Optional(CONF_PULSELENGTH): cv.positive_int,
        vol.Optional(CONF_SIGNAL_REPETITIONS, default=DEFAULT_SIGNAL_REPETITIONS): cv.positive_int,
        vol.Optional(CONF_PROTOCOL, default=DEFAULT_PROTOCOL): cv.positive_int,
        vol.Optional(CONF_LENGTH, default=DEFAULT_LENGTH): cv.positive_int,
        vol.Optional(CONF_UNIQUE_ID): cv.string,
    }
)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_GPIO): cv.positive_int,
        vol.Required(CONF_SWITCHES): vol.Schema({cv.string: SWITCH_SCHEMA}),
    }
)


def setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Find and return switches controlled by a generic RF device via GPIO."""

    gpio = config[CONF_GPIO]
    rfdevice = RFDevice(gpio)
    rfdevice_lock = RLock()
    switches = config[CONF_SWITCHES]

    devices = []
    for dev_name, properties in switches.items():
        devices.append(
            RPiRFSwitch(
                properties.get(CONF_NAME, dev_name),
                properties.get(CONF_UNIQUE_ID),
                rfdevice,
                rfdevice_lock,
                properties.get(CONF_PROTOCOL),
                properties.get(CONF_PULSELENGTH),
                properties.get(CONF_LENGTH),
                properties.get(CONF_SIGNAL_REPETITIONS),
                properties.get(CONF_CODE_ON),
                properties.get(CONF_CODE_OFF),
            )
        )
    if devices:
        rfdevice.enable_tx()

    add_entities(devices)

    hass.bus.listen_once(EVENT_HOMEASSISTANT_STOP, lambda event: rfdevice.cleanup())


class RPiRFSwitch(SwitchEntity):
    """Representation of a GPIO RF switch."""

    def __init__(
        self,
        name,
        unique_id,
        rfdevice,
        lock,
        protocol,
        pulselength,
        length,
        signal_repetitions,
        code_on,
        code_off,
    ):
        """Initialize the switch."""
        self._name = name
        self._attr_unique_id = unique_id if unique_id else "{}_{}".format(code_on, code_off)
        self._state = False
        self._rfdevice = rfdevice
        self._lock = lock
        self._protocol = protocol
        self._pulselength = pulselength
        self._length = length
        self._code_on = code_on
        self._code_off = code_off
        self._rfdevice.tx_repeat = signal_repetitions
    
    @property
    def should_poll(self):
        """No polling needed."""
        return False

    @property
    def name(self):
        """Return the name of the switch."""
        return self._name

    @property
    def is_on(self):
        """Return true if device is on."""
        return self._state

    def _send_code(self, code_list, protocol, pulselength, length):
        """Send the code(s) with a specified pulselength."""
        with self._lock:
            _LOGGER.info("Sending code(s): %s", code_list)
            for code in code_list:
                self._rfdevice.tx_code(code, protocol, pulselength, length)
        return True

    def turn_on(self, **kwargs):
        """Turn the switch on."""
        if self._send_code(self._code_on, self._protocol, self._pulselength, self._length):
            self._state = True
            self.schedule_update_ha_state()

    def turn_off(self, **kwargs):
        """Turn the switch off."""
        if self._send_code(self._code_off, self._protocol, self._pulselength, self._length):
            self._state = False
            self.schedule_update_ha_state()
