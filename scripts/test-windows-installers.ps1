# SPDX-License-Identifier: MIT
# Mock-only tests: no Windows features, downloads, drivers or WSL are changed.
param([string]$EvidencePath)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$files = @('scripts/install-windows.ps1', 'scripts/install-windows-components.ps1', 'mountfile/data/install-windows-components.ps1')
foreach ($relative in $files) {
    $tokens = $null; $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $root $relative), [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw "PowerShell parse failure: $relative`: $errors" }
}
if ((Get-FileHash (Join-Path $root $files[1])).Hash -ne (Get-FileHash (Join-Path $root $files[2])).Hash) {
    throw 'The source and packaged component scripts differ.'
}
# Evaluate only function definitions, never the installer's entry point.
$definitions = $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $false)
foreach ($definition in $definitions) { Invoke-Expression $definition.Extent.Text }
$RebootCode = 3010
$Distro = 'MountFileTools'
$script:assertions = @()
function Assert-Equal($Actual, $Expected, [string]$Label) {
    if ($Actual -ne $Expected) {
        $script:assertions += [pscustomobject]@{ id=$Label; status='failed' }
        throw "$Label`: expected $Expected, got $Actual"
    }
    $script:assertions += [pscustomobject]@{ id=$Label; status='pass' }
}
function Test-Admin { return $true }
function Get-SystemExe([string]$Name) { return 'mock-wsl' }
$script:state = 'Disabled'
$script:updates = 0
function Get-WindowsOptionalFeature { return @{State=$script:state} }
function Enable-WindowsOptionalFeature { return @{RestartNeeded=$true} }
function mock-wsl {
    param([Parameter(ValueFromRemainingArguments=$true)][object[]]$Arguments)
    $script:updates++
    $global:LASTEXITCODE = $script:wslExit
    Write-Output 'mock WSL output'
}
$script:wslExit = 0
Assert-Equal (Install-WSLFeatures) 3010 'Reboot is not reported as success'
Assert-Equal $script:updates 0 'No WSL update before reboot'
$script:state = 'Enabled'
Assert-Equal (Install-WSLFeatures) 0 'Enabled features install runtime'
Assert-Equal $script:updates 1 'Runtime update was invoked once'
$script:wslExit = 42
$failed = $false
try { Install-WSLFeatures | Out-Null } catch { $failed = $true }
Assert-Equal $failed $true 'Runtime failure propagates'
$script:wslExit = 0
# A ready default distribution avoids feature changes/imports entirely.
function Test-Path { return $true }
function Test-WSLTools { return $true }
$before = $script:updates
Assert-Equal (Install-ExtTools) 0 'Existing debugfs is reused'
Assert-Equal $script:updates $before 'Ready WSL performs no update'
# CLI without a registered driver does not pass ImDisk readiness.
function Find-ImDisk { return 'mock-imdisk.exe' }
function Get-Service { return $null }
Assert-Equal (Test-ImDiskReady) $false 'Missing driver fails readiness'
function Get-Service { return @{Name='imdisk'} }
Assert-Equal (Test-ImDiskReady) $true 'CLI and registered driver pass readiness'
if ($EvidencePath) {
    $evidenceFiles = @($files + 'scripts/test-windows-installers.ps1')
    $hashes = @()
    foreach ($relative in $evidenceFiles) {
        $hashes += [pscustomobject]@{
            path = $relative
            sha256 = (Get-FileHash (Join-Path $root $relative) -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    }
    $report = [ordered]@{
        schema = 'mountfile.platform-adapter-evidence/v1'
        adapter = 'windows-installer-mock-suite'
        runner = @{
            platform = 'windows'
            powershell = $PSVersionTable.PSVersion.ToString()
            os = [System.Environment]::OSVersion.VersionString
        }
        verdict = 'pass'
        checks = @($script:assertions)
        check_count = $script:assertions.Count
        system_changes_performed = $false
        files = $hashes
    }
    $report | ConvertTo-Json -Depth 6 | Set-Content -Path $EvidencePath -Encoding UTF8
}
Write-Output "PowerShell parser and $($script:assertions.Count) mock assertions passed. No system changes performed."
