/*
   pii.yar — bundled rule set for issue #87 (Task 1.3).
   Conservative patterns for personal data that must never leave evidence:
   PEM private keys, email addresses, payment-card shapes, IBAN and Russian
   domestic passport numbers.  Every rule carries the `pii` tag so callers can
   filter on tags instead of rule names.

   The regular expressions stay inside the RE2 subset supported by YARA:
   no back-references and no non-capturing groups.
*/

rule PII_PrivateKeyPEM : pii
{
    meta:
        description = "PEM-encoded private key header"
        author = "mount-file"
        reference = "issue #87"
    strings:
        $rsa = "-----BEGIN RSA PRIVATE KEY-----" ascii
        $dsa = "-----BEGIN DSA PRIVATE KEY-----" ascii
        $ec = "-----BEGIN EC PRIVATE KEY-----" ascii
        $pkcs8 = "-----BEGIN PRIVATE KEY-----" ascii
        $openssh = "-----BEGIN OPENSSH PRIVATE KEY-----" ascii
    condition:
        any of them
}

rule PII_Email_Address : pii
{
    meta:
        description = "Email address"
        author = "mount-file"
        reference = "issue #87"
    strings:
        $a = /[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,10}/ ascii
    condition:
        $a
}

rule PII_Payment_Card_Number : pii
{
    meta:
        description = "16-digit payment card number, optional space or dash grouping"
        author = "mount-file"
        reference = "issue #87"
    strings:
        $a = /[0-9]{4}[ -]?[0-9]{4}[ -]?[0-9]{4}[ -]?[0-9]{4}/ ascii
    condition:
        $a
}

rule PII_IBAN : pii
{
    meta:
        description = "IBAN account number"
        author = "mount-file"
        reference = "issue #87"
    strings:
        $a = /[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}/ ascii
    condition:
        $a
}

rule PII_RU_Passport : pii
{
    meta:
        description = "Russian internal passport series and number"
        author = "mount-file"
        reference = "issue #87"
    strings:
        $a = /[0-9]{2} ?[0-9]{2} ?[0-9]{6}/ ascii
    condition:
        $a
}
