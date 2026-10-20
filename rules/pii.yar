/*
   rules/pii.yar — built-in PII detection rules (Task 1.3, issue #87).

   Deliberately conservative: these rules look for *structured* identifiers
   (card numbers, IBAN, SSN, private keys) so that a scan over a whole evidence
   image does not drown the analyst in false positives.  They are read-only
   detections; no file content is ever transmitted anywhere.

   Usage:
       python -m mountfile yara-scan dump.img --rules rules/ --json
*/

rule PII_CreditCard_Visa_Mastercard
{
    meta:
        author = "mount-file"
        description = "Payment card number (Visa / Mastercard / Discover) with separators"
        category = "pii"
        severity = "high"
    strings:
        $cc1 = /4[0-9]{3}[ -]?[0-9]{4}[ -]?[0-9]{4}[ -]?[0-9]{4}/
        $cc2 = /5[1-5][0-9]{2}[ -]?[0-9]{4}[ -]?[0-9]{4}[ -]?[0-9]{4}/
        $cc3 = /6(011|5[0-9]{2})[ -]?[0-9]{4}[ -]?[0-9]{4}[ -]?[0-9]{4}/
    condition:
        any of them
}

rule PII_IBAN
{
    meta:
        author = "mount-file"
        description = "International Bank Account Number"
        category = "pii"
        severity = "medium"
    strings:
        $iban = /[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}/
    condition:
        $iban
}

rule PII_US_SSN
{
    meta:
        author = "mount-file"
        description = "US Social Security Number in dashed form"
        category = "pii"
        severity = "high"
    strings:
        $ssn = /[0-9]{3}-[0-9]{2}-[0-9]{4}/
    condition:
        $ssn
}

rule PII_PrivateKeyPEM
{
    meta:
        author = "mount-file"
        description = "PEM-encoded private key material left on disk"
        category = "pii"
        severity = "high"
    strings:
        $rsa = "-----BEGIN RSA PRIVATE KEY-----"
        $ec = "-----BEGIN EC PRIVATE KEY-----"
        $pkcs8 = "-----BEGIN PRIVATE KEY-----"
        $openssh = "-----BEGIN OPENSSH PRIVATE KEY-----"
    condition:
        any of them
}
