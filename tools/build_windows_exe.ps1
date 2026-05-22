$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$env:PYTHONPATH = Join-Path $root "src"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Expected venv Python at $python. Create .venv and install requirements first."
}

Push-Location $root
try {
    Get-Process |
        Where-Object { $_.ProcessName -like "TRIKI-Control*" -and $_.Path -like "$root\dist\*" } |
        Stop-Process -Force -ErrorAction SilentlyContinue

    function Invoke-TrikiBuild {
        param(
            [Parameter(Mandatory = $true)]
            [string] $Name,
            [switch] $Windowed
        )

        $args = @(
            "-m", "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onefile",
            "--paths", (Join-Path $root "src"),
            "--name", $Name,
            "--collect-all", "bleak",
            "--collect-all", "winrt",
            "--collect-all", "webview",
            "--collect-all", "pystray",
            "--collect-all", "PIL",
            "--collect-all", "pythonnet",
            "--collect-all", "clr_loader"
        )

        if ($Windowed) {
            $args += "--windowed"
        }
        else {
            $args += "--console"
        }

        $args += (Join-Path $root "src\triki_control\desktop.py")
        & $python @args

        if ($LASTEXITCODE -ne 0) {
            exit $LASTEXITCODE
        }
    }

    Invoke-TrikiBuild -Name "TRIKI-Control" -Windowed
    Invoke-TrikiBuild -Name "TRIKI-Control-Debug"

    Write-Host "Built dist\TRIKI-Control.exe"
    Write-Host "Built dist\TRIKI-Control-Debug.exe"
}
finally {
    Pop-Location
}
