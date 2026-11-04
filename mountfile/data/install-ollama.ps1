# SPDX-License-Identifier: MIT
# Windows PowerShell 5.1+. Called only after the user confirms in MountFile.
[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9._:/-]{1,128}$')]
    [string]$Model = 'llama3.2:3b'
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'Continue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$work = Join-Path ([IO.Path]::GetTempPath()) ('mountfile-ollama-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $work | Out-Null
$installer = Join-Path $work 'OllamaSetup.exe'

try {
    Write-Host 'MountFile: downloading the official Ollama for Windows installer.' -ForegroundColor Cyan
    Invoke-WebRequest -UseBasicParsing -Uri 'https://ollama.com/download/OllamaSetup.exe' -OutFile $installer

    Write-Host 'Checking the installer signature...'
    $signature = Get-AuthenticodeSignature -FilePath $installer
    if ($signature.Status -ne 'Valid' -or
        $signature.SignerCertificate.Subject -notmatch '(^|, )O=Ollama Inc\.(,|$)') {
        throw 'The Ollama installer signature or publisher check failed; the file was not run.'
    }

    Write-Host 'Installing Ollama for the current Windows user (no administrator rights required)...'
    $process = Start-Process -FilePath $installer `
        -ArgumentList '/VERYSILENT', '/NORESTART', '/SUPPRESSMSGBOXES' `
        -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "The Ollama installer exited with code $($process.ExitCode)."
    }

    $ollama = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
    if (-not (Test-Path -LiteralPath $ollama)) {
        $command = Get-Command 'ollama.exe' -ErrorAction SilentlyContinue
        if ($command) { $ollama = $command.Source }
    }
    if (-not (Test-Path -LiteralPath $ollama)) {
        throw 'Installation completed, but ollama.exe was not found. Restart Windows or install Ollama manually.'
    }

    Write-Host 'Waiting for the local Ollama API...'
    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        try {
            Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 2 | Out-Null
            $ready = $true
            break
        } catch {
            Start-Sleep -Seconds 2
        }
    }
    if (-not $ready) {
        Write-Host 'The server did not start automatically; starting ollama serve in the background.'
        Start-Process -FilePath $ollama -ArgumentList 'serve' -WindowStyle Hidden | Out-Null
        for ($i = 0; $i -lt 60; $i++) {
            try {
                Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 2 | Out-Null
                $ready = $true
                break
            } catch {
                Start-Sleep -Seconds 2
            }
        }
    }
    if (-not $ready) {
        throw 'Ollama installed, but its local API did not start at http://127.0.0.1:11434.'
    }

    Write-Host "Downloading model $Model. This may take a while and use several gigabytes of disk space." -ForegroundColor Cyan
    & $ollama pull $Model
    if ($LASTEXITCODE -ne 0) {
        throw "ollama pull $Model failed with exit code $LASTEXITCODE. Ollama is installed; retry the model download later."
    }

    Write-Host "Setup complete. Model $Model is ready. Return to MountFile and click 'Проверить и настроить Ollama'." -ForegroundColor Green
} catch {
    Write-Host "Setup failed: $($_.Exception.Message)" -ForegroundColor Red
} finally {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ''
Read-Host 'Press Enter to close this setup window' | Out-Null
