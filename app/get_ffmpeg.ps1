# INSTALL_REQUIREMENTS.bat step 4: put ffmpeg.exe + ffprobe.exe into the tool's "ffmpeg" folder
# when FFmpeg is not on this PC yet (PATH, C:\ffmpeg\bin or the ffmpeg folder).
param(
    [string]$Url = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip',
    [switch]$Force
)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Target = Join-Path $Root 'ffmpeg'

if (-not $Force) {
    if ((Test-Path (Join-Path $Target 'ffmpeg.exe')) -and (Test-Path (Join-Path $Target 'ffprobe.exe'))) {
        Write-Host ('FFmpeg: OK (' + $Target + ')'); exit 0
    }
    if ((Get-Command ffmpeg -ErrorAction SilentlyContinue) -and (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
        Write-Host 'FFmpeg: OK (on PATH)'; exit 0
    }
    if ((Test-Path 'C:\ffmpeg\bin\ffmpeg.exe') -and (Test-Path 'C:\ffmpeg\bin\ffprobe.exe')) {
        Write-Host 'FFmpeg: OK (C:\ffmpeg\bin)'; exit 0
    }
}

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = 'SilentlyContinue'  # the PowerShell 5.1 progress bar makes downloads very slow
$Zip = Join-Path $env:TEMP 'recap_ffmpeg_download.zip'
$Unpack = Join-Path $env:TEMP 'recap_ffmpeg_download'
Write-Host ('FFmpeg not found - downloading it (about 100 MB) from ' + $Url)
Invoke-WebRequest -Uri $Url -OutFile $Zip -UseBasicParsing
if (Test-Path $Unpack) { Remove-Item -LiteralPath $Unpack -Recurse -Force }
Expand-Archive -LiteralPath $Zip -DestinationPath $Unpack -Force
New-Item -ItemType Directory -Force -Path $Target | Out-Null
foreach ($name in @('ffmpeg.exe', 'ffprobe.exe')) {
    $found = Get-ChildItem -LiteralPath $Unpack -Recurse -Filter $name | Select-Object -First 1
    if (-not $found) { throw ($name + ' is not in the downloaded zip') }
    Copy-Item -LiteralPath $found.FullName -Destination (Join-Path $Target $name) -Force
}
Remove-Item -LiteralPath $Zip -Force
Remove-Item -LiteralPath $Unpack -Recurse -Force
Write-Host ('FFmpeg ready: ' + $Target)
exit 0
