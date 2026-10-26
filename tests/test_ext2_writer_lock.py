# SPDX-License-Identifier: MIT
"""Process-level locking regressions for native ext2 mutations."""

import subprocess
import sys
import time
from pathlib import Path

from mountfile.ext2_native import _image_process_lock


def test_image_lock_serializes_separate_processes(tmp_path):
    image = tmp_path / "image.ext2"
    image.write_bytes(b"x" * 4096)
    marker = tmp_path / "child-acquired-lock"
    script = """
import sys
from mountfile.ext2_native import _image_process_lock
with _image_process_lock(sys.argv[1]):
    open(sys.argv[2], 'w').write('acquired')
"""
    child = None
    try:
        with _image_process_lock(image):
            child = subprocess.Popen(
                [sys.executable, "-c", script, str(image), str(marker)],
                cwd=Path(__file__).resolve().parents[1],
            )
            time.sleep(0.25)
            assert child.poll() is None
            assert not marker.exists(), "second process acquired the held image lock"
        assert child.wait(timeout=5) == 0
        assert marker.read_text() == "acquired"
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait(timeout=5)
