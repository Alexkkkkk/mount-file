# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""mountfile — file manager for raw disk images.

Supports MBR/GPT partition tables, browsing of FAT/NTFS/EXT/... via The
Sleuth Kit (pytsk3), and read-write operations through the safe package RW
facade: native FAT32; ext2/3/4 via e2fsprogs/debugfs; NTFS via ntfs-3g on Linux
or ImDisk plus the Windows filesystem driver on Windows; and exFAT/FAT12/16 via
ImDisk on Windows. Other detected filesystems need an available RW backend.
"""

__version__ = "1.60.2"
