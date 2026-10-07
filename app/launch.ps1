param([switch]$CheckOnly)

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

    foreach ($f in @('recap_runtime\services\whisper_service.py', 'recap_runtime\services\ai_service.py', 'app\check_python.py')) {
        if (-not (Test-Path -LiteralPath (Join-Path $Root $f))) { Fail ('Bundled file missing: ' + $f) 12 }
    }

    # Python candidates: config.json python_path, the py launcher (3.12 is what INSTALL_REQUIREMENTS.bat uses), PATH.
    $candidates = New-Object System.Collections.Generic.List[string]
    if ($Config -and $Config.python_path) { $candidates.Add([string]$Config.python_path) }
    $ErrorActionPreference = 'Continue'  # a missing py / python must not stop the search
    foreach ($version in @('-3.12', '-3')) {
        try {
            $pyExe = (& py $version -c "import sys; print(sys.executable)" 2>$null | Select-Object -Last 1)
            if ($pyExe) { $candidates.Add([string]$pyExe) }
        } catch {}
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $candidates.Add($cmd.Source) }

    $probe = Join-Path $Root 'app\check_python.py'
    $selected = $null
    $tried = New-Object System.Collections.Generic.List[string]
    foreach ($py in ($candidates | Select-Object -Unique)) {
        if (-not $py -or -not (Test-Path -LiteralPath $py)) { continue }
        $answer = ''
        try { $answer = ((& $py $probe 2>&1) | Out-String).Trim() } catch { $answer = $_.Exception.Message }
        if ($LASTEXITCODE -eq 0) { $selected = $py; break }
        $tried.Add($py + '  ->  ' + $answer)
    }
    $ErrorActionPreference = 'Stop'

    if (-not $selected) {
        if ($tried.Count -gt 0) {
            Write-Host 'Python found, but not ready:' -ForegroundColor Yellow
            foreach ($line in $tried) { Write-Host ('  ' + $line) }
        }
        Fail 'Python with the required packages was not found. Run INSTALL_REQUIREMENTS.bat first.' 20
    }
    Write-Host ('Python: ' + $selected) -ForegroundColor Green
    if ($CheckOnly) { exit 0 }
    & $selected (Join-Path $Root 'app\controller.py')
    exit $LASTEXITCODE
}
catch {
    Write-Host ('UNHANDLED ERROR: ' + $_.Exception.Message) -ForegroundColor Red
    exit 99
}
