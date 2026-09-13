"""The Windows helper's microphone recorder must survive a dead stream (sleep, lock, device reset)."""

from __future__ import annotations

import sys
import time
import types
import unittest


class FakeStream:
    """Stands in for sounddevice.InputStream; delivers audio only while `alive`."""

    instances: list["FakeStream"] = []

    def __init__(self, *, samplerate, channels, dtype, device, callback, blocksize, extra_settings=None):
        self.device = device
        self.extra_settings = extra_settings
        self.callback = callback
        self.blocksize = blocksize
        self.started = False
        self.closed = False
        FakeStream.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def close(self) -> None:
        self.closed = True

    def deliver(self, blocks: int = 1) -> None:
        # int16 little-endian 0x0100 = 256 counts, a clearly audible sample.
        for _ in range(blocks):
            self.callback(bytes([0, 1]) * self.blocksize, self.blocksize, None, None)


class RecorderStreamRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeStream.instances = []
        fake_sd = types.ModuleType("sounddevice")
        fake_sd.InputStream = FakeStream
        fake_sd._terminate = lambda: None
        fake_sd._initialize = lambda: None
        self._real_sd = sys.modules.get("sounddevice")
        sys.modules["sounddevice"] = fake_sd
        from doc_reader.windows_helper import Recorder

        self.Recorder = Recorder

    def tearDown(self) -> None:
        if self._real_sd is not None:
            sys.modules["sounddevice"] = self._real_sd
        else:
            sys.modules.pop("sounddevice", None)

    def test_armed_stream_is_reused_while_it_delivers_audio(self) -> None:
        recorder = self.Recorder()
        self.assertTrue(recorder.arm(None))
        FakeStream.instances[0].deliver()
        self.assertTrue(recorder.arm(None))
        self.assertEqual(len(FakeStream.instances), 1)
        self.assertFalse(recorder.is_stale())

    def test_quiet_stream_is_reopened_on_the_next_arm(self) -> None:
        recorder = self.Recorder()
        recorder.STALE_SECONDS = 0.05
        recorder.arm(None)
        time.sleep(0.08)
        self.assertTrue(recorder.is_stale())
        self.assertTrue(recorder.arm(None))
        self.assertEqual(len(FakeStream.instances), 2)
        self.assertTrue(FakeStream.instances[0].closed)
        self.assertEqual(recorder.reopen_count, 1)

    def test_start_reopens_a_quiet_stream_before_capturing(self) -> None:
        recorder = self.Recorder()
        recorder.STALE_SECONDS = 0.05
        recorder.arm(None)
        time.sleep(0.08)
        recorder.start(None)
        self.assertEqual(len(FakeStream.instances), 2)
        FakeStream.instances[1].deliver(blocks=10)
        audio, _elapsed = recorder.stop()
        self.assertGreater(len(audio), 44)
        self.assertGreater(recorder.captured_seconds, 0.2)

    def test_empty_capture_marks_the_stream_dead(self) -> None:
        recorder = self.Recorder()
        recorder.arm(None)
        recorder.start(None)
        recorder.started_at -= 1.0  # pretend the key was held for a second
        audio, elapsed = recorder.stop()
        self.assertEqual(len(audio), 44)
        self.assertGreaterEqual(elapsed, 1.0)
        self.assertEqual(recorder.captured_seconds, 0.0)
        self.assertTrue(recorder.is_stale())
        self.assertIn("no audio", recorder.last_error)
        recorder.arm(None)
        self.assertEqual(len(FakeStream.instances), 2)

    def test_reopen_always_replaces_the_stream(self) -> None:
        recorder = self.Recorder()
        recorder.arm(None)
        FakeStream.instances[0].deliver()
        self.assertTrue(recorder.reopen(None))
        self.assertEqual(len(FakeStream.instances), 2)
        self.assertTrue(FakeStream.instances[0].closed)


class PasteKeyWaitTests(unittest.TestCase):
    """A missed key-up (lock screen, sleep, elevated window) must not stall every paste."""

    def setUp(self) -> None:
        from doc_reader import windows_helper as helper

        self.helper = helper
        helper._forget_pressed_keys()

    def tearDown(self) -> None:
        self.helper._forget_pressed_keys()

    def _key(self, name: str):
        from pynput.keyboard import Key

        return getattr(Key, name)

    def test_no_keys_down_returns_immediately(self) -> None:
        waited = self.helper._wait_for_keys_released()
        self.assertLess(waited, 0.05)

    def test_phantom_modifier_from_long_ago_is_ignored(self) -> None:
        with self.helper._pressed_lock:
            self.helper._pressed_keys[self._key("cmd")] = time.monotonic() - 60
        waited = self.helper._wait_for_keys_released()
        self.assertLess(waited, 0.05)
        self.assertEqual(self.helper._held_modifiers(), [])

    def test_non_modifier_keys_never_block(self) -> None:
        with self.helper._pressed_lock:
            self.helper._pressed_keys[self._key("f8")] = time.monotonic()
            self.helper._pressed_keys[self._key("space")] = time.monotonic()
        waited = self.helper._wait_for_keys_released()
        self.assertLess(waited, 0.05)

    def test_recent_modifier_waits_only_up_to_the_cap(self) -> None:
        with self.helper._pressed_lock:
            self.helper._pressed_keys[self._key("ctrl_r")] = time.monotonic()
        waited = self.helper._wait_for_keys_released(max_seconds=0.2)
        self.assertGreaterEqual(waited, 0.2)
        self.assertLess(waited, 0.45)


class SilentEndpointTests(RecorderStreamRecoveryTests):
    """Frames that arrive as exact digital silence mean the wrong endpoint is open."""

    def test_exact_silence_for_a_real_hold_marks_the_stream_for_reopen(self) -> None:
        recorder = self.Recorder()
        recorder.arm(None)
        recorder.start(None)
        stream = FakeStream.instances[0]
        for _ in range(40):  # 1.3 s of zero frames
            stream.callback(b"\x00\x00" * stream.blocksize, stream.blocksize, None, None)
        recorder.started_at -= 1.3
        audio, elapsed = recorder.stop()
        self.assertGreater(len(audio), 44)
        self.assertLess(recorder.captured_peak, recorder.SILENCE_PEAK)
        self.assertIn("only silence", recorder.last_error)
        self.assertTrue(recorder.is_stale())
        recorder.arm(None)
        self.assertEqual(recorder.reopen_count, 1)

    def test_signal_frames_count_as_a_live_microphone(self) -> None:
        recorder = self.Recorder()
        recorder.arm(None)
        stream = FakeStream.instances[0]
        stream.callback(b"\x00\x00" * stream.blocksize, stream.blocksize, None, None)
        time.sleep(0.05)
        self.assertGreater(recorder.silent_for(), 0.04)
        stream.deliver()  # non-zero samples
        self.assertLess(recorder.silent_for(), 0.02)

    def test_reopen_prefers_the_windows_default_via_wasapi(self) -> None:
        import sounddevice as sd

        sd.query_hostapis = lambda: [{"name": "MME", "default_input_device": 1}, {"name": "Windows WASAPI", "default_input_device": 23}]
        sd.query_devices = lambda index=None: {"name": f"Device {index}"}
        sd.default = types.SimpleNamespace(device=[1, 4])
        sd.WasapiSettings = lambda auto_convert=False: {"auto_convert": auto_convert}
        recorder = self.Recorder()
        self.assertTrue(recorder.arm(None))
        self.assertEqual(recorder.device_index_used, 23)
        self.assertEqual(recorder.device_name, "Device 23")


if __name__ == "__main__":
    unittest.main()
