# Kết quả kiểm tra

Kiểm tra ngày **14/09/2026**, trực tiếp trên Windows x64, Python **3.12.14** trong `.venv` của project.

## Cập nhật GPU và nhập API key tại CLI

Đã chuyển môi trường hiện tại sang **paddlepaddle-gpu 3.3.1 / CUDA 12.6**, gỡ gói Paddle CPU, đặt mặc định `OCR_DEVICE=gpu:0`. Driver NVIDIA 610.47 nhận **GeForce GTX 1650, 4 GB VRAM, compute capability 7.5**. `check_environment.py` đã tính tensor thật trên `Place(gpu:0)` và trả 0 lỗi; `pip check` không có dependency lỗi.

PaddleOCR thật trên GPU vẫn nhận đúng hai câu/mốc thời gian của clip 3,5 giây mô tả bên dưới. Benchmark trên cùng clip, ROI, 4 FPS, model và cùng bản Paddle GPU dùng lần lượt thiết bị CPU/GPU; bỏ một lượt warmup mỗi thiết bị, lấy trung vị hai lượt:

| Thiết bị | Thời gian xử lý 14 mẫu, không gồm tải/khởi tạo model |
| --- | --- |
| CPU | 4,838 giây |
| GTX 1650 (`gpu:0`) | 0,346 giây |

Tỷ lệ khoảng **13,96×** trên clip kiểm thử ngắn này. **Nội dung, ID và timestamp giống nhau**; không suy rộng tỷ lệ này cho mọi video. Kết quả cục bộ: `temp/ocr_gpu_benchmark.json`; có thể chạy lại bằng `python tests/benchmark_ocr.py <video.mp4>`.

**Cảnh báo còn tồn tại:** wheel Paddle GPU cu126 in thông báo compiled cuDNN 9.9 nhưng dependency chính nó yêu cầu/cài là cuDNN 9.5.1.17. Đây là tình huống được báo tại [Paddle issue #79388](https://github.com/PaddlePaddle/Paddle/issues/79388). Các phép tính và OCR của project đã chạy thành công, nhưng không khẳng định cảnh báo vô hại cho mọi model/operator. Project giữ đúng dependency của wheel, không giấu warning hay sửa metadata để vượt `pip check`.

CLI `main.py` và `translate_subtitles.py` tự hỏi key khi cần API nhưng cấu hình chưa có key; `--ask-api-key` cho phép nhập key thay thế. Đã thử **terminal Windows thật**: prompt nhập ẩn nhận được key kiểm thử và không hiển thị ký tự đã nhập. Không gọi API bằng key kiểm thử. Các test xác nhận chỉ hỏi một lần, không ghi key vào file/cache/repr, không hỏi với SRT Việt hiện có hoặc cache đầy đủ, báo lỗi rõ khi stdin không tương tác.

Kết quả unit test sau cập nhật: **55/55 đạt**. Bản lock chính hiện là môi trường GPU; môi trường CPU ban đầu được giữ trong `requirements-lock-cpu-windows-py312.txt`.

## Môi trường và dependency

Các kết quả CPU dưới đây là mốc kiểm tra ban đầu trước khi chuyển GPU; kết quả cập nhật ở phần trên.

- Cài thành công toàn bộ `requirements.txt` vào môi trường riêng.
- `python -m pip check`: **No broken requirements found**.
- `python check_environment.py`: **0 lỗi**; import thật thành công `numpy`, `cv2`, `dotenv`, `google.genai`, `edge_tts`, `paddle`, `paddleocr`.
- Phiên bản chính: PaddlePaddle 3.3.1, PaddleOCR 3.7.0, PaddleX 3.7.2, OpenCV contrib 4.10.0.84, NumPy 2.3.5, google-genai 1.75.0, edge-tts 7.2.8, python-dotenv 1.2.3.
- FFmpeg/FFprobe **9.0.1 essentials**, có libass/subtitles và libx264, chạy thật từ `.tools/ffmpeg/ffmpeg-9.0.1-essentials_build/bin`.
- Dependency gián tiếp đã đóng băng trong `requirements-lock-windows-py312.txt`. Python 3.11 được hỗ trợ theo cú pháp source và yêu cầu phiên bản của các dependency trực tiếp; lần kiểm tra này dùng Python 3.12, không khẳng định đã chạy trên mọi phiên bản Python/mọi máy Windows.

## Syntax, import và CLI

`compileall`/`py_compile` đã chạy qua source và tests, không có lỗi syntax. Các module ứng dụng được import bởi bộ kiểm thử; import thư viện thực tế được kiểm tra riêng như trên.

`--help` của `main.py`, `extract_subtitles.py`, `translate_subtitles.py`, `tts_from_srt.py`, `mix_audio.py`, `burn_subtitle.py`, `check_environment.py` chạy được từ thư mục làm việc khác project. Có kiểm tra output encoding Windows cp1252 và đường dẫn Unicode/khoảng trắng/dấu nháy.

CLI `main.py <video-mẫu> --ocr-only` chạy OCR thật và tạo SRT. CLI `--translate` khi thiếu key báo lỗi dễ hiểu, trả mã 1 và vẫn giữ nguyên SRT Trung. Test tích hợp `main --translate` với SRT Việt đã sửa xác nhận không gọi API và không thay file.

## Kiểm thử logic

**46/46 kiểm thử đạt ở mốc CPU ban đầu**; sau khi bổ sung CLI key và chọn GPU là **55/55**. Log lần chạy cuối tại `temp/validation-unit.log`.

Các kiểm thử trong `tests/test_*.py` bao gồm:

- SRT UTF-8/BOM/CRLF, IDs/timestamps hợp lệ, ghi file nguyên tử và từ chối ghi đè bản hiện có.
- Cấu hình, đường dẫn cấu hình theo project, đường dẫn CLI theo thư mục hiện tại, lỗi thiếu FFmpeg.
- OCR grouping, fuzzy, câu ngắn khác nhau, khoảng trống, chống gộp trôi chuỗi, thứ tự box, câu cuối kéo dài tới EOF với video FPS thấp.
- Batch/context/glossary dịch, kiểm tra ID/text JSON, bảo toàn ID/timestamp, cache khi quota lỗi, retry hữu hạn và không log API key, backup khi chủ động `--force`.
- SDK Gemini thật khởi tạo cấu hình/serialize request/parse response qua `httpx.MockTransport`. Đây là kiểm thử SDK không dùng mạng, không phải bằng chứng dịch vụ Gemini đã dịch thật.
- Đo WAV thật, cache theo nội dung và hash file, chỉnh lời/timing, overlap theo **start câu tiếp theo**, sample placement, clip ở cuối video, giữ output cũ khi lỗi.

Chạy lại bằng:

```powershell
python -X utf8 -m unittest discover -s tests -v
```

## PaddleOCR thật

`tests/smoke_ocr.py` tạo video MP4 960×540 dài 3,5 giây, 24 FPS, chứa chữ Trung ở phía trên và hai câu phụ đề ở phía dưới. Sau đó chạy model PaddleOCR thật; không thay kết quả bằng mock.

**Đạt:** 84 frame nguồn, chỉ OCR 14 frame ở 4 FPS; chữ phía trên không xuất hiện trong SRT; nhận đúng hai câu:

```srt
1
00:00:00,500 --> 00:00:01,500
你好，欢迎来到中国

2
00:00:02,000 --> 00:00:03,000
我们一起去修仙吧
```

Video và SRT dùng đường dẫn có tiếng Trung, tiếng Việt, khoảng trắng. Lần chạy đủ pipeline OCR dùng FFprobe thật để lấy duration.

Trong quá trình thử đã phát hiện lỗi oneDNN/PIR của Paddle 3.3.1 trên Windows; source đặt `enable_mkldnn=False` và đã chạy lại thành công. Cache model/extension được đưa về thư mục project bằng các biến môi trường được thư viện hỗ trợ.

## FFmpeg thật

`tests/smoke_media.py` tạo các video 16 giây bằng FFmpeg, có và không có audio gốc. WAV tone dài 2,1 giây là **fixture kiểm tra timing**, không phải giọng TTS và không được sử dụng làm backend ứng dụng.

**Đạt:**

- Timeline 384.000 samples ở 24 kHz, đúng 16 giây.
- Audio đầu bắt đầu ở sample 292.800 = **12,200 giây**, không có mẫu âm thanh trước đó.
- Tổng mẫu ở vùng overlap đúng bằng phép cộng hai WAV; WAV đầu dài 2,1 giây kết thúc ở 14,300 giây.
- Cắt phần vượt cuối video, có cảnh báo; không thay tốc độ đọc.
- Mix được nguồn có và không có track audio; giữ được audio gốc theo volume đã cấu hình.
- Cả ba MP4 kết quả có track audio và duration video 16 giây.
- Burn SRT Việt bằng libass; so sánh pixel xác nhận có chữ trong khoảng subtitle và không có chữ ngoài khoảng đó.
- Tên thư mục/tập tin có tiếng Việt, tiếng Trung, khoảng trắng, `[]` và dấu nháy đơn.

Frame burn mẫu cũng đã được xem trực tiếp: chữ Việt và dấu hiển thị đúng, không bị cắt ở mép hình.

Chạy lại bằng:

```powershell
python tests/smoke_media.py --ffmpeg 'C:\Tools\ffmpeg\bin\ffmpeg.exe' --ffprobe 'C:\Tools\ffmpeg\bin\ffprobe.exe'
```

## Edge TTS và giới hạn kiểm chứng

Đã thử gọi **dịch vụ Edge TTS thật** với câu Việt ngắn. Môi trường sandbox chặn kết nối tới `speech.platform.bing.com:443` (`Access is denied`). Lần yêu cầu chạy ngoài sandbox bị hủy trước khi có kết quả thực thi; vì vậy **chưa xác minh được Edge tạo giọng thành công qua mạng** và chưa có WAV giọng Edge thật để nghe thử. Không dùng tone fixture để thay thế hoặc báo thành công cho bước này.

`edge_tts` import thành công; code production gọi `edge_tts.Communicate.save()` rồi FFmpeg chuyển MP3 thành WAV. Kiểm thử cache, WAV, lỗi và timing đã đạt. Trên mạng cho phép truy cập dịch vụ, chạy `python tts_from_srt.py output/video_vi.srt` hoặc smoke test có `--edge-only` để kiểm chứng phần dịch vụ.

Chưa gọi Gemini dịch thật vì workspace không có `GEMINI_API_KEY`. Các tình huống 429/5xx/timeout được kiểm tra bằng phản hồi lỗi có kiểm soát. Chất lượng lời dịch phụ thuộc model, glossary và việc duyệt SRT.

Các kiểm tra OCR/media dùng clip ngắn tự tạo; chưa có video phim thật của người dùng để đánh giá chữ mờ, nhiều dòng, phụ đề đổi nhanh hoặc thời gian xử lý video dài.

## Gemini model configuration fix (2026-09-14)

Default and local .env now select gemini-3.6-flash, listed in the official Gemini model catalog. Both translation CLIs accept --gemini-model. HTTP 404 triggers model metadata discovery; errors remain sanitized and no model is silently substituted. All 57 unit tests pass, including 404/source preservation and metadata failure. Both CLI help commands pass. No live generation with the user key was performed; account access to the new model remains unverified.

## Manual translation workflow (2026-09-14)

Added --manual-translate for exclusive draft creation and --dub-only for direct Vietnamese SRT dubbing without OCR/Gemini. All 61 tests pass, including edit preservation, missing SRT handling and API bypass. Ran the manual CLI against aaa.mp4: existing aaa_zh.srt reused and existing aaa_vi.srt preserved. No TTS or paid API call performed in this check.

## Edge TTS retry recovery (2026-09-14)

64 tests pass, including capped retry delays, partial file cleanup, request spacing, timeout, and resume without regenerating completed cues. Defaults/local .env use 6 attempts, 5s exponential delay capped at 60s, 1s spacing, and 90s async timeout. Sandbox live probe failed with ClientConnectorError; approved network probe succeeded on the actual subtitle ID 196, producing 3.552s PCM WAV in temp/edge_retry_probe_196.wav. Full remaining synthesis was not run. Sandbox could not read existing WAVs (PermissionError); approved read-only validation confirmed 195/315 matching cached WAVs, with the first missing ID 196.

## Automatic empty subtitle filtering (2026-09-14)

read_srt now recognizes ID/timestamp headers, skips empty cue bodies and removes blank dialogue lines left after manual edits. It validates all headers before filtering and preserves remaining IDs/times. No source SRT is rewritten. The shared reader applies to TTS, mixing and burn output. All 68 tests pass, including empty cues at start/middle/end, all-empty input, malformed timing/duplicate IDs, source byte preservation and exclusion of cached speech for deleted dialogue.

## Separate speech/display SRTs (2026-09-14)

Added prepare_tts_srt.py and automatic main integration. Adjacent matching speech within 0.3s is merged, preserving first ID/text/start and maximum end. Display SRT remains untouched. mix_audio --display-srt separates burn input from speech timeline. Full 72-test suite passed, then one additional mixer routing regression passed (73 tests total). Actual aaa_vi.srt: 267 display cues -> 155 speech cues (112 merged) in aaa_vi_tts.srt. No paid API, synthesis or full video rendering was run for this change.

## VietDub Studio web application and voice speed (2026-09-14)

Implemented local Flask 3.1.3 + Waitress 3.0.2 app on loopback port 7860. Features: reference/upload MP4, isolated project data, legacy CLI adoption, GPU OCR, manual SRT editor/import/export with conflict checks and backups, optional Gemini (key via stdin only), separate speech/display SRT, Edge TTS, per-voice speed, original/voice volume, burn, ranged video/audio preview, subprocess progress and cancellation. Cleanup is preview-token based, checks paths/fingerprints, blocks during jobs, rejects symlinks/junctions, and preserves registered input videos, SRT/source/dependencies/model cache.

Validation: all 90 tests passed outside the sandbox on Windows (required for taskkill cancellation tests), then CLI help test including web_app.py passed. pip check passed. Browser verified rendering, 267-cue editor, actual prepare job completion, cleanup preview, and no JS console errors. Actual source/WAV range requests returned 206. No user files were deleted through cleanup testing; deletion tests used temporary fixture files.

Live HTTP integration: tests/smoke_web_pipeline.py ran actual GTX 1650 OCR on a 3.5s test clip, obtained two Chinese cues, saved two Vietnamese lines through editor API, generated actual Edge speech, sped up individual voice WAVs at 1.1x, mixed and burned final video, and verified download Range 206. Report: temp/web_pipeline_smoke.json; the isolated test project is retained. No Gemini API call was made. Existing Paddle cuDNN warning remains as previously documented.

Real FFmpeg speed test: 2.1s tone fixture shortened to 1.908833s at 1.1x, exact voice start sample 292800 (12.2s at 24kHz), timeline 384000 samples (16s). Original WAV hash unchanged, original audio present, final video duration unchanged, and independent display SRT burned at 5.5s. Report: temp/speed_smoke_5jav3uf7/report.json. Tones are testing-only fixtures. Speed reduces overlap but does not guarantee all speech fits; live clip correctly warned about remaining overlap and end truncation.

The current local .env and imported aaa project are set to voice_speed 1.1 by user request. Source default/.env.example remain 1.0. No full re-render of the user aaa video was performed for web UI validation. The app is a local tool, not a public multi-user hosting deployment.

## Blur hard subtitle and mirror export (2026-09-14)

Added configurable `BLUR_CHINESE_SUBTITLES`, `BLUR_LEFT/TOP/RIGHT/BOTTOM`, `BLUR_STRENGTH`, and `MIRROR_VIDEO` to the `.env`, CLI and VietDub Studio. The production FFmpeg graph applies the selected blur region on the source image, then horizontal mirror, then burns Vietnamese SRT last. This keeps an asymmetric blur region with its original pixels and leaves Vietnamese glyphs readable.

Validation on this Windows machine: `py_compile` passed for all source and test files (excluding runtime data/cache directories); `pip check` passed; `check_environment.py` completed with 0 errors on `gpu:0`; and the full suite passed **94/94**. The real libass/FFmpeg integration fixture checked that the source image is mirrored, the lower hard-subtitle pattern loses high-frequency detail after blur, cue text appears only inside its timestamp, and the final SRT glyph matches a normal burn rather than a mirrored glyph.

## Invisible subtitle cue recovery (2026-09-14)

`read_srt`, SRT validation, writing and the web editor now remove non-speaking zero-width format marks (`U+200B`, `U+200C`, `U+200D`, `U+2060`, `U+FEFF`) before TTS. A zero-width-only cue is treated as an empty cue, while readable text with an accidental mark is normalized. The source SRT is not rewritten during reading. The regression test covers filtering an invisible cue, preserving the original source bytes, normalizing visible text, and rejecting an invisible-only cue on write.

Live recovery on a local 326.9-second project found 47 `U+200B`-only display cues. The original run was stopped after 11 valid WAV checkpoints because Edge correctly had no speech to return for the first such cue. After the fix, the restarted job skipped all 47 empty cues, reused the 11 valid WAVs, synthesized 144 speech cues, and completed the burn/blur/mirror render. The output contains H.264 video, AAC audio, duration 326.900 seconds; an upper-frame comparison confirmed the output is horizontally mirrored (mean mirror error 1.02 vs 47.18 without mirror).

## Edge TTS bare-sentence recovery (2026-09-15)

Added a one-shot fallback only for Edge `NoAudioReceived`: a Vietnamese sentence with no terminal punctuation is retried once with `.` in the Edge request. The source SRT and cache key remain the original text; the fallback never creates silent audio or skips valid speech. Punctuated sentences and non-`NoAudioReceived` errors are unchanged. `python -m py_compile tts_from_srt.py tests\\test_audio.py` and `python -m unittest tests.test_common tests.test_audio -q` passed (**32 tests**), including the fallback, punctuation guard, and temporary MP3 cleanup.

## Edge TTS deferred no-audio recovery (2026-09-15)

When Edge exhausts the normal retry budget with its typed `NoAudioReceived` response, the cue is now deferred while later cues continue to checkpoint. One bounded recovery pass retries every deferred cue at the end; any remaining IDs are reported together. Other errors still fail immediately, no silent WAV is written, and successful WAV/manifest entries remain resumable. Full suite passed: `python -m unittest discover -s tests -q` (**100 tests**).

## Edge TTS WordBoundary recovery (2026-09-15)

Live Edge probe with `vi-VN-HoaiMyNeural` showed a reproducible request-shape issue: `Nhưng mẫu phi...` with the default `SentenceBoundary` returned `NoAudioReceived`, while the **same exact text** with `WordBoundary` returned a 11,232-byte MP3. Replacing the ASCII ellipsis with `…` also returned audio, but production intentionally does not alter that punctuation because it can affect prosody.

`EdgeTTSProvider` now uses `WordBoundary` as its primary request mode and retries the original text once with `SentenceBoundary` only after typed `NoAudioReceived`. `edge-tts` sends the same SSML text/voice/rate/volume for either mode; only its metadata options differ. If both modes fail, the existing bare-sentence period fallback uses the primary `WordBoundary` mode. The deterministic mock regression test checks exact text and `WordBoundary` -> `SentenceBoundary` ordering, safe cleanup of a partial MP3, and separately checks that the period fallback happens only after both modes fail. `python -m py_compile tts_from_srt.py tests\\test_audio.py tests\\test_common.py` and `python -m unittest tests.test_common tests.test_audio -q` passed (**36 tests**); the complete suite passed (**101 tests**).

## Video nguồn tua chậm có bước duyệt (2026-09-16)

VietDub Studio có thêm Bước 0: FFmpeg tạo một MP4 xem trước, kéo chậm đồng thời video và audio Trung. App khóa OCR, SRT, TTS và render cho tới khi người dùng xem rồi xác nhận dùng bản chậm. Sau xác nhận, OCR và `mix_audio` cùng nhận đúng file nguồn chậm; quay lại video gốc hoặc đổi nguồn sao lưu SRT/timeline cũ thay vì tái sử dụng chúng.

`slow_video.py` được kiểm tra với FFmpeg thật: video 3,2 giây/25 FPS có audio tại 0,25× thành 12,8 giây có audio; fixture 29,97 FPS không audio tại 0,5× thành 5,005 giây (lệch một frame hợp lệ). Từ 16/09, encoder mặc định là GPU NVIDIA `h264_nvenc`; thử thật trên GTX 1650 cho video 2 giây tại 0,80× cho đầu ra 2,50 giây, H.264 và AAC. Test bảo vệ cả file gốc, output tạm lỗi, metadata review, source stale, SRT thủ công, và input của project khác. `node --check web/static/app.js`, `py_compile`, `pip check`, `check_environment.py` (GPU `gpu:0`, FFmpeg/FFprobe) và toàn bộ **123 tests** đều đạt. Không gọi Gemini hoặc Edge TTS thật cho thay đổi này.

## RVC cục bộ và RMVPE CUDA (2026-09-17)

VietDub Studio có RVC + RMVPE chạy trong CUDA runtime riêng. Project không kèm hoặc tự tải model giọng `.pth`; người dùng đặt model và `.index` tương ứng mà mình có quyền sử dụng vào `models\rvc`. App chỉ giải quyết model từ thư mục cục bộ này, không nhận đường dẫn model hoặc nguồn audio từ trình duyệt.

Pipeline RVC đã chạy thật trên GTX 1650 với một model cục bộ trong quá trình kiểm tra: source 7,608 giây xuất WAV 48 kHz 7,600 giây, `used_gpu=true`. Sau khi sửa `PYTHONPATH` cho CLI `infer/cli.py`, regression bao gồm đường dẫn RVC child và redaction metadata đạt. Kiểm tra tại mốc đó: `node --check web/static/app.js`, `py_compile`, `pip check` cho `.venv` và RVC runtime đều đạt; toàn bộ **144 tests** đạt.

## Danh mục Edge thật và dọn model thử (2026-09-17)

Kiểm tra `edge-tts` 7.2.8 trên máy này trả hai giọng `vi-VN`: Hoài My (nữ) và Nam Minh (nam). Các mục nữ “cute/mềm/tự nhiên” cũ thực chất đều là Hoài My với lệch F0 vài Hz, nên đã được bỏ khỏi web. Dropdown chỉ còn người nói Edge thật; dự án cũ tự bỏ `tts_pitch` đã lưu theo profile để quay về `TTS_PITCH=+0Hz` của cấu hình. `TTS_PITCH` vẫn là tinh chỉnh kỹ thuật trong `.env`, không phải một giọng mới.

Đã gỡ hai model RVC thử nghiệm cùng index và installer tương ứng: `suara_wanita_1/2` tổng 651.19 MiB, sau khi đối chiếu SHA-256. Thư mục `models/rvc` chỉ còn hướng dẫn cho model `.pth` do người dùng tự thêm; pipeline RVC/RMVPE CUDA vẫn hoạt động với model cục bộ khác.

Xác minh sau khi dọn: `py_compile` cho 45 file Python source/test, `node --check web/static/app.js`, `pip check` cho `.venv` và RVC runtime đều đạt. Toàn bộ **144 tests** đạt trong 13.880 giây.

## Cao độ Hoài My theo từng project (2026-09-17)

Web có thanh **Cao độ giọng Edge** từ -20 đến +20 Hz, mặc định `+0Hz`; giá trị được chuẩn hóa thành dạng Edge `+NHz`/`-NHz`, lưu riêng cho project và dùng cho cả nút nghe thử lẫn TTS cuối. Với Hoài My, +4 đến +12 Hz là mức bắt đầu được gợi ý. Đây là chỉnh F0, không phải một người nói mới.

Migration chỉ xóa một lần các `tts_pitch` ẩn của profile cũ. Một marker nội bộ bảo vệ lựa chọn rõ ràng mới qua lần khởi động app/worker tiếp theo. Pitch đã nằm trong manifest signature của Edge TTS, nên đổi mức sẽ không dùng lại WAV cũ; TTS tạo WAV khớp rồi mix đo duration/overlap lại.

Xác minh: `node --check web/static/app.js`, `py_compile` toàn bộ source/test và full suite **145 tests** đều đạt. Regression bao gồm giới hạn API web ±20 Hz, preview nhận `+4Hz`, migration một lần và cache từ chối WAV có pitch khác.

## Logo, công bố video chậm an toàn và OCR chú thích trong khung hình (2026-09-17)

Bản local thêm ảnh logo theo project (PNG/JPG/WebP), với vị trí góc, kích thước, độ trong suốt và lề. FFmpeg nhận logo như input ảnh riêng, ghép sau bước lật gương nên logo không bị ngược. Render video chậm cũng kiểm tra tên output bất thường và thử `os.replace` lại khi Windows/Explorer đang giữ file; nếu vẫn không thể thay, bản video cũ được giữ và log hướng dẫn đóng chương trình đang mở file.

OCR chú thích giữa khung hình là track hiển thị riêng: người dùng khoanh một hình chữ nhật trực tiếp trên video nguồn bằng con trỏ, app crop chỉ vùng đó ở các frame mẫu, gom OCR thành `*_annotation_zh.srt`, rồi dịch thành `*_annotation_vi.srt`. Track này không vào TTS, WAV hay timeline audio. Khi xuất, SRT chú thích được chuyển thành ASS UTF-8 tạm với `pos` và `clip` trong đúng vùng đã chọn; Vietnamese xuất hiện theo timestamp cue. Nếu video lật gương, tọa độ ngang được đổi theo source để bản dịch vẫn nằm trên dòng chữ gốc. Đổi vùng hoặc đổi timeline sao lưu SRT chú thích và render cũ để không dùng nhầm vị trí/thời gian.

Xác minh trên máy Windows này: `py_compile` thành công cho 47 file source/test, `node --check web/static/app.js`, `pip check`, và `check_environment.py` đều đạt; PaddleOCR thật nhận GPU `NVIDIA GeForce GTX 1650` với `gpu:0`, FFmpeg/FFprobe đều sẵn sàng. Toàn bộ **166 tests** đạt. Regression gồm ASS escape/timing/clip, crop custom không quay lại ROI phụ đề đáy, track annotation không lọt vào audio, route web/backup/config, graph logo/mirror, và render FFmpeg/libass thật xác nhận chữ annotation chỉ xuất hiện đúng cue trong vùng đã chọn. App local đã được khởi động lại trên `http://127.0.0.1:7860` và smoke HTTP xác nhận giao diện/JS mới đang được phục vụ.

Bổ sung cuối cùng: sau OCR, giao diện cũng cho tải `*_annotation_zh.srt` để dịch tay và nhập lại `*_annotation_vi.srt`. Endpoint chỉ cho phép thao tác khi vùng đã khoanh và SRT Trung riêng đã tồn tại, xác thực SRT UTF-8, sao lưu bản Việt annotation cũ, và không ghi vào track thoại. Toàn bộ suite chạy lại sau thay đổi này: **166 tests** đạt.

## Khắc phục lỗi nạp project có OCR chú thích (2026-09-17)

Đã thêm bốn ô tọa độ ẩn (`annotation_left`, `annotation_top`, `annotation_right`, `annotation_bottom`) cho vùng OCR tùy chỉnh. Chúng được dùng bởi cùng cơ chế lưu/nạp cài đặt project với các điều khiển khác; trước đó thiếu các phần tử này nên trình duyệt dừng khi mở project với lỗi `Cannot read properties of null (reading 'value')`.

Regression nay kiểm tra cả mọi cài đặt trong `optionNames` lẫn mọi lệnh DOM dạng `$('id')` của `app.js` đều có phần tử HTML tương ứng. Toàn bộ **166 tests**, kiểm tra cú pháp Python/JavaScript và một lượt mở thật project `Tap1Den3` trong trình duyệt cục bộ đều đạt; console không có lỗi JavaScript. App đã khởi động lại tại `http://127.0.0.1:7860`.

## Chép SRT Trung của OCR khoanh vùng (2026-09-17)

Thẻ OCR chú thích trong khung hình có nút **Chép SRT Trung**. Sau khi `*_annotation_zh.srt` đã có, app tải trước file UTF-8 riêng theo project rồi mới bật nút; thao tác chép giữ nguyên ID, timestamp và chữ Trung. Cache này tách khỏi SRT thoại chính, nên không thể chép nhầm `*_zh.srt` của phần phụ đề đáy. Nếu server Waitress đang giữ HTML cũ trong khi một tác vụ dài chạy, JavaScript tạo nút tương thích khi trang được tải lại thay vì buộc phải dừng tác vụ để restart server.

Xác minh: route giới hạn `/files/annotation_zh` trả đúng nội dung SRT và từ chối loại file tùy ý; `tests.test_web` (**23 tests**), `node --check web/static/app.js` và toàn bộ **166 tests** đạt. Một lượt mở trang cục bộ với template đang chạy đã xác nhận nút được tạo và console không có lỗi JavaScript.

## Báo thiếu phụ đề Việt của OCR khoanh vùng (2026-09-17)

Track chú thích giữa hình nay đối chiếu SRT Trung và Việt theo cả **ID** lẫn timestamp ở độ chính xác mili-giây. Giao diện chỉ báo đã dịch hoàn toàn khi tất cả cue khớp; nếu bản Việt chỉ có một phần, thẻ chuyển màu vàng và hiện rõ dạng `Đã dịch 17/36 câu` hoặc `Thiếu 19/36 câu`. Phần thiếu, ID thừa hoặc mốc thời gian lệch được báo trước khi xuất: cue thiếu bản Việt không hiện, còn cue Việt tự thêm/lệch mốc vẫn burn theo timestamp của chính nó để người sửa tay chủ động quyết định. Nhập SRT Việt vẫn là thao tác do người dùng quyết định, vẫn sao lưu bản cũ và không tự ghi đè/dịch bù; response chỉ trả cảnh báo coverage.

Xác minh: thêm regression cho track thiếu cue, ID thừa, timestamp lệch, track khớp hoàn toàn và warning khi nhập SRT một phần. `py_compile`, `node --check web/static/app.js` và toàn bộ **168 tests** đạt.

## Đưa chú thích Việt lên cao (2026-09-17)

Thẻ OCR chú thích có thêm thanh **Độ cao chữ Việt**, mặc định `12% từ trên`. Vùng khoanh vẫn chỉ phục vụ PaddleOCR và giữ bề rộng/vị trí ngang của chú thích; chỉnh thanh này không OCR lại, không sửa SRT và không thay timestamp. Khi burn, ASS dùng `\\an8` để cạnh trên của bản dịch bắt đầu tại độ cao đã chọn và để câu nhiều dòng phát triển xuống dưới. Vì vậy chữ Việt có thể nằm cao, rõ ràng thay vì bị giữ ở giữa vùng OCR lớn. `ANNOTATION_DISPLAY_Y` nhận `0.05–0.80` cho CLI/.env.

Regression kiểm tra vị trí ASS cao, giữ nguyên crop ngang, giới hạn input, truyền cấu hình qua bước render có mirror và validation web/config.

## Nhập trực tiếp SRT chú thích Việt (2026-09-18)

Track chú thích trong khung hình có thể nhận thẳng SRT Việt do GPT hoặc người dùng tạo. Chỉ cần khoanh vùng để đặt chiều ngang/vị trí chữ, sau đó **Nhập SRT Việt từ GPT**; route không đòi `*_annotation_zh.srt`, không OCR, không gọi Gemini và không đưa cue vào TTS. Khi không có track Trung, trạng thái API là `direct_vietnamese` để giao diện báo rõ không có dữ liệu cần đối chiếu.

Nút **Chỉ chèn chú thích** chạy render riêng từ video nguồn sang `final_video.mp4`, giữ audio gốc và không cần SRT thoại/WAV. CLI tương đương là `burn_subtitle.py input/video.mp4 --annotation-srt notes_vi.srt --output result.mp4`. Regression bảo vệ import trực tiếp, route render không yêu cầu OCR/TTS/SRT thoại, cú pháp giao diện và dispatch CLI chỉ-annotation.

Xác minh sau thay đổi: `py_compile`, `node --check web/static/app.js`, `pip check` và toàn bộ **171 tests** đều đạt, gồm render FFmpeg/libass thật cho track annotation không có SRT thoại.

## OCR tăng cường cho chú thích dọc/cách điệu (2026-09-18)

Thẻ **Chú thích tùy chỉnh** có thêm **OCR tăng cường** cho trường hợp chữ Trung vẫn hiện trên frame nhưng PP-OCRv5 Mobile không tạo cue. Bước này quét lại đúng vùng đã khoanh bằng PP-OCRv5 Server trên `OCR_DEVICE` (GPU khi cấu hình `gpu:0`), nên chậm hơn OCR thường và lần đầu tải model lớn hơn. Nên khoanh sát chữ để giảm nhiễu và thời gian quét.

Kiểm tra trực tiếp frame `00:59` của video mẫu: mobile bỏ sót/đọc sai chữ dọc cách điệu, còn PP-OCRv5 Server nhận `苟圣` với độ tin cậy khoảng `0.979`. Khi chạy lại, `*_annotation_zh.srt` cũ được sao lưu trước khi thay; `*_annotation_vi.srt` được giữ nguyên, vì vậy phần tiếng Việt sửa tay không bị mất.

Sau khi tích hợp, kiểm tra lại cùng frame bằng chính cấu hình production nhận `苟圣` ở `0.982718` trên `gpu:0`. `py_compile`, `node --check web/static/app.js`, `pip check` và toàn bộ **172 tests** đều đạt.

## Thời lượng video tua chậm dài (2026-09-19)

Khi tính thời lượng đích cho video tua chậm, app nay ưu tiên `duration` của **container MP4** trước `duration` của video stream. Nếu container không có giá trị hợp lệ, code mới dùng duration của video stream làm fallback. Điều này tránh báo sai với MP4 có stream metadata không phản ánh toàn bộ timeline.

Regression dùng nguồn dài `10763.514898s`: tại tốc độ `0.80x`, thời lượng đích đúng là `10763.514898 / 0.8 = 13454.393622s`. Coverage kiểm tra cả ưu tiên container duration, fallback khi container thiếu duration và phép so khớp duration sau khi FFmpeg xuất file.

