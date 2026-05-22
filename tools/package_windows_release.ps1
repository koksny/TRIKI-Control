param(
    [string] $Version = "",
    [switch] $Build,
    [switch] $ExeAsset
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$env:PYTHONPATH = Join-Path $root "src"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Expected venv Python at $python. Create .venv and install requirements first."
}

Push-Location $root
try {
    if ([string]::IsNullOrWhiteSpace($Version)) {
        $Version = (& $python -c "from triki_control.metadata import APP_VERSION; print(APP_VERSION)").Trim()
    }

    if ($Build) {
        & (Join-Path $root "tools\build_windows_exe.ps1")
        if ($LASTEXITCODE -ne 0) {
            exit $LASTEXITCODE
        }
    }

    $releaseDir = Join-Path $root "release"
    $stageDir = Join-Path $releaseDir "TRIKI-Control-$Version-windows"
    $zipPath = Join-Path $releaseDir "TRIKI-Control-$Version-windows.zip"
    $exeAssetPath = Join-Path $releaseDir "TRIKI-Control-$Version-windows.exe"

    New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null
    if (Test-Path -LiteralPath $stageDir) {
        Remove-Item -LiteralPath $stageDir -Recurse -Force
    }
    if (Test-Path -LiteralPath $zipPath) {
        Remove-Item -LiteralPath $zipPath -Force
    }
    if ($ExeAsset -and (Test-Path -LiteralPath $exeAssetPath)) {
        Remove-Item -LiteralPath $exeAssetPath -Force
    }
    New-Item -ItemType Directory -Force -Path $stageDir | Out-Null

    Copy-Item -LiteralPath (Join-Path $root "dist\TRIKI-Control.exe") -Destination $stageDir
    Copy-Item -LiteralPath (Join-Path $root "dist\TRIKI-Control-Debug.exe") -Destination $stageDir
    Copy-Item -LiteralPath (Join-Path $root "README.md") -Destination $stageDir
    Copy-Item -LiteralPath (Join-Path $root "CREDITS.md") -Destination $stageDir
    Copy-Item -LiteralPath (Join-Path $root "LICENSE") -Destination $stageDir
    Copy-Item -LiteralPath (Join-Path $root "docs") -Destination (Join-Path $stageDir "docs") -Recurse

    @"
TRIKI Control $Version

Created by Wojciech "Koksny" GĂłrny, Koksny.com.
Released under the MIT License.

Start with TRIKI-Control.exe.

Use TRIKI-Control-Debug.exe only when you need a console window for BLE logs.
Pair TRIKI from the app, wait until the Pair button turns green, then choose a profile or edit mappings.
"@ | Set-Content -LiteralPath (Join-Path $stageDir "START-HERE.txt") -Encoding UTF8

    Compress-Archive -Path (Join-Path $stageDir "*") -DestinationPath $zipPath -Force
    Write-Host "Packaged $zipPath"

    if ($ExeAsset) {
        Copy-Item -LiteralPath (Join-Path $root "dist\TRIKI-Control.exe") -Destination $exeAssetPath -Force
        Write-Host "Packaged $exeAssetPath"
    }
}
finally {
    Pop-Location
}
