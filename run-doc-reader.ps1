# Doc Reader launcher for Windows.
#
# Prepares a Python 3.12 virtual environment in .venv (PyTorch with CUDA when an
# NVIDIA GPU is present, plus Kokoro, faster-whisper, PySide6, pynput, sounddevice),
# then hands off to `python -m doc_reader.windows_app <command>`.
#
#   .\run-doc-reader.ps1                 # start everything and open the web app
#   .\run-doc-reader.ps1 status          # health of speech service, web app, helper
#   .\run-doc-reader.ps1 stop
#   .\run-doc-reader.ps1 doctor
#   .\run-doc-reader.ps1 enable-startup  # launch at login
#   .\run-doc-reader.ps1 --prepare-only  # just build the environment
#   .\run-doc-reader.ps1 cli <file.pdf> [--mode smart]   # command-line reader
#
# Environment overrides:
#   DOC_READER_VENV_DIR   alternate venv location
#   DOC_READER_TORCH      "cuda" | "cpu"   (default: auto-detect NVIDIA)
#   DOC_READER_CUDA_INDEX PyTorch wheel index (default cu124)

[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Args
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir = if ($env:DOC_READER_VENV_DIR) { $env:DOC_READER_VENV_DIR } else { Join-Path $ScriptDir ".venv" }
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$ReqFile = Join-Path $ScriptDir "requirements-windows.txt"
$BaseReqFile = Join-Path $ScriptDir "requirements.txt"
$StampFile = Join-Path $VenvDir ".requirements-windows.sha256"
$TorchVersion = "2.6.0"
$TorchVisionVersion = "0.21.0"
$TorchAudioVersion = "2.6.0"
$CudaIndex = if ($env:DOC_READER_CUDA_INDEX) { $env:DOC_READER_CUDA_INDEX } else { "cu124" }

function Write-Log([string] $Message) {
    Write-Host "[doc-reader] $Message"
}

function Find-Command([string] $Name) {
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Test-NvidiaGpu {
    if ($env:DOC_READER_TORCH -eq "cpu") { return $false }
    if ($env:DOC_READER_TORCH -eq "cuda") { return $true }
    if (Find-Command "nvidia-smi") { return $true }
    try {
        $gpus = Get-CimInstance Win32_VideoController -ErrorAction Stop | Select-Object -ExpandProperty Name
        foreach ($gpu in $gpus) { if ($gpu -match "NVIDIA") { return $true } }
    } catch {}
    return $false
}

function Invoke-Checked([string] $Exe, [string[]] $Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Exe $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
    }
}

# ---------------------------------------------------------------- create venv
$uv = Find-Command "uv"
if (-not (Test-Path $VenvPython)) {
    Write-Log "Creating virtual environment at $VenvDir ..."
    if ($uv) {
        Invoke-Checked $uv @("venv", "--python", "3.12", $VenvDir)
    } else {
        $py = Find-Command "py"
        $python = $null
        if ($py) {
            foreach ($version in @("3.12", "3.11", "3.10")) {
                & $py "-$version" -c "import sys" 2>$null
                if ($LASTEXITCODE -eq 0) { $python = @($py, "-$version"); break }
            }
        }
        if (-not $python) {
            $candidate = Find-Command "python"
            if ($candidate) {
                $ver = & $candidate -c "import sys; print('%d.%d' % sys.version_info[:2])"
                if ($ver -in @("3.10", "3.11", "3.12")) { $python = @($candidate) }
            }
        }
        if (-not $python) {
            throw "Python 3.10-3.12 is required (PyTorch does not support newer versions yet). Install uv (winget install astral-sh.uv) or Python 3.12 from python.org and re-run."
        }
        Invoke-Checked $python[0] ($python[1..($python.Length - 1)] + @("-m", "venv", $VenvDir))
    }
}

function Invoke-Pip([string[]] $PipArgs) {
    if ($uv) {
        Invoke-Checked $uv (@("pip", "install", "--python", $VenvPython) + $PipArgs)
    } else {
        Invoke-Checked $VenvPython (@("-m", "pip", "install") + $PipArgs)
    }
}

# ---------------------------------------------------------------- install deps
$reqHash = (Get-FileHash -Algorithm SHA256 $ReqFile).Hash + (Get-FileHash -Algorithm SHA256 $BaseReqFile).Hash
$installedHash = if (Test-Path $StampFile) { (Get-Content $StampFile -Raw).Trim() } else { "" }
if ($reqHash -ne $installedHash) {
    if (-not $uv) {
        Invoke-Checked $VenvPython @("-m", "pip", "install", "--upgrade", "pip")
    }
    $hasTorch = $false
    & $VenvPython -c "import torch" 2>$null
    if ($LASTEXITCODE -eq 0) { $hasTorch = $true }
    if (-not $hasTorch) {
        if (Test-NvidiaGpu) {
            Write-Log "NVIDIA GPU detected: installing PyTorch $TorchVersion ($CudaIndex) ..."
            Invoke-Pip @("--index-url", "https://download.pytorch.org/whl/$CudaIndex", "torch==$TorchVersion", "torchvision==$TorchVisionVersion", "torchaudio==$TorchAudioVersion")
        } else {
            Write-Log "No NVIDIA GPU detected: installing CPU PyTorch $TorchVersion ..."
            Invoke-Pip @("--index-url", "https://download.pytorch.org/whl/cpu", "torch==$TorchVersion", "torchvision==$TorchVisionVersion", "torchaudio==$TorchAudioVersion")
        }
    }
    Write-Log "Installing/updating dependencies from requirements-windows.txt ..."
    if ($uv) {
        Invoke-Pip @("--extra-index-url", "https://download.pytorch.org/whl/$CudaIndex", "--index-strategy", "unsafe-best-match", "-r", $ReqFile)
    } else {
        Invoke-Pip @("-r", $ReqFile)
    }
    Set-Content -Path $StampFile -Value $reqHash -Encoding ascii
}

# ---------------------------------------------------------------- system tools (optional)
$winget = Find-Command "winget"
if (-not (Find-Command "ffplay")) {
    $ffplayGlob = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages\Gyan.FFmpeg*\*\bin\ffplay.exe"
    if (-not (Get-ChildItem $ffplayGlob -ErrorAction SilentlyContinue)) {
        if ($winget -and $env:DOC_READER_SKIP_WINGET -ne "1") {
            Write-Log "ffmpeg not found: installing with winget (needed for audio playback and dictation)."
            & $winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements --silent | Out-Null
        } else {
            Write-Log "ffmpeg (ffplay) not found. Install it with: winget install Gyan.FFmpeg"
        }
    }
}

# ---------------------------------------------------------------- dispatch
if ($Args.Count -gt 0 -and $Args[0] -eq "--prepare-only") {
    Write-Log "Environment ready: $VenvDir"
    exit 0
}

$env:PYTHONPATH = if ($env:PYTHONPATH) { "$ScriptDir;$env:PYTHONPATH" } else { $ScriptDir }
if ($Args.Count -gt 0 -and $Args[0] -eq "cli") {
    & $VenvPython -m doc_reader @($Args[1..($Args.Count - 1)])
    exit $LASTEXITCODE
}
if ($Args.Count -gt 0 -and (Test-Path $Args[0]) -and ($Args[0] -match "\.(pdf|docx|txt|md|markdown)$")) {
    & $VenvPython -m doc_reader @Args
    exit $LASTEXITCODE
}
& $VenvPython -m doc_reader.windows_app @Args
exit $LASTEXITCODE
