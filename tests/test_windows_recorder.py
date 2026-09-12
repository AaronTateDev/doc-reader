"""The Windows helper's microphone recorder must survive a dead stream (sleep, lock, device reset)."""

from __future__ import annotations

import sys
import time
import types
import unittest


class FakeStream:
    """Stands in for sounddevice.InputStream; delivers audio only while `alive`."""

    instances: list["FakeStream"] = []

    def __init__(self, *, samplerate, channels, dtype, device, callback, blocksize):
        self.device = device
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
        for _ in range(blocks):
            self.callback(b"\x01\x00" * self.blocksize, self.blocksize, None, None)


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


if __name__ == "__main__":
    unittest.main()
