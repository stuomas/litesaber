from adafruit_debouncer import Button # type: ignore
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
from pwmio import PWMOut
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
        print("Going to sleep!")
        self._persist_current_state()

        self.blade.set_off()

        self.switch.turn_off_led()
        self.switch.release_pins()

        keepalive_interval = 90
        time_alarm = alarm.time.TimeAlarm(monotonic_time=time.monotonic() + keepalive_interval)
        pin_alarm = alarm.pin.PinAlarm(pin=board.EXTERNAL_BUTTON, value=False, edge=True, pull=True)

        alarm.exit_and_deep_sleep_until_alarms(pin_alarm, time_alarm)

    def swing(self):
        try:
            self.sounds.device.stop()
            self.sounds.play_sound_by_index(random.randint(11, 18))
        except Exception as e:
            print(f"Error playing swing sound: {e}")

    def hit(self):
        try:
            self.sounds.device.stop()
            self.sounds.play_sound_by_index(random.randint(3, 10))
        except Exception as e:
            print(f"Error playing hit sound: {e}")

    def change_color(self):
        self.sounds.play_sound_by_index(13)
        self.blade.set_next_color(self.is_on())
        if self.is_on():
            self.switch.update_led_color(self.blade.chosen_color_index)

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
                f.write(str(self.blade.chosen_color_index))
        except OSError as e:
            print(f"Cannot write to filesystem: {type(e).__name__}, {str(e)}")


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
        try:
            x, _, z = self.device.acceleration
            return x * x + z * z
        except Exception as e:
            print(f"Error reading accelerometer: {e}")
        return 0
    
    def get_xy_accel(self):
        try:
            x, y, _ = self.device.acceleration
            return x * x + y * y
        except Exception as e:
            print(f"Error reading accelerometer: {e}")
            return 0
    
    def get_zy_accel(self):
        try:
            _, y, z = self.device.acceleration
            return z * z + y * y
        except Exception as e:
            print(f"Error reading accelerometer: {e}")
            return 0


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
        """Show the blade color on the switch LED. Index -1 = rainbow."""
        if index == -1:
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



class Blade:
    def __init__(self):
        self._color_index = 0
        self.neopixel_amount = 72
        self.brightness = 0.95
        self.off = fancy.CRGB(0.0, 0.0, 0.0)
        self.palette = [
            fancy.CRGB(255, 0, 0),    # red
            fancy.CRGB(255, 100, 0),  # yellow
            fancy.CRGB(0, 255, 0),    # green
            fancy.CRGB(0, 200, 200),  # cyan
            fancy.CRGB(0, 0, 255),    # blue
            fancy.CRGB(128, 0, 255),  # violet
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

    def get_palette(self):
        return self.palette

    def set_next_color(self, change_immediately=False):
        if self.chosen_color_index == -1:
            previous_color = fancy.CRGB(255, 0, 0)
            self.chosen_color_index = 0
        else:
            previous_color = self.palette[self.chosen_color_index]
            self.chosen_color_index += 1
            if self.chosen_color_index >= len(self.palette):
                self.chosen_color_index = -1
                if change_immediately:
                    self._set_to_rainbow()
                    return

        if change_immediately:
            self._fade_to_color(previous_color, self.palette[self.chosen_color_index])

    def set_on(self):
        if self.chosen_color_index == -1:
            for i in range(self.neopixel_amount):
                color = fancy.palette_lookup(self.palette, i / self.neopixel_amount)
                color = fancy.gamma_adjust(color)
                self.device[i] = color.pack()
                self.device.show()
                time.sleep(0.002)
        else:
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
            time.sleep(0.004)

    def flash(self):
        self.device[0] = [128, 128, 128]
        self.device.show()
        time.sleep(0.05)
        self.device[0] = [0, 0, 0]
        self.device.show()

    def get_current_color(self):
        if self.chosen_color_index == -1:
            return fancy.CRGB(255, 255, 255)
        return self.palette[self.chosen_color_index]

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
    long_press_handled = False
    while True:
        lightsaber.switch.device.update()
        await asyncio.sleep(0.01)
        
        current_button_state = not lightsaber.switch.new_value()

        if current_button_state and not lightsaber.switch.last_button_state:
            # Button just pressed
            lightsaber.switch.press_start_time = time.monotonic()
            long_press_handled = False

        elif current_button_state:
            # Button held
            press_duration = time.monotonic() - lightsaber.switch.press_start_time
            if press_duration >= lightsaber.switch.long_press_threshold and not long_press_handled:
                if lightsaber.is_on():
                    lightsaber.change_color()
                else:
                    lightsaber.turn_on()  # Turn on if currently off
                long_press_handled = True

        elif not current_button_state and lightsaber.switch.last_button_state:
            # Button released
            press_duration = lightsaber.switch.get_press_duration()
            if press_duration < lightsaber.switch.long_press_threshold:
                if lightsaber.is_on():
                    lightsaber.turn_off()
                else:
                    lightsaber.turn_on()

        lightsaber.switch.last_button_state = current_button_state


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
    elif isinstance(alarm.wake_alarm, alarm.pin.PinAlarm):
        lightsaber.turn_on()
    
    switch_task = loop.create_task(poll_switch(lightsaber))
    accel_task = loop.create_task(poll_accelerometer(lightsaber))
    watchdog_task = loop.create_task(feed_watchdog())

    try:
        loop.run_forever()
    finally:
        loop.close()


if __name__ == "__main__":
    main()