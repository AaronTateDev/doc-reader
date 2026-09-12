from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from doc_reader.platform_tools import LOCAL_STT_LABEL, dictation_hotkey_label
from doc_reader.webapp import INDEX_HTML, SPEECH_BACKENDS, ReaderService, _service_health_cache_clear


class FakeSpeechHandler(BaseHTTPRequestHandler):
    calls: list[dict[str, object]] = []
    health_payload: dict[str, object] | None = None

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._json(
                self.health_payload
                or {
                    "ok": True,
                    "engines": {
                        "kokoro": {
                            "enabled": True,
                            "loaded": True,
                        },
                        "whisper": {
                            "enabled": True,
                            "loaded": True,
                        },
                    },
                }
            )
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length)
        if self.path == "/v1/audio/transcriptions":
            word_timestamps = self.headers.get("X-Doc-Reader-Word-Timestamps")
            self.calls.append({
                "path": self.path,
                "body": body,
                "content_type": self.headers.get("Content-Type"),
                "word_timestamps": word_timestamps,
            })
            if word_timestamps == "1":
                self._json({
                    "ok": True,
                    "text": "hello world. next phrase.",
                    "language": "en",
                    "duration": 2.5,
                    "segments": [{
                        "start": 0,
                        "end": 2.5,
                        "text": "hello world. next phrase.",
                        "words": [
                            {"word": "hello", "start": 0.0, "end": 0.4},
                            {"word": "world.", "start": 0.4, "end": 1.1},
                            {"word": "next", "start": 1.2, "end": 1.6},
                            {"word": "phrase.", "start": 1.6, "end": 2.5},
                        ],
                    }],
                })
            else:
                self._json({
                    "ok": True,
                    "text": "uploaded audio transcript",
                    "language": "en",
                    "duration": 1.25,
                    "segments": [{"start": 0, "end": 1.25, "text": "uploaded audio transcript"}],
                })
            return

        payload = json.loads(body.decode("utf-8"))
        self.calls.append({"path": self.path, "payload": payload})
        if self.path != "/v1/audio/speech":
            self.send_error(404)
            return
        audio = f"fake-wav:{payload.get('text')}".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(audio)))
        self.end_headers()
        self.wfile.write(audio)

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _json(self, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class WebappLibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeSpeechHandler.calls = []
        FakeSpeechHandler.health_payload = None
        _service_health_cache_clear()
        os.environ["DOC_READER_SERVICE_HEALTH_CACHE_SECONDS"] = "0"
        os.environ["DOC_READER_SERVICE_HEALTH_FAIL_CACHE_SECONDS"] = "0"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeSpeechHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.speech_url = f"http://127.0.0.1:{self.server.server_port}"
        self.old_env = {
            "DOC_READER_WEB_SPEECH_BACKEND": os.environ.get("DOC_READER_WEB_SPEECH_BACKEND"),
            "DOC_READER_TTS_UMBRA_URL": os.environ.get("DOC_READER_TTS_UMBRA_URL"),
            "DOC_READER_TTS_MAC_URL": os.environ.get("DOC_READER_TTS_MAC_URL"),
            "DOC_READER_STT_URL": os.environ.get("DOC_READER_STT_URL"),
            "DOC_READER_ANALYSIS_ENABLED": os.environ.get("DOC_READER_ANALYSIS_ENABLED"),
            "DOC_READER_ANALYSIS_BACKEND": os.environ.get("DOC_READER_ANALYSIS_BACKEND"),
        }
        os.environ["DOC_READER_WEB_SPEECH_BACKEND"] = "tailscale-4090"
        os.environ["DOC_READER_TTS_UMBRA_URL"] = self.speech_url
        os.environ["DOC_READER_TTS_MAC_URL"] = self.speech_url
        os.environ.pop("DOC_READER_STT_URL", None)
        os.environ["DOC_READER_ANALYSIS_ENABLED"] = "0"
        os.environ["DOC_READER_ANALYSIS_BACKEND"] = "heuristic"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_library_upsert_prepares_audio_and_keeps_clawdad_filter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reader = ReaderService(Path(directory))
            item, queued = reader.upsert_library_item({
                "source": "clawdad",
                "source_item_id": "clawdad:test-message",
                "kind": "clawdad-message",
                "text": "Read this Clawdad message.",
                "prepare_audio": True,
                "source_meta": {
                    "projectPath": "/tmp/project",
                    "sessionId": "session-1",
                    "requestId": "request-1",
                },
            })
            self.assertTrue(queued)

            ready = self._wait_for_ready(reader, item.id)
            self.assertEqual(ready["audio"]["state"], "ready")
            data, content_type = reader.library_audio(item.id)
            self.assertEqual(content_type, "audio/wav")
            self.assertIn(b"Read this Clawdad message", data)

            state = reader.state()
            self.assertEqual(len(state["library"]), 1)
            self.assertEqual(len(state["clawdad"]), 1)
            self.assertEqual(len(state["readings"]), 0)
            self.assertEqual(FakeSpeechHandler.calls[0]["path"], "/v1/audio/speech")

            same_item, _queued_again = reader.upsert_library_item({
                "source": "clawdad",
                "source_item_id": "clawdad:test-message",
                "kind": "clawdad-message",
                "text": "Read this Clawdad message.",
                "prepare_audio": True,
            })
            self.assertEqual(same_item.id, item.id)

    def test_web_history_keeps_more_than_one_hundred_cards(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reader = ReaderService(Path(directory))
            for index in range(125):
                reader.add_text(f"Reading item {index} has several countable words.")

            state = reader.state()
            self.assertEqual(len(state["library"]), 125)
            self.assertEqual(state["metrics"]["tts_items"], 125)
            self.assertGreater(state["metrics"]["tts_words"], 125)

    def test_native_helper_reset_control_is_exposed(self) -> None:
        self.assertIn('id="nativeHelperReset"', INDEX_HTML)
        self.assertIn("/api/native/reset", INDEX_HTML)

    def test_speech_status_copy_uses_neutral_service_labels(self) -> None:
        self.assertIn("local speech online", INDEX_HTML)
        self.assertIn("remote speech online", INDEX_HTML)
        self.assertIn("speech ready", INDEX_HTML)
        self.assertIn("Speech-to-text", INDEX_HTML)
        self.assertNotIn("4090 online", INDEX_HTML)
        self.assertNotIn("4090 Whisper", INDEX_HTML)
        self.assertNotIn("Transcribing on 4090", INDEX_HTML)
        self.assertNotIn("model ready", INDEX_HTML)

    def test_native_status_is_lightweight_and_reflects_stt_toggle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reader = ReaderService(Path(directory))

            enabled_status = reader.native_status()
            self.assertTrue(enabled_status["stt"]["enabled"])
            self.assertNotIn("library", enabled_status)
            self.assertNotIn("tts", enabled_status)

            reader.update_settings({"stt_enabled": False})
            disabled_status = reader.native_status()

            self.assertFalse(disabled_status["stt"]["enabled"])
            self.assertEqual(disabled_status["stt"]["hotkey"], dictation_hotkey_label())

    def test_default_tts_backend_is_mac_local(self) -> None:
        old_backend = os.environ.pop("DOC_READER_WEB_SPEECH_BACKEND", None)
        try:
            with tempfile.TemporaryDirectory() as directory:
                reader = ReaderService(Path(directory))

                status = reader.tts_status()

                self.assertEqual(status["backend"], "local-kokoro")
                self.assertEqual(status["label"], SPEECH_BACKENDS["local-kokoro"])
        finally:
            if old_backend is not None:
                os.environ["DOC_READER_WEB_SPEECH_BACKEND"] = old_backend

    def test_stt_prefers_mac_whisper_when_both_services_are_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reader = ReaderService(Path(directory))

            status = reader.stt_status()

            self.assertEqual(status["backend"], "mac-whisper")
            self.assertEqual(status["label"], LOCAL_STT_LABEL)
            self.assertTrue(status["ready"])

    def test_metrics_split_stt_and_tts_words(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reader = ReaderService(Path(directory))
            reader.add_text("read aloud words only", label="Text", kind="text")
            reader.add_text("dictated words arrive here", label="Dictation", kind="dictation")

            metrics = reader.metrics_snapshot()
            self.assertEqual(metrics["tts_words"], 4)
            self.assertEqual(metrics["stt_words"], 4)
            self.assertEqual(metrics["tts_items"], 1)
            self.assertEqual(metrics["stt_items"], 1)

    def test_audio_file_transcription_creates_dictation_card(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reader = ReaderService(Path(directory))

            result = reader.transcribe_audio_file(
                "meeting.m4a",
                b"fake audio bytes",
                content_type="audio/mp4",
            )

            self.assertEqual(result["text"], "uploaded audio transcript")
            item = result["item"]
            self.assertEqual(item["kind"], "dictation")
            self.assertEqual(item["source"], "audio-upload")
            self.assertEqual(item["title"], "Audio: meeting.m4a")
            self.assertEqual(item["sourceMeta"]["filename"], "meeting.m4a")
            self.assertEqual(item["sourceMeta"]["contentType"], "audio/mp4")
            self.assertEqual(item["word_count"], 3)
            self.assertEqual(reader.metrics_snapshot()["stt_items"], 1)
            self.assertEqual(FakeSpeechHandler.calls[-1]["path"], "/v1/audio/transcriptions")
            self.assertIsNone(FakeSpeechHandler.calls[-1]["word_timestamps"])

    def test_audio_file_transcription_can_save_phrase_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reader = ReaderService(Path(directory))

            result = reader.transcribe_audio_file(
                "meeting.m4a",
                b"fake audio bytes",
                content_type="audio/mp4",
                timestamped=True,
            )

            expected_text = (
                "[00:00.0 - 00:01.1] hello world.\n"
                "[00:01.2 - 00:02.5] next phrase."
            )
            self.assertEqual(result["text"], expected_text)
            self.assertEqual(result["plain_text"], "hello world. next phrase.")
            item = result["item"]
            self.assertEqual(item["text"], expected_text)
            self.assertEqual(item["source"], "audio-upload")
            self.assertTrue(item["sourceItemId"].endswith(":phrase"))
            self.assertEqual(item["sourceMeta"]["timestamped"], True)
            self.assertEqual(item["sourceMeta"]["timestampMode"], "phrase")
            self.assertEqual(item["word_count"], 4)
            self.assertEqual(reader.metrics_snapshot()["stt_words"], 4)
            self.assertEqual(FakeSpeechHandler.calls[-1]["word_timestamps"], "1")

    def test_stt_falls_back_to_mac_whisper_when_umbra_is_unavailable(self) -> None:
        old_umbra = os.environ.get("DOC_READER_TTS_UMBRA_URL")
        old_mac = os.environ.get("DOC_READER_TTS_MAC_URL")
        try:
            os.environ["DOC_READER_TTS_UMBRA_URL"] = "http://127.0.0.1:9"
            os.environ["DOC_READER_TTS_MAC_URL"] = self.speech_url
            with tempfile.TemporaryDirectory() as directory:
                reader = ReaderService(Path(directory))

                status = reader.stt_status()
                self.assertEqual(status["backend"], "mac-whisper")
                self.assertEqual(status["label"], LOCAL_STT_LABEL)
                self.assertTrue(status["ready"])

                result = reader.transcribe_audio_file(
                    "meeting.m4a",
                    b"fake audio bytes",
                    content_type="audio/mp4",
                )

                self.assertEqual(result["transcription"]["service_label"], LOCAL_STT_LABEL)
                self.assertEqual(FakeSpeechHandler.calls[-1]["path"], "/v1/audio/transcriptions")
        finally:
            if old_umbra is None:
                os.environ.pop("DOC_READER_TTS_UMBRA_URL", None)
            else:
                os.environ["DOC_READER_TTS_UMBRA_URL"] = old_umbra
            if old_mac is None:
                os.environ.pop("DOC_READER_TTS_MAC_URL", None)
            else:
                os.environ["DOC_READER_TTS_MAC_URL"] = old_mac

    def test_stt_refuses_service_without_whisper(self) -> None:
        FakeSpeechHandler.health_payload = {
            "ok": True,
            "engines": {
                "kokoro": {
                    "enabled": True,
                    "loaded": True,
                },
                "whisper": {
                    "enabled": False,
                    "loaded": False,
                },
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            reader = ReaderService(Path(directory))

            with self.assertRaisesRegex(RuntimeError, "No Whisper speech-to-text service is ready"):
                reader.transcribe_audio_file(
                    "meeting.m4a",
                    b"fake audio bytes",
                    content_type="audio/mp4",
                )

        self.assertFalse(
            any(call.get("path") == "/v1/audio/transcriptions" for call in FakeSpeechHandler.calls)
        )

    def test_dictation_card_text_can_be_edited(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reader = ReaderService(Path(directory))
            item = reader.add_text("old draft words", label="Dictation", kind="dictation")

            result = reader.update_item_text(item.id, "edited draft words now")

            updated = result["item"]
            self.assertEqual(updated["id"], item.id)
            self.assertEqual(updated["kind"], "dictation")
            self.assertEqual(updated["text"], "edited draft words now")
            self.assertEqual(updated["snippet"], "edited draft words now")
            self.assertEqual(updated["word_count"], 4)
            self.assertEqual(reader.item_text(item.id)["text"], "edited draft words now")
            self.assertEqual(reader.metrics_snapshot()["stt_words"], 4)
            self.assertEqual(result["state"]["status"], "Library card saved.")

    def test_timestamped_audio_card_edit_keeps_clean_word_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reader = ReaderService(Path(directory))
            result = reader.transcribe_audio_file(
                "meeting.m4a",
                b"fake audio bytes",
                content_type="audio/mp4",
                timestamped=True,
            )
            item = result["item"]

            updated = reader.update_item_text(
                str(item["id"]),
                "[00:00.0 - 00:01.0] edited words here",
            )["item"]

            self.assertEqual(updated["title"], "Audio: meeting.m4a")
            self.assertEqual(updated["text"], "[00:00.0 - 00:01.0] edited words here")
            self.assertEqual(updated["word_count"], 3)
            self.assertEqual(reader.metrics_snapshot()["stt_words"], 3)

    def test_heuristic_analysis_writes_style_map(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reader = ReaderService(Path(directory))
            reader.add_text(
                "We should add structure to the recordings. What did we discuss?",
                label="Dictation",
                kind="dictation",
            )

            analysis = reader.run_library_analysis_once(reason="test")
            self.assertEqual(analysis["pending_items"], 0)
            self.assertEqual(analysis["items_analyzed"], 1)
            self.assertEqual(analysis["style_map"]["items_analyzed"], 1)
            self.assertTrue((Path(directory) / "library-analysis.json").is_file())

    def _wait_for_ready(self, reader: ReaderService, item_id: str) -> dict[str, object]:
        deadline = time.time() + 5
        last = {}
        while time.time() < deadline:
            last = reader.library_item_payload(item_id=item_id)
            if last.get("audio", {}).get("state") == "ready":
                return last
            time.sleep(0.05)
        self.fail(f"library audio did not become ready: {last}")


if __name__ == "__main__":
    unittest.main()
