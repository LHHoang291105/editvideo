"""One cancellable subprocess job; the web server stays responsive."""
from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from typing import Any
import uuid


class JobManager:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.lock = threading.RLock()
        self.job: dict[str, Any] | None = None
        self.process: subprocess.Popen | None = None
        self.lines: deque[dict[str, Any]] = deque(maxlen=2000)
        self.sequence = 0

    def busy(self) -> bool:
        return bool(self.job and self.job['status'] in ('running', 'stopping'))

    def require_idle(self) -> None:
        if self.busy():
            raise ValueError('Có tác vụ đang chạy. Dừng hoặc chờ hoàn tất trước khi sửa/dọn dữ liệu.')

    def snapshot(self, after: int = 0) -> dict[str, Any]:
        with self.lock:
            return {'job': dict(self.job) if self.job else None,
                    'logs': [line for line in self.lines if line['seq'] > after], 'cursor': self.sequence}

    def start(self, request: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.require_idle()
            self.lines.clear()
            self.sequence = 0
            self.job = {'id': uuid.uuid4().hex, 'project': request['project'], 'action': request['action'],
                        'status': 'running', 'started': time.time(), 'progress': None, 'stage': 'Đang khởi động'}
            flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            try:
                self.process = subprocess.Popen(
                    [sys.executable, '-u', str(self.root / 'web_worker.py')], cwd=self.root,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding='utf-8', errors='replace',
                    env={**os.environ, 'PYTHONIOENCODING': 'utf-8'}, creationflags=flags,
                    start_new_session=os.name != 'nt',
                )
            except OSError:
                self.job['status'] = 'failed'
                raise
            threading.Thread(target=self._watch, args=(self.process, request), daemon=True).start()
            return dict(self.job)

    def _watch(self, process: subprocess.Popen, request: dict[str, Any]) -> None:
        secret = request.get('api_key', '')
        code = 1
        try:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(json.dumps(request, ensure_ascii=False))
            process.stdin.close()
            request.clear()
            for raw in process.stdout:
                line = raw.strip()
                if not line:
                    continue
                if secret:
                    line = line.replace(secret, '[API key]')
                with self.lock:
                    self.sequence += 1
                    self.lines.append({'seq': self.sequence, 'text': line[-5000:]})
                    match = re.search(r'(TTS|Timeline|VC)\s*\[(\d+)/(\d+)\]', line)
                    if match and self.job:
                        self.job['stage'] = (
                            'Tạo giọng' if match[1] == 'TTS' else
                            'Ghép timeline' if match[1] == 'Timeline' else
                            'Đổi giọng RVC (GPU)'
                        )
                        self.job['progress'] = round(100 * int(match[2]) / max(1, int(match[3])))
                    elif self.job and self.job['action'] == 'voice' and ('RVC:' in line or 'Voice Lab:' in line):
                        self.job['stage'] = 'Đổi giọng RVC (GPU)'
                        self.job['progress'] = None
                    elif self.job and 'FFmpeg:' in line:
                        self.job['stage'] = (
                            'Tua chậm video gốc' if self.job['action'] == 'slow' else
                            'Đổi giọng / ghép MP4' if self.job['action'] == 'voice' else 'Xuất video'
                        )
                        self.job['progress'] = None
                    elif self.job and 'Tăng tốc giọng Việt' in line:
                        self.job['stage'] = 'Điều chỉnh tốc độ giọng Việt'
                        self.job['progress'] = None
            code = process.wait()
        except Exception:
            with self.lock:
                self.sequence += 1
                self.lines.append({'seq': self.sequence, 'text': 'Không đọc được tiến trình worker.'})
            if process.poll() is None:
                process.kill()
                process.wait()
        finally:
            if process.stdout:
                process.stdout.close()
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            with self.lock:
                if self.job:
                    self.job['status'] = ('cancelled' if self.job['status'] == 'stopping'
                                          else 'completed' if code == 0 else 'failed')
                    self.job['finished'] = time.time()
                    self.job['exit_code'] = code
                self.process = None

    def cancel(self) -> None:
        with self.lock:
            if not self.busy() or self.process is None:
                return
            self.job['status'] = 'stopping'
            process = self.process
        # Terminate descendants (including FFmpeg), not just the Python parent.
        if os.name == 'nt':
            result = subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                    capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=20)
            if result.returncode and process.poll() is None:
                with self.lock:
                    self.job['status'] = 'running'
                raise RuntimeError('Windows chưa cho phép dừng tiến trình. Hãy kiểm tra quyền chạy app.')
        else:
            import signal
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
