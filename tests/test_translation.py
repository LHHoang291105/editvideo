"""Network-free contract tests; Gemini is mocked only at the API boundary."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from common import Subtitle, read_srt, write_srt
from config import Settings
from translate_subtitles import (
    ResponseValidationError,
    TranslationError,
    _generate,
    _generate_with_retry,
    load_glossary,
    translate_subtitles,
    validate_translation,
)


class FakeApiError(Exception):
    def __init__(self, code: int) -> None:
        super().__init__("SDK detail with secret-api-key MUST NOT BE LOGGED")
        self.code = code


class TranslationTests(unittest.TestCase):
    def test_404_lists_models_without_retry_or_changing_source(self) -> None:
        client = MagicMock()
        client.models.list.return_value = [
            SimpleNamespace(name="models/gemini-3.6-flash", supported_actions=["generateContent"]),
            SimpleNamespace(name="models/embedding-model", supported_actions=["embedContent"]),
        ]
        source_before = self.source.read_bytes()
        with patch("translate_subtitles._create_client", return_value=client), patch(
            "translate_subtitles._generate", side_effect=FakeApiError(404)
        ) as generate:
            with self.assertRaises(TranslationError) as caught:
                translate_subtitles(self.source, self.destination, self.settings)
        message = str(caught.exception)
        self.assertIn("gemini-3.6-flash", message)
        self.assertIn("--gemini-model", message)
        self.assertNotIn("embedding-model", message)
        self.assertNotIn("secret-api-key", message)
        self.assertEqual(self.source.read_bytes(), source_before)
        self.assertFalse(self.destination.exists())
        generate.assert_called_once()
        client.close.assert_called_once()

    def test_404_metadata_failure_preserves_original_error_and_hides_secret(self) -> None:
        client = MagicMock()
        client.models.list.side_effect = FakeApiError(403)
        with patch("translate_subtitles._generate", side_effect=FakeApiError(404)):
            with self.assertRaises(TranslationError) as caught:
                _generate_with_retry(client, "prompt", [7], self.settings)
        message = str(caught.exception)
        self.assertIn("404", message)
        self.assertIn("403", message)
        self.assertNotIn("secret-api-key", message)

    def setUp(self) -> None:
        self.work = tempfile.TemporaryDirectory(prefix="dịch 中文 ")
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.source = self.root / "phim 中文_zh.srt"
        self.destination = self.root / "output with spaces" / "phim 中文_vi.srt"
        self.glossary = self.root / "glossary.json"
        self.glossary.write_text('{"师尊": "sư tôn"}', encoding="utf-8")
        self.cues = [
            Subtitle(7, 1.2, 3.5, "师尊，你好"),
            Subtitle(11, 5.001, 6.75, "你去哪里"),
            Subtitle(20, 12.2, 13.7, "我去修炼"),
        ]
        write_srt(self.source, self.cues)
        self.settings = SimpleNamespace(
            gemini_api_key="test-key-never-used",
            gemini_model="gemini-2.5-flash",
            translation_batch_size=2,
            translation_context_size=1,
            translation_retries=0,
            translation_retry_delay=0.0,
            gemini_timeout=120.0,
            glossary_path=self.glossary,
            temp_dir=self.root / "temp",
            output_dir=self.destination.parent,
            project_dir=self.root,
        )

    def test_batches_keep_ids_timestamps_and_include_adjacent_context(self) -> None:
        client = MagicMock()
        with patch("translate_subtitles._create_client", return_value=client), patch(
            "translate_subtitles._generate", side_effect=[{7: "Chào sư tôn.", 11: "Người đi đâu?"}, {20: "Ta đi tu luyện."}]
        ) as generate:
            result = translate_subtitles(self.source, self.destination, self.settings)
        self.assertEqual(len(generate.call_args_list), 2)
        self.assertEqual([(cue.id, cue.start, cue.end) for cue in result], [(cue.id, cue.start, cue.end) for cue in self.cues])
        first_prompt = json.loads(generate.call_args_list[0].args[1])
        second_prompt = json.loads(generate.call_args_list[1].args[1])
        self.assertEqual([row["id"] for row in first_prompt["target_subtitles"]], [7, 11])
        self.assertEqual(first_prompt["context_after"], [{"id": 20, "text_zh": "我去修炼"}])
        self.assertEqual(second_prompt["context_before"][0]["text_vi"], "Người đi đâu?")
        self.assertEqual(second_prompt["glossary"], {"师尊": "sư tôn"})
        self.assertEqual(read_srt(self.destination), result)
        client.close.assert_called_once()

    def test_existing_manual_srt_is_never_overwritten_or_sent_to_api(self) -> None:
        edited = [Subtitle(7, 1.5, 3.8, "Bản đã sửa thủ công.")]
        write_srt(self.destination, edited)
        original_bytes = self.destination.read_bytes()
        self.settings.gemini_api_key = ""
        self.source.unlink()
        with patch("translate_subtitles._create_client") as create:
            result = translate_subtitles(self.source, self.destination, self.settings)
        create.assert_not_called()
        self.assertEqual(result, edited)
        self.assertEqual(self.destination.read_bytes(), original_bytes)

    def test_main_translate_mode_reuses_edited_srt_without_api_key(self) -> None:
        import main as pipeline

        video = self.root / "phim 中文.mp4"
        video.touch()  # Cached SRT mode must not attempt OCR/decode this placeholder.
        write_srt(self.destination.parent / self.source.name, self.cues)
        edited = [Subtitle(7, 1.5, 3.8, "Bản đã sửa thủ công.")]
        write_srt(self.destination, edited)
        original_bytes = self.destination.read_bytes()
        settings = Settings(
            output_dir=self.destination.parent,
            glossary_path=self.glossary,
            temp_dir=self.settings.temp_dir,
            gemini_api_key="",
        )
        with patch("main.load_settings", return_value=settings), patch(
            "translate_subtitles._create_client"
        ) as create:
            exit_code = pipeline.main([str(video), "--translate"])
        self.assertEqual(exit_code, 0)
        create.assert_not_called()
        self.assertEqual(self.destination.read_bytes(), original_bytes)

    def test_quota_failure_keeps_source_and_completed_cache_then_resumes(self) -> None:
        source_bytes = self.source.read_bytes()
        with patch("translate_subtitles._create_client", return_value=MagicMock()), patch(
            "translate_subtitles._generate", side_effect=[{7: "Chào sư tôn.", 11: "Người đi đâu?"}, FakeApiError(429)]
        ):
            with self.assertRaises(TranslationError) as raised:
                translate_subtitles(self.source, self.destination, self.settings)
        self.assertIn("429", str(raised.exception))
        self.assertNotIn("secret-api-key", str(raised.exception))
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.source.read_bytes(), source_bytes)
        self.assertEqual(len(list(self.settings.temp_dir.rglob("*.json"))), 1)
        with patch("translate_subtitles._create_client", return_value=MagicMock()), patch(
            "translate_subtitles._generate", return_value={20: "Ta đi tu luyện."}
        ) as generate:
            result = translate_subtitles(self.source, self.destination, self.settings)
        generate.assert_called_once()
        self.assertEqual(generate.call_args.args[2], [20])
        self.assertEqual(result[0].text, "Chào sư tôn.")

    def test_glossary_change_invalidates_cache(self) -> None:
        answers = [{7: "Chào sư tôn.", 11: "Người đi đâu?"}, {20: "Ta đi tu luyện."}]
        with patch("translate_subtitles._create_client", return_value=MagicMock()), patch(
            "translate_subtitles._generate", side_effect=answers
        ):
            translate_subtitles(self.source, self.destination, self.settings)
        self.destination.unlink()
        self.glossary.write_text('{"师尊": "sư phụ"}', encoding="utf-8")
        with patch("translate_subtitles._create_client", return_value=MagicMock()), patch(
            "translate_subtitles._generate", side_effect=answers
        ) as generate:
            translate_subtitles(self.source, self.destination, self.settings)
        self.assertEqual(generate.call_count, 2)

    def test_force_backs_up_manual_output_after_success(self) -> None:
        write_srt(self.destination, [Subtitle(7, 1.2, 3.5, "Bản sửa tay")])
        original_bytes = self.destination.read_bytes()
        with patch("translate_subtitles._create_client", return_value=MagicMock()), patch(
            "translate_subtitles._generate", side_effect=[{7: "Chào.", 11: "Đi đâu?"}, {20: "Tu luyện."}]
        ):
            translate_subtitles(self.source, self.destination, self.settings, overwrite=True)
        backups = list(self.destination.parent.glob("*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original_bytes)
        self.assertNotEqual(self.destination.read_bytes(), original_bytes)

    def test_force_failure_does_not_modify_existing_output(self) -> None:
        write_srt(self.destination, [Subtitle(7, 1.2, 3.5, "Bản sửa tay")])
        original_bytes = self.destination.read_bytes()
        with patch("translate_subtitles._create_client", return_value=MagicMock()), patch(
            "translate_subtitles._generate", side_effect=FakeApiError(403)
        ):
            with self.assertRaises(TranslationError):
                translate_subtitles(self.source, self.destination, self.settings, overwrite=True)
        self.assertEqual(self.destination.read_bytes(), original_bytes)
        self.assertEqual(list(self.destination.parent.glob("*.bak")), [])

    def test_same_input_output_is_rejected_even_with_force(self) -> None:
        original_bytes = self.source.read_bytes()
        with self.assertRaises(TranslationError):
            translate_subtitles(self.source, self.source, self.settings, overwrite=True)
        self.assertEqual(self.source.read_bytes(), original_bytes)

    def test_fully_cached_translation_needs_no_api_key_or_sdk_client(self) -> None:
        with patch("translate_subtitles._create_client", return_value=MagicMock()), patch(
            "translate_subtitles._generate", side_effect=[{7: "Chào.", 11: "Đi đâu?"}, {20: "Tu luyện."}]
        ):
            first = translate_subtitles(self.source, self.destination, self.settings)
        self.destination.unlink()
        self.settings.gemini_api_key = ""
        with patch("translate_subtitles._create_client") as create:
            second = translate_subtitles(self.source, self.destination, self.settings)
        create.assert_not_called()
        self.assertEqual(second, first)

    def test_retries_are_finite_and_do_not_log_secrets(self) -> None:
        self.settings.translation_retries = 2
        with patch("translate_subtitles._generate", side_effect=FakeApiError(429)) as generate, patch(
            "translate_subtitles.time.sleep"
        ) as sleep, self.assertLogs("translate_subtitles", level="WARNING") as logs:
            with self.assertRaises(TranslationError):
                _generate_with_retry(MagicMock(), "{}", [7], self.settings)
        self.assertEqual(generate.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertNotIn("secret-api-key", " ".join(logs.output))

    def test_auth_error_is_not_retried(self) -> None:
        self.settings.translation_retries = 3
        with patch("translate_subtitles._generate", side_effect=FakeApiError(403)) as generate, patch(
            "translate_subtitles.time.sleep"
        ) as sleep:
            with self.assertRaises(TranslationError):
                _generate_with_retry(MagicMock(), "{}", [7], self.settings)
        generate.assert_called_once()
        sleep.assert_not_called()

    def test_network_timeout_is_retried(self) -> None:
        self.settings.translation_retries = 1
        with patch("translate_subtitles._generate", side_effect=[TimeoutError(), {7: "Xin chào."}]) as generate, patch(
            "translate_subtitles.time.sleep"
        ):
            result = _generate_with_retry(MagicMock(), "{}", [7], self.settings)
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(result, {7: "Xin chào."})

    def test_glossary_must_be_nonblank_string_map(self) -> None:
        for invalid in [[], {"师尊": 7}, {"": "sư tôn"}, {"师尊": "  "}]:
            with self.subTest(invalid=invalid):
                self.glossary.write_text(json.dumps(invalid), encoding="utf-8")
                with self.assertRaises(TranslationError):
                    load_glossary(self.glossary)

    def test_real_sdk_configuration_and_response_validation(self) -> None:
        try:
            from google.genai import types
        except ImportError:
            self.skipTest("google-genai chưa được cài; chạy lại sau pip install requirements.txt")
        client = MagicMock()
        client.models.generate_content.return_value.text = '{"subtitles":[{"id":7,"text":"Xin chào."}]}'
        self.assertEqual(_generate(client, "context", [7], self.settings), {7: "Xin chào."})
        request = client.models.generate_content.call_args.kwargs
        self.assertEqual(request["model"], "gemini-2.5-flash")
        self.assertEqual(request["contents"], "context")
        self.assertIsInstance(request["config"], types.GenerateContentConfig)
        self.assertEqual(request["config"].response_mime_type, "application/json")
        self.assertEqual(request["config"].response_json_schema["required"], ["subtitles"])
        for raw in [
            '{"subtitles":',
            '{"subtitles":[{"id":7,"id":8,"text":"A"}]}',
            '{"subtitles":[{"id":8,"text":"Sai ID"}]}',
            None,
        ]:
            with self.subTest(raw=raw):
                client.models.generate_content.return_value.text = raw
                with self.assertRaises(ResponseValidationError):
                    _generate(client, "context", [7], self.settings)

    def test_sdk_serializes_json_schema_and_parses_response_without_network(self) -> None:
        try:
            import httpx
            from google import genai
            from google.genai import types
        except ImportError:
            self.skipTest("google-genai chưa được cài; chạy lại sau pip install requirements.txt")
        requests: list[dict] = []

        def serve(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={
                "candidates": [{
                    "content": {"role": "model", "parts": [{
                        "text": '{"subtitles":[{"id":7,"text":"Xin chào."}]}'
                    }]},
                    "finishReason": "STOP",
                }],
            })

        with httpx.Client(transport=httpx.MockTransport(serve)) as transport:
            with genai.Client(
                api_key="unit-test-placeholder",
                vertexai=False,
                http_options=types.HttpOptions(httpx_client=transport, retry_options=types.HttpRetryOptions(attempts=1)),
            ) as client:
                self.assertEqual(_generate(client, "context", [7], self.settings), {7: "Xin chào."})
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["generationConfig"]["responseMimeType"], "application/json")
        self.assertIn("responseJsonSchema", requests[0]["generationConfig"])
        self.assertIn("systemInstruction", requests[0])


class ResponseValidationTests(unittest.TestCase):
    def test_reordered_ids_are_allowed_but_preserved(self) -> None:
        self.assertEqual(validate_translation({"subtitles": [
            {"id": 11, "text": " Đi đâu? "}, {"id": 7, "text": "Xin chào."}
        ]}, [7, 11]), {11: "Đi đâu?", 7: "Xin chào."})

    def test_invalid_responses_are_rejected(self) -> None:
        invalid = [
            {"subtitles": [{"id": 7, "text": "A"}]},
            {"subtitles": [{"id": 7, "text": "A"}, {"id": 7, "text": "B"}]},
            {"subtitles": [{"id": 7, "text": "A"}, {"id": 12, "text": "B"}]},
            {"subtitles": [{"id": "7", "text": "A"}, {"id": 11, "text": "B"}]},
            {"subtitles": [{"id": 7, "text": " "}, {"id": 11, "text": "B"}]},
            {"subtitles": [{"id": 7, "text": 123}, {"id": 11, "text": "B"}]},
            {"subtitles": [{"id": 7, "text": "A", "start": 0}, {"id": 11, "text": "B"}]},
            {"subtitles": [{"id": 7, "text": "A\x00"}, {"id": 11, "text": "B"}]},
            {"subtitles": "bad"},
            {"subtitles": [], "timestamps": []},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(ResponseValidationError):
                    validate_translation(payload, [7, 11])

    def test_boolean_id_is_not_treated_as_integer(self) -> None:
        with self.assertRaises(ResponseValidationError):
            validate_translation({"subtitles": [{"id": True, "text": "A"}]}, [1])

    def test_internal_empty_lines_are_normalized_for_valid_srt(self) -> None:
        self.assertEqual(validate_translation({"subtitles": [{"id": 7, "text": " A\r\n\r\nB "}]}, [7]), {7: "A\nB"})


if __name__ == "__main__":
    unittest.main()
