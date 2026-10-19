# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""The bundled app icon must exist in every format a build can ask for."""

import os

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
