"""Lightsaber logic. code.py imports this on a button press or power-up; keepalive
wakeups are handled in code.py alone so this file doesn't need to be compiled for them."""
from adafruit_debouncer import Button # type: ignore
import adafruit_fancyled.adafruit_fancyled as fancy
import adafruit_lis3dh
import alarm
import audiobusio
import audiocore
import asyncio
import board
from digitalio import DigitalInOut, Direction, Pull
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
# Duration of a color fade to or from rainbow
PER_PIXEL_FADE_SEC = 0.4
# After a swing or hit sound, wait this long before another swing sound can start
MOTION_SOUND_COOLDOWN_SEC = 0.3
# Without accelerometer, movement can't be detected. Sleep this long after the
# last button press if the blade is on (IDLE_SLEEP_SEC is used when it's off).
NO_ACCEL_ON_SLEEP_SEC = 600.0
WATCHDOG_TIMEOUT = 4.0
WATCHDOG_FEED_INTERVAL = 0.5
# A task that hasn't run for this long is considered hung and the board is reset.
TASK_STALL_LIMIT = 2.5
# microcontroller.nvm layout: the drive is read-only for CircuitPython (PC can write
# code to it), so everything the saber must remember lives in nvm instead.
NVM_COLOR = 0  # 1 byte: chosen color + 1
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
        now = time.monotonic()
        # Updated by each polling task; feed_watchdog resets the board if one goes stale
        self.heartbeats = {"switch": now, "accel": now}
        self.last_activity = now

    def turn_on(self):
        self.power.ensure_on()
        self.sounds.play_sound_by_index(0)
        self.switch.update_led_color(self.blade.chosen_color_index)
        self.switch.set_state(True)
        self.blade.set_on()
        print("Lightsaber ON")

    def turn_off(self):
        self.sounds.play_sound_by_index(2)
        self.blade.set_off()
        self.switch.turn_off_led()
        self.switch.set_state(False)
        print("Lightsaber OFF")

    def deep_sleep(self):
        print("Going to sleep")
        self.blade.save_color()
        if self.is_on():
            # Retract like a normal turn off, and let the sound finish before sleep cuts it
            self.sounds.play_sound_by_index(2)
            self.blade.set_off()
            self.sounds.wait_until_done(timeout=2.0)
        self.switch.turn_off_led()
        self.switch.release_pins()
        self._sleep_until_alarms()

    def swing(self):
        print("swung")
        self.sounds.play_sound_by_index(random.randint(11, 18))

    def hit(self):
        print("hit")
        self.sounds.play_sound_by_index(random.randint(3, 10))

    def change_color(self):
        # Changing color is use, even if the saber is held still: restart the idle timer
        self.last_activity = time.monotonic()
        self.power.ensure_on()  # a tap while off still plays the sound
        self.sounds.play_sound_by_index(13)
        self.blade.set_next_color(self.is_on())
        if self.is_on():
            self.switch.update_led_color(self.blade.chosen_color_index)

    def is_on(self):
        return self.switch.pressed()


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
        self.rainbow_enabled = False
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

    def update_led_color(self, index):
        """Show the blade color on the switch LED. Index past the palette = rainbow."""
        if index >= len(self.palette):
            self.rainbow_enabled = True
            if not self.animation_task or self.animation_task.done():
                self.animation_task = asyncio.create_task(self._animate_rainbow())
        else:
            self._stop_rainbow()
            prev_color = self.palette[self.current_index]
            self.current_index = index
            self._fade_colors(prev_color, self.palette[index])

    def turn_off_led(self):
        self._stop_rainbow()
        self._set_led(0.0, 0.0, 0.0)

    async def _animate_rainbow(self):
        step = 0.0
        while self.rainbow_enabled:
            color = fancy.palette_lookup(self.palette, step)
            self._set_led(color.red, color.green, color.blue)
            step = (step + 0.01) % 1.0
            await asyncio.sleep(0.09)

    def _stop_rainbow(self):
        self.rainbow_enabled = False
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
    """Remembers the chosen color across sleeps and power-offs.

    Uses microcontroller.nvm (outside the filesystem, so an interrupted write
    can't corrupt code.py) and writes only when the value has changed.
    """
    LEGACY_FILE = "/state"

    def __init__(self, color_count):
        self.color_count = color_count
        self.nvm = _nvm()
        self.saved = None

    def load(self):
        index = self._read_nvm()
        if index is not None:
            self.saved = index
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

    def save(self, index):
        if index == self.saved:
            return
        try:
            if self.nvm is not None:
                self.nvm[NVM_COLOR] = index + 1  # 0xFF (erased) and 0 mean "nothing saved"
            else:
                with open(self.LEGACY_FILE, "w") as f:
                    f.write(str(index))
            self.saved = index
        except Exception as e:
            print("Cannot save color:", e)

    def _read_nvm(self):
        if self.nvm is None:
            return None
        try:
            value = self.nvm[NVM_COLOR]
        except Exception:
            return None
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
            value = self.color_count - 1
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
        # Color options are the palette colors followed by rainbow
        self.rainbow_index = len(self.palette)
        self.color_count = len(self.palette) + 1
        self._rainbow_frame = None
        self.color_store = ColorStore(self.color_count)
        self.chosen_color_index = self.color_store.load()
        self.device = self._init_neopixels()

    def save_color(self):
        self.color_store.save(self.chosen_color_index)

    def set_next_color(self, change_immediately=False):
        previous = self._frame(self.chosen_color_index)
        self.chosen_color_index = (self.chosen_color_index + 1) % self.color_count
        if change_immediately:
            self._fade(previous, self._frame(self.chosen_color_index))

    def set_unstable(self):
        pass

    def set_on(self):
        frame = self._frame(self.chosen_color_index)
        for i in range(self.neopixel_amount):
            self._show_step(i, frame[i])

    def set_off(self):
        for i in range(self.neopixel_amount - 1, -1, -1):
            self._show_step(i, self.off)

    def _init_neopixels(self):
        pixels = neopixel.NeoPixel(board.EXTERNAL_NEOPIXELS, self.neopixel_amount, auto_write=False)
        pixels.brightness = self.brightness
        return pixels

    def _frame(self, index):
        """Packed color of every pixel for the given color option."""
        if index == self.rainbow_index:
            if self._rainbow_frame is None:
                self._rainbow_frame = [
                    fancy.gamma_adjust(fancy.palette_lookup(self.palette, i / self.neopixel_amount)).pack()
                    for i in range(self.neopixel_amount)
                ]
            return self._rainbow_frame
        return [self.palette[index].pack()] * self.neopixel_amount

    def _fade(self, old, new):
        uniform = old.count(old[0]) == len(old) and new.count(new[0]) == len(new)
        if not uniform:
            self._fade_per_pixel(old, new)
            return
        steps = 100
        for step in range(1, steps + 1):
            self.device.fill(_mix(old[0], new[0], step / steps))
            self.device.show()

    def _fade_per_pixel(self, old, new):
        """Fade to/from rainbow. Time-based so it lasts PER_PIXEL_FADE_SEC however
        slow each frame is to compute; integer math and a single slice write keep frames cheap."""
        channels = []
        for a, b in zip(old, new):
            r, g, bl = (a >> 16) & 0xFF, (a >> 8) & 0xFF, a & 0xFF
            channels.append((r, g, bl, ((b >> 16) & 0xFF) - r, ((b >> 8) & 0xFF) - g, (b & 0xFF) - bl))
        start = time.monotonic()
        while True:
            k = int((time.monotonic() - start) / PER_PIXEL_FADE_SEC * 256)
            if k >= 256:
                break
            self.device[:] = [
                ((r + (dr * k >> 8)) << 16) | ((g + (dg * k >> 8)) << 8) | (bl + (db * k >> 8))
                for r, g, bl, dr, dg, db in channels
            ]
            self.device.show()
        self.device[:] = new
        self.device.show()

    def _show_step(self, i, color):
        self.device[i] = color
        self.device.show()
        time.sleep(0.004)


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
    def __init__(self):
        self.device = audiobusio.I2SOut(board.I2S_BIT_CLOCK, board.I2S_WORD_SELECT, board.I2S_DATA)
        self.files = self._get_sound_files()
        self._file = None
        self._wave = None

    def play_sound_by_index(self, index, loop=False):
        if not 0 <= index < len(self.files):
            return
        try:
            self.stop()
            self._file = open(self.files[index], "rb")
            self._wave = audiocore.WaveFile(self._file)
            self.device.play(self._wave, loop=loop)
        except Exception as e:
            print("Cannot play sound:", e)

    def wait_until_done(self, timeout):
        end = time.monotonic() + timeout
        while self.device.playing and time.monotonic() < end:
            time.sleep(0.01)

    def play_sound_by_name(self, name, loop=False):
        # Not implemented
        pass

    def stop(self):
        """Stop playback and close the previous sound file."""
        self.device.stop()
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
    """EXTERNAL_POWER feeds the LED strip and the audio amplifier. Even dark, the 72 LEDs
    draw ~40-70 mA, so it is switched off whenever the blade is off and no sound plays."""

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
        self.on = True

    def off_if_idle(self):
        if self.on and not self.sounds.device.playing:
            self.pin.value = False
            self.on = False


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
        except (OSError, RuntimeError) as e:
            accel.consecutive_errors += 1
            if accel.consecutive_errors >= accel.max_consecutive_errors:
                accel.disable(e)
                lightsaber.last_activity = now
            continue

        xz_accel = x * x + z * z
        if tapped:
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
    if turn_on_at_start:
        lightsaber.turn_on()
    switch.device.update()
    if not switch.new_value():
        # Button is still held from the press that woke us up: ignore that press
        switch.last_button_state = True
        switch.press_start_time = 0  # so releasing it doesn't count as a short press
        long_press_handled = True

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

        elif current_button_state:
            # Button held
            press_duration = time.monotonic() - switch.press_start_time
            if press_duration >= switch.long_press_threshold and not long_press_handled:
                if lightsaber.is_on():
                    lightsaber.change_color()
                else:
                    lightsaber.turn_on()
                long_press_handled = True

        elif not current_button_state and switch.last_button_state:
            # Button released
            press_duration = switch.get_press_duration()
            if press_duration < switch.long_press_threshold:
                if lightsaber.is_on():
                    lightsaber.turn_off()
                else:
                    lightsaber.turn_on()

        switch.last_button_state = current_button_state


async def power_down_when_idle(lightsaber):
    while True:
        await asyncio.sleep(0.1)
        if not lightsaber.is_on():
            # Blade off: power down LEDs and amplifier once the last sound has finished
            lightsaber.power.off_if_idle()


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
    wdog.feed()

    loop = asyncio.get_event_loop()
    if hasattr(loop, "set_exception_handler"):
        loop.set_exception_handler(handle_task_exception)
    # Woken up by the button: ignite right away instead of needing a second press
    woken_by_button = isinstance(alarm.wake_alarm, alarm.pin.PinAlarm)
    loop.create_task(poll_switch(lightsaber, turn_on_at_start=woken_by_button))
    loop.create_task(poll_accelerometer(lightsaber))
    loop.create_task(power_down_when_idle(lightsaber))
    loop.create_task(feed_watchdog(lightsaber))
    loop.run_forever()
    # run_forever should never return; if it does, don't sit idle with the watchdog off
    raise RuntimeError("Event loop stopped")
