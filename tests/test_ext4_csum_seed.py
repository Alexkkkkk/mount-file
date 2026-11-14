# SPDX-License-Identifier: MIT
"""ext4 ``metadata_csum_seed`` profiles must stay readable.

e2fsprogs 1.47.2 (the CI runner default) enables ``metadata_csum_seed`` on every
newly formatted ext4 filesystem. Rejecting the flag made the native reader refuse
healthy images, so the seed is now parsed from ``s_checksum_seed`` and used for
inode, directory and group-descriptor checksums.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from mountfile.ext3_reader import NativeExt4Image
from mountfile.ext_probe import _INCOMPAT_CSUM_SEED, probe_ext_superblock


@pytest.mark.skipif(
    not shutil.which("mkfs.ext4"),
    reason="e2fsprogs mkfs.ext4 is required",
)
def test_native_ext4_reads_metadata_csum_seed_profile(tmp_path):
    image = tmp_path / "seeded.img"
    with image.open("wb") as stream:
        stream.truncate(32 * 1024**2)
    subprocess.run(
        [
            shutil.which("mkfs.ext4"),
            "-F",
            "-q",
            "-O",
            "metadata_csum_seed,^fast_commit,^orphan_file,^sparse_super2",
            str(image),
        ],
        check=True,
        capture_output=True,
    )

    profile = probe_ext_superblock(image)
    if not profile.feature_incompat & _INCOMPAT_CSUM_SEED:
        pytest.skip("this e2fsprogs build did not enable metadata_csum_seed")
    assert profile.checksum_seed is not None

    source = tmp_path / "payload.bin"
    source.write_bytes(b"seeded metadata payload")
    debugfs = shutil.which("debugfs")
    if debugfs is None:
        pytest.skip("e2fsprogs debugfs is required to seed a file")
    subprocess.run(
        [debugfs, "-w", "-R", f"write {source} /payload.bin", str(image)],
        check=True,
        capture_output=True,
    )

    reader = NativeExt4Image(image, volume_size=image.stat().st_size)
    try:
        assert reader.read_file("/payload.bin") == source.read_bytes()
    finally:
        reader.close()
