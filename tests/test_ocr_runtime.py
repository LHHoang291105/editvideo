from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from config import Settings
from ocr_runtime import select_ocr_device


class DeviceTests(unittest.TestCase):
    def test_cpu_build_does_not_silently_accept_gpu(self) -> None:
        paddle = MagicMock()
        paddle.is_compiled_with_cuda.return_value = False
        with patch.dict("sys.modules", {"paddle": paddle}), self.assertRaisesRegex(RuntimeError, "bản CPU"):
            select_ocr_device(Settings(ocr_device="gpu:0"))
        paddle.set_device.assert_not_called()

    def test_unavailable_gpu_fails(self) -> None:
        paddle = MagicMock()
        paddle.is_compiled_with_cuda.return_value = True
        paddle.device.cuda.device_count.return_value = 1
        with patch.dict("sys.modules", {"paddle": paddle}), self.assertRaisesRegex(RuntimeError, "Không thấy GPU"):
            select_ocr_device(Settings(ocr_device="gpu:1"))
        paddle.set_device.assert_not_called()

    def test_gpu_device_is_selected_explicitly(self) -> None:
        paddle = MagicMock()
        paddle.__version__ = "test-version"
        paddle.is_compiled_with_cuda.return_value = True
        paddle.device.cuda.device_count.return_value = 1
        with patch.dict("sys.modules", {"paddle": paddle}):
            self.assertEqual(select_ocr_device(Settings(ocr_device="gpu:0")), "gpu:0")
        paddle.set_device.assert_called_once_with("gpu:0")


if __name__ == "__main__":
    unittest.main()
