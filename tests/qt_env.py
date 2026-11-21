# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Single, correct answer to "can this test process use Qt?".

``pytest.importorskip("PyQt6")`` is not usable here: PyQt6 the *module* can be
installed while importing it still fails, for example on a headless Linux runner
without ``libEGL.so.1``. ``importorskip`` only skips on ModuleNotFoundError, so
that case raised a hard ImportError and failed the whole job instead of skipping
(it is exactly how the ``autofix`` workflow went red on ``main``).

``mountfile.qt_compat`` performs the real probe - it tries PyQt6 and falls back to
PyQt5, and raises when neither can be imported. Importing it is therefore the
honest availability check.
"""

from __future__ import annotations

import pytest

try:  # pragma: no cover - depends on the environment
    import mountfile.qt_compat as _qt_compat  # noqa: F401

    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

needs_qt = pytest.mark.skipif(not HAVE_QT, reason="no usable Qt bindings")
