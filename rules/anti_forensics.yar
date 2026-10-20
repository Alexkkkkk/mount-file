/*
   rules/anti_forensics.yar — built-in anti-forensic detection rules
   (Task 1.3, issue #87).

   Targets tooling whose presence on an image is itself evidence: wiping
   utilities, timestomping tools, event-log clearing, and the commands that
   produce them.  Read-only detections.

   Usage:
       python -m mountfile yara-scan dump.img --rules rules/ --json
*/

rule AntiForensics_CCleaner
{
    meta:
        author = "mount-file"
        description = "CCleaner / secure-deletion artefact"
        category = "anti_forensics"
        severity = "medium"
    strings:
        $name = "CCleaner" nocase
        $recuva = "Recuva" nocase
        $wipe = "Wipe Free Space" nocase
    condition:
        any of them
}

rule AntiForensics_TimestompTools
{
    meta:
        author = "mount-file"
        description = "Timestamp manipulation tooling or command"
        category = "anti_forensics"
        severity = "high"
    strings:
        $setmace = "SetMace" nocase
        $timestomp = "timestomp" nocase
        $nircmd = "nircmd" nocase
        $touch_h = "touch -t" nocase
        $touch_d = "touch -d" nocase
    condition:
        any of them
}

rule AntiForensics_EventLogClearing
{
    meta:
        author = "mount-file"
        description = "Windows event log clearing or deletion"
        category = "anti_forensics"
        severity = "high"
    strings:
        $wevt = "wevtutil cl" nocase
        $psclear = "Clear-EventLog" nocase
        $del_log = /del[ \t]+["']?%SystemRoot%\\System32\\winevt\\Logs/i
        $auditpol = "auditpol /clear" nocase
    condition:
        any of them
}

rule AntiForensics_SecureDelete
{
    meta:
        author = "mount-file"
        description = "Secure deletion / shredding utilities"
        category = "anti_forensics"
        severity = "medium"
    strings:
        $sdelete = "SDelete" nocase
        $eraser = "Eraser Setup" nocase
        $shred = "shred -" nocase
        $bleachbit = "BleachBit" nocase
    condition:
        any of them
}
