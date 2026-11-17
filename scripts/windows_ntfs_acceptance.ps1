# SPDX-License-Identifier: MIT
<#
.SYNOPSIS
  Build a disposable NTFS VHDX and collect independent Windows checker evidence.

This is intentionally an acceptance harness, not a replacement for the
production replay executor. It uses Windows' own chkdsk against a real mounted
NTFS volume and fails closed when the runner cannot attach a VHD or run the
checker. The resulting JSON distinguishes clean, repaired and unavailable
states; no chkdsk result is silently converted into NTFS replay parity.
#>
[CmdletBinding()]
param(
    [string]$OutputDirectory = (Join-Path $env:RUNNER_TEMP "mountfile-ntfs-windows"),
    [switch]$KeepVhd
)

$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$vhd = Join-Path $OutputDirectory "mountfile-ntfs-acceptance.vhdx"
$diskpartScript = Join-Path $OutputDirectory "diskpart.txt"
$reportPath = Join-Path $OutputDirectory "windows-ntfs-acceptance.json"
$drive = "R:"
$report = [ordered]@{
    schema = "mountfile.windows-ntfs-acceptance/v1"
    checker = "chkdsk"
    checker_authority = "windows_native"
    status = "unavailable"
    vhd = $vhd
    drive = $drive
    stages = @()
    claims = @(
        "This artifact proves only the observed Windows checker result for this disposable VHDX."
        "It does not prove NTFS replay parity or Windows crash-fixture equivalence."
    )
}

function Invoke-Checked([string]$File, [string[]]$Arguments, [string]$Name) {
    $output = & $File @Arguments 2>&1 | Out-String
    $code = $LASTEXITCODE
    $stage = [ordered]@{
        name = $Name
        command = "$File $($Arguments -join ' ')"
        exit_code = $code
        output = $output.Trim()
    }
    $script:report.stages += $stage
    if ($code -ne 0) {
        throw "$Name failed with exit code $code`n$output"
    }
    return $output
}

function Invoke-Diskpart([string[]]$Lines, [string]$Name) {
    $Lines | Set-Content -LiteralPath $diskpartScript -Encoding ASCII
    return Invoke-Checked "diskpart.exe" @("/s", $diskpartScript) $Name
}

try {
    if (-not (Get-Command chkdsk.exe -ErrorAction SilentlyContinue)) {
        throw "chkdsk.exe is unavailable"
    }
    if (-not (Get-Command diskpart.exe -ErrorAction SilentlyContinue)) {
        throw "diskpart.exe is unavailable"
    }

    Invoke-Diskpart @(
        "create vdisk file=$vhd maximum=65536 type=expandable"
        "select vdisk file=$vhd"
        "attach vdisk"
        "create partition primary"
        "format fs=ntfs quick label=MountFileAcceptance"
        "assign letter=R"
    ) "create-and-mount-vhd"

    $disk = Get-DiskImage -ImagePath $vhd | Get-Disk
    $partition = Get-Partition -DiskNumber $disk.Number |
        Where-Object { $_.DriveLetter -eq "R" } | Select-Object -First 1
    if ($null -eq $partition) { throw "cannot resolve mounted NTFS partition" }
    $sectorSize = [int64]$disk.PhysicalSectorSize
    if ($sectorSize -lt 512) { throw "cannot resolve physical disk sector size" }
    $device = "\\.\PhysicalDrive$($disk.Number)"
    $rawBefore = Join-Path $OutputDirectory "raw-before"
    $rawAfter = Join-Path $OutputDirectory "raw-after"
    & python scripts/windows_ntfs_raw_snapshot.py `
        --device $device --offset ([int64]$partition.Offset) `
        --volume-size ([int64]$partition.Size) `
        --sector-size $sectorSize --out $rawBefore
    if ($LASTEXITCODE -ne 0) { throw "raw pre-repair NTFS snapshot failed" }
    $report.raw_device = $device
    $report.raw_partition_offset = [int64]$partition.Offset
    $report.raw_partition_size = [int64]$partition.Size
    $report.raw_physical_sector_size = $sectorSize

    Set-Content -LiteralPath "$drive\before.txt" -Value "mountfile-windows-acceptance"
    $beforeHash = (Get-FileHash -Algorithm SHA256 "$drive\before.txt").Hash
    $scanBefore = & chkdsk.exe $drive /scan 2>&1 | Out-String
    $scanBeforeCode = $LASTEXITCODE
    $report.stages += [ordered]@{
        name = "chkdsk-scan-clean-volume"
        command = "chkdsk $drive /scan"
        exit_code = $scanBeforeCode
        output = $scanBefore.Trim()
    }
    if ($scanBeforeCode -ne 0) { throw "clean NTFS VHDX failed chkdsk /scan" }

    # Simulate an interrupted/unclean lifecycle at the volume level. This is
    # deliberately not called a replay fixture: no fabricated $LogFile record
    # is accepted as equivalent to a real power-loss capture.
    Invoke-Checked "fsutil.exe" @("dirty", "set", $drive) "mark-volume-dirty"
    $dirtyScan = & chkdsk.exe $drive /scan 2>&1 | Out-String
    $dirtyScanCode = $LASTEXITCODE
    $report.stages += [ordered]@{
        name = "chkdsk-scan-dirty-volume"
        command = "chkdsk $drive /scan"
        exit_code = $dirtyScanCode
        output = $dirtyScan.Trim()
    }

    $repair = & chkdsk.exe $drive /f 2>&1 | Out-String
    $repairCode = $LASTEXITCODE
    $report.stages += [ordered]@{
        name = "chkdsk-repair-dirty-volume"
        command = "chkdsk $drive /f"
        exit_code = $repairCode
        output = $repair.Trim()
    }
    if ($repairCode -ne 0) { throw "chkdsk /f failed" }

    $scanAfter = & chkdsk.exe $drive /scan 2>&1 | Out-String
    $scanAfterCode = $LASTEXITCODE
    $report.stages += [ordered]@{
        name = "chkdsk-scan-after-repair"
        command = "chkdsk $drive /scan"
        exit_code = $scanAfterCode
        output = $scanAfter.Trim()
    }
    if ($scanAfterCode -ne 0) { throw "repaired NTFS VHDX failed chkdsk /scan" }

    & python scripts/windows_ntfs_raw_snapshot.py `
        --device $device --offset ([int64]$partition.Offset) `
        --volume-size ([int64]$partition.Size) `
        --sector-size $sectorSize --out $rawAfter
    if ($LASTEXITCODE -ne 0) { throw "raw post-repair NTFS snapshot failed" }
    $comparison = Join-Path $OutputDirectory "raw-snapshot-comparison.json"
    & python scripts/compare_ntfs_snapshots.py `
        --before $rawBefore --after $rawAfter --out $comparison
    if ($LASTEXITCODE -ne 0) { throw "raw NTFS snapshot comparison failed" }
    $report.raw_snapshot_comparison = Get-Content $comparison | ConvertFrom-Json

    $afterHash = (Get-FileHash -Algorithm SHA256 "$drive\before.txt").Hash
    if ($beforeHash -ne $afterHash) { throw "sentinel hash changed during checker lifecycle" }
    $report.sentinel_sha256_before = $beforeHash
    $report.sentinel_sha256_after = $afterHash
    $report.dirty_scan_exit_code = $dirtyScanCode
    $report.status = "checked_and_repaired"
}
catch {
    $report.status = "blocked"
    $report.error = $_.Exception.Message
    $report.error_type = $_.Exception.GetType().FullName
    $report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $reportPath -Encoding UTF8
    throw
}
finally {
    try {
        if (Get-Volume -DriveLetter R -ErrorAction SilentlyContinue) {
            Invoke-Diskpart @(
                "select vdisk file=$vhd"
                "detach vdisk"
            ) "detach-vhd"
        }
    } catch {
        $report.cleanup_error = $_.Exception.Message
    }
    $report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $reportPath -Encoding UTF8
    if (-not $KeepVhd -and (Test-Path $vhd)) {
        Remove-Item -Force $vhd -ErrorAction SilentlyContinue
    }
}
Write-Output ($report | ConvertTo-Json -Depth 8)
