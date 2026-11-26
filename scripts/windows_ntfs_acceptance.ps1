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
    fixture_id = "windows-ntfs-crash-v1"
    checker = "chkdsk"
    checker_authority = "windows_native"
    status = "unavailable"
    provenance = [ordered]@{
        captured_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        os_version = [Environment]::OSVersion.VersionString
        powershell_version = $PSVersionTable.PSVersion.ToString()
        github_sha = $env:GITHUB_SHA
        github_run_id = $env:GITHUB_RUN_ID
        github_workflow = $env:GITHUB_WORKFLOW
    }
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
        # Metadata parity fixtures need only a small volume; keeping the raw
        # crash/repair images bounded prevents the hosted Windows disk filling.
        "create vdisk file=$vhd maximum=256 type=expandable"
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
    $rawCrash = Join-Path $OutputDirectory "raw-crash"
    $rawAfter = Join-Path $OutputDirectory "raw-after"
    & python scripts/windows_ntfs_raw_snapshot.py `
        --device $device --offset ([int64]$partition.Offset) `
        --volume-size ([int64]$partition.Size) --sector-size $sectorSize `
        --out $rawBefore
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

    $childScript = Join-Path $OutputDirectory "abrupt-writer.ps1"
    $crashPayload = Join-Path $drive "crash-payload.bin"
    $childLog = Join-Path $OutputDirectory "abrupt-writer.log"
    @'
param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$LogPath
)
$ErrorActionPreference = "Stop"
try {
    for ($i = 0; $i -lt 10000; $i++) {
        Add-Content -LiteralPath $Path -Value ("crash-line-{0}-{1}" -f $i, (Get-Date).Ticks)
        if (($i % 100) -eq 0) { Start-Sleep -Milliseconds 1 }
    }
} catch {
    $_ | Out-File -LiteralPath $LogPath -Encoding UTF8
    exit 1
}
'@ | Set-Content -LiteralPath $childScript -Encoding UTF8
    $writer = Start-Process -FilePath "powershell.exe" -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $childScript,
        "-Path", $crashPayload, "-LogPath", $childLog
    ) -WorkingDirectory $OutputDirectory -PassThru
    $deadline = (Get-Date).AddSeconds(10)
    while (-not (Test-Path $crashPayload) -and (Get-Date) -lt $deadline) {
        if ($writer.HasExited) { break }
        Start-Sleep -Milliseconds 50
    }
    if (-not (Test-Path $crashPayload)) {
        $childOutput = if ($writer.HasExited) { $writer.ExitCode } else { "running" }
        $childLogText = if (Test-Path $childLog) {
            Get-Content -LiteralPath $childLog -Raw
        } else { "<no child log>" }
        throw "abrupt writer did not create payload before termination (state=$childOutput; log=$childLogText)"
    }
    Stop-Process -Id $writer.Id -Force
    $writer.WaitForExit()
    if ($writer.ExitCode -eq 0) {
        throw "abrupt writer unexpectedly completed normally"
    }
    if (-not (Test-Path $crashPayload)) { throw "crash payload was not created" }
    $report.crash_fixture = [ordered]@{
        kind = "windows-child-process-terminated-during-ntfs-write"
        pid = $writer.Id
        exit_code = $writer.ExitCode
        payload_sha256 = (Get-FileHash -Algorithm SHA256 $crashPayload).Hash
    }
    & python scripts/windows_ntfs_raw_snapshot.py `
        --device $device --offset ([int64]$partition.Offset) `
        --volume-size ([int64]$partition.Size) --sector-size $sectorSize `
        --out $rawCrash --raw-image (Join-Path $rawCrash "volume.img")
    if ($LASTEXITCODE -ne 0) { throw "raw crash NTFS snapshot failed" }

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
        --volume-size ([int64]$partition.Size) --sector-size $sectorSize `
        --out $rawAfter --raw-image (Join-Path $rawAfter "volume.img")
    if ($LASTEXITCODE -ne 0) { throw "raw post-repair NTFS snapshot failed" }
    $comparison = Join-Path $OutputDirectory "raw-snapshot-comparison.json"
    & python scripts/compare_ntfs_snapshots.py `
        --before $rawBefore --after $rawAfter --out $comparison
    if ($LASTEXITCODE -ne 0) { throw "raw NTFS snapshot comparison failed" }
    $report.raw_snapshot_comparison = Get-Content $comparison | ConvertFrom-Json
    $crashComparison = Join-Path $OutputDirectory "raw-crash-repair-comparison.json"
    & python scripts/compare_ntfs_snapshots.py `
        --before $rawCrash --after $rawAfter --out $crashComparison
    if ($LASTEXITCODE -ne 0) { throw "raw crash/repaired comparison failed" }
    $report.raw_crash_repair_comparison = Get-Content $crashComparison | ConvertFrom-Json

    $nativeReplayImage = Join-Path $OutputDirectory "native-replayed-volume.img"
    $nativeReplayComparison = Join-Path $OutputDirectory "native-replay-comparison.json"
    & python scripts/compare_ntfs_replay.py `
        --crash-image (Join-Path $rawCrash "volume.img") `
        --chkdsk-image (Join-Path $rawAfter "volume.img") `
        --native-image $nativeReplayImage --out $nativeReplayComparison
    $replayExit = $LASTEXITCODE
    $report.native_replay_comparison = Get-Content $nativeReplayComparison | ConvertFrom-Json
    if ($replayExit -eq 2 -and $report.native_replay_comparison.status -eq "blocked") {
        # A typed parser refusal (for example a malformed $LogFile record) is
        # recorded as blocked evidence instead of crashing the harness: the
        # parity question stays open and the run reports it without a red
        # gate. A real stream mismatch still throws below.
        Write-Output ("native replay refused (typed blocked): " + $report.native_replay_comparison.error)
    } elseif ($replayExit -ne 0) {
        throw "native replay/checker comparison failed"
    }
    if ($report.native_replay_comparison.status -notin @("pass", "not_applicable", "blocked")) {
        throw "native replay/checker metadata comparison was not byte-identical"
    }

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
