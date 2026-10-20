# Video Translator: phụ đề Trung → phụ đề và giọng Việt

Project Python chạy trên Windows x64: nhận video MP4 có phụ đề Trung dính ở dưới hình, dùng PaddleOCR trích SRT Trung, dịch theo batch bằng Gemini, đọc bản SRT Việt hiện tại bằng Edge TTS rồi ghép giọng vào đúng timestamp bằng FFmpeg. Có thể giữ SRT rời hoặc burn SRT Việt vào video; khi xuất có thể làm mờ dải phụ đề Trung, lật ngang video kiểu gương, chèn logo, và OCR/dịch riêng một chú thích Trung nằm giữa khung hình.

Project có giao diện **VietDub Studio chạy local trên trình duyệt** và các lệnh CLI. OCR dùng **NVIDIA GPU mặc định** (`OCR_DEVICE=gpu:0`); có cấu hình CPU. Giọng Việt có thể tăng tốc riêng khi ghép bằng FFmpeg. Tính năng **đổi màu giọng bằng model RVC** là tùy chọn, dùng RVC + RMVPE trong CUDA runtime riêng để nhận WAV/MP3/MP4 và xuất WAV hoặc MP4 mà không tăng pitch thô.

## Mở giao diện web trên Windows

Sau khi cài dependencies, nhấp đúp **start_web.bat**, hoặc chạy:

```powershell
.\.venv\Scripts\python.exe web_app.py
```

Mở **http://127.0.0.1:7860**. Giữ terminal chạy app; Ctrl+C để tắt. Có thể dùng `--port 7861` khi cổng đang bận hoặc `--no-browser` để không tự mở trình duyệt. Web server dùng [Flask + Waitress](https://flask.palletsprojects.com/en/stable/deploying/waitress/), chỉ lắng nghe loopback, không triển khai công khai ra Internet.

### Quy trình trên web

1. **Thêm video**: nhập đường dẫn MP4 có sẵn trên máy (không sao chép), hoặc chọn file để tải vào app (tối đa 8 GB). Mỗi video mới dùng thư mục dữ liệu riêng trong `projects/<id>/`, không dùng nhầm WAV giữa video.
2. Nếu đang làm bằng CLI, chọn **Kết nối dữ liệu CLI đang có** và chọn đúng video: dùng `output/`, `audio/`, `temp/` ở thư mục project. Chỉ kết nối một bộ CLI vì các video CLI dùng chung audio. Trên máy hiện tại đã kết nối video `aaa`.
3. Nếu muốn video chậm hơn trước khi làm phụ đề, dùng **Bước 0 · Tua chậm video Trung**. Chọn tốc độ, bấm tạo và xem lại bản chậm. Cả hình lẫn tiếng Trung được kéo chậm trên cùng timeline; video được encode bằng **NVIDIA NVENC GPU** mặc định. Khi hài lòng, bấm **Đã xem — dùng video này cho các bước tiếp**; từ đó OCR, timestamp SRT, giọng Việt và video cuối đều dựa trên bản chậm. Video gốc không bị sửa. Khi đổi giữa video gốc và video chậm, app sao lưu SRT, cảnh báo overlap và video cũ để không dùng nhầm timeline; WAV hợp lệ được giữ làm cache vì không chứa timestamp.
4. **OCR** trích SRT Trung bằng GPU hoặc CPU, điều chỉnh vùng cắt và sample FPS ở phần mở rộng. Mặc định dùng lại SRT Trung đã có. Tick **OCR lại từ video** để nhận dạng lại sau khi đổi ROI: sao lưu Trung cũ, giữ bản Việt.
5. **Biên tập phụ đề**: tiếng Trung tham khảo bên trái, nhập tiếng Việt bên phải; bấm timestamp để xem video tại câu đó. Sau OCR, nút **Chép SRT Trung** chép nguyên file SRT Trung (ID, timestamp và chữ) vào bộ nhớ tạm. Tìm kiếm, phân trang, nhập file SRT UTF-8 và tải bản Việt. **Lưu phụ đề** tạo backup trước khi thay file Việt. Mục trống được bỏ. Nếu SRT đổi ở nơi khác, app báo xung đột để tải lại, không ghi đè bản mới.
6. Có thể tự dịch hoàn toàn. Nếu dùng **Dịch Gemini**, nhập key bằng ô ẩn hoặc dùng key trong `.env`; key ở ô nhập chỉ truyền đến worker qua stdin, không lưu vào registry hoặc dòng lệnh. Dịch Gemini không ghi đè SRT Việt đã có.
7. Chọn giọng, volume gốc/Việt và **tốc độ giọng Việt 1.0–1.5×**. Web chỉ hiển thị người nói Edge tiếng Việt có thật: Hoài My (nữ) và Nam Minh (nam). Có nút **Nghe thử giọng** để chọn người nói trước khi tạo WAV. Phần **Cao độ giọng Edge** cho phép nâng hoặc hạ tông từ -20 đến +20 Hz; với Hoài My, nên thử +4 đến +12 Hz trước. Đây chỉ là cao độ, không tạo người nói mới. Mức 1.10× nhanh hơn khoảng 10%. Mỗi WAV được tăng tốc riêng rồi đặt đúng start_time; nhạc/tiếng gốc và video giữ nguyên. Không phải gọi TTS lại khi chỉ thay tốc độ ghép. Tăng tốc có thể giảm chồng tiếng nhưng không đảm bảo hết overlap ở mọi câu.
8. Nếu muốn thay màu giọng của một audio/video riêng, mở thẻ **Đổi màu giọng bằng model RVC**. Sau khi cài RVC, đặt model `.pth` mà bạn có quyền sử dụng vào `models\rvc`; nếu có file `.index` cùng tên, đặt cạnh model để app tự nhận. App chỉ liệt kê model cục bộ do bạn thêm. Chọn audio của project hoặc tải WAV/MP3/MP4, rồi chọn preset xử lý **Sáng, trẻ**, **Sáng rõ** hoặc **Mềm**. Model quyết định màu giọng; RMVPE lấy F0/ngữ điệu từ nguồn, còn preset chỉ tinh chỉnh nhẹ trong RVC. Kết quả được giữ riêng khỏi cache Edge TTS và có nút nghe/tải WAV; với nguồn MP4, app tạo thêm MP4 giữ nguyên hình và thay audio.
9. Nếu cần che chữ Trung dính, tick **Làm mờ phụ đề Trung gốc**. App làm mờ một dải hình chữ nhật có thể chỉnh ở phần mở rộng, rồi chèn SRT Việt lên trên. Đây là làm mờ cả vùng hình phía sau chữ, không phải xóa riêng từng ký tự Trung. Tick **Lật ngang video kiểu gương** để lật toàn bộ hình; SRT Việt luôn được chèn cuối cùng nên vẫn đọc bình thường. Thẻ **Chèn logo** nhận PNG/JPG/WebP, chọn góc, kích thước, độ trong suốt và lề; ảnh logo chỉ được ghép vào video cuối.
10. Với tên nhân vật hoặc chú thích nằm giữa hình, dùng thẻ **Chú thích tùy chỉnh · Chú thích nằm trong khung hình**. Mở đúng video nguồn mà app đang dùng, bấm **Khoanh vùng trên video** rồi kéo chuột bao vị trí chữ. Bạn có thể bấm **OCR Trung** và **Dịch Gemini**, hoặc bỏ qua hoàn toàn hai bước này và chọn **Nhập SRT Việt từ GPT** để dùng SRT chú thích tiếng Việt do GPT/người dùng tạo. Nếu chữ dọc/cách điệu hiện rõ trên frame nhưng OCR thường bỏ sót, dùng **OCR tăng cường**: nó dùng PP-OCRv5 Server trên `OCR_DEVICE` (GPU khi đặt `gpu:0`), chậm hơn OCR thường và lần đầu cần tải model lớn hơn. Bước này sao lưu SRT Trung cũ trước khi thay, còn `*_annotation_vi.srt` được giữ nguyên. App tạo `*_annotation_zh.srt` khi OCR được dùng, còn `*_annotation_vi.srt` là track được chèn vào video. Với SRT Việt trực tiếp, bấm **Chỉ chèn chú thích** để xuất MP4 giữ audio gốc, không cần SRT thoại, Edge TTS hay Gemini. Dùng thanh **Độ cao chữ Việt** để đưa chữ lên cao; mặc định là **12% từ trên**. Thanh này chỉ đổi chỗ chèn chữ Việt, không đổi vùng khoanh hoặc timestamp. Khi dùng OCR, thẻ hiển thị rõ số câu khớp, ví dụ **Đã dịch 17/36 câu**; câu OCR thiếu bản Việt hoặc lệch timestamp sẽ được báo màu vàng. Khi sửa tay, nhập lại **toàn bộ** SRT chú thích Việt để thay track cũ; bản trước luôn được sao lưu. Track này không đi vào SRT thoại, TTS, WAV hoặc audio timeline. Khi đổi vùng, SRT chú thích cũ và video cuối cũ được sao lưu để không chèn nhầm vị trí. Tăng `Frame / giây` nếu chú thích chớp rất nhanh.
11. Bấm **Tạo giọng & xuất video**. App gộp câu lặp thành SRT `_vi_tts.srt` riêng, dùng lại WAV hợp lệ, ghép audio và tùy chọn chèn bản `_vi.srt` lên hình. Có các nút chạy từng bước: gộp TTS, chỉ tạo giọng, chỉ ghép video. Nếu chỉ đổi tốc độ/volume và đã có WAV/SRT TTS khớp, dùng **Chỉ ghép video**.
12. Xem tiến trình/log, bấm **Dừng** khi cần. App dừng cây tiến trình worker/FFmpeg, giữ checkpoint đã hoàn thành. Chạy lại để tiếp tục. Chỉ chạy một tác vụ web tại một thời điểm; tránh chạy CLI đồng thời trên cùng bộ dữ liệu. Không chỉnh hoặc dọn project khi tác vụ đang chạy.
13. Chuyển **Kết quả** để xem MP4. Bấm **Lưu video thành…** để mở hộp Save As của Windows, chọn thư mục và đặt tên file `.mp4` theo ý bạn; app sao chép bản render hoàn chỉnh tới nơi đó. Bản nội bộ vẫn là `final_video.mp4` trong project để xem lại, xuất lại và dọn dẹp an toàn. Liên kết **Tải video** vẫn dùng cơ chế download của trình duyệt.

### Dọn dẹp dữ liệu

Chọn video ở thanh bên → **Dọn dẹp dữ liệu** → chọn nhóm → **Kiểm tra dữ liệu có thể dọn**. App hiển thị dung lượng và từng file. Chỉ khi nhấn nút xóa và xác nhận mới xóa đúng danh sách đã xem trước. Bản xem trước có hiệu lực 5 phút; file đã đổi sẽ không được xóa theo bản xem trước cũ.

Các nhóm: file tạm FFmpeg/timeline, WAV và manifest, video kết quả, bản sao lưu `.bak`, cache dịch Gemini. Xóa WAV cần tạo giọng lại; xóa cache dịch có thể tốn quota khi dịch lại. **Không xóa video đầu vào của bất kỳ project đã đăng ký, SRT đang sửa, source code, `.env`, `.venv`, FFmpeg hoặc model OCR.** Không đi qua symlink/junction và không xóa đệ quy thư mục. Nếu Windows giữ file, app liệt kê các file chưa xóa được. Tính năng dọn không tự chạy nền.

Registry nằm trong `web_data/projects.json`; log của tác vụ web giữ trong bộ nhớ tới khi tắt app. API key không nằm trong registry. Các file kết quả vẫn còn khi khởi động lại.

Các file web mới: `web_app.py`, `web_store.py`, `web_jobs.py`, `web_worker.py`, `web/templates/index.html`, `web/static/app.js`, `web/static/style.css`, `start_web.bat`.

## 1. Cài trên Windows

### Python và thư viện

Dùng Python **3.11+ bản 64-bit**; nên bắt đầu với **3.11 hoặc 3.12** để thuận tiện cài các wheel Paddle trên Windows. Source dùng cú pháp Python 3.11+; việc có wheel cho các Python mới hơn phụ thuộc thư viện. Tải Python từ [python.org](https://www.python.org/downloads/windows/), chọn thêm Python vào PATH khi cài.

Mở PowerShell tại thư mục project. Ví dụ nếu project nằm ở `D:\EditVideo\video-translator`:

```powershell
Set-Location 'D:\EditVideo\video-translator'
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
```

Nếu cài Python 3.11, thay `py -3.12` bằng `py -3.11`. Nếu máy không có Python Launcher `py`, dùng `python -m venv .venv` sau khi kiểm tra `python --version`.

Có thể kích hoạt môi trường để dùng lệnh `python` ngắn gọn trong các ví dụ bên dưới:

```powershell
.\.venv\Scripts\Activate.ps1
python --version
```

Nếu PowerShell chặn `Activate.ps1`, không cần đổi execution policy: thay mỗi `python` bên dưới bằng `.\.venv\Scripts\python.exe`. Không cần quyền Administrator để chạy project trong thư mục có quyền ghi.

Các dependency chính:

| Thư viện | Vai trò |
| --- | --- |
| `paddleocr==3.7.0` | API OCR 3.x |
| `paddlepaddle-gpu==3.3.1` | Backend mặc định CUDA 12.6, thay cho gói Paddle CPU |
| `opencv-contrib-python==4.10.0.84` | Đọc frame/timestamp, cắt vùng phụ đề |
| `numpy==2.3.5` | Đặt/mix mẫu audio trên timeline nằm trên đĩa |
| `google-genai==1.75.0` | Gemini API và structured JSON output |
| `edge-tts==7.2.8` | Tạo giọng từ dịch vụ TTS trực tuyến của Edge |
| `python-dotenv==1.2.3` | Đọc cấu hình UTF-8 trong `.env` |

Dùng đúng `requirements.txt`; không cài thêm `opencv-python` hoặc `opencv-python-headless` vào cùng môi trường vì các gói này cùng cung cấp module `cv2`. FFmpeg là chương trình riêng, không được cài bởi pip. PaddleOCR tải model thật trong lần khởi tạo đầu; bước này cần mạng và có thể mất vài phút. API OCR tham khảo [PaddleOCR Quick Start](https://www.paddleocr.ai/latest/en/quick_start.html).

### GPU NVIDIA và chuyển từ bản CPU cũ

`requirements.txt` cài PaddlePaddle GPU 3.3.1 từ [kho CUDA 12.6 chính thức của Paddle](https://www.paddlepaddle.org.cn/packages/stable/cu126/paddlepaddle-gpu/), cùng CUDA/cuDNN dạng package trong `.venv`. Cần driver NVIDIA hoạt động (`nvidia-smi`); lần cài đầu tải các thư viện GPU khá lớn. Workspace này dùng GTX 1650 4 GB. Máy không có NVIDIA GPU nên dùng lựa chọn CPU bên dưới.

Nếu đang có bản CPU cũ trong môi trường, gỡ nó trước khi cài GPU vì cả hai gói cung cấp cùng module `paddle`:

```powershell
python -m pip uninstall -y paddlepaddle
python -m pip install -r requirements.txt
python -m pip check
python check_environment.py
```

Trong `.env`, đặt `OCR_DEVICE=gpu:0`. Khi bắt đầu OCR, log ghi `OCR dùng gpu:0` và tên GPU. Nếu bản Paddle không hỗ trợ CUDA hoặc không tìm thấy GPU đã chọn, chương trình báo lỗi; không tự âm thầm chuyển sang CPU. Model vẫn là hai model PP-OCRv5 mobile như bản CPU.

Đã kiểm tra thực tế trên GTX 1650: cùng clip mẫu, 14 frame OCR mất khoảng 4,84 giây trên CPU và 0,35 giây trên GPU sau khi model đã khởi tạo. Kết quả SRT giống nhau. Đây là số đo clip ngắn, không phải cam kết tốc độ cho mọi video; xem `VALIDATION.md`.

Wheel GPU cu126 hiện có cảnh báo compiled cuDNN 9.9/runtime 9.5 dù dùng đúng dependency do nó khai báo; [issue Paddle #79388](https://github.com/PaddlePaddle/Paddle/issues/79388) mô tả tình huống này. OCR của project đã chạy được với bộ cài đó. Nếu gặp lỗi cuDNN khi xử lý video khác, giữ log để chẩn đoán hoặc chọn chế độ CPU ở dưới; project không tự giấu cảnh báo.

### Cài RVC + RMVPE GPU cho tính năng đổi giọng

RVC chạy trong môi trường riêng ở `.tools\rvc-runtime`, không cài PyTorch/fairseq vào `.venv` đang dùng PaddleOCR. Trên máy NVIDIA trước RTX 50 như GTX 1650, script dùng wheel PyTorch CUDA 11.8 theo hướng dẫn RVC hiện tại. Chạy một lần khi có Internet:

```powershell
Set-Location 'D:\EditVideo\video-translator'
powershell -ExecutionPolicy Bypass -File .\tools\setup_rvc_gpu.ps1
```

Script tải mã nguồn RVC đã pin phiên bản, PyTorch CUDA, Hubert Base và `rmvpe.pt`, sau đó bắt buộc kiểm tra `torch.cuda.is_available()` trước khi báo thành công. Các file này chỉ cần tải lần đầu; inference sau đó chạy offline.

#### Dùng model RVC của bạn

RVC runtime không tự chứa hoặc tải sẵn model giọng `.pth`. Đặt model RVC mà bạn có quyền sử dụng vào `models\rvc`, ví dụ:

```text
models\rvc\cute_female.pth
models\rvc\cute_female.index
```

File `.index` là tùy chọn nhưng nên cùng tên với `.pth` để app tự nhận. VietDub Studio chỉ liệt kê model cục bộ trong thư mục này và không nhận đường dẫn model từ trình duyệt. Chỉ thêm model có nguồn gốc và giấy phép bạn đã kiểm tra. Xem `models\rvc\README.txt` để biết cấu trúc thư mục.

Không bật tinh chỉnh F0 hậu kỳ nếu chưa cần. Preset mặc định dùng RMVPE/RVC để giữ formant và ngữ điệu; mục nâng cao chỉ dùng filter Rubber Band khi FFmpeg hỗ trợ nó, và giữ formant để tránh giọng chipmunk.

Cùng engine đó có CLI khi cần xử lý một file ngoài giao diện:

```powershell
python convert_voice.py 'C:\Video\loi_noi.mp4' --model .\models\rvc\cute_female.pth --index .\models\rvc\cute_female.index --preset cute --output .\output\loi_noi_cute.mp4
```

Với input WAV/MP3, đặt `--output` là `.wav`. Với input MP4, output `.mp4` tạo thêm WAV cùng tên để nghe/kiểm tra. Mặc định CLI không ghi đè output đã có; thêm `--force` chỉ khi muốn thay sau khi job hoàn tất.

Muốn dùng CPU thay thế, không cài chồng hai gói:

```powershell
python -m pip uninstall -y paddlepaddle-gpu
python -m pip install -r requirements-cpu.txt
```

Sau đó đặt `OCR_DEVICE=cpu` trong `.env`. Thư viện dùng chung nằm trong `requirements-common.txt`.

Backend CPU đặt `enable_mkldnn=False` để tránh lỗi oneDNN/PIR `ArrayAttribute` gặp trên bộ Paddle 3.3.1 Windows khi kiểm tra. Nó vẫn chạy suy luận OCR bằng Paddle CPU. Thông báo `No ccache found` không phải lỗi bắt buộc phải cài thêm công cụ.

`requirements-lock-windows-py312.txt` là bản đóng băng cả dependency gián tiếp của môi trường GPU Windows/Python 3.12. `requirements-lock-cpu-windows-py312.txt` lưu môi trường CPU ban đầu. Có thể cài file lock tương ứng thay cho requirements chính khi muốn tái tạo môi trường; với Python khác, dùng `requirements.txt` (GPU) hoặc `requirements-cpu.txt` để pip chọn dependency gián tiếp phù hợp.

### FFmpeg và FFprobe

1. Vào [trang tải FFmpeg](https://ffmpeg.org/download.html), chọn bản Windows được dẫn tới [Gyan builds](https://www.gyan.dev/ffmpeg/builds/).
2. Giải nén bản có `ffmpeg.exe`, `ffprobe.exe`; nếu burn SRT, bản đó cần filter `subtitles`/libass và encoder `libx264`.
3. Thêm thư mục `bin` của bản vừa giải nén vào PATH, mở lại PowerShell và kiểm tra:

```powershell
ffmpeg -version
ffprobe -version
ffmpeg -hide_banner -filters | Select-String 'subtitles'
```

Hoặc không sửa PATH, đặt đường dẫn đầy đủ trong `.env` như phần sau. Nếu thiếu FFmpeg/FFprobe, script báo rõ tên chương trình thiếu và cách cấu hình.

### File `.env` và Gemini API key

Chỉ sao chép khi chưa có `.env`, để giữ cấu hình đã sửa:

```powershell
if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
```

Mở `.env` bằng trình soạn thảo, nhập key theo [hướng dẫn Gemini API key](https://ai.google.dev/gemini-api/docs/api-key):

```dotenv
GEMINI_API_KEY=nhap_api_key_cua_ban
GEMINI_MODEL=gemini-3.6-flash
```

Bạn cũng có thể để `GEMINI_API_KEY` trống và nhập trực tiếp trong CLI. Khi cần gọi Gemini lần đầu, chương trình sẽ hiện:

```text
Nhập Gemini API key (ẩn ký tự, chỉ dùng cho lần chạy này):
```

Nhập/dán key rồi nhấn Enter; màn hình không hiện ký tự. Key chỉ dùng trong bộ nhớ của lần chạy đó, không tự ghi vào `.env`, log hoặc cache. Nếu đã có key trong `.env` nhưng muốn nhập key khác, dùng:

```powershell
python main.py input/video.mp4 --translate --ask-api-key
python translate_subtitles.py output/video_zh.srt --ask-api-key
```

Không hỏi key khi chỉ OCR, khi giữ SRT Việt hiện có, hoặc khi toàn bộ bản dịch đã có trong cache. Với lệnh không chạy trong terminal tương tác (ví dụ job tự động hoặc stdin được chuyển hướng), đặt key trong `.env`/biến môi trường; chương trình sẽ báo lỗi thay vì chờ nhập. Prompt xuất hiện ở bước dịch sau OCR, chỉ khi thực sự cần gọi API.

Model mặc định có thể đổi qua `GEMINI_MODEL` tùy model được cấp cho tài khoản. Gemini có hạn mức và có thể tính phí theo tài khoản/model; Edge TTS dùng dịch vụ trực tuyến không yêu cầu API key riêng theo [tài liệu edge-tts](https://github.com/rany2/edge-tts). Cả hai cần truy cập mạng. Video được OCR tại máy; bước dịch gửi văn bản/context/glossary sang Gemini, bước TTS gửi lời Việt sang dịch vụ Edge.

Nếu không dùng PATH, thêm đường dẫn FFmpeg thực tế, bọc bằng **nháy đơn** để giữ dấu `\` trong Windows path:

```dotenv
FFMPEG_BIN='C:\Tools\ffmpeg\bin\ffmpeg.exe'
FFPROBE_BIN='C:\Tools\ffmpeg\bin\ffprobe.exe'
```

Trong workspace dùng để kiểm tra project, bản FFmpeg được giải nén tại `.tools/ffmpeg/ffmpeg-9.0.1-essentials_build/bin`. Nếu thư mục này còn trên máy của bạn, có thể dùng đường dẫn tuyệt đối tới hai `.exe` trong đó. `.tools` và `.venv` là dữ liệu cục bộ, không phải điều kiện bắt buộc của source khi chuyển sang máy khác.

Workspace hiện tại đã có `.env` với đường dẫn FFmpeg cục bộ và key để trống: chỉ cần nhập Gemini key rồi thêm video. Bản ZIP source không kèm `.env`, `.venv`, `.tools` hoặc model cache; trên máy khác vẫn thực hiện các bước cài ở trên.

Không commit `.env`; `.env.example` không chứa key thật. Biến môi trường của process có ưu tiên cao hơn giá trị trong `.env`.

## 2. Chạy nhanh

Đặt video vào `input/video.mp4`, chạy từ thư mục project sau khi kích hoạt `.venv`:

```powershell
python main.py input/video.mp4
```

Lệnh mặc định chạy đủ OCR → dịch → TTS → ghép audio, không burn chữ Việt. Kết quả:

```text
output/video_zh.srt
output/video_vi.srt
output/final_video.mp4
audio/001.wav
audio/002.wav
audio/manifest.json
audio/overlap_warnings.json
```

Tên SRT theo tên gốc video: `tap01.mp4` tạo `tap01_zh.srt` và `tap01_vi.srt`. Các WAV đặt tên theo **ID SRT** với tối thiểu ba chữ số; ID 1000 tạo `1000.wav`.

Chỉ OCR:

```powershell
python main.py input/video.mp4 --ocr-only
```

Tạo riêng một bản MP4 tua chậm để kiểm tra trước khi OCR; cả hình và audio gốc cùng chậm, còn file đầu vào không bị sửa:

```powershell
python slow_video.py input/video.mp4 --speed 0.8 --output output/video_slow.mp4
```

Trong VietDub Studio, hãy dùng **Bước 0** thay cho lệnh này để có nút duyệt và tự chọn đúng video chậm làm nguồn OCR. `SOURCE_SPEED=0.80` là tốc độ mặc định trong `.env`; giao diện cho phép chọn 0.50–0.95×. `SLOW_VIDEO_ENCODER=h264_nvenc` dùng GPU NVIDIA; chỉ đổi thành `libx264` nếu máy không có NVENC.

OCR nếu chưa có SRT Trung, dịch rồi dừng để sửa SRT Việt:

```powershell
python main.py input/video.mp4 --translate
```

Tạo WAV riêng từ SRT Việt:

```powershell
python tts_from_srt.py output/video_vi.srt
```

Ghép các WAV đã tạo vào video:

```powershell
python mix_audio.py input/video.mp4 output/video_vi.srt
```

Ghép và burn SRT Việt:

```powershell
python main.py input/video.mp4 --burn-subtitles
```

Các CLI xử lý video/phụ đề có `--help`, `--env duong_dan.env`, `--verbose`. Đường dẫn có dấu hoặc khoảng trắng được hỗ trợ; bọc đối số bằng dấu nháy, ví dụ `python main.py 'input/Phim 中文 tập 01.mp4'`.

## 3. Quy trình sửa phụ đề thủ công

### Hai SRT riêng cho hiển thị và tạo giọng

`main.py` tự tạo `output/video_vi_tts.srt` từ `output/video_vi.srt` trước khi lồng tiếng. Bản `_vi.srt` bạn sửa luôn là nguồn chèn chữ và giữ nguyên nội dung/timestamp. Bản `_vi_tts.srt` chỉ dùng cho TTS và timeline audio: gộp câu giống nhau đứng liền nhau nếu chồng thời gian hoặc cách nhau tối đa 0,3 giây. So sánh chuẩn hóa khoảng trắng, chữ hoa/thường và dấu kết câu, giữ dấu tiếng Việt, không fuzzy. Câu khác hoặc lặp lại sau khoảng nghỉ dài được giữ. Nhân vật thực sự nhắc lại cùng một câu ngay sát nhau vẫn có thể bị gộp; cần kiểm tra khi có đoạn thoại như vậy.

Nhóm gộp giữ ID, lời và start của câu đầu, end lớn nhất trong nhóm. WAV của ID đầu có thể dùng lại nếu vẫn khớp; WAV các ID đã gộp không đưa vào timeline. Bản `_vi_tts.srt` được tạo lại từ nguồn khi chạy main; bản tạo giọng cũ khác nội dung được sao lưu `.bak`. Hãy sửa bản `_vi.srt` để thay lời.

Lệnh đầy đủ vẫn là `python main.py input/video.mp4 --dub-only --burn-subtitles`.

Nếu chạy từng bước riêng:

```powershell
python prepare_tts_srt.py output/video_vi.srt
python tts_from_srt.py output/video_vi_tts.srt
python mix_audio.py input/video.mp4 output/video_vi_tts.srt --burn-subtitles --display-srt output/video_vi.srt
```

`prepare_tts_srt.py --max-gap 0.1` cho phép giảm khoảng nghỉ được gộp ở quy trình từng bước; main dùng 0,3 giây.

### Tự dịch hoàn toàn, không dùng Gemini/API key

1. Chạy `python main.py input/video.mp4 --manual-translate`. Chương trình dùng SRT Trung có sẵn hoặc OCR nếu chưa có, tạo `output/video_vi.draft.srt` rồi dừng. Bản nháp ban đầu chứa lời Trung và giữ ID/timestamp nguồn.
2. Mở bản nháp, thay từng lời Trung bằng lời Việt; giữ nguyên ID và dòng thời gian. Lưu UTF-8 thành `output/video_vi.srt` (bỏ `.draft` khỏi tên file). Nếu đã có bản Việt, chương trình giữ nguyên file đó để bạn sửa tiếp. Bản nháp đang sửa cũng không bị ghi đè.
3. Chạy `python main.py input/video.mp4 --dub-only` để tạo giọng và ghép video từ bản Việt. Thêm `--burn-subtitles` nếu muốn chèn chữ Việt. Chế độ này bỏ qua OCR/Gemini và báo lỗi nếu chưa có SRT Việt.

Hai chế độ này không yêu cầu Gemini key. Edge TTS vẫn cần Internet để tạo giọng. Có thể dùng riêng `tts_from_srt.py` và `mix_audio.py` như bên dưới.

Khi xóa watermark, bạn có thể xóa hết nội dung một mục và để lại ID/timestamp. Các lệnh tự bỏ qua mục trống khi đọc SRT, kể cả dòng chỉ có khoảng trắng hoặc ký tự vô hình như `U+200B` (zero-width space); dòng trống còn sót giữa lời thoại cũng được bỏ qua. Log báo số mục trống đã lọc. File bạn đang sửa không bị tự ghi đè, ID/timestamp còn lại giữ nguyên. WAV cũ của mục đã xóa lời không được dùng khi ghép. Khoảng video không có phụ đề vẫn giữ nguyên độ dài và audio gốc theo volume đã cấu hình; chức năng này không cắt video. Timestamp sai hoặc ID trùng vẫn báo lỗi. Nếu toàn bộ SRT không còn lời, bước TTS dừng với thông báo SRT không có lời thoại.

### Dịch bằng Gemini rồi sửa lại

1. Chạy `python main.py input/video.mp4 --translate`.
2. Sửa `output/video_vi.srt` bằng trình soạn thảo hoặc phần mềm phụ đề, lưu **UTF-8**. Có thể sửa lời và timestamp; ID phải là số nguyên dương, không trùng, các mục xếp theo start time.
3. Chạy `python tts_from_srt.py output/video_vi.srt`.
4. Chạy `python mix_audio.py input/video.mp4 output/video_vi.srt`.

Pipeline **không tự ghi đè bất kỳ SRT Việt đang tồn tại**, kể cả khi source/glossary thay đổi. Nếu chạy lại `main.py`, nó dùng bản Việt hiện có; không cần Gemini key cho những batch không cần dịch. Nếu SRT Việt sai định dạng, chương trình báo lỗi để bạn sửa, không thay nó bằng bản mới.

TTS có manifest kiểm tra text, giọng, sample rate và hash WAV. Sửa text sẽ tạo lại đúng WAV bị ảnh hưởng; chỉ sửa timestamp thì dùng lại giọng và đặt lại vị trí theo SRT. Nếu sửa SRT rồi bỏ qua bước TTS, `mix_audio.py` từ chối audio cũ không khớp text/config.

Khi **chủ động muốn thay bản dịch**, dùng script dịch riêng với `--force`:

```powershell
python translate_subtitles.py output/video_zh.srt output/video_vi.srt --force
```

Chỉ sau khi dịch đủ các batch, script mới tạo `video_vi.srt.<ngày-giờ>-<mã>.bak` rồi thay bản Việt. Nếu API lỗi, bản Việt cũ và SRT Trung vẫn còn nguyên. `--force` cho phép thay output nhưng vẫn dùng cache hợp lệ; thay glossary/model/context làm cache thay đổi. Muốn buộc gọi lại API với cùng mọi cấu hình, đổi `TEMP_DIR` sang một thư mục cache mới.

Để OCR lại khi đã thay video hoặc vùng chữ:

```powershell
python main.py input/video.mp4 --ocr-only --reocr
```

`--reocr` thay SRT Trung theo yêu cầu nhưng không thay bản Việt. Sau đó kiểm tra lại lời/timing Việt trước khi ghép.

## 4. Cấu hình OCR và glossary

Các giá trị mặc định dưới đây nằm trong `.env.example`:

| Cấu hình | Mặc định | Ý nghĩa |
| --- | --- | --- |
| `SAMPLE_FPS` | `4` | Khoảng 4 frame/giây gửi vào OCR; không OCR mọi frame |
| `ROI_LEFT`, `ROI_RIGHT` | `0.05`, `0.95` | Giới hạn ngang, theo tỷ lệ chiều rộng |
| `ROI_TOP`, `ROI_BOTTOM` | `0.65`, `0.98` | Giới hạn dọc, theo tỷ lệ chiều cao |
| `OCR_CONFIDENCE` | `0.60` | Bỏ kết quả có độ tin cậy thấp hơn ngưỡng |
| `FUZZY_THRESHOLD` | `0.84` | Độ giống tối thiểu khi gom câu |
| `OCR_MAX_GAP` | `0.35` giây | Cho phép OCR mất chữ ngắn rồi nhận lại cùng câu |
| `OCR_MIN_DURATION` | `0.20` giây | Bỏ nhóm quá ngắn |
| `OCR_DEVICE` | `gpu:0` | GPU NVIDIA số 0; dùng `cpu` khi chọn môi trường CPU |
| `OCR_DET_MODEL`, `OCR_REC_MODEL` | `PP-OCRv5_mobile_det`, `PP-OCRv5_mobile_rec` | Hai model nhẹ mặc định |
| `BLUR_CHINESE_SUBTITLES` | `false` | Bật làm mờ dải phụ đề Trung khi xuất video |
| `BLUR_LEFT`, `BLUR_TOP`, `BLUR_RIGHT`, `BLUR_BOTTOM` | `0.05`, `0.80`, `0.95`, `0.96` | Dải cần làm mờ theo tỷ lệ video gốc; nên ôm vừa vùng chữ Trung |
| `BLUR_STRENGTH` | `14` | Độ mờ 1–50; tăng dần nếu còn thấy chữ Trung |
| `MIRROR_VIDEO` | `false` | Lật ngang hình video; SRT Việt được burn sau bước lật |
| `ANNOTATION_OCR_ENABLED` | `false` | Bật track chú thích trong khung hình; trên web tự bật khi bạn khoanh vùng |
| `ANNOTATION_LEFT`, `ANNOTATION_TOP`, `ANNOTATION_RIGHT`, `ANNOTATION_BOTTOM` | `0.20`, `0.20`, `0.80`, `0.50` | Vùng OCR khi dùng OCR, đồng thời là bề rộng/căn ngang khi chèn SRT Việt trực tiếp; `0 <= trái < phải <= 1` |
| `ANNOTATION_FONT_SIZE` | `28` | Cỡ chữ ASS 12–100 cho chú thích trong khung hình |
| `ANNOTATION_DISPLAY_Y` | `0.12` | Độ cao chữ Việt theo tỷ lệ từ trên xuống, 0.05–0.80; chỉ đổi chỗ chèn, không đổi vùng OCR/timestamp |
| `BURN_LOGO` | `false` | Bật ghép ảnh logo vào video cuối; web yêu cầu tải ảnh trước |
| `LOGO_POSITION` | `top-right` | `top-left`, `top-right`, `bottom-left`, hoặc `bottom-right` |
| `LOGO_WIDTH_PERCENT`, `LOGO_OPACITY`, `LOGO_MARGIN_PERCENT` | `12`, `0.85`, `3` | Độ rộng theo % video, độ trong suốt 0.05–1, và lề theo % |

ROI phải bao hết dòng phụ đề, tránh logo/tên kênh/chữ chạy phía dưới. Ví dụ phụ đề rất sát đáy: giảm `ROI_TOP` hoặc tăng `ROI_BOTTOM` tới `1.0` nếu cần. Có thể tăng sampling khi chữ đổi nhanh:

```powershell
python main.py input/video.mp4 --ocr-only --sample-fps 6 --reocr
```

OpenCV vẫn duyệt frame để xác định mốc lấy mẫu; **chỉ frame được chọn và đã crop ROI** mới đưa vào PaddleOCR. Khi gom câu, code bỏ qua khác biệt dấu câu/khoảng trắng, dùng khoảng cách ký tự và phiếu theo số lần nhận dạng/độ tin cậy. Câu 5–11 ký tự cho phép tối đa 1 sửa khác, câu từ 12 ký tự tối đa 2, đồng thời phải đạt fuzzy threshold. Câu ≤4 ký tự đòi khớp chuẩn hóa để tránh gộp nhầm hai lời đáp ngắn khác nghĩa.

### Chú thích ở giữa khung hình

Vùng phụ đề đáy (`ROI_*`) và vùng chú thích (`ANNOTATION_*`) là hai track khác nhau. Trên web, vùng chú thích được chọn bằng chuột trên **video nguồn đang được xử lý**: video gốc, hoặc video tua chậm đã duyệt. App tính cả phần letterbox khi map con trỏ vào ảnh thật, nên vùng chọn không bị lệch với video dọc hay vuông. Nếu đã bật lật gương, video nguồn vẫn là nơi chọn; lúc xuất app tự đổi tọa độ ngang để câu Việt nằm trên chính chữ Trung đã bị lật.

`output/<video>_annotation_zh.srt` và `output/<video>_annotation_vi.srt` có SRT chuẩn với ID/timestamp riêng. Bản Việt không bao giờ được đưa vào `prepare_tts_srt.py`, `tts_from_srt.py` hoặc timeline audio. Nó được chuyển thành ASS tạm khi FFmpeg burn video: chiều ngang vẫn theo vùng đã chọn, còn **Độ cao chữ Việt** đặt vị trí dọc độc lập để bản dịch có thể nằm cao trên màn hình. Mặc định `0.12` đặt cạnh trên của chữ tại 12% chiều cao video; chọn giá trị nhỏ hơn để nâng cao hơn. Chỉnh `Cỡ chữ chú thích` trong web nếu bản dịch dài; chữ sẽ tự xuống dòng theo bề rộng vùng đã khoanh.

Không bắt buộc phải OCR tiếng Trung. Nếu dùng GPT hoặc một công cụ khác để xem video và tạo thẳng SRT tiếng Việt cho các chú thích, mở đúng **video nguồn đang xử lý**, bấm **Khoanh vùng trên video** để đặt bề rộng/vị trí chữ, rồi chọn **Nhập SRT Việt từ GPT**. App ghi riêng file `*_annotation_vi.srt`, giữ timestamp do SRT cung cấp và sao lưu bản cũ khi thay. Khi trạng thái hiện **SRT Việt trực tiếp**, bấm **Chỉ chèn chú thích** để tạo `final_video.mp4` có lớp chữ này và **giữ audio gốc**; không OCR Trung, không gọi Gemini, không tạo Edge TTS và không cần SRT thoại. Dùng **Tạo giọng & xuất video** hoặc **Chỉ ghép video** nếu muốn kết hợp lớp chú thích này với luồng lồng tiếng thông thường.

Trên web, sau OCR có nút **Chép SRT Trung** để đưa nguyên SRT chú thích vào bộ nhớ tạm, nút tải SRT Trung và nút nhập SRT Việt. Nút chép chỉ bật sau khi app đã đọc xong file SRT của đúng project, để thao tác clipboard luôn có đủ ID/timestamp/chữ. App đối chiếu ID và timestamp của hai track, rồi hiện rõ `đã dịch/tổng`, số câu thiếu, câu Việt thừa hoặc mốc thời gian lệch. Chỉ cue có cả ID và timestamp khớp mới được tính là đã dịch đúng cue OCR. Cue Việt tự thêm hoặc lệch timestamp vẫn được burn theo timestamp của chính nó, nên app báo để bạn quyết định có muốn giữ hay sửa. Vì đây là thao tác thay bản dịch do bạn chọn, app sao lưu `*_annotation_vi.srt` cũ trước khi ghi file mới. SRT nhập phải hợp lệ UTF-8, có ID/timestamp; nó chỉ thay lớp chữ ở giữa hình.

Nếu cần dùng track này bằng CLI sau khi đã OCR/dịch, đặt `ANNOTATION_*` đúng vùng và chạy:

```powershell
python translate_subtitles.py output/video_annotation_zh.srt output/video_annotation_vi.srt --ask-api-key
python mix_audio.py input/video.mp4 output/video_vi_tts.srt --burn-subtitles --display-srt output/video_vi.srt --annotation-srt output/video_annotation_vi.srt
```

`--annotation-srt` chỉ là lớp hiển thị: lệnh vẫn dùng SRT đối số thứ hai cho WAV/timeline giọng. Không chọn vùng dựa trên bản **Kết quả** đã burn, vì bản đó có thể đã lật hình hoặc có chữ chèn thêm; web tự chuyển về video nguồn trước khi cho khoanh.

Để chèn **chỉ** SRT chú thích Việt do GPT tạo, không cần SRT thoại hoặc OCR Trung:

```powershell
python burn_subtitle.py input/video.mp4 --annotation-srt output/video_annotation_vi.srt --output output/video_annotation.mp4
```

Lệnh này giữ audio gốc, dùng `ANNOTATION_LEFT/RIGHT` để căn ngang và `ANNOTATION_DISPLAY_Y` để đặt độ cao chữ.

Để chèn logo bằng CLI, thêm ảnh logo và các tùy chọn vị trí vào chính lệnh render:

```powershell
python mix_audio.py input/video.mp4 output/video_vi_tts.srt --burn-subtitles --display-srt output/video_vi.srt --logo input/logo.png --logo-position top-right --logo-width-percent 12 --logo-opacity 0.85
```

Logo được scale theo chiều rộng video, không bị lật cùng video gốc, và chỉ xuất hiện trong file MP4 mới.

Sửa `glossary.json` bằng UTF-8, dạng object Trung → Việt:

```json
{
  "李长生": "Lý Trường Sinh",
  "师尊": "sư tôn",
  "师兄": "sư huynh",
  "灵石": "linh thạch",
  "筑基": "Trúc Cơ"
}
```

Dùng `{}` nếu chưa có thuật ngữ. Không để tên khóa hoặc bản dịch trống. Prompt ưu tiên lời thoại Việt tự nhiên, thống nhất tên riêng/xưng hô và ngữ cảnh cổ trang/tu tiên. Bản dịch do model tạo vẫn cần đọc duyệt; glossary là chỉ dẫn cho model, không tự thay chuỗi máy móc sau khi dịch.

| Cấu hình dịch | Mặc định | Ý nghĩa |
| --- | --- | --- |
| `TRANSLATION_BATCH_SIZE` | `40` | Khoảng 30–50 thường thuận tiện; giới hạn cấu hình 1–100 |
| `TRANSLATION_CONTEXT_SIZE` | `5` | Tối đa 5 câu trước và sau batch làm ngữ cảnh; câu trước kèm bản đã dịch |
| `TRANSLATION_RETRIES` | `3` | 3 lần thử lại sau lần đầu, tối đa 4 request; `0` để không retry |
| `TRANSLATION_RETRY_DELAY` | `5` giây | Chờ tăng dần khi lỗi tạm thời, có giới hạn và jitter |
| `GEMINI_TIMEOUT` | `120` giây | Timeout mỗi request |

Gemini chỉ trả JSON `{id,text}`; code kiểm tra đủ ID, không trùng/thừa, không text rỗng và chỉ lấy lời Việt. ID/start/end của SRT kết quả luôn lấy từ SRT Trung; không giao việc đoán timing cho AI. SDK và cấu trúc JSON tham khảo [Google Gen AI Python SDK](https://googleapis.github.io/python-genai/).

## 5. Timing, giọng đọc và ghép video

Timing OCR có độ phân giải lấy mẫu: ở 4 FPS, hai lần đọc cách nhau khoảng 0,25 giây. Start là lần đầu thấy câu; end là lần đầu thấy khoảng trống/câu mới theo quy tắc gom. Có thể bỏ sót câu xuất hiện quá nhanh hoặc chữ nhỏ/mờ; đây không phải bộ căn âm thanh theo lời nói. Nếu OpenCV không trả timestamp hợp lệ, chương trình cảnh báo và dùng `frame_index/FPS`; fallback này phù hợp CFR và có thể lệch ở video VFR. Khi gặp cảnh báo, nên chuyển nguồn sang CFR rồi OCR lại hoặc sửa timing trong SRT.

Sau khi có SRT Việt, việc đặt TTS là xác định trực tiếp theo timestamp, tới mẫu audio gần nhất. Ví dụ SRT bắt đầu `12.200s`, WAV dài `2.100s` thì giọng phát từ `12.200s` đến `14.300s`, bất kể end time hiển thị của phụ đề. Nếu câu tiếp theo bắt đầu `14.000s`, cảnh báo overlap `0.300s` được ghi vào log và `audio/overlap_warnings.json`.

Mặc định source giữ tốc độ 1.0×; có thể chọn `VOICE_SPEED=1.1` hoặc `--voice-speed 1.1` để tăng riêng từng đoạn giọng Việt bằng FFmpeg atempo. WAV gốc và start_time từng câu giữ nguyên; audio gốc/video không tăng tốc. Thời lượng WAV sau chuyển đổi được đo lại để cảnh báo overlap. Không tự dời câu tiếp theo. Các đoạn overlap được cộng lên nhau; có limiter khi mix để hạn chế clipping. Có thể rút gọn lời Việt hoặc sửa timestamp rồi chạy lại TTS/mix. Phần lời kéo dài quá cuối video bị cắt ở cuối timeline kèm cảnh báo.

Giọng mặc định là `vi-VN-HoaiMyNeural`. Lúc kiểm tra hiện tại, Edge có Hoài My (nữ) và Nam Minh (nam) cho `vi-VN`; giao diện chỉ hiển thị hai người nói này. Phần **Cao độ giọng Edge** lưu riêng theo project trong khoảng -20 đến +20 Hz; `+4Hz` đến `+12Hz` là điểm bắt đầu hợp lý để nâng tông Hoài My. `TTS_PITCH=+0Hz` trong `.env` cũng cho phép phạm vi này, nhưng chỉ là tinh chỉnh F0 và không tạo ra người nói hoặc chất giọng mới. Khi đổi voice hoặc pitch, manifest cache tự nhận đó là cấu hình mới và tạo lại WAV cần thiết. Dùng danh sách giọng của dịch vụ để kiểm tra:

```powershell
python -m edge_tts --list-voices | Select-String 'vi-VN'
```

`TTS_RATE=+0%` được giữ cố định trong v1. `TTS_RETRIES=6` nghĩa là tối đa **6 lượt tổng cộng** cho lỗi TTS thông thường; khác `TRANSLATION_RETRIES` là số lần retry sau lượt đầu. Edge trả MP3; code dùng FFmpeg giải mã thành WAV **mono PCM 16-bit**, mặc định 24 kHz, rồi đo duration bằng số mẫu thật.

Khi Edge trả lỗi kiểu `NoAudioReceived`, code thử nguyên văn câu với `SentenceBoundary` trước, rồi `WordBoundary`; hai lựa chọn này chỉ là metadata của request, không đổi SRT, timestamp hay vị trí audio. Với câu không có dấu kết thúc, sau khi cả hai cách đều lỗi, Edge có thêm đúng một lần thử nội bộ với dấu chấm; SRT và chữ dùng cho cache vẫn giữ nguyên. Mỗi câu Edge lỗi chỉ được thử tối đa 2 lượt logic trong hàng chính rồi tạm hoãn đến cuối hàng, không tạo WAV im lặng. Sau 2 lỗi Edge liên tiếp, request Edge kế tiếp nghỉ 30 giây; lỗi tiếp theo tăng lên tối đa 60 giây. Một WAV hợp lệ sẽ xóa trạng thái chờ. Lượt phục hồi dùng cùng giới hạn; nếu vẫn không tạo được đủ WAV thì job báo ID lỗi và giữ toàn bộ checkpoint để chạy lại.

Điều chỉnh âm lượng lúc ghép:

```powershell
python mix_audio.py input/video.mp4 output/video_vi.srt --original-volume 0.10 --voice-volume 1.0
```

`ORIGINAL_VOLUME=0.15`, `VOICE_VOLUME=1.0` là mặc định. Âm thanh gốc gồm cả nhạc, hiệu ứng và **giọng Trung**; v1 không tách các thành phần đó. Đặt `--original-volume 0` sẽ tắt toàn bộ audio gốc. Video không có audio gốc vẫn được ghép giọng Việt. Khi không burn, video được stream copy, audio được xuất AAC.

Hai cách xuất chữ:

```powershell
# A: SRT rời, không burn
python mix_audio.py input/video.mp4 output/video_vi.srt

# B: burn trong khi xuất
python mix_audio.py input/video.mp4 output/video_vi.srt --burn-subtitles

# Hoặc burn từ video đã ghép
python burn_subtitle.py output/final_video.mp4 output/video_vi.srt --output output/final_video_burned.mp4

# Làm mờ dải phụ đề Trung, lật ngang kiểu gương và burn tiếng Việt dễ đọc
python main.py input/video.mp4 --dub-only --burn-subtitles --blur-chinese-subtitles --blur-strength 18 --mirror-video
```

Burn dùng libass, font `Arial`, cỡ `24`, viền chữ; đổi `BURN_FONT`/`BURN_FONT_SIZE` trong `.env`. Video được encode lại H.264. Tick làm mờ khiến FFmpeg làm mờ một **dải hình chữ nhật** trên video gốc, trước khi lật và burn chữ Việt. Vì vậy nó cũng làm mờ phần hình nằm sau phụ đề Trung; chỉnh `BLUR_*` sát chữ để giảm vùng ảnh hưởng. Hiệu ứng không sửa SRT nào, và SRT rời vẫn được giữ dù chọn burn.

## 6. Thư mục, cache và chạy nhiều video

```text
video-translator/
├── main.py
├── extract_subtitles.py
├── translate_subtitles.py
├── tts_from_srt.py
├── mix_audio.py
├── burn_subtitle.py
├── slow_video.py             # Tạo MP4 tua chậm để xem trước OCR
├── voice_conversion.py        # WAV/MP3/MP4 → RVC + RMVPE → WAV/MP4
├── convert_voice.py           # CLI cho tính năng đổi giọng RVC
├── common.py                  # SRT UTF-8, validation, ghi file nguyên tử
├── media.py                   # FFmpeg/FFprobe, lỗi và progress
├── config.py
├── api_key_prompt.py          # Nhập key ẩn tại terminal, chỉ giữ trong bộ nhớ
├── ocr_runtime.py             # Kiểm tra Paddle/CUDA và log thiết bị OCR
├── check_environment.py       # Kiểm tra imports và chương trình FFmpeg
├── requirements.txt
├── requirements-common.txt
├── requirements-cpu.txt
├── requirements-lock-windows-py312.txt
├── .env.example
├── tools/setup_rvc_gpu.ps1    # Cài RVC/RMVPE/PyTorch CUDA trong runtime riêng
├── models/rvc/                # Model giọng .pth/.index cục bộ; file nhị phân bị bỏ qua
├── glossary.json
├── README.md
├── VALIDATION.md              # Kết quả kiểm tra và giới hạn kiểm chứng
├── tests/
├── input/
├── output/
├── audio/
└── temp/
```

Các đường dẫn tương đối **trong cấu hình `.env`** (`OUTPUT_DIR`, `AUDIO_DIR`, `TEMP_DIR`, `GLOSSARY_PATH`) tính từ thư mục chứa project, kể cả khi `.env` tùy chọn nằm ở chỗ khác. Đường dẫn nhập trực tiếp qua **CLI** tính từ thư mục terminal hiện tại. Vì vậy nên `Set-Location` vào project trước khi chạy ví dụ.

Mỗi batch dịch thành công được lưu dưới `temp/translation_cache/`. Cache dựa trên toàn bộ bytes SRT nguồn, model, glossary, phiên bản prompt, batch/context và request. Lỗi Gemini không làm mất SRT Trung và không tạo SRT Việt dở dang. Khi bị 429, chương trình retry có giới hạn rồi báo kiểm tra quota/billing; chạy lại sẽ tiếp tục từ batch đã xong. Xem [hướng dẫn lỗi và quota Gemini](https://ai.google.dev/gemini-api/docs/troubleshooting).

WAV và `audio/manifest.json` là checkpoint TTS. Không xóa manifest nếu muốn tiếp tục dùng cache; nếu manifest hỏng có thể chạy `tts_from_srt.py ... --force` để tạo lại. Cache model mặc định được đặt trong `temp/model_cache` qua `PADDLE_PDX_CACHE_HOME`, `PADDLE_EXTENSION_DIR`, `HF_HOME` và `MODELSCOPE_CACHE`; giá trị đã có trong biến môi trường process được giữ nguyên. Code bỏ bước thăm dò kết nối các nguồn model để giảm thời gian khởi tạo, nhưng vẫn tải model thật khi chưa có cache. Không giả định model đã có khi chuyển project sang máy mới.

Timeline audio tạm là float32 trên đĩa, đọc WAV từng khối và dùng memory map; không tạo hàng nghìn input FFmpeg hoặc nạp toàn bộ video vào RAM. Với 24 kHz, timeline một giờ dùng khoảng **346 MB đĩa** ngoài WAV/video. Thư mục timeline tạm được dọn sau khi mix; cache dịch/WAV được giữ để tiếp tục.

Output mặc định luôn là `output/final_video.mp4` và lần ghép mới thay file kết quả sau khi xử lý thành công. Dùng thư mục riêng theo video để tránh nhầm SRT có cùng tên gốc hoặc thay audio/kết quả của video trước:

```powershell
python main.py input/tap01.mp4 --output-dir output/tap01 --audio-dir audio/tap01 --translate
python tts_from_srt.py output/tap01/tap01_vi.srt --audio-dir audio/tap01
python mix_audio.py input/tap01.mp4 output/tap01/tap01_vi.srt --audio-dir audio/tap01 --output output/tap01/final_video.mp4
```

Nếu chạy nguyên pipeline: `python main.py input/tap02.mp4 --output-dir output/tap02 --audio-dir audio/tap02`. Tránh chạy đồng thời nhiều job vào cùng output/audio directory.

## 7. Interface thay TTS về sau

`TTSProvider` trong `tts_from_srt.py` là `Protocol` gồm `name`, `options(settings)` và `async synthesize(text, output_wav, settings)`. Backend phải viết WAV mono PCM 16-bit đúng `audio_sample_rate`; `options` phải chứa mọi tùy chọn làm thay đổi giọng để cache không dùng nhầm audio.

`tts_from_srt(..., provider=...)` và `mix_audio(..., provider=...)` nhận provider tùy chọn; truyền cùng provider/cấu hình cho hai bước. Hiện chỉ có `EdgeTTSProvider` hoạt động. F5-TTS/Fish Speech có thể thêm theo interface này ở phiên bản sau, không có backend giả hay lựa chọn CLI chưa được triển khai.

## 8. Kiểm tra và xử lý lỗi

Chạy kiểm tra dependency, syntax, import và logic:

```powershell
python -m pip check
python check_environment.py
python -m py_compile main.py extract_subtitles.py translate_subtitles.py tts_from_srt.py mix_audio.py burn_subtitle.py slow_video.py voice_conversion.py web_app.py web_store.py web_worker.py config.py common.py media.py
python -m unittest discover -s tests -v
python main.py --help
python slow_video.py --help
python tts_from_srt.py --help
python mix_audio.py --help
```

Các unit test mock dịch vụ ở ranh giới mạng để kiểm tra retry/cache/timing và bảo vệ file; production code gọi PaddleOCR/Gemini/Edge thật. Kiểm tra SDK dùng client/schema thật với transport trong bộ nhớ, không tiêu quota. Xem `VALIDATION.md` để biết những bước đã được chạy trên môi trường kiểm tra và những phần chưa xác nhận trực tiếp.

Có smoke test OCR dùng **model PaddleOCR thật** với video phụ đề Trung tự tạo; cần mạng tải model lần đầu và font Microsoft YaHei mặc định trên Windows:

```powershell
python tests/smoke_ocr.py --ffprobe 'C:\Tools\ffmpeg\bin\ffprobe.exe'
```

Có smoke test ghép/burn video thật, dùng WAV tone làm dữ liệu kiểm tra timing. Thêm `--edge` để kiểm tra thêm dịch vụ Edge TTS thật qua mạng:

```powershell
python tests/smoke_media.py --ffmpeg 'C:\Tools\ffmpeg\bin\ffmpeg.exe' --ffprobe 'C:\Tools\ffmpeg\bin\ffprobe.exe'
python tests/smoke_media.py --ffmpeg 'C:\Tools\ffmpeg\bin\ffmpeg.exe' --ffprobe 'C:\Tools\ffmpeg\bin\ffprobe.exe' --edge-only
```

| Hiện tượng | Cách xử lý |
| --- | --- |
| `python` không nhận / import thiếu | Dùng `.\.venv\Scripts\python.exe`; cài requirements bằng chính interpreter đó |
| Paddle không có wheel phù hợp | Kiểm tra Windows x64, Python 3.11/3.12 x64, pip mới; giữ đúng requirements |
| Không tìm thấy FFmpeg/FFprobe | Sửa PATH và mở lại terminal, hoặc cấu hình hai đường dẫn `.exe` trong `.env` |
| Thiếu `subtitles`/libass hoặc `libx264` | Cài build FFmpeg có các thành phần đó, kiểm tra `-filters`/`-encoders` |
| OCR khởi tạo/tải model lỗi | Kiểm tra mạng, proxy, quyền ghi cache, driver và `OCR_DEVICE`; không đổi model sang gói 2.x |
| Chọn GPU nhưng Paddle là bản CPU | Gỡ `paddlepaddle`, cài `requirements.txt`, chạy `check_environment.py` |
| OCR không thấy chữ / gộp sai | Kiểm tra ROI, độ nét chữ, confidence, fuzzy threshold; tăng sample FPS nếu câu đổi nhanh; chạy lại `--reocr` |
| Gemini 401/403 | Kiểm tra API key và quyền truy cập; lỗi quyền không được retry liên tục |
| Gemini 404 | Kiểm tra `GEMINI_MODEL` được hỗ trợ cho key; SRT Trung vẫn còn |
| Gemini 429/timeout/5xx | Kiểm tra quota hoặc kết nối, chờ rồi chạy lại; batch đã xong được tái sử dụng |
| Edge timeout/lỗi dịch vụ | Kiểm tra Internet, chờ rồi chạy lại TTS; WAV hoàn thành được giữ |
| Audio không khớp SRT/config | Chạy lại TTS sau khi sửa lời/giọng/sample rate, rồi mix lại |
| Cảnh báo overlap | Rút gọn lời hoặc chỉnh timing thủ công nếu muốn; có thể tăng riêng giọng Việt bằng `VOICE_SPEED` |
| Chữ Việt lỗi dấu | Lưu `.srt`, `.json`, `.env` UTF-8; chọn font hỗ trợ tiếng Việt khi burn |
| Muốn đổi bản dịch nhưng chạy lại không đổi | SRT Việt được bảo vệ có chủ đích; dùng `translate_subtitles.py ... --force` khi muốn thay |

Lệnh trả mã `0` khi thành công, `1` khi lỗi thông thường, `130` khi dừng bằng Ctrl+C. Hãy kiểm tra SRT và xem/nghe một đoạn kết quả trước khi xử lý cả loạt video.

Gemini 404: CLI sẽ thử lấy danh sách model hỗ trợ generateContent bằng key hiện tại. Chọn model text/JSON phù hợp qua --gemini-model TEN_MODEL (main.py hoặc translate_subtitles.py). Tùy chọn này ưu tiên hơn GEMINI_MODEL trong .env và không lưu key. Đổi model tạo cache dịch riêng; SRT Trung và cache cũ vẫn được giữ. Danh sách chính thức: https://ai.google.dev/gemini-api/docs/models

### Edge TTS không trả audio

Chương trình giữ WAV đã hoàn thành. `TTS_RETRIES=6` thử tối đa 6 lượt với các lỗi TTS thông thường, chờ 5/10/20/40/60 giây giữa các lượt. Cue chỉ chứa khoảng trắng hoặc ký tự vô hình như `U+200B` được lọc trước khi gọi Edge, nên không thể gây retry vô ích. `TTS_REQUEST_INTERVAL=1` thêm khoảng nghỉ giữa các câu cần tạo mới; WAV cache không phải chờ. `TTS_TIMEOUT=90` giới hạn thời gian chờ tác vụ TTS mỗi lượt (giây). Các tùy chọn này không làm thay đổi tốc độ giọng hoặc vô hiệu cache.

Với lỗi Edge đúng kiểu `NoAudioReceived`, chương trình gửi nguyên văn câu bằng `SentenceBoundary` trước, rồi `WordBoundary`; đây chỉ là metadata request nên SRT gốc, cache text, giọng, tốc độ và âm lượng không đổi. Nếu cả hai không có audio và câu thiếu dấu câu cuối, code mới thử thêm một lần nội bộ với dấu `.` qua `SentenceBoundary`. Câu đã có dấu câu cuối không bị đổi text.

Lỗi `NoAudioReceived` không dùng chuỗi 6 retry liên tiếp: mỗi cue có tối đa 2 lượt logic, sau đó tạm hoãn để checkpoint các cue còn lại và phục hồi ở cuối hàng. Sau 2 lỗi Edge liên tiếp, request Edge sau đó chờ 30 giây; thất bại tiếp theo tăng lên tối đa 60 giây. WAV đã kiểm tra hợp lệ sẽ reset trạng thái này. Nếu phục hồi vẫn thiếu WAV, job nêu rõ ID lỗi và giữ checkpoint để lần chạy sau tiếp tục. Lỗi khác từ Edge, FFmpeg, cấu hình giọng hoặc WAV vẫn dùng `TTS_RETRIES` rồi dừng để không che mất nguyên nhân thật. Chạy lại cùng lệnh `--dub-only`; không dùng `--force` nếu muốn giữ audio đã có.
