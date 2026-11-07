# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""mountfile — file manager for raw disk images.

Supports MBR/GPT partition tables and bounded in-process read-only providers
for FAT12/16/32, ext2/3/4, exFAT and NTFS.  The optional ``pytsk3`` backend is
an explicit compatibility fallback for unrecognized whole-image filesystems.
Read-write operations use the safe package RW facade and advertise only the
validated native profiles; external tools and OS mount drivers remain
optional compatibility backends rather than prerequisites for native reads.
"""

__version__ = "1.80.0"
