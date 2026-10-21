"""Lightsaber logic. code.py imports this on a button press or power-up; keepalive
wakeups are handled in code.py alone so this file doesn't need to be compiled for them."""
from adafruit_debouncer import Button # type: ignore
import adafruit_fancyled.adafruit_fancyled as fancy
import adafruit_lis3dh
import alarm
import audiobusio
import audiocore
import audiomixer
import asyncio
import board
from digitalio import DigitalInOut, Direction, Pull
import math
import microcontroller
from microcontroller import watchdog as wdog
import neopixel
import os
from pwmio import PWMOut
import random
import time


NEOPIXEL_AMOUNT = 72
BRIGHTNESS = 0.90
IDLE_SLEEP_SEC = 45.0
# Holding the button this long shuts the saber down without the keepalive, so the power
# bank switches itself off and cuts all power. The power bank button starts it again.
SHUTDOWN_HOLD_SEC = 10.0
# Duration of the fade when changing color
COLOR_FADE_SEC = 0.4
# Rainbow blade scrolls from hilt to tip at this speed
RAINBOW_SCROLL_PIXELS_PER_SEC = 12.0
# Positions computed between two pixels, so the scroll moves smoothly instead of in pixel jumps
RAINBOW_SUBSTEPS = 8
# How often the lit blade is redrawn (rainbow scroll, shimmer) and the hum volume updated
BLADE_FRAME_SEC = 0.02
# After a swing or hit sound, wait this long before another swing sound can start
MOTION_SOUND_COOLDOWN_SEC = 0.3
# Idle hum (1_idle.wav) volume while lit, 0.0-1.0
HUM_LEVEL = 0.25
# Extra hum volume at full swing speed
HUM_MOTION_BOOST = 0.35
# Hum volume is multiplied by this while another sound plays
HUM_DUCKED_FACTOR = 0.4
# xz acceleration (m/s^2 squared) counted as "not moving" for the motion hum; gravity alone is ~96
MOTION_REST = 100.0
# Color options: the palette colors, then rainbow, then fire
RAINBOW_INDEX = 6
FIRE_INDEX = 7
# Clash: a bright pulse runs from hilt to tip and fades out. Only a few LEDs are bright at
# a time, so it stays within what the power bank can supply (two strips of 72 LEDs).
CLASH_PULSE_COLOR = 0xC8C8C8
CLASH_PULSE_SEC = 0.3
CLASH_PULSE_WIDTH = 12
# Fast swings blend the blade towards a dim white, by up to this fraction at full speed
SWING_GLOW_MAX = 0.25
SWING_GLOW_COLOR = 0xA0A0A0
SWING_GLOW_MIN_MOTION = 0.03
# Glow steps for solid colors, where the glow is built into cached shade tables
SWING_GLOW_STEPS = 16
# Ignition: a white-hot point leads the blade out. Retraction: the edge glows like an ember.
IGNITION_TIP_COLOR = 0xFFFFFF
RETRACT_EMBER_COLOR = 0xFF6020
# Red (unstable) ignition stutters: chance per step that the lit part flickers dim
IGNITION_SPUTTER_CHANCE = 0.12
# Color change: the new color runs up the blade from the hilt, with a soft edge this long
WIPE_SOFT_PIXELS = 8
# "Breathing" of the lit blade: dims by up to this fraction of the light and back,
# about once per period. The blade is redrawn this often.
PULSE_DEPTH = 0.4
PULSE_PERIOD_SEC = 2.0
PULSE_FRAME_SEC = 0.02
# Each breath is randomly up to this fraction longer/shorter and deeper/shallower,
# so it never feels like a metronome
PULSE_VARIATION = 0.15
# Share of each breath spent dimming; brightening back is quicker, like breathing in
PULSE_DIM_SHARE = 0.6
# Eyes see brightness roughly as (light output) ** (1 / 2.2). The breath is shaped on
# that "seen" scale, so it doesn't seem to linger near full brightness.
PERCEIVED_GAMMA = 2.2
# Plasma: faint bands of light flowing from hilt to tip on solid colors (not rainbow, crackle
# or fire). Two waves of different length and speed, so the flow never repeats exactly.
# Each is up to this fraction dimmer, this long, and moves this fast:
RIPPLE_DEPTH = 0.12
RIPPLE_WAVELENGTH_PIXELS = 24
RIPPLE_SPEED_PIXELS_PER_SEC = 18.0
RIPPLE2_DEPTH = 0.08
RIPPLE2_WAVELENGTH_PIXELS = 15
RIPPLE2_SPEED_PIXELS_PER_SEC = 11.0
# Ripple positions computed between pixels, for smooth movement
RIPPLE_SUBSTEPS = 4
# Brightness steps in the precomputed shade table of the current color
SHADE_LEVELS = 64
# Unstable "Kylo Ren" crackle, used instead of the pulse for this color (red)
UNSTABLE_COLOR_INDEX = 0
UNSTABLE_FRAME_SEC = 0.03
# The whole blade crackles: each frame this many random pixels get a new shade of red
UNSTABLE_PIXELS_PER_FRAME = 20
# Shades picked from with equal chance: mostly full red, sometimes dimmer, rarely dark
UNSTABLE_SHADES = (
    (0xFF0000,) * 6 + (0xD00000,) * 3 + (0xA00000,) * 3
    + (0x700000,) * 2 + (0x400000, 0x180000)
)
# Brightness drops by up to this fraction, rarely by much (mostly bright, occasional deep dips)
UNSTABLE_FLICKER_DEPTH = 0.4
# Fire blade ("Fire2012"): flames rise from the hilt. Lower cooling = taller flames,
# higher sparking = livelier. Sparks start in the first FIRE_SPARK_PIXELS.
FIRE_FRAME_SEC = 0.03
FIRE_COOLING = 45
FIRE_SPARKING = 120
FIRE_SPARK_PIXELS = 7
# Heat 0-255 to color: never black (embers), up to a warm yellow. On average this draws
# less current than the violet blade.
FIRE_HEAT_COLORS = ((0.0, (60, 6, 0)), (0.35, (255, 40, 0)), (0.7, (255, 120, 5)), (1.0, (255, 170, 40)))
# The fire blade with effects off
FIRE_STILL_COLOR = 0xFF5000
# Settings gestures: hold the button with the blade pointing straight up or down and flip
# the blade to point the other way. Up -> down toggles effects, down -> up toggles sound.
# The accelerometer axis along the blade, and its sign when the tip points up
# (measured on the saber: tip up reads y = -9.3 m/s^2):
BLADE_AXIS = 1  # 0 = x, 1 = y, 2 = z
BLADE_UP_DIRECTION = -1
# Vertical = at least this much gravity along the blade, at most this much sideways (m/s^2)
VERTICAL_MIN_ACCEL = 8.0
VERTICAL_MAX_SIDEWAYS = 4.0
# Without accelerometer, movement can't be detected. Sleep this long after the
# last button press if the blade is on (IDLE_SLEEP_SEC is used when it's off).
NO_ACCEL_ON_SLEEP_SEC = 600.0
WATCHDOG_TIMEOUT = 4.0
WATCHDOG_FEED_INTERVAL = 0.5
# A task that hasn't run for this long is considered hung and the board is reset.
TASK_STALL_LIMIT = 2.5
# microcontroller.nvm layout: the drive is read-only for CircuitPython (PC can write
# code to it), so everything the saber must remember lives in nvm instead.
NVM_COLOR = 0  # 1 byte: chosen color + 1, plus the bits below
NVM_EFFECTS_OFF_BIT = 0x40
NVM_SOUND_OFF_BIT = 0x20
NVM_LOG_LENGTH = 1  # 2 bytes, little endian: bytes used by the error log
NVM_LOG_START = 3  # error log text up to the end of nvm (~4 KB)
# Longest single error entry kept (the traceback is cut after this)
ERROR_ENTRY_MAX_BYTES = 1024


def _nvm():
    return getattr(microcontroller, "nvm", None)


def _saved_log_length(nvm):
    length = nvm[NVM_LOG_LENGTH] | (nvm[NVM_LOG_LENGTH + 1] << 8)
    # Erased flash reads 0xFFFF
    return length if length <= len(nvm) - NVM_LOG_START else 0


def log_error(message, exc=None):
    """Print an error and save it to nvm, where it survives resets. Never raises.

    When the log is full, new errors are only printed: the first errors are usually
    the interesting ones, and this way a repeating error can't wear the flash.
    """
    print("ERROR:", message, repr(exc))
    try:
        nvm = _nvm()
        if nvm is None:
            return
        entry = "--- t={:.1f}s wake={}: {}\n".format(
            time.monotonic(), type(alarm.wake_alarm).__name__, message)
        if exc is not None:
            try:
                import traceback  # only loaded when an error is logged
                entry += "".join(traceback.format_exception(exc, exc, exc.__traceback__))
            except Exception:
                entry += repr(exc) + "\n"
        data = entry.encode()[:ERROR_ENTRY_MAX_BYTES]
        used = _saved_log_length(nvm)
        new_length = used + len(data)
        if NVM_LOG_START + new_length > len(nvm):
            return
        # Length and text in a single write: one flash erase per logged error
        nvm[NVM_LOG_LENGTH:NVM_LOG_START + new_length] = (
            bytes((new_length & 0xFF, new_length >> 8))
            + bytes(nvm[NVM_LOG_START:NVM_LOG_START + used])
            + data
        )
    except Exception:
        pass


def print_saved_errors():
    """Show errors saved by log_error on the USB serial console."""
    nvm = _nvm()
    if nvm is None:
        return
    used = _saved_log_length(nvm)
    if not used:
        return
    print("=== Saved errors. To clear, in the REPL: "
          "import microcontroller; microcontroller.nvm[1:3] = bytes(2)")
    try:
        print(bytes(nvm[NVM_LOG_START:NVM_LOG_START + used]).decode())
    except Exception:
        print(bytes(nvm[NVM_LOG_START:NVM_LOG_START + used]))
    print("=== End of saved errors")


class Lightsaber:
    def __init__(self, accelerometer, blade, sounds, switch, power, sleep_until_alarms):
        self.accelerometer = accelerometer
        self.blade = blade
        self.sounds = sounds
        self.switch = switch
        self.power = power
        self._sleep_until_alarms = sleep_until_alarms
        sounds.muted = not blade.color_store.sound_on
        now = time.monotonic()
        # Updated by each polling task; feed_watchdog resets the board if one goes stale
        self.heartbeats = {"switch": now, "accel": now}
        self.last_activity = now

    def turn_on(self):
        self.power.ensure_on()
        self.sounds.play_sound_by_index(0)
        self.sounds.start_hum()
        self.switch.update_led_color(self.blade.chosen_color_index)
        self.switch.set_state(True)
        self.blade.set_on()
        print("Lightsaber ON")

    def turn_off(self):
        self.sounds.stop_hum()
        self.sounds.play_sound_by_index(2)
        self.blade.set_off()
        self.switch.turn_off_led()
        self.switch.set_state(False)
        print("Lightsaber OFF")

    def deep_sleep(self, keepalive=True):
        print("Going to sleep")
        self.blade.save_color(sound_on=not self.sounds.muted)
        if self.is_on():
            # Retract like a normal turn off, and let the sound finish before sleep cuts it
            self.sounds.stop_hum()
            self.sounds.play_sound_by_index(2)
            self.blade.set_off()
            self.sounds.wait_until_done(timeout=2.0)
        self.switch.turn_off_led()
        self.switch.release_pins()
        self._sleep_until_alarms(keepalive=keepalive)

    def shutdown(self, color_before_press):
        """Very long press: sleep without the keepalive. The power bank sees no load and
        switches itself off within a couple of minutes, so the saber has no power at all
        until the power bank button is pressed. Until then the saber button still wakes it.
        Does not return."""
        print("Shutting down without keepalive")
        self.blade.chosen_color_index = color_before_press  # undo the long-press color change
        self.switch.blink(times=3)
        wdog.feed()  # the retract and its sound take a moment
        self.deep_sleep(keepalive=False)

    def swing(self):
        print("swung")
        self.sounds.play_sound_by_index(random.randint(11, 18))

    def hit(self):
        print("hit")
        self.sounds.play_sound_by_index(random.randint(3, 10))
        self.blade.clash_flash()

    def set_motion(self, intensity):
        """Swing intensity 0.0-1.0 from the accelerometer; drives the hum volume."""
        sounds = self.sounds
        # Rise fast, fade slowly, so the hum swells with a swing and settles after it
        rate = 0.5 if intensity > sounds.motion else 0.1
        sounds.motion += (intensity - sounds.motion) * rate

    def change_color(self):
        # Changing color is use, even if the saber is held still: restart the idle timer
        self.last_activity = time.monotonic()
        self.power.ensure_on()
        self.sounds.play_sound_by_index(13)
        self.blade.set_next_color(self.is_on())
        if self.is_on():
            self.switch.update_led_color(self.blade.chosen_color_index)

    def is_on(self):
        return self.switch.pressed()

    def blade_direction(self):
        """+1 or -1 when the blade points straight up or down (which is which depends on
        how the board is mounted), 0 otherwise or without accelerometer."""
        reading = self.accelerometer.last
        if reading is None:
            return 0
        along = reading[BLADE_AXIS]
        sideways = sum(v * v for i, v in enumerate(reading) if i != BLADE_AXIS)
        if sideways > VERTICAL_MAX_SIDEWAYS ** 2:
            return 0
        if along >= VERTICAL_MIN_ACCEL:
            return 1
        if along <= -VERTICAL_MIN_ACCEL:
            return -1
        return 0

    def toggle_effects(self):
        self.last_activity = time.monotonic()
        self.blade.toggle_effects()
        print("Effects", "on" if self.blade.effects_on else "off")

    def toggle_sound(self):
        """All sounds off/on. One long dip of the blade confirms; turning sound back on
        while lit also restarts the hum, so it is heard too."""
        self.last_activity = time.monotonic()
        sounds = self.sounds
        sounds.muted = not sounds.muted
        if sounds.muted:
            sounds.stop_hum()
            sounds.stop()
            sounds.pause_output()  # amplifier sleeps without an I2S clock
        else:
            if self.power.on:
                sounds.resume_output()
            if self.is_on():
                sounds.start_hum()
        self.blade.confirm(dips=1, dip_sec=0.3)
        print("Sound", "off" if sounds.muted else "on")


class Accelerometer:
    def __init__(self):
        self.tap_threshold = 127
        self.swing_threshold = 180
        self.hit_threshold = 290
        # 0: disabled, 1: single taps, 2: double taps
        self.tap_detection = 1
        # Give up on the accelerometer after this many failed reads in a row
        self.max_consecutive_errors = 50
        self.consecutive_errors = 0
        self.device = None
        self._int1 = None
        self.last = None  # latest (x, y, z), None until the first good reading

    @property
    def available(self):
        return self.device is not None

    def start(self, attempts=3):
        """Try to initialize the LIS3DH. Returns False if it's not available."""
        error = None
        for _ in range(attempts):
            try:
                self.device = self._init_lis3dh()
                return True
            except Exception as e:
                error = e
                self._release_int1()
                time.sleep(0.2)
        log_error("Accelerometer not available, running without it", error)
        return False

    def disable(self, error):
        log_error("Accelerometer stopped responding, running without it", error)
        self.device = None
        self.last = None
        self._release_int1()

    def sample(self):
        """Returns (tapped, x, y, z). Raises OSError on I2C errors."""
        tapped = self.device.tapped
        x, y, z = self.device.acceleration
        return tapped, x, y, z

    def _init_lis3dh(self):
        i2c = board.I2C()
        self._int1 = DigitalInOut(board.ACCELEROMETER_INTERRUPT)
        lis3dh = adafruit_lis3dh.LIS3DH_I2C(i2c, int1=self._int1)
        lis3dh.range = adafruit_lis3dh.RANGE_4_G
        lis3dh.set_tap(self.tap_detection, self.tap_threshold, click_cfg=0x04)  # only y-direction taps
        return lis3dh

    def _release_int1(self):
        if self._int1 is not None:
            try:
                self._int1.deinit()
            except Exception:
                pass
            self._int1 = None


class Switch:
    def __init__(self):
        self.state = asyncio.Event()
        self.btn_pin = DigitalInOut(board.EXTERNAL_BUTTON)
        # RGB LED ring in the switch, active low: duty 65535 = off
        self.led_r = PWMOut(board.D5, frequency=5000, duty_cycle=65535)
        self.led_g = PWMOut(board.D6, frequency=5000, duty_cycle=65535)
        self.led_b = PWMOut(board.D9, frequency=5000, duty_cycle=65535)
        self.last_button_state = False
        self.press_start_time = 0
        self.long_press_threshold = 0.25
        self.animation_task = None
        self.animation = None  # "rainbow", "fire" or None
        self.current_index = 0
        self.palette = [
            fancy.CRGB(255, 0, 0),
            fancy.CRGB(200, 200, 0), # on purpose different from the blade, looks better
            fancy.CRGB(0, 255, 0),
            fancy.CRGB(0, 200, 200),
            fancy.CRGB(0, 0, 255),
            fancy.CRGB(128, 0, 255),
        ]
        self.device = self._init_switch()

    def _init_switch(self):
        self.btn_pin.direction = Direction.INPUT
        self.btn_pin.pull = Pull.UP
        return Button(self.btn_pin, value_when_pressed=False)

    def release_pins(self):
        self.btn_pin.deinit()
        self.led_r.deinit()
        self.led_g.deinit()
        self.led_b.deinit()

    @property
    def rainbow_enabled(self):
        return self.animation == "rainbow"

    def update_led_color(self, index):
        """Show the blade color on the switch LED: cycling colors for rainbow, a flicker for fire."""
        if index == FIRE_INDEX:
            self._start_animation("fire")
        elif index >= len(self.palette):
            self._start_animation("rainbow")
        else:
            self._stop_animation()
            prev_color = self.palette[self.current_index]
            self.current_index = index
            self._fade_colors(prev_color, self.palette[index])

    def turn_off_led(self):
        self._stop_animation()
        self._set_led(0.0, 0.0, 0.0)

    def blink(self, times):
        """Flash the LED ring white, e.g. to confirm a shutdown."""
        self._stop_animation()
        for _ in range(times):
            self._set_led(1.0, 1.0, 1.0)
            time.sleep(0.1)
            self._set_led(0.0, 0.0, 0.0)
            time.sleep(0.1)

    def _start_animation(self, kind):
        self.animation = kind
        if not self.animation_task or self.animation_task.done():
            self.animation_task = asyncio.create_task(self._animate())

    async def _animate(self):
        step = 0.0
        while self.animation:
            if self.animation == "fire":
                self._set_led(0.7 + 0.3 * random.random(), 0.1 + 0.3 * random.random(), 0.0)
                await asyncio.sleep(0.06)
            else:
                color = fancy.palette_lookup(self.palette, step)
                self._set_led(color.red, color.green, color.blue)
                step = (step + 0.01) % 1.0
                await asyncio.sleep(0.09)

    def _stop_animation(self):
        self.animation = None
        if self.animation_task and not self.animation_task.done():
            self.animation_task.cancel()

    def _fade_colors(self, old_color, new_color):
        steps = 50
        for i in range(1, steps + 1):
            ratio = i / steps
            self._set_led(
                old_color.red * (1 - ratio) + new_color.red * ratio,
                old_color.green * (1 - ratio) + new_color.green * ratio,
                old_color.blue * (1 - ratio) + new_color.blue * ratio,
            )
            time.sleep(0.002)

    def _set_led(self, r, g, b):
        """Channels 0.0-1.0."""
        self.led_r.duty_cycle = 65535 - int(r * 65535)
        self.led_g.duty_cycle = 65535 - int(g * 65535)
        self.led_b.duty_cycle = 65535 - int(b * 65535)

    def new_value(self):
        return self.device.value

    def pressed(self):
        return self.state.is_set()

    def set_state(self, state):
        if state:
            self.state.set()
        else:
            self.state.clear()

    def get_press_duration(self):
        if not self.last_button_state:
            return 0
        return time.monotonic() - self.press_start_time


class ColorStore:
    """Remembers the chosen color and the effects setting across sleeps and power-offs.

    Uses microcontroller.nvm (outside the filesystem, so an interrupted write
    can't corrupt code.py) and writes only when the value has changed.
    """
    LEGACY_FILE = "/state"

    def __init__(self, color_count, rainbow_index):
        self.color_count = color_count
        self.rainbow_index = rainbow_index
        self.nvm = _nvm()
        self.saved = None
        self.effects_on = True  # set by load()
        self.sound_on = True

    def load(self):
        index = self._read_nvm()
        if index is not None:
            self.saved = self.nvm[NVM_COLOR]
            self.effects_on = not self.nvm[NVM_COLOR] & NVM_EFFECTS_OFF_BIT
            self.sound_on = not self.nvm[NVM_COLOR] & NVM_SOUND_OFF_BIT
            return index
        # Older versions saved the color to a file; carry it over once
        index = self._read_file()
        if index is not None:
            if self.nvm is not None:
                self.save(index)
            else:
                self.saved = index
            return index
        print("No saved color, using default")
        return 0

    def save(self, index, effects_on=True, sound_on=True):
        if self.nvm is None:
            value = index
        else:
            # 0xFF (erased) and 0 mean "nothing saved"
            value = ((index + 1) | (0 if effects_on else NVM_EFFECTS_OFF_BIT)
                     | (0 if sound_on else NVM_SOUND_OFF_BIT))
        if value == self.saved:
            return
        try:
            if self.nvm is not None:
                self.nvm[NVM_COLOR] = value
            else:
                with open(self.LEGACY_FILE, "w") as f:
                    f.write(str(index))
            self.saved = value
        except Exception as e:
            print("Cannot save color:", e)

    def _read_nvm(self):
        if self.nvm is None:
            return None
        try:
            value = self.nvm[NVM_COLOR]
        except Exception:
            return None
        if value & 0x80:  # erased flash (0xFF) or garbage
            return None
        value &= ~(NVM_EFFECTS_OFF_BIT | NVM_SOUND_OFF_BIT)
        if 1 <= value <= self.color_count:
            return value - 1
        return None

    def _read_file(self):
        try:
            with open(self.LEGACY_FILE, "r") as f:
                value = int(f.read())
        except Exception:
            return None
        if value == -1:  # old versions used -1 for rainbow
            value = self.rainbow_index
        if 0 <= value < self.color_count:
            return value
        return None


class Blade:
    def __init__(self):
        self.neopixel_amount = NEOPIXEL_AMOUNT
        self.brightness = BRIGHTNESS
        self.off = 0
        self.palette = [
            fancy.CRGB(255, 0, 0),    # red
            fancy.CRGB(255, 100, 0),  # yellow
            fancy.CRGB(0, 255, 0),    # green
            fancy.CRGB(0, 200, 200),  # cyan
            fancy.CRGB(0, 0, 255),    # blue
            fancy.CRGB(128, 0, 255),  # violet
        ]
        # Color options are the palette colors followed by rainbow and fire
        self.rainbow_index = RAINBOW_INDEX
        self.fire_index = FIRE_INDEX
        self.color_count = FIRE_INDEX + 1
        self._rainbow_phases = None  # built on first use
        self._rainbow_pos = 0.0  # scroll position in substeps
        self._last_animation = 0.0
        self._last_frame_time = 0.0
        self._new_breath(0.0)
        self._ripple_pos = 0.0  # in substeps
        self._last_ripple = 0.0
        self._ripple_table = None  # built on first use
        self._ripple_table2 = None
        self._ripple_pos2 = 0.0
        self._heat = [0] * self.neopixel_amount  # fire blade
        self._last_fire = 0.0
        self._fire_colors = None  # heat 0-255 -> color, built on first use
        self._clash_start = None
        self.motion = 0.0  # swing intensity 0.0-1.0 (for the swing glow)
        self._shades_color = None
        self._shades = None  # the current color at SHADE_LEVELS + 1 brightness steps
        self._shade_cache = {}  # glow step -> shade table, for the current color
        self._crackle_frame = None  # current per-pixel shades of the unstable blade
        self._crackle_dim = 0.0
        self._last_crackle = 0.0
        self._shown = [self.off] * self.neopixel_amount  # what the LEDs show right now
        self.color_store = ColorStore(self.color_count, self.rainbow_index)
        self.chosen_color_index = self.color_store.load()
        self.effects_on = self.color_store.effects_on
        self.device = self._init_neopixels()

    def save_color(self, sound_on=None):
        if sound_on is None:
            sound_on = self.color_store.sound_on
        self.color_store.save(self.chosen_color_index, self.effects_on, sound_on)

    def confirm(self, dips, dip_sec):
        """Dip the blade brightness to acknowledge a setting change."""
        shown = self._shown
        for _ in range(dips):
            self._show(_scaled(shown, 0.25))
            time.sleep(dip_sec)
            self._show(shown)
            time.sleep(0.08)

    def toggle_effects(self):
        """Effects (breathing, flowing bands, crackle, rainbow scroll) on/off. The blade dips
        twice to confirm, then continues in the new mode."""
        self.effects_on = not self.effects_on
        self.confirm(dips=2, dip_sec=0.08)
        self._crossfade()

    def set_next_color(self, change_immediately=False):
        self.chosen_color_index = (self.chosen_color_index + 1) % self.color_count
        if self.is_fire():
            self._warm_fire()
        if change_immediately:
            self._wipe()

    def is_unstable(self):
        return self.chosen_color_index == UNSTABLE_COLOR_INDEX

    def is_rainbow(self):
        return self.chosen_color_index == self.rainbow_index

    def is_fire(self):
        return self.chosen_color_index == self.fire_index

    def set_on(self):
        now = time.monotonic()
        self._crackle_frame = None
        self._clash_start = None
        if self.is_fire():
            self._warm_fire()
        self._new_breath(now)  # the first breath starts at full brightness
        self._ignite(self._render(now))
        # Continue the animation from exactly this frame
        now = time.monotonic()
        self._new_breath(now)
        self._last_ripple = self._last_animation = now

    def set_off(self):
        """Retract from tip to hilt; the receding edge glows like an ember."""
        n = self.neopixel_amount
        shown = list(self._shown)
        for i in range(n - 1, -1, -1):
            shown[i] = _mix(shown[i], RETRACT_EMBER_COLOR, 0.6)
            if i + 1 < n:
                shown[i + 1] = self.off
            self.device[:] = shown
            self.device.show()
            time.sleep(0.004)
        self._show([self.off] * n)

    def _ignite(self, frame):
        """Extend the blade from hilt to tip behind a white-hot leading point. The red
        (unstable) blade stutters on the way out."""
        n = self.neopixel_amount
        sputter = self.effects_on and self.is_unstable()
        for i in range(n):
            out = frame[:i] + [_mix(frame[i], IGNITION_TIP_COLOR, 0.7)] + [self.off] * (n - i - 1)
            if i > 0:
                out[i - 1] = _mix(frame[i - 1], IGNITION_TIP_COLOR, 0.3)
            if sputter and random.random() < IGNITION_SPUTTER_CHANCE:
                out = _scaled(out, 0.3)
            self.device[:] = out
            self.device.show()
            time.sleep(0.004)
        self._show(frame)

    def _init_neopixels(self):
        pixels = neopixel.NeoPixel(board.EXTERNAL_NEOPIXELS, self.neopixel_amount, auto_write=False)
        # Fixed: all effects are drawn into the pixel values, so frames can be blended exactly
        pixels.brightness = self.brightness
        return pixels

    def animate(self, motion=0.0):
        """Draw the next animation frame. Only call while the blade is lit.
        motion: swing intensity 0.0-1.0, for the swing glow."""
        self.motion = motion
        now = time.monotonic()
        if now - self._last_frame_time < PULSE_FRAME_SEC:
            return
        self._last_frame_time = now
        frame = self._render(now)
        if frame != self._shown:  # with effects off, nothing changes: skip the redraw
            self._show(frame)

    def clash_flash(self):
        """Start the clash pulse; the animation frames draw it, so this doesn't block."""
        self._clash_start = time.monotonic()

    def _show(self, frame):
        self.device[:] = frame
        self.device.show()
        self._shown = frame

    def _render(self, now):
        """The current color's animated frame at this moment, with every effect applied:
        crackle (red), fire, or breathing plus a flowing plasma (solid) or a scroll (rainbow),
        then the swing glow and clash pulse. With effects off: the plain color
        (rainbow standing still), and only the clash pulse."""
        glowing = self.effects_on and self.motion > SWING_GLOW_MIN_MOTION
        if not self.effects_on:
            frame = self._frame(self.chosen_color_index)
        elif self.is_unstable():
            frame = self._crackle(now)
        elif self.is_fire():
            frame = self._fire(now)
        else:
            light = self._breath(now)
            if self.is_rainbow():
                self._advance_rainbow(now)
                frame = _scaled(self._rainbow_frame(), light)
            else:
                # Solid colors: the swing glow is built into the shade table, which is much
                # cheaper than blending every pixel on every frame
                glow_step = round(self.motion * SWING_GLOW_STEPS) if glowing else 0
                glowing = False
                shades = self._shade_table(glow_step)
                k = light * SHADE_LEVELS
                wave1, wave2 = self._ripple(now)
                frame = [shades[int(k * a * b)] for a, b in zip(wave1, wave2)]
        if glowing:
            frame = _blend_all(frame, SWING_GLOW_COLOR, SWING_GLOW_MAX * self.motion)
        if self._clash_start is not None:
            self._clash_pulse(frame, now)
        return frame

    def _clash_pulse(self, frame, now):
        """A bright band running from hilt to tip, fading as it goes."""
        t = now - self._clash_start
        if t >= CLASH_PULSE_SEC:
            self._clash_start = None
            return
        n = self.neopixel_amount
        half = CLASH_PULSE_WIDTH / 2
        center = t / CLASH_PULSE_SEC * (n + CLASH_PULSE_WIDTH) - half
        fade = 1 - t / CLASH_PULSE_SEC
        for i in range(max(0, int(center - half)), min(n, int(center + half) + 1)):
            weight = fade * (1 - abs(center - i) / half)
            if weight > 0:
                frame[i] = _mix(frame[i], CLASH_PULSE_COLOR, weight)

    def _wipe(self):
        """Color change: the new color's live animation runs up the blade from the hilt
        over what was shown, with a soft edge."""
        n = self.neopixel_amount
        old = self._shown
        start = time.monotonic()
        while True:
            now = time.monotonic()
            t = (now - start) / COLOR_FADE_SEC
            new = self._render(now)
            if t >= 1:
                break
            front = t * (n + WIPE_SOFT_PIXELS)
            out = []
            for i in range(n):
                w = (front - i) / WIPE_SOFT_PIXELS
                if w >= 1:
                    out.append(new[i])
                elif w <= 0:
                    out.append(old[i])
                else:
                    out.append(_mix(old[i], new[i], w))
            self._show(out)
        self._show(new)
        self._last_frame_time = now

    def _crossfade(self):
        """Blend from what is shown now into the new color's live animation, which keeps
        moving during the fade, so neither end of the fade jumps."""
        old = [((c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF) for c in self._shown]
        start = time.monotonic()
        while True:
            now = time.monotonic()
            k = int((now - start) / COLOR_FADE_SEC * 256)
            new = self._render(now)
            if k >= 256:
                break
            self._show([
                ((r + ((((c >> 16) & 0xFF) - r) * k >> 8)) << 16)
                | ((g + ((((c >> 8) & 0xFF) - g) * k >> 8)) << 8)
                | (b + (((c & 0xFF) - b) * k >> 8))
                for (r, g, b), c in zip(old, new)
            ])
        self._show(new)
        self._last_frame_time = now

    def _new_breath(self, start):
        """Pick the length and depth of the next breath, with a little randomness."""
        v = PULSE_VARIATION
        self._breath_start = start
        self._breath_period = PULSE_PERIOD_SEC * (1 + v * (2 * random.random() - 1))
        depth = min(0.9, PULSE_DEPTH * (1 + v * (2 * random.random() - 1)))
        self._breath_min_seen = (1 - depth) ** (1 / PERCEIVED_GAMMA)

    def _breath(self, now):
        """Light output factor (1.0 = full) of the breathing at this moment."""
        u = (now - self._breath_start) / self._breath_period
        if u >= 1:
            # Next breath. After a long pause (blocking animation), start it fresh now.
            self._new_breath(now if u >= 2 else self._breath_start + self._breath_period)
            u = (now - self._breath_start) / self._breath_period
        if u < PULSE_DIM_SHARE:
            x = u / PULSE_DIM_SHARE  # dimming
        else:
            x = 1 - (u - PULSE_DIM_SHARE) / (1 - PULSE_DIM_SHARE)  # brightening back
        # Half steady, half sine: soft turns at the top and bottom that never stop
        eased = 0.5 * x + 0.25 * (1 - math.cos(math.pi * x))
        seen = 1 - (1 - self._breath_min_seen) * eased
        return seen ** PERCEIVED_GAMMA

    def _ripple(self, now):
        """The two plasma waves flowing from hilt to tip, as per-pixel factors (1.0 = no
        dimming); the frame is their product."""
        n = self.neopixel_amount
        steps = RIPPLE_SUBSTEPS
        len1 = RIPPLE_WAVELENGTH_PIXELS * steps
        len2 = RIPPLE2_WAVELENGTH_PIXELS * steps
        if self._ripple_table is None:
            self._ripple_table = _wave_table(len1, RIPPLE_DEPTH, n * steps)
            self._ripple_table2 = _wave_table(len2, RIPPLE2_DEPTH, n * steps)
        dt = min(now - self._last_ripple, 0.05)
        self._last_ripple = now
        self._ripple_pos = (self._ripple_pos + dt * RIPPLE_SPEED_PIXELS_PER_SEC * steps) % len1
        self._ripple_pos2 = (self._ripple_pos2 + dt * RIPPLE2_SPEED_PIXELS_PER_SEC * steps) % len2
        # Negative offsets move the bands towards the tip (higher pixel numbers)
        o1 = (-int(self._ripple_pos)) % len1
        o2 = (-int(self._ripple_pos2)) % len2
        return (self._ripple_table[o1:o1 + n * steps:steps],
                self._ripple_table2[o2:o2 + n * steps:steps])

    def _shade_table(self, glow_step=0):
        """The current solid color at SHADE_LEVELS + 1 brightness steps, blended towards
        white for the swing glow. Built once per color and glow step, then cached."""
        color = self.palette[self.chosen_color_index].pack()
        if color != self._shades_color:
            self._shade_cache = {}
            self._shades_color = color
        shades = self._shade_cache.get(glow_step)
        if shades is None:
            r, g, b = (color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF
            shades = [
                (r * j // SHADE_LEVELS) << 16 | (g * j // SHADE_LEVELS) << 8 | (b * j // SHADE_LEVELS)
                for j in range(SHADE_LEVELS + 1)
            ]
            if glow_step:
                amount = SWING_GLOW_MAX * glow_step / SWING_GLOW_STEPS
                shades = _blend_all(shades, SWING_GLOW_COLOR, amount)
            self._shade_cache[glow_step] = shades
        self._shades = shades
        return shades

    def _advance_rainbow(self, now):
        """Move the rainbow scroll position."""
        # Cap the step so the rainbow doesn't jump after a blocking animation or a pause
        dt = min(now - self._last_animation, 0.05)
        self._last_animation = now
        total = self.neopixel_amount * RAINBOW_SUBSTEPS
        self._rainbow_pos = (self._rainbow_pos + dt * RAINBOW_SCROLL_PIXELS_PER_SEC * RAINBOW_SUBSTEPS) % total

    def _crackle(self, now):
        """The unstable blade: random pixels all along the blade change shade, plus an
        uneven flicker of the whole blade. Changes every UNSTABLE_FRAME_SEC."""
        frame = self._crackle_frame
        if frame is None:
            frame = self._crackle_frame = self._frame(self.chosen_color_index)
        if now - self._last_crackle >= UNSTABLE_FRAME_SEC:
            self._last_crackle = now
            n = self.neopixel_amount
            shades = UNSTABLE_SHADES
            for _ in range(UNSTABLE_PIXELS_PER_FRAME):
                frame[random.randrange(n)] = shades[random.randrange(len(shades))]
            # random() cubed: mostly near full brightness, now and then a deep dip
            self._crackle_dim = UNSTABLE_FLICKER_DEPTH * random.random() ** 3
        return _scaled(frame, 1 - self._crackle_dim)

    def _fire(self, now):
        """The fire blade: flames rising from the hilt (the classic "Fire2012" effect)."""
        if now - self._last_fire >= FIRE_FRAME_SEC:
            self._last_fire = now
            self._fire_step()
        colors = self._fire_palette()
        return [colors[h] for h in self._heat]

    def _warm_fire(self):
        """Start with flames already burning instead of a dark blade."""
        self._heat = [0] * self.neopixel_amount
        for _ in range(60):
            self._fire_step()

    def _fire_step(self):
        n = self.neopixel_amount
        heat = self._heat
        cooling = FIRE_COOLING * 10 // n + 2
        for i in range(n):
            h = heat[i] - random.randrange(cooling)
            heat[i] = h if h > 0 else 0
        for k in range(n - 1, 1, -1):  # heat drifts up towards the tip
            heat[k] = (heat[k - 1] + 2 * heat[k - 2]) // 3
        if random.random() < FIRE_SPARKING / 255:
            y = random.randrange(FIRE_SPARK_PIXELS)
            heat[y] = min(255, heat[y] + random.randrange(160, 256))

    def _fire_palette(self):
        if self._fire_colors is None:
            colors = []
            stops = FIRE_HEAT_COLORS
            for h in range(256):
                t = h / 255
                for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
                    if t <= t1:
                        f = (t - t0) / (t1 - t0)
                        r, g, b = (int(a + (z - a) * f) for a, z in zip(c0, c1))
                        colors.append((r << 16) | (g << 8) | b)
                        break
            self._fire_colors = colors
        return self._fire_colors

    def _frame(self, index):
        """Packed color of every pixel for the given color option, without animation."""
        if index == self.fire_index:
            return [FIRE_STILL_COLOR] * self.neopixel_amount
        if index == self.rainbow_index:
            return self._rainbow_frame()
        return [self.palette[index].pack()] * self.neopixel_amount

    def _rainbow_frame(self):
        """Rainbow at the current scroll position: just a slice of a precomputed list."""
        if self._rainbow_phases is None:
            self._build_rainbow()
        n = self.neopixel_amount
        # Negative offset moves the colors towards the tip (higher pixel numbers)
        offset = (-int(self._rainbow_pos)) % (n * RAINBOW_SUBSTEPS)
        start = offset // RAINBOW_SUBSTEPS
        return self._rainbow_phases[offset % RAINBOW_SUBSTEPS][start:start + n]

    def _build_rainbow(self):
        """For each substep, the rainbow shifted by that fraction of a pixel, stored twice
        in a row so any rotation is a single slice."""
        n = self.neopixel_amount
        base = [
            fancy.gamma_adjust(fancy.palette_lookup(self.palette, i / n)).pack()
            for i in range(n)
        ]
        self._rainbow_phases = []
        for step in range(RAINBOW_SUBSTEPS):
            t = step / RAINBOW_SUBSTEPS
            row = [_mix(base[i], base[(i + 1) % n], t) for i in range(n)]
            self._rainbow_phases.append(row + row)


def _wave_table(length, depth, extra):
    """One sine wave of brightness factors, repeated so every offset is one strided slice."""
    wave = [1 - depth * (0.5 + 0.5 * math.sin(2 * math.pi * k / length)) for k in range(length)]
    return [wave[k % length] for k in range(length + extra)]


def _blend_all(frame, color, amount):
    """Every pixel blended towards color by amount (0.0-1.0), integer math."""
    a = int(amount * 256)
    cr, cg, cb = (color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF
    out = []
    for c in frame:
        r, g, b = (c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF
        out.append(((r + ((cr - r) * a >> 8)) << 16) | ((g + ((cg - g) * a >> 8)) << 8)
                   | (b + ((cb - b) * a >> 8)))
    return out


def _scaled(frame, light):
    """frame with every pixel dimmed to light (0.0-1.0), integer math."""
    k = int(light * 256)
    if k >= 256:
        return list(frame)
    return [
        ((((c >> 16) & 0xFF) * k >> 8) << 16) | ((((c >> 8) & 0xFF) * k >> 8) << 8) | ((c & 0xFF) * k >> 8)
        for c in frame
    ]


def _mix(a, b, t):
    """Linear blend between two packed 0xRRGGBB colors, t in 0..1."""
    if a == b:
        return a
    ar, ag, ab = (a >> 16) & 0xFF, (a >> 8) & 0xFF, a & 0xFF
    br, bg, bb = (b >> 16) & 0xFF, (b >> 8) & 0xFF, b & 0xFF
    return (
        (int(ar + (br - ar) * t) << 16)
        | (int(ag + (bg - ag) * t) << 8)
        | int(ab + (bb - ab) * t)
    )


class Sounds:
    """Two mixer voices: the looping hum, and effects (on/off/swing/clash) on top of it."""
    HUM_INDEX = 1

    def __init__(self):
        self.device = audiobusio.I2SOut(board.I2S_BIT_CLOCK, board.I2S_WORD_SELECT, board.I2S_DATA)
        self.files = self._get_sound_files()
        self._file = None
        self._wave = None
        self._hum_wave = None
        self._hum_on = False
        self.motion = 0.0  # swing intensity 0.0-1.0, set by Lightsaber.set_motion
        self.muted = False  # set from the saved setting by Lightsaber
        self.mixer = self._make_mixer()
        self.hum_voice = self.mixer.voice[0]
        self.fx_voice = self.mixer.voice[1]
        self.fx_voice.level = 1.0
        # Output starts with Power.ensure_on(): with no I2S clock the amplifier sleeps

    def resume_output(self):
        if not self.device.playing:
            self.device.play(self.mixer)

    def pause_output(self):
        self.device.stop()

    def _make_mixer(self):
        # The mixer must match the files' format; read it from the first file
        rate, bits, channels = 22050, 16, 1
        if self.files:
            try:
                with open(self.files[0], "rb") as f:
                    wave = audiocore.WaveFile(f)
                    rate, bits, channels = wave.sample_rate, wave.bits_per_sample, wave.channel_count
                    wave.deinit()
            except Exception as e:
                print("Cannot read sound format:", e)
        return audiomixer.Mixer(
            voice_count=2, sample_rate=rate, channel_count=channels,
            bits_per_sample=bits, samples_signed=(bits == 16), buffer_size=2048)

    def play_sound_by_index(self, index, loop=False):
        if self.muted or not 0 <= index < len(self.files):
            return
        try:
            self.stop()
            self._file = open(self.files[index], "rb")
            self._wave = audiocore.WaveFile(self._file)
            self.fx_voice.play(self._wave, loop=loop)
        except Exception as e:
            print("Cannot play sound:", e)

    def start_hum(self):
        if self.muted or self.HUM_INDEX >= len(self.files):
            return
        try:
            if self._hum_wave is None:
                # Kept open while the board is awake; it loops for as long as the blade is lit
                self._hum_wave = audiocore.WaveFile(open(self.files[self.HUM_INDEX], "rb"))
            self._hum_on = True
            self.update_hum()
            self.hum_voice.play(self._hum_wave, loop=True)
        except Exception as e:
            print("Cannot play hum:", e)

    def stop_hum(self):
        self._hum_on = False
        self.hum_voice.stop()

    def update_hum(self):
        """Set hum volume from swing motion, quieter while an effect plays."""
        if not self._hum_on:
            return
        level = HUM_LEVEL + HUM_MOTION_BOOST * self.motion
        if self.fx_voice.playing:
            level *= HUM_DUCKED_FACTOR
        self.hum_voice.level = min(level, 1.0)

    def wait_until_done(self, timeout):
        end = time.monotonic() + timeout
        while self.fx_voice.playing and time.monotonic() < end:
            time.sleep(0.01)

    def play_sound_by_name(self, name, loop=False):
        # Not implemented
        pass

    def stop(self):
        """Stop the effect sound and close its file. The hum keeps playing."""
        self.fx_voice.stop()
        if self._wave is not None:
            try:
                self._wave.deinit()
            except Exception:
                pass
            self._wave = None
        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None

    def _get_sound_files(self):
        wavs = []
        try:
            filenames = os.listdir('/sounds')
        except OSError as e:
            log_error("Cannot list /sounds", e)
            return wavs
        for filename in filenames:
            if filename.lower().endswith('.wav') and not filename.startswith('.'):
                wavs.append("/sounds/" + filename)
        wavs.sort()
        return wavs


class Power:
    """EXTERNAL_POWER feeds the LED strips and the audio amplifier. It stays on the whole
    time the board is awake: the strips draw ~100 mA even when dark, and that load is what
    keeps the power bank from switching itself off until the keepalive takes over."""

    def __init__(self, sounds):
        self.sounds = sounds
        self.pin = DigitalInOut(board.EXTERNAL_POWER)
        self.pin.direction = Direction.OUTPUT
        self.pin.value = False
        self.on = False

    def ensure_on(self):
        if self.on:
            return
        self.pin.value = True
        time.sleep(0.01)  # let the LEDs and amplifier power up
        if not self.sounds.muted:
            self.sounds.resume_output()
        self.on = True


async def feed_watchdog(lightsaber):
    while True:
        await asyncio.sleep(WATCHDOG_FEED_INTERVAL)
        now = time.monotonic()
        for name, last in lightsaber.heartbeats.items():
            if now - last > TASK_STALL_LIMIT:
                log_error("Task '{}' stalled".format(name))
                restart_after_error()
        wdog.feed()


async def poll_accelerometer(lightsaber):
    accel = lightsaber.accelerometer
    prev_xy_accel = 0.0
    still_since = time.monotonic()
    # One sound per movement: re-armed when the movement calms below the swing threshold
    swing_armed = True
    hit_armed = True
    swing_allowed_at = 0.0
    while True:
        await asyncio.sleep(0.01)
        now = time.monotonic()
        lightsaber.heartbeats["accel"] = now

        if not accel.available:
            limit = NO_ACCEL_ON_SLEEP_SEC if lightsaber.is_on() else IDLE_SLEEP_SEC
            if now - lightsaber.last_activity > limit:
                print("Idle, going to sleep")
                lightsaber.deep_sleep()
            continue

        try:
            tapped, x, y, z = accel.sample()
            accel.consecutive_errors = 0
            accel.last = (x, y, z)
        except (OSError, RuntimeError) as e:
            accel.consecutive_errors += 1
            if accel.consecutive_errors >= accel.max_consecutive_errors:
                accel.disable(e)
                lightsaber.last_activity = now
            continue

        xz_accel = x * x + z * z
        intensity = (xz_accel - MOTION_REST) / (accel.hit_threshold - MOTION_REST)
        lightsaber.set_motion(min(max(intensity, 0.0), 1.0))
        if tapped and not lightsaber.switch.last_button_state:
            # (ignored while the button is held: flipping the blade can register as a tap)
            print("tapped")
            lightsaber.change_color()
        elif lightsaber.is_on():
            # Check hit first: its threshold is higher, so it also passes the swing check
            if xz_accel > accel.hit_threshold:
                if hit_armed:  # a hit may interrupt a swing sound
                    lightsaber.hit()
                    hit_armed = swing_armed = False
                    swing_allowed_at = now + MOTION_SOUND_COOLDOWN_SEC
            elif xz_accel > accel.swing_threshold:
                if swing_armed and now >= swing_allowed_at:
                    lightsaber.swing()
                    swing_armed = False
                    swing_allowed_at = now + MOTION_SOUND_COOLDOWN_SEC
            else:
                swing_armed = hit_armed = True

        xy_accel = x * x + y * y
        if abs(xy_accel - prev_xy_accel) >= 11.0:
            prev_xy_accel = xy_accel
            still_since = now
        # Button presses and color changes also count as use, not just movement
        still_since = max(still_since, lightsaber.last_activity)
        if now - still_since > IDLE_SLEEP_SEC:
            print("Idle, going to sleep")
            lightsaber.deep_sleep()


async def poll_switch(lightsaber, turn_on_at_start=False):
    switch = lightsaber.switch
    long_press_handled = False
    press_direction = 0  # blade direction when the press started (for the flip gesture)
    color_at_press = lightsaber.blade.chosen_color_index
    ignore_press = False
    if turn_on_at_start:
        lightsaber.turn_on()
    switch.device.update()
    if not switch.new_value():
        # Button is still held from the press that woke us up: ignore that press
        switch.last_button_state = True
        switch.press_start_time = time.monotonic()
        long_press_handled = True
        ignore_press = True

    while True:
        switch.device.update()
        await asyncio.sleep(0.01)
        lightsaber.heartbeats["switch"] = time.monotonic()

        current_button_state = not switch.new_value()

        if current_button_state != switch.last_button_state:
            lightsaber.last_activity = time.monotonic()

        if current_button_state and not switch.last_button_state:
            # Button just pressed
            switch.press_start_time = time.monotonic()
            long_press_handled = False
            ignore_press = False
            color_at_press = lightsaber.blade.chosen_color_index
            press_direction = lightsaber.blade_direction() if lightsaber.is_on() else 0

        elif current_button_state:
            # Button held
            press_duration = time.monotonic() - switch.press_start_time
            if ignore_press:
                pass
            elif press_duration >= SHUTDOWN_HOLD_SEC:
                lightsaber.shutdown(color_at_press)
            elif long_press_handled:
                pass
            elif press_direction:
                # Pressed with the blade vertical: flipping it the other way while holding
                # toggles effects (up -> down) or sound (down -> up). Without a flip this
                # becomes a color change on release.
                if lightsaber.blade_direction() == -press_direction:
                    if press_direction == BLADE_UP_DIRECTION:
                        lightsaber.toggle_effects()  # up -> down
                    else:
                        lightsaber.toggle_sound()  # down -> up
                    long_press_handled = True
            elif press_duration >= switch.long_press_threshold:
                if lightsaber.is_on():
                    lightsaber.change_color()
                else:
                    lightsaber.turn_on()
                long_press_handled = True

        elif not current_button_state and switch.last_button_state:
            # Button released
            press_duration = switch.get_press_duration()
            if ignore_press:
                pass
            elif press_duration < switch.long_press_threshold:
                if lightsaber.is_on():
                    lightsaber.turn_off()
                else:
                    lightsaber.turn_on()
            elif not long_press_handled and lightsaber.is_on():
                lightsaber.change_color()  # vertical long press without a flip

        switch.last_button_state = current_button_state


async def animate_blade(lightsaber):
    blade = lightsaber.blade
    while True:
        await asyncio.sleep(BLADE_FRAME_SEC)
        if lightsaber.is_on():
            blade.animate(motion=lightsaber.sounds.motion)
            lightsaber.sounds.update_hum()


def restart_after_error():
    """Recover from an error without a reset loop: exiting to deep sleep releases every
    pin and peripheral, the board wakes up a second later on the keepalive path and then
    waits for the button as usual. An error that happens on every start then costs one
    button press, not a board stuck resetting and draining the battery. Does not return."""
    alarm.exit_and_deep_sleep_until_alarms(
        alarm.time.TimeAlarm(monotonic_time=time.monotonic() + 1))


def handle_task_exception(loop, context):
    log_error("Task failed: {}".format(context.get("message", "")), context.get("exception"))
    restart_after_error()


def run(sleep_until_alarms):
    """Run the lightsaber until it goes to deep sleep. code.py has already enabled the watchdog."""
    print_saved_errors()
    accelerometer = Accelerometer()
    accelerometer.start()
    wdog.feed()

    sounds = Sounds()
    lightsaber = Lightsaber(
        accelerometer,
        Blade(),
        sounds,
        Switch(),
        Power(sounds),
        sleep_until_alarms,
    )
    lightsaber.power.ensure_on()  # stays on while awake, see Power
    wdog.feed()

    loop = asyncio.get_event_loop()
    if hasattr(loop, "set_exception_handler"):
        loop.set_exception_handler(handle_task_exception)
    # Woken up by the button: ignite right away instead of needing a second press
    woken_by_button = isinstance(alarm.wake_alarm, alarm.pin.PinAlarm)
    loop.create_task(poll_switch(lightsaber, turn_on_at_start=woken_by_button))
    loop.create_task(poll_accelerometer(lightsaber))
    loop.create_task(animate_blade(lightsaber))
    loop.create_task(feed_watchdog(lightsaber))
    loop.run_forever()
    # run_forever should never return; if it does, don't sit idle with the watchdog off
    raise RuntimeError("Event loop stopped")
