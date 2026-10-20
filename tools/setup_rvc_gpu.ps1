<#
Install the isolated RVC + RMVPE CUDA runtime used by VietDub Studio.

Run from the project root:
  powershell -ExecutionPolicy Bypass -File .\tools\setup_rvc_gpu.ps1

This script never installs Torch/RVC into the PaddleOCR `.venv`.  It follows
the upstream RVC Windows instructions for NVIDIA GPUs before RTX 50: Python
3.12 and PyTorch CUDA 11.8.  A modern NVIDIA driver runs cu118 wheels on the
GTX 1650 used by this project.
#>
[CmdletBinding()]
param(
    [switch]$SkipModelAssets,
    [switch]$RefreshRvc
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$toolsRoot = Join-Path $projectRoot '.tools'
$rvcRoot = Join-Path $toolsRoot 'rvc'
$runtimeRoot = Join-Path $toolsRoot 'rvc-runtime'
$runtimePython = Join-Path $runtimeRoot 'Scripts\python.exe'
$modelsRoot = Join-Path $projectRoot 'models\rvc'
$rvcCommit = '81eed5e8f68b6bed1789f682fe78cdd324495afc'
$repository = 'https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI.git'

function Invoke-Checked {
    param([Parameter(Mandatory = $true)][string]$File, [Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Lệnh thất bại ($LASTEXITCODE): $File $($Arguments -join ' ')"
    }
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw 'Thiếu Git. Cài Git for Windows rồi chạy lại tools\setup_rvc_gpu.ps1.'
}
if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw 'Thiếu Python Launcher (py). Cài Python 3.12 x64 rồi chạy lại.'
}

try {
    & py -3.12 --version | Out-Null
} catch {
    throw 'Cần Python 3.12 x64. Cài từ python.org, sau đó chạy lại script này.'
}

New-Item -ItemType Directory -Force -Path $toolsRoot, $modelsRoot | Out-Null

if (Test-Path $rvcRoot) {
    if (-not (Test-Path (Join-Path $rvcRoot '.git'))) {
        throw "Đường dẫn $rvcRoot đã tồn tại nhưng không phải checkout RVC. Đổi tên thư mục này rồi chạy lại."
    }
    if ($RefreshRvc) {
        Invoke-Checked git -C $rvcRoot fetch --depth 1 origin $rvcCommit
        Invoke-Checked git -C $rvcRoot checkout --detach $rvcCommit
    }
} else {
    Write-Host 'Tải mã nguồn RVC (không tải model giọng cá nhân)...'
    Invoke-Checked git clone --depth 1 $repository $rvcRoot
    Invoke-Checked git -C $rvcRoot fetch --depth 1 origin $rvcCommit
    Invoke-Checked git -C $rvcRoot checkout --detach $rvcCommit
}

if (-not (Test-Path $runtimePython)) {
    Write-Host 'Tạo Python runtime RVC riêng...'
    Invoke-Checked py -3.12 -m venv $runtimeRoot
}

Write-Host 'Cài PyTorch CUDA 11.8 cho RVC...'
Invoke-Checked $runtimePython -m pip install --upgrade pip setuptools wheel
Invoke-Checked $runtimePython -m pip install 'torch==2.7.1+cu118' 'torchaudio==2.7.1+cu118' --index-url https://download.pytorch.org/whl/cu118 --extra-index-url https://pypi.org/simple

$requirements = Join-Path $rvcRoot 'requirments_cu118_py312.txt'
if (-not (Test-Path $requirements)) {
    throw "Checkout RVC không có $requirements. Hãy chạy lại với -RefreshRvc."
}
Write-Host 'Cài dependency RVC/RMVPE...'
Invoke-Checked $runtimePython -m pip install -r $requirements

if (-not $SkipModelAssets) {
    Write-Host 'Tải Hubert Base và RMVPE (cần Internet lần đầu, sau đó inference chạy offline)...'
    # RVC's pinned transformers/tokenizers versions require huggingface_hub < 1.
    # Keep the CLI available without upgrading it past that compatible range.
    Invoke-Checked $runtimePython -m pip install --upgrade 'huggingface_hub>=0.26,<1.0'
    $hf = Join-Path $runtimeRoot 'Scripts\hf.exe'
    if (-not (Test-Path $hf)) {
        throw 'Không tìm thấy lệnh hf sau khi cài huggingface_hub.'
    }
    Invoke-Checked $hf download lj1995/VoiceConversionWebUI --revision main --include 'hubert_base/*' --local-dir (Join-Path $rvcRoot 'assets')
    Invoke-Checked $hf download lj1995/VoiceConversionWebUI rmvpe.pt --revision main --local-dir (Join-Path $rvcRoot 'assets\rmvpe')
}

# ``-SkipModelAssets`` is useful only when the two assets were provisioned
# already. Do not claim the runtime is usable merely because ``cli.py --help``
# works: actual RMVPE inference also needs a complete local HuBERT checkpoint.
$hubertRoot = Join-Path $rvcRoot 'assets\hubert_base'
$hubertConfig = Join-Path $hubertRoot 'config.json'
$hubertWeights = (Test-Path (Join-Path $hubertRoot 'model.safetensors')) -or (Test-Path (Join-Path $hubertRoot 'pytorch_model.bin'))
$rmvpeModel = Join-Path $rvcRoot 'assets\rmvpe\rmvpe.pt'
if (-not (Test-Path $hubertConfig) -or -not $hubertWeights -or -not (Test-Path $rmvpeModel)) {
    throw 'Thiếu Hubert Base hoặc rmvpe.pt. Chạy lại không dùng -SkipModelAssets để tải asset cần cho inference offline.'
}

$modelReadme = Join-Path $modelsRoot 'README.txt'
if (-not (Test-Path $modelReadme)) {
@'
Đặt model RVC mà bạn có quyền sử dụng vào thư mục này.

Ví dụ:
  cute_female.pth
  cute_female.index    (tùy chọn, cùng tên với .pth)

VietDub Studio sẽ tự liệt kê các file .pth ở đây. Model RVC quyết định timbre
giọng nữ; các preset cute/sáng/mềm chỉ tinh chỉnh F0 nhẹ qua RMVPE/RVC.
'@ | Set-Content -LiteralPath $modelReadme -Encoding utf8
}

Write-Host 'Kiểm tra CUDA...'
$cudaCheck = @'
import sys
import torch
print('torch:', torch.__version__)
print('cuda:', torch.version.cuda)
print('cuda available:', torch.cuda.is_available())
if not torch.cuda.is_available():
    sys.exit(2)
print('gpu:', torch.cuda.get_device_name(0))
'@
$cudaCheck | & $runtimePython -
if ($LASTEXITCODE -ne 0) {
    throw 'PyTorch RVC không thấy CUDA GPU. Kiểm tra driver NVIDIA, rồi chạy lại setup.'
}

$cli = Join-Path $rvcRoot 'infer\cli.py'
Invoke-Checked $runtimePython $cli --help
Write-Host ''
Write-Host 'RVC + RMVPE GPU đã sẵn sàng.' -ForegroundColor Green
Write-Host "Đặt model nữ .pth (và .index nếu có) vào: $modelsRoot"
Write-Host 'Mở hoặc tải lại http://127.0.0.1:7860, vào thẻ “Đổi sang giọng nữ cute”.'
