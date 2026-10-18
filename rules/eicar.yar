/*
   eicar.yar — acceptance-test rule for issue #87 (Task 1.3).
   The EICAR test string is harmless and exists precisely so that a scanner can
   prove it is wired up correctly.
*/

rule EICAR_Test_String : test malware
{
    meta:
        description = "Standard EICAR anti-malware test file"
        author = "mount-file"
        reference = "https://www.eicar.org/download-anti-malware-testfile/"
    strings:
        $eicar = "X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
    condition:
        $eicar
}
