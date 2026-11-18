param(
 [Parameter(Mandatory=$true)][string]$Exe,
 [Parameter(Mandatory=$true)][string]$ProjectRoot,
 [string]$Name='UniFlow V1 Abnahme'
)
$ErrorActionPreference='Stop'
$exePath=(Resolve-Path -LiteralPath $Exe).Path
$project=(Resolve-Path -LiteralPath $ProjectRoot).Path
if (-not(Test-Path -LiteralPath (Join-Path $project 'config/default.yaml'))) {throw 'Invalid UniFlow project'}
$path=Join-Path ([Environment]::GetFolderPath('Desktop')) "$Name.lnk"
$shell=New-Object -ComObject WScript.Shell
$link=$shell.CreateShortcut($path)
$link.TargetPath=$exePath
$link.Arguments='--project-root "'+$project+'"'
$link.WorkingDirectory=$env:TEMP
$link.IconLocation="$exePath,0"
$link.Description='UniFlow - separates Abnahmeprojekt'
$link.Save()
Write-Output $path
