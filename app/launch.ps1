$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

function Fail([string]$Message, [int]$Code = 1) {
    Write-Host ''
    Write-Host ('ERROR: ' + $Message) -ForegroundColor Red
    exit $Code
}

try {
    $Config = $null
    $ConfigPath = Join-Path $Root 'config.json'
    if (Test-Path -LiteralPath $ConfigPath) {
        try { $Config = Get-Content -Raw -Encoding UTF8 -LiteralPath $ConfigPath | ConvertFrom-Json } catch { $Config = $null }
    }

    foreach ($f in @('recap_runtime\services\whisper_service.py', 'recap_runtime\services\ai_service.py')) {
        if (-not (Test-Path -LiteralPath (Join-Path $Root $f))) { Fail ('Bundled runtime file missing: ' + $f) 12 }
    }

    $candidates = New-Object System.Collections.Generic.List[string]
    if ($Config -and $Config.python_path) { $candidates.Add([string]$Config.python_path) }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $candidates.Add($cmd.Source) }
    try {
        $pyExe = (& py -3 -c "import sys; print(sys.executable)" 2>$null | Select-Object -Last 1)
        if ($pyExe) { $candidates.Add([string]$pyExe) }
    } catch {}

    $probeCode = @'
import importlib.util
from google import genai
from dotenv import load_dotenv
missing = [name for name in ("PIL", "edge_tts", "faster_whisper") if importlib.util.find_spec(name) is None]
if missing: raise RuntimeError("missing: " + ", ".join(missing))
print("CORE_OK")
'@
    $selected = $null
    foreach ($py in ($candidates | Select-Object -Unique)) {
        if (-not $py -or -not (Test-Path -LiteralPath $py)) { continue }
        try {
            & $py -c $probeCode 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) { $selected = $py; break }
        } catch {}
    }
    if (-not $selected) { Fail 'Python with the required packages was not found. Run INSTALL_REQUIREMENTS.bat first.' 20 }
    Write-Host ('Python: ' + $selected) -ForegroundColor Green
    & $selected (Join-Path $Root 'app\controller.py')
    exit $LASTEXITCODE
}
catch {
    Write-Host ('UNHANDLED ERROR: ' + $_.Exception.Message) -ForegroundColor Red
    exit 99
}
