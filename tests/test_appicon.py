# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""The bundled app icon must exist in every format a build can ask for."""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import pytest

from mountfile import appicon


def test_icon_files_shipped():
    for name in (appicon.ICON_NAME, appicon.ICO_NAME, appicon.ICNS_NAME):
        path = appicon.data_file(name)
        assert path, f"{name} is missing from mountfile/data"
        assert os.path.getsize(path) > 1000


def test_icon_path_is_the_png_and_resolves():
    path = appicon.icon_path()
    assert path and path.endswith(appicon.ICON_NAME)


def test_icon_file_matches_platform():
    path = appicon.icon_file()
    assert path
    if appicon.sys.platform.startswith("win"):
        assert path.endswith(appicon.ICO_NAME)
    elif appicon.sys.platform == "darwin":
        assert path.endswith(appicon.ICNS_NAME)
    else:
        assert path.endswith(appicon.ICON_NAME)


def test_ico_contains_the_small_sizes():
    """A single-size .ico looks blurry in the taskbar and the explorer."""
    Image = pytest.importorskip("PIL.Image")

    with Image.open(appicon.data_file(appicon.ICO_NAME)) as ico:
        sizes = {s for s in ico.info.get("sizes", set())}
    if not sizes:  # some Pillow builds do not report the directory
        return
    for expected in ((16, 16), (32, 32), (48, 48), (256, 256)):
        assert expected in sizes, f"{expected} missing from icon.ico"


def test_app_icon_is_optional():
    """No Qt / no artwork must return None instead of raising."""
    icon = appicon.app_icon()
    assert icon is None or not icon.isNull()


def test_app_icon_is_none_without_a_running_application():
    """A fresh interpreter has no QApplication, so no QIcon may be built.

    Qt aborts the whole process (``Fatal Python error: Aborted``, exit code
    134) instead of raising when a QIcon is constructed without a live
    application - that is what killed the full pytest run in CI as soon as
    an earlier test's QApplication had been collected. Runs in its own
    interpreter, because a QApplication created by another test in the same
    session would hide the bug.
    """
    code = (
        "from mountfile import appicon; "
        "assert appicon._qt_app_running() is False, 'unexpected QApplication'; "
        "assert appicon.app_icon() is None"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
