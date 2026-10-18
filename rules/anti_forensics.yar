/*
   anti_forensics.yar — bundled rule set for issue #87 (Task 1.3).
   Flags artefacts that are typically produced when someone tries to defeat an
   investigation: wiping utilities, timestomping utilities and the PowerShell
   history-clearing idioms.  Patterns are deliberately literal so they cannot
   fire on ordinary binaries.
*/

rule AntiForensics_Wiping_Utilities : anti_forensics
{
    meta:
        description = "Names of common secure-deletion / anti-forensic utilities"
        author = "mount-file"
        reference = "issue #87"
    strings:
        $a1 = "cipher.exe /w" nocase
        $a2 = "sdelete" nocase
        $a3 = "SecureDeleteFile" nocase
        $a4 = "EraseFile" nocase
        $a5 = "shred -" nocase
        $a6 = "wipefs" nocase
    condition:
        any of them
}

rule AntiForensics_Timestomp_Utilities : anti_forensics
{
    meta:
        description = "Timestomping command lines and API names"
        author = "mount-file"
        reference = "issue #87"
    strings:
        $a1 = "SetFileTime" ascii
        $a2 = "timestomp" nocase
        $a3 = "Touch.exe -t" nocase
        $a4 = "NtSetInformationFile" ascii
    condition:
        any of them
}

rule AntiForensics_Log_Clearing : anti_forensics
{
    meta:
        description = "Event log and history clearing idioms"
        author = "mount-file"
        reference = "issue #87"
    strings:
        $a1 = "wevtutil cl" nocase
        $a2 = "Clear-EventLog" nocase
        $a3 = "Remove-Item (Get-PSReadlineOption).HistorySavePath" nocase
        $a4 = "HistorySavePath" nocase
    condition:
        any of them
}

rule AntiForensics_Prefetch_Deletion : anti_forensics
{
    meta:
        description = "Prefetch / USN journal tampering"
        author = "mount-file"
        reference = "issue #87"
    strings:
        $a1 = "fsutil usn deletejournal" nocase
        $a2 = "del /f /q C:\\Windows\\Prefetch" nocase
        $a3 = "\\Prefetch\\" nocase
    condition:
        any of them
}
