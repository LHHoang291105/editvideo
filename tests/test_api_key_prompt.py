from __future__ import annotations

from dataclasses import replace
import getpass
from pathlib import Path
import tempfile
import unittest
import warnings
from unittest.mock import MagicMock, patch

from api_key_prompt import prompt_api_key
from common import Subtitle, write_srt
from config import Settings
from translate_subtitles import translate_subtitles


class KeyPromptTests(unittest.TestCase):
    def test_existing_key_does_not_prompt(self) -> None:
        settings = Settings(gemini_api_key="configured-test-key")
        with patch("api_key_prompt.getpass.getpass") as prompt:
            self.assertIs(prompt_api_key(settings), settings)
        prompt.assert_not_called()

    def test_prompt_returns_key_without_changing_original_settings_or_showing_repr(self) -> None:
        settings = Settings()
        with patch("api_key_prompt.sys.stdin.isatty", return_value=True), patch(
            "api_key_prompt.getpass.getpass", return_value="  entered-test-key  "
        ):
            updated = prompt_api_key(settings)
        self.assertEqual(updated.gemini_api_key, "entered-test-key")
        self.assertEqual(settings.gemini_api_key, "")
        self.assertNotIn("entered-test-key", repr(updated))

    def test_force_overrides_configured_key_in_memory(self) -> None:
        settings = Settings(gemini_api_key="configured-test-key")
        with patch("api_key_prompt.sys.stdin.isatty", return_value=True), patch(
            "api_key_prompt.getpass.getpass", return_value="new-test-key"
        ):
            updated = prompt_api_key(settings, force=True)
        self.assertEqual(updated.gemini_api_key, "new-test-key")
        self.assertEqual(settings.gemini_api_key, "configured-test-key")

    def test_noninteractive_session_fails_without_reading_stdin(self) -> None:
        with patch("api_key_prompt.sys.stdin.isatty", return_value=False), patch("api_key_prompt.getpass.getpass") as prompt:
            with self.assertRaisesRegex(RuntimeError, "không tương tác"):
                prompt_api_key(Settings())
        prompt.assert_not_called()

    def test_blank_eof_and_unsafe_echo_are_handled(self) -> None:
        def unsafe_echo(*args: object) -> str:
            warnings.warn("echo unsupported", getpass.GetPassWarning)
            self.fail("Must stop before echoing input")

        for behavior in (lambda *args: "  ", lambda *args: (_ for _ in ()).throw(EOFError()), unsafe_echo):
            with self.subTest(behavior=behavior), patch("api_key_prompt.sys.stdin.isatty", return_value=True), patch(
                "api_key_prompt.getpass.getpass", side_effect=behavior
            ), self.assertRaises(RuntimeError):
                prompt_api_key(Settings())

    def test_translation_prompts_once_and_never_writes_key_to_cache(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source, output = root / "zh.srt", root / "vi.srt"
            glossary = root / "glossary.json"
            glossary.write_text("{}", encoding="utf-8")
            write_srt(source, [Subtitle(1, 0, 1, "你好"), Subtitle(2, 1, 2, "谢谢")])
            settings = Settings(glossary_path=glossary, temp_dir=root / "temp", translation_batch_size=1)
            provider = MagicMock(return_value=replace(settings, gemini_api_key="entered-test-secret"))
            with patch("translate_subtitles._create_client") as create, patch(
                "translate_subtitles._generate_with_retry", side_effect=[{1: "Xin chào."}, {2: "Cảm ơn."}]
            ):
                translate_subtitles(source, output, settings, key_provider=provider)
            provider.assert_called_once_with(settings)
            self.assertEqual(create.call_args.args[0].gemini_api_key, "entered-test-secret")
            for path in root.rglob("*"):
                if path.is_file():
                    self.assertNotIn("entered-test-secret", path.read_text(encoding="utf-8"))
            # Edited/existing output skips key entry entirely.
            provider.reset_mock()
            translate_subtitles(source, output, settings, key_provider=provider)
            provider.assert_not_called()
            # Even without output, complete batch cache needs no key.
            output.unlink()
            translate_subtitles(source, output, settings, key_provider=provider)
            provider.assert_not_called()


if __name__ == "__main__":
    unittest.main()
