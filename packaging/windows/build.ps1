param([string]$Python = 'py')
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path "$PSScriptRoot/../..").Path
Set-Location $root
& $Python -m venv .build-venv
if ($LASTEXITCODE) { exit $LASTEXITCODE }
$py = Join-Path $root '.build-venv/Scripts/python.exe'
& $py -m pip install -r packaging/windows/requirements-lock.txt
if ($LASTEXITCODE) { exit $LASTEXITCODE }
& $py -m pip install --no-deps -e .
if ($LASTEXITCODE) { exit $LASTEXITCODE }
& $py -m PyInstaller --noconfirm --clean packaging/windows/UniFlow.spec
exit $LASTEXITCODE
