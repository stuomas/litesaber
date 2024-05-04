from adafruit_debouncer import Button
import adafruit_fancyled.adafruit_fancyled as fancy
import adafruit_lis3dh
import alarm
import audiobusio
import audiocore
import asyncio
import board
from digitalio import DigitalInOut, Direction, Pull
from microcontroller import watchdog as wdog
import neopixel
import os
import random
import time
from watchdog import WatchDogMode


class Lightsaber:
    def __init__(self, accelerometer, blade, sounds, switch):
        self.accelerometer = accelerometer
        self.blade = blade
        self.sounds = sounds
        self.switch = switch

    def turn_on(self):
        self.sounds.play_sound_by_index(0)
        self.blade.set_on()
        self.switch.set_state(True)
        print("Lightsaber ON")

    def turn_off(self):
        self.sounds.play_sound_by_index(2)
        self.blade.set_off()
        self.switch.set_state(False)
        print("Lightsaber OFF")

    def deep_sleep(self):
        print("Going to sleep")
        self._persist_current_state()
        keepalive_interval = 90
        self.switch.release_pin()
        self.switch.turn_off_led()
        time_alarm = alarm.time.TimeAlarm(monotonic_time=time.monotonic() + keepalive_interval)
        pin_alarm = alarm.pin.PinAlarm(pin=board.EXTERNAL_BUTTON, value=False, edge=True, pull=True)
        alarm.exit_and_deep_sleep_until_alarms(pin_alarm, time_alarm)

    def swing(self):
        self.sounds.device.stop()
        self.sounds.play_sound_by_index(random.randint(11, 18))

    def hit(self):
        self.sounds.device.stop()
        self.sounds.play_sound_by_index(random.randint(3, 10))

    def change_color(self):
        self.sounds.play_sound_by_index(13)
        self.blade.set_next_color(self.is_on())

    def is_on(self):
        return self.switch.pressed()
    
    def keepalive(self):
        # Anker PowerCore 5000 turns off after ~2 minutes if power consumption is under
        # some limit. To prevent that, do periodically something consuming if the saber
        # is in deep sleep.
        print("Keepalive")
        self.blade.flash()

    def _persist_current_state(self):
        try:
            with open("/state", "w") as f:
                f.write("{}".format(self.blade.chosen_color_index))
        except OSError:
            print("Cannot write to filesystem, check that it's writable for circuitpython")


class Accelerometer:
    def __init__(self):
        self.tap_threshold = 127
        self.swing_threshold = 180
        self.hit_threshold = 290
        self.shake_threshold = 20
        # 0: disabled, 1: single taps, 2: double taps
        self.tap_detection = 1
        self.device = self._init_lis3dh()

    def _init_lis3dh(self):
        i2c = board.I2C()
        int1 = DigitalInOut(board.ACCELEROMETER_INTERRUPT)
        lis3dh = adafruit_lis3dh.LIS3DH_I2C(i2c, int1=int1)
        lis3dh.range = adafruit_lis3dh.RANGE_4_G
        lis3dh.set_tap(self.tap_detection, self.tap_threshold, click_cfg=0x04)  # only y-direction taps
        return lis3dh
    
    def shaken(self):
        shaken = self.device.shake(shake_threshold=self.shake_threshold)
        if shaken:
            print("shaken")
        return shaken
    
    def tapped(self):
        tapped = self.device.tapped
        if tapped:
            print("tapped")
        return tapped
    
    def swung(self):
        swung = self.get_xz_accel() > self.swing_threshold
        if swung:
            print("swung")
        return swung
    
    def hit(self):
        hit = (self.get_xz_accel() > self.hit_threshold)
        if hit:
            print("hit")
        return hit
    
    def get_xz_accel(self):
        x, _, z = self.device.acceleration
        return x * x + z * z
    
    def get_xy_accel(self):
        x, y, _ = self.device.acceleration
        return x * x + y * y
    
    def get_zy_accel(self):
        _, y, z = self.device.acceleration
        return z * z + y * y


class Switch:
    def __init__(self):
        self.state = asyncio.Event()
        self.btn_pin = DigitalInOut(board.EXTERNAL_BUTTON)
        self.led_pin = DigitalInOut(board.D4)
        self.device = self._init_switch()

    def _init_switch(self):
        self.btn_pin.direction = Direction.INPUT
        self.btn_pin.pull = Pull.UP
        self.led_pin.direction = Direction.OUTPUT
        self.turn_on_led()
        return Button(self.btn_pin, value_when_pressed=False)
    
    def release_pin(self):
        self.btn_pin.deinit()
    
    def new_value(self):
        return self.device.value
    
    def pressed(self):
        return self.state.is_set()
    
    def set_state(self, state):
        if state:
            self.state.set()
        else:
            self.state.clear()

    def turn_on_led(self):
        self.led_pin.value = True

    def turn_off_led(self):
        self.led_pin.value = False


class Blade:
    def __init__(self):
        self._color_index = 0
        self.neopixel_amount = 72
        self.brightness = 0.95
        self.off = fancy.CRGB(0.0, 0.0, 0.0)
        self.palette = [
            fancy.CRGB(255, 0, 0),    # red
            fancy.CRGB(200, 200, 0),  # yellow
            fancy.CRGB(0, 255, 0),    # green
            fancy.CRGB(0, 200, 200),  # cyan
            fancy.CRGB(0, 0, 255),    # blue
            fancy.CRGB(200, 0, 200),  # magenta
        ]
        self.chosen_color_index = self._get_chosen_color()
        self.device = self._init_neopixels()

    def _get_chosen_color(self):
        saved_color = 0
        try:
            with open("/state", "r") as f:
                saved_color = int(f.read())
        except FileNotFoundError:
            print("No saved color, using default")
        except Exception as e:
            print("Other exception while reading file: %s", e)
        return saved_color

    def set_next_color(self, change_immediately=False):
        previous_color = self.palette[self.chosen_color_index]
        self.chosen_color_index += 1
        if self.chosen_color_index >= len(self.palette):
            self.chosen_color_index = -1
            if change_immediately:
                self._set_to_rainbow()
                return
        if change_immediately:
            self._fade_to_color(previous_color, self.palette[self.chosen_color_index])

    def set_unstable(self):
        pass

    def set_on(self):
        color = self.palette[self.chosen_color_index]
        self._set_to_color_in_steps(0, self.neopixel_amount, 1, color.pack())

    def set_off(self):
        self._set_to_color_in_steps(self.neopixel_amount - 1, -1, -1, self.off.pack())

    def _init_neopixels(self):
        pixels = neopixel.NeoPixel(board.EXTERNAL_NEOPIXELS, self.neopixel_amount, auto_write=False)
        pixels.brightness = self.brightness
        return pixels
    
    def _fade_to_color(self, old, new):
        fade_gradient = (
            (0.0, old),
            (1.0, new)
        )
        palette = fancy.expand_gradient(fade_gradient, 100)
        for i in range(100):
            self.device.fill(fancy.palette_lookup(palette, i / 100).pack())
            self.device.show()

    def _set_to_rainbow(self):
        for i in range(self.neopixel_amount):
            color = fancy.palette_lookup(self.palette, i / self.neopixel_amount)
            color = fancy.gamma_adjust(color)
            self.device[i] = color.pack()
        self.device.show()

    def _set_to_color_in_steps(self, start, stop, step, color):
        for i in range(start, stop, step):
            self.device[i] = color
            self.device.show()
            time.sleep(0.006)

    def flash(self):
        self.device[0] = [128, 128, 128]
        self.device.show()
        time.sleep(0.05)
        self.device[0] = [0, 0, 0]
        self.device.show()


class Sounds:
    def __init__(self):
        self.device = audiobusio.I2SOut(board.I2S_BIT_CLOCK, board.I2S_WORD_SELECT, board.I2S_DATA)
        self.files = self._get_sound_files()

    def play_sound_by_index(self, index, loop=False):
        try:
            n = self.files[index]
            wave_file = open(n, "rb")
            wave = audiocore.WaveFile(wave_file)
            self.device.play(wave, loop=loop)
        except:
            return

    def play_sound_by_name(self, name, loop=False):
        # Not implemented
        pass

    def _get_sound_files(self):
        wavs = []
        for filename in os.listdir('/sounds'):
            if filename.lower().endswith('.wav') and not filename.startswith('.'):
                wavs.append("/sounds/"+filename)
            wavs.sort()
        return wavs


def enable_ext_power():
    external_power = DigitalInOut(board.EXTERNAL_POWER)
    external_power.direction = Direction.OUTPUT
    external_power.value = True


def enable_watchdog():
    wdog.timeout = 2.0
    wdog.mode = WatchDogMode.RESET


async def feed_watchdog():
    while True:
        await asyncio.sleep(1)
        wdog.feed()


async def poll_accelerometer(lightsaber):
    idle_threshold_sec = 45.0
    prev_xy_accel = 0.0
    idle_time = 0.0
    while True:
        await asyncio.sleep(0.01)
        if lightsaber.accelerometer.tapped():
            lightsaber.change_color()
        elif lightsaber.accelerometer.swung() and lightsaber.switch.pressed():
            lightsaber.swing()
        elif lightsaber.accelerometer.hit() and lightsaber.switch.pressed():
            lightsaber.hit()
        if abs(lightsaber.accelerometer.get_xy_accel() - prev_xy_accel) < 11.0:
            idle_time += 0.01
        else:
            idle_time = 0.0
            prev_xy_accel = lightsaber.accelerometer.get_xy_accel()
        if idle_time > idle_threshold_sec:
            print("Idle, going to sleep")
            lightsaber.deep_sleep()


async def poll_switch(lightsaber):
    while True:
        lightsaber.switch.device.update()
        await asyncio.sleep(0.01)
        if not lightsaber.switch.new_value() and not lightsaber.switch.pressed():
            lightsaber.turn_on()
        elif lightsaber.switch.new_value() and lightsaber.switch.pressed():
            lightsaber.turn_off()


def main():
    enable_ext_power()
    enable_watchdog()
    loop = asyncio.get_event_loop()

    lightsaber = Lightsaber(
        Accelerometer(),
        Blade(),
        Sounds(),
        Switch()
    )

    if isinstance(alarm.wake_alarm, alarm.time.TimeAlarm):
        lightsaber.keepalive()
        lightsaber.deep_sleep()

    switch_task = loop.create_task(poll_switch(lightsaber))
    accel_task = loop.create_task(poll_accelerometer(lightsaber))
    watchdog_task = loop.create_task(feed_watchdog())

    try:
        loop.run_forever()
    finally:
        loop.close()


if __name__ == "__main__":
    main()