"""Shared Qt application lifecycle for the test process."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from filelock import FileLock

# Hypothesis profiles: быстрый профиль для локального цикла, насыщенный —
# для CI/ночного прогона. Активный профиль выбирается переменной
# HYPOTHESIS_PROFILE (по умолчанию "dev" — 25 примеров на свойство).
import os

from hypothesis import settings

settings.register_profile("dev", max_examples=25, deadline=None)
settings.register_profile("ci", max_examples=100, deadline=None)
_settings_profile = os.environ.get("HYPOTHESIS_PROFILE", "dev")
settings.load_profile(_settings_profile)


@pytest.fixture(scope="session")
def shared_probe_cache(tmp_path_factory, worker_id):
    """Дорогой ресурс, собираемый ровно один раз на весь прогон.

    Под pytest-xdist каждый воркер имеет свой session-контекст, поэтому
    голый scope="session" выполняет фикстуру N_workers раз. Файловая
    блокировка гарантирует: первый воркер строит, остальные переиспользуют.
    """
    if worker_id == "master":
        return {"built_once": True, "by": "serial"}

    root = tmp_path_factory.getbasetemp().parent
    cache = root / "shared_probe_cache.flag"
    lock = root / "shared_probe_cache.lock"
    with FileLock(str(lock)):
        if cache.exists():
            return {"built_once": True, "by": "reuse"}
        cache.write_text("built")
        return {"built_once": True, "by": "first-worker"}


@pytest.fixture(scope="session", autouse=True)
def _shared_qapplication():
    """Keep one QApplication alive; Qt cannot safely recreate it per module."""
    try:
        from mountfile.qt_compat import QApplication
    except ImportError:
        yield None
        return

    if QApplication is None:
        yield None
        return

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app
