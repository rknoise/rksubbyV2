# rksubbyV2 telepítő: mindent a projektmappába tesz (Python, csomagok, ffmpeg, NVIDIA-gyorsítás,
# felismerő modell), és asztali ikont készít. Előre telepített Python nem kell hozzá.
# Indítás: install.bat (vagy: powershell -ExecutionPolicy Bypass -File tools\install.ps1 [-NoGpu] [-NoModel] [-NoShortcut])
# Eltávolítás: a projektmappa és az asztali "rksubby" ikon törlése.

param([switch]$NoGpu, [switch]$NoModel, [switch]$NoShortcut)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'      # az Invoke-WebRequest így sokkal gyorsabb
[Console]::OutputEncoding = [Text.Encoding]::UTF8
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Root = Split-Path -Parent $PSScriptRoot
$Tools = Join-Path $Root '.tools'
$Venv = Join-Path $Root '.venv'
$Py = Join-Path $Venv 'Scripts\python.exe'
New-Item -ItemType Directory -Force $Tools | Out-Null

# A uv a Pythont és a letöltött csomagokat is a projektmappában tartja (a mappa törlésével minden eltűnik)
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Tools 'python'
$env:UV_CACHE_DIR = Join-Path $Tools 'uv-cache'

function Step($n, $text) {
    Write-Host ''
    Write-Host "[$n/6] $text" -ForegroundColor Cyan
}

function Check($what) {
    if ($LASTEXITCODE -ne 0) { throw "$what nem sikerült (kilépési kód: $LASTEXITCODE)." }
}

try {
    if ($Root -match '[^\x00-\x7F]') {
        Write-Host 'Figyelem: ékezet van a mappa útvonalában. Ha hibát látsz, tedd a mappát pl. ide: C:\rksubbyV2' -ForegroundColor Yellow
    }

    Step 1 'Telepítő eszköz (uv) letöltése...'
    $uv = Join-Path $Tools 'uv.exe'
    if (-not (Test-Path $uv)) {
        $zip = Join-Path $Tools 'uv.zip'
        Invoke-WebRequest -UseBasicParsing -OutFile $zip `
            -Uri 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip'
        Expand-Archive -Path $zip -DestinationPath $Tools -Force
        Remove-Item $zip
    }
    & $uv --version
    Check 'A uv indítása'

    Step 2 'Python 3.12 és saját környezet (.venv)...'
    & $uv venv $Venv --python 3.12 --python-preference only-managed --seed --allow-existing
    Check 'A Python-környezet létrehozása'

    Step 3 'Csomagok: faster-whisper, ffmpeg, PyQt5, ... (pár perc)'
    & $uv pip install --python $Py -r (Join-Path $Root 'requirements.txt')
    Check 'A csomagok telepítése'

    $nvidia = $null -ne (Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue | Where-Object { $_.Name -match 'NVIDIA' })
    if ($nvidia -and -not $NoGpu) {
        Step 4 'NVIDIA-gyorsítás: CUDA 12 / cuDNN 9 könyvtárak (kb. 1-2 GB)...'
        & $uv pip install --python $Py -r (Join-Path $Root 'requirements-gpu.txt')
        Check 'Az NVIDIA-könyvtárak telepítése'
        $model = 'distil-large-v3'
    } else {
        Step 4 'Nincs NVIDIA-kártya (vagy kihagyva): a felismerés CPU-n fut, kisebb modellel.'
        $model = 'small.en'
    }

    if (-not $NoModel) {
        Step 5 "Felismerő modell letöltése: $model (egyszeri, a nagy modell kb. 1,5 GB)..."
        & $Py -c "from faster_whisper.utils import download_model; print(download_model('$model'))"
        Check 'A modell letöltése'
    } else {
        Step 5 'Modell: kihagyva (az első indításkor tölti le).'
    }

    if (-not $NoShortcut) {
        Step 6 'Asztali ikon és Start menü...'
        $ws = New-Object -ComObject WScript.Shell
        foreach ($dir in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {
            $lnk = $ws.CreateShortcut((Join-Path $dir 'rksubby.lnk'))
            $lnk.TargetPath = Join-Path $Venv 'Scripts\pythonw.exe'
            $lnk.Arguments = '"' + (Join-Path $Root 'rksubby.py') + '"'
            $lnk.WorkingDirectory = $Root
            $lnk.IconLocation = (Join-Path $Root 'subby\web\rksubby.ico') + ',0'
            $lnk.Description = 'rksubby - élő magyar felirat'
            $lnk.Save()
        }
    } else {
        Step 6 'Ikon: kihagyva.'
    }

    Write-Host ''
    Write-Host 'Kész! Indítás: az asztali "rksubby" ikon (vagy a start.bat). Az első indításkor a beállító varázsló nyílik meg.' -ForegroundColor Green
    exit 0
}
catch {
    Write-Host ''
    Write-Host "HIBA: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'Ellenőrizd az internetkapcsolatot, majd futtasd újra az install.bat-ot (a már letöltött részeket nem tölti le újra).'
    exit 1
}
