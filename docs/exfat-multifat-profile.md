# Native exFAT multi-FAT profile

The bounded native exFAT provider now uses the validated boot-region `NumberOfFats` and `VolumeFlags` metadata. Bit 0 of `VolumeFlags` selects the active FAT and is exposed as `ExfatBootProfile.active_fat`; the selected copy is used for chain traversal and allocation checks. The boot and backup regions must agree on geometry, including `NumberOfFats`; status fields such as dirty/media-status reporting may be stale in the backup copy.

Read and write access remains fail-closed: the caller must provide an explicit volume extent, all ranges are checked against that extent, reserved boot flags and invalid active-FAT selections are rejected, and a volume reporting media failure is not writable. A two-FAT write mirrors every ordinary FAT update to both declared copies, while the selected active copy remains authoritative for reads. Existing one-FAT images retain their previous behavior.

File streams honor the exFAT NoFatChain flag. Contiguous streams are read directly from the cluster heap without interpreting FAT entries; ordinary streams are bounded by the cluster count and reject cycles, invalid links, overlong chains, and cross-linked ownership. The allocation bitmap is authoritative for stream allocation, while ordinary free-cluster allocation also requires a zero active-FAT entry. Entry-set checksums, filename hashes, Unicode up-case collisions, `ValidDataLength`/`DataLength`, and directory stream lengths are validated before mutation.

Writes are staged in a same-directory temporary image, read back through the native reader, and published only after source identity and selected-volume bounds are rechecked. Unsupported metadata layouts and inconsistent mandatory system entries refuse before the source image is changed.
