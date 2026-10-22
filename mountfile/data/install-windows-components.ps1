# SPDX-License-Identifier: MIT
# Windows PowerShell 5.1+. No Python required; also shipped in the frozen EXE.
[CmdletBinding()]
param(
    [switch]$SkipExt,
    [switch]$SkipImDisk,
    [switch]$DryRun,
    [switch]$EnableWSLOnly,
    [switch]$InstallImDiskOnly
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Distro = 'MountFileTools'
$RootfsBase = 'https://cloud-images.ubuntu.com/wsl/jammy/current'
$ImDiskUrl = 'https://static.ltr-data.se/files/imdiskinst.exe'
$RebootCode = 3010

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$identity).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}
function Get-SystemExe([string]$Name) {
    $directory = Join-Path $env:SystemRoot 'System32'
    if ([Environment]::Is64BitOperatingSystem -and -not [Environment]::Is64BitProcess) {
        $directory = Join-Path $env:SystemRoot 'Sysnative'
    }
    return Join-Path $directory $Name
}
function Invoke-Elevated([string]$Mode) {
    # Only machine-level operations are elevated. WSL registration stays in the
    # original Windows user's profile even if UAC requires another admin account.
    $ps = Get-SystemExe 'WindowsPowerShell\v1.0\powershell.exe'
    $arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -{1}' -f $PSCommandPath, $Mode
    $process = Start-Process -FilePath $ps -ArgumentList $arguments -Verb RunAs -Wait -PassThru
    return $process.ExitCode
}
function Install-WSLFeatures {
    if (-not (Test-Admin)) { throw 'Для включения WSL необходимы права администратора.' }
    $restart = $false
    foreach ($name in @('Microsoft-Windows-Subsystem-Linux', 'VirtualMachinePlatform')) {
        $feature = Get-WindowsOptionalFeature -Online -FeatureName $name
        if ($feature.State -eq 'EnablePending') { $restart = $true; continue }
        if ($feature.State -ne 'Enabled') {
            Write-Host "Включение компонента Windows: $name"
            $result = Enable-WindowsOptionalFeature -Online -FeatureName $name -All -NoRestart
            if ($result.RestartNeeded) { $restart = $true }
        }
    }
    if ($restart) { return $RebootCode }
    # Install/update the WSL runtime and kernel too: enabling Windows features
    # alone is insufficient for a fresh WSL2 installation.
    $wsl = Get-SystemExe 'wsl.exe'
    & $wsl --update --web-download | Out-Host
    if ($LASTEXITCODE -eq $RebootCode) { return $RebootCode }
    if ($LASTEXITCODE -ne 0) { throw "Установка WSL runtime: код $LASTEXITCODE" }
    return 0
}
function Find-ImDisk {
    $cmd = Get-Command 'imdisk.exe' -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidate = Get-SystemExe 'imdisk.exe'
    if (Test-Path -LiteralPath $candidate) { return $candidate }
    return $null
}
function Test-ImDiskReady {
    if (-not (Find-ImDisk)) { return $false }
    return $null -ne (Get-Service -Name 'imdisk' -ErrorAction SilentlyContinue)
}
function Install-ImDisk {
    if (Test-ImDiskReady) { Write-Host 'ImDisk уже установлен.'; return 0 }
    if (-not (Test-Admin)) { return (Invoke-Elevated 'InstallImDiskOnly') }
    $work = Join-Path ([IO.Path]::GetTempPath()) ('mountfile-imdisk-' + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $work | Out-Null
    $oldSilent = $env:IMDISK_SILENT_SETUP
    try {
        $setup = Join-Path $work 'imdiskinst.exe'
        Write-Host 'Загрузка ImDisk с официального сайта автора...'
        Invoke-WebRequest -UseBasicParsing -Uri $ImDiskUrl -OutFile $setup
        $signature = Get-AuthenticodeSignature -FilePath $setup
        if ($signature.Status -ne 'Valid' -or
            $signature.SignerCertificate.Subject -notmatch '(Olof Lagerkvist|LTR Data|LAGERKVIST TEKNISK)') {
            throw 'Проверка подписи или издателя ImDisk не пройдена. Файл не запущен.'
        }
        $env:IMDISK_SILENT_SETUP = '1'
        $process = Start-Process -FilePath $setup -ArgumentList '-y' -WorkingDirectory $work -Wait -PassThru
        if ($process.ExitCode -eq $RebootCode) { return $RebootCode }
        if ($process.ExitCode -ne 0) { throw "Установщик ImDisk: код $($process.ExitCode)" }
        if (-not (Test-ImDiskReady)) { throw 'Установщик завершился, но ImDisk CLI/драйвер не обнаружен.' }
        # Start the driver without attaching an image or formatting a disk.
        Start-Service -Name 'imdisk'
        Write-Host 'ImDisk CLI и драйвер установлены.'
        return 0
    } finally {
        $env:IMDISK_SILENT_SETUP = $oldSilent
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    }
}
function Test-WSLTools([string]$Wsl, [string]$Distribution = '') {
    $arguments = @()
    if ($Distribution) { $arguments += @('--distribution', $Distribution) }
    $arguments += @('--exec', 'sh', '-lc', 'command -v debugfs >/dev/null && command -v wslpath >/dev/null')
    try { & $Wsl @arguments 2>$null | Out-Null } catch { return $false }
    return $LASTEXITCODE -eq 0
}
function Install-ExtTools {
    if (Get-Command 'debugfs.exe' -ErrorAction SilentlyContinue) {
        Write-Host 'Нативный debugfs.exe уже доступен.'; return 0
    }
    $wsl = Get-SystemExe 'wsl.exe'
    if ((Test-Path -LiteralPath $wsl) -and (Test-WSLTools $wsl)) {
        Write-Host 'debugfs доступен в текущем WSL-дистрибутиве.'; return 0
    }
    if (Test-Path -LiteralPath $wsl) {
        # Older MountFile EXEs only look in the default distro. Install the
        # requested package there when it is already a working Debian/Ubuntu.
        $hasApt = $false
        try {
            & $wsl --user root --exec sh -lc 'command -v apt-get >/dev/null' 2>$null | Out-Null
            $hasApt = $LASTEXITCODE -eq 0
        } catch { $hasApt = $false }
        if ($hasApt) {
            Write-Host 'Установка e2fsprogs в существующий Debian/Ubuntu WSL...'
            & $wsl --user root --exec sh -lc 'export DEBIAN_FRONTEND=noninteractive; apt-get update && apt-get install -y --no-install-recommends e2fsprogs' | Out-Host
            if ($LASTEXITCODE -ne 0 -or -not (Test-WSLTools $wsl)) {
                throw 'Не удалось установить или проверить e2fsprogs в существующем WSL.'
            }
            Write-Host 'debugfs в текущем WSL готов; поддерживается и старый MountFile EXE.'
            return 0
        }
        if (Test-WSLTools $wsl $Distro) {
            Write-Host "debugfs уже доступен в $Distro."; return 0
        }
    }
    $code = if (Test-Admin) { Install-WSLFeatures } else { Invoke-Elevated 'EnableWSLOnly' }
    if ($code -eq $RebootCode) { return $RebootCode }
    if ($code -ne 0) { throw "Включение компонентов WSL: код $code" }
    if (-not (Test-Path -LiteralPath $wsl)) { throw 'wsl.exe не найден после включения компонентов Windows.' }
    # Inbox WSL works with --import and does not require Store or creating a
    # Linux account interactively. Existing Debian/Ubuntu gets e2fsprogs; otherwise a dedicated distro is imported.
    $listed = & $wsl --list --quiet 2>$null
    $listCode = $LASTEXITCODE
    $names = @($listed | ForEach-Object { ($_ -replace "`0", '').Trim() } | Where-Object { $_ })
    if ($listCode -ne 0) { throw 'WSL не готов. Перезагрузите Windows, повторите установку; проверьте виртуализацию.' }
    if ($Distro -notin $names) {
        $arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64') { 'arm64' } else { 'amd64' }
        if (-not [Environment]::Is64BitOperatingSystem) { throw 'Для автоматической установки WSL нужен 64-битный Windows.' }
        $file = "ubuntu-jammy-wsl-$arch-ubuntu22.04lts.rootfs.tar.gz"
        $work = Join-Path ([IO.Path]::GetTempPath()) ('mountfile-wsl-' + [guid]::NewGuid())
        New-Item -ItemType Directory -Path $work | Out-Null
        try {
            $archive = Join-Path $work $file
            $sums = Join-Path $work 'SHA256SUMS'
            Write-Host "Загрузка Ubuntu rootfs ($arch, около 325 МБ), проверка SHA-256..."
            Invoke-WebRequest -UseBasicParsing -Uri "$RootfsBase/SHA256SUMS" -OutFile $sums
            $line = Get-Content -LiteralPath $sums | Where-Object { $_ -match ('^[a-fA-F0-9]{64}\s+\*?' + [regex]::Escape($file) + '$') } | Select-Object -First 1
            if (-not $line) { throw "Нет официального SHA-256 для $file" }
            Invoke-WebRequest -UseBasicParsing -Uri "$RootfsBase/$file" -OutFile $archive
            $expected = ($line -split '\s+')[0]
            if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $expected) {
                throw 'SHA-256 Ubuntu rootfs не совпал. Импорт не выполнен.'
            }
            $location = Join-Path $env:LOCALAPPDATA 'MountFile\wsl\MountFileTools'
            # Never import over existing/partial files; keep them for diagnosis.
            if (Test-Path -LiteralPath $location) {
                if (Get-ChildItem -LiteralPath $location -Force | Select-Object -First 1) {
                    throw "Каталог $location уже содержит файлы. Проверьте частичную установку вручную."
                }
            } else { New-Item -ItemType Directory -Path $location -Force | Out-Null }
            & $wsl --import $Distro $location $archive --version 2 | Out-Host
            if ($LASTEXITCODE -ne 0) { throw "Импорт $Distro не удался. Проверьте поддержку WSL2 и виртуализацию в BIOS/UEFI." }
        } finally {
            Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    if (-not (Test-WSLTools $wsl $Distro)) {
        Write-Host "Установка e2fsprogs в $Distro..."
        & $wsl --distribution $Distro --user root --exec sh -lc 'export DEBIAN_FRONTEND=noninteractive; apt-get update && apt-get install -y --no-install-recommends e2fsprogs' | Out-Host
        if ($LASTEXITCODE -ne 0) { throw 'Установка e2fsprogs не удалась. Проверьте сеть и повторите установку.' }
    }
    if (-not (Test-WSLTools $wsl $Distro)) { throw 'Проверка debugfs/wslpath в WSL не пройдена.' }
    Write-Host "debugfs и wslpath доступны в $Distro. Существующие дистрибутивы не изменены."
    return 0
}

$exitCode = 0
try {
    if ([Environment]::OSVersion.Version.Build -lt 19041) {
        throw 'Требуется Windows 10 сборка 19041 или новее либо Windows 11.'
    }
    if ($DryRun) {
        Write-Host 'План установки (без изменений, загрузок и UAC):'
        if (-not $SkipExt) { Write-Host '  Проверить debugfs; при необходимости включить WSL2 и установить Ubuntu/e2fsprogs в MountFileTools.' }
        if (-not $SkipImDisk) { Write-Host '  Проверить ImDisk; загрузить официальный подписанный драйвер и установить его.' }
        exit 0
    }
    if ($EnableWSLOnly) { exit (Install-WSLFeatures) }
    if ($InstallImDiskOnly) { exit (Install-ImDisk) }
    $logDir = Join-Path $env:LOCALAPPDATA 'MountFile\logs'
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $log = Join-Path $logDir ('components-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
    Start-Transcript -Path $log | Out-Null
    Write-Host 'MountFile: установка системных компонентов. Образы и диски не изменяются.'
    # Install ImDisk before WSL so a requested reboot never hides its failure.
    if (-not $SkipImDisk) {
        $code = Install-ImDisk
        if ($code -eq $RebootCode) { $exitCode = $RebootCode }
        elseif ($code -ne 0) { throw "ImDisk: код $code" }
    }
    if (-not $SkipExt) {
        $code = Install-ExtTools
        if ($code -eq $RebootCode) { $exitCode = $RebootCode }
        elseif ($code -ne 0) { throw "WSL: код $code" }
    }
    if ($exitCode -eq $RebootCode) {
        Write-Host 'Нужна перезагрузка Windows. Перезагрузите компьютер самостоятельно и повторно запустите установку.' -ForegroundColor Yellow
    } else {
        Write-Host 'Проверка компонентов пройдена. Перезапустите MountFile и заново откройте образ.' -ForegroundColor Green
        Write-Host 'Для записи через ImDisk запускайте MountFile от имени администратора.'
    }
    Write-Host "Журнал: $log"
} catch {
    Write-Host "Установка не завершена: $($_.Exception.Message)" -ForegroundColor Red
    $exitCode = 1
} finally {
    try { Stop-Transcript | Out-Null } catch { }
}
exit $exitCode
