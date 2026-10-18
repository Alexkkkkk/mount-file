import pytest

from mountfile.universal_fs_rw import UniversalFSRW


def test_facade_uses_package_detector_instead_of_missing_module(tmp_path):
    image = tmp_path / "not-an-image.bin"
    image.write_bytes(b"not a filesystem")
    with pytest.raises(RuntimeError, match="no filesystem detected"):
        UniversalFSRW(image, auto_backup=False)
