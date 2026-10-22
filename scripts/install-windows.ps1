# SPDX-License-Identifier: MIT
# One-click source installation; system-only mode also works next to an EXE.
[CmdletBinding()]
param(
    [switch]$ComponentsOnly,
    [switch]$Minimal,
    [switch]$NoSys,
    [switch]$DryRun,
    [switch]$SkipExt,
    [switch]$SkipImDisk
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

function Find-Python {
    $candidates = @()
    $installed = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
    if (Test-Path -LiteralPath $installed) { $candidates += @{Exe=$installed; Args=@()} }
    $launcher = Get-Command 'py.exe' -ErrorAction SilentlyContinue
    if ($launcher) {
        $candidates += @{Exe=$launcher.Source; Args=@('-3.12')}
        $candidates += @{Exe=$launcher.Source; Args=@('-3')}
    }
    $python = Get-Command 'python.exe' -ErrorAction SilentlyContinue
    if ($python -and $python.Source -notlike '*\WindowsApps\*') {
        $candidates += @{Exe=$python.Source; Args=@()}
    }
    foreach ($candidate in $candidates) {
        $arguments = @($candidate.Args) + @('-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 2)')
        & $candidate.Exe @arguments 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { return $candidate }
    }
    return $null
}
try {
    if ($ComponentsOnly) {
        $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $PSScriptRoot 'install-windows-components.ps1'))
        if ($DryRun) { $arguments += '-DryRun' }
        if ($SkipExt) { $arguments += '-SkipExt' }
        if ($SkipImDisk) { $arguments += '-SkipImDisk' }
        & powershell.exe @arguments
        exit $LASTEXITCODE
    }
    if ($DryRun) {
        Write-Host 'План: найти Python 3.10+; при отсутствии установить Python 3.12 через WinGet.'
        Write-Host 'Создать .venv, установить MountFile и зависимости, проверить импорты и pip check.'
        if (-not $NoSys -and -not $Minimal) {
            $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $PSScriptRoot 'install-windows-components.ps1'), '-DryRun')
            if ($SkipExt) { $arguments += '-SkipExt' }
            if ($SkipImDisk) { $arguments += '-SkipImDisk' }
            & powershell.exe @arguments
            exit $LASTEXITCODE
        }
        exit 0
    }
    if (-not (Test-Path -LiteralPath (Join-Path $root 'pyproject.toml'))) {
        throw 'Исходники MountFile не найдены. Для готового EXE используйте install-components.bat.'
    }
    $python = Find-Python
    if (-not $python) {
        $winget = Get-Command 'winget.exe' -ErrorAction SilentlyContinue
        if (-not $winget) {
            throw 'Python 3.10+ и WinGet отсутствуют. Установите App Installer из Microsoft Store или Python с https://python.org, затем повторите запуск.'
        }
        Write-Host 'Установка Python 3.12 через WinGet...'
        & $winget.Source install --exact --id Python.Python.3.12 --source winget --scope user --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
        if ($LASTEXITCODE -ne 0) { throw "WinGet не установил Python: код $LASTEXITCODE" }
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
        $python = Find-Python
        if (-not $python) { throw 'Python не найден после установки. Перезапустите install.bat.' }
    }
    $arguments = @($python.Args) + @((Join-Path $root 'install_mountfile.py'))
    if ($Minimal) { $arguments += '--minimal' }
    if ($NoSys) { $arguments += '--no-sys' }
    if ($SkipExt) { $arguments += '--skip-ext' }
    if ($SkipImDisk) { $arguments += '--skip-imdisk' }
    & $python.Exe @arguments
    exit $LASTEXITCODE
} catch {
    Write-Host "Установка не завершена: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
