# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Тесты автообновления (mountfile/updater.py).

Сеть не требуется: urlopen подменяется заглушкой, поэтому тесты
работают и без доступа к GitHub (и в том числе проверяют, что при
недоступной сети проверка обновлений не валит приложение).

Run:  python -m pytest tests/test_updater.py -v
  or:  python tests/test_updater.py
"""

import io
import json
import os
import sys
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mountfile import updater


# ---------------------------------------------------------------- заглушки
class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _release(tag, assets=(), body="notes"):
    return {
        "tag_name": tag,
        "html_url": "https://github.com/Alexkkkkk/mount-file/releases/tag/%s" % tag,
        "body": body,
        "assets": [
            {
                "name": name,
                "size": len(payload),
                "browser_download_url": "https://example.invalid/%s" % name,
            }
            for name, payload in assets
        ],
    }


def _fake_urlopen(release, asset_bytes=b"", calls=None):
    def _open(req, timeout=None):
        url = getattr(req, "full_url", req)
        if calls is not None:
            calls.append(url)
        if url.endswith(".sha256"):
            import hashlib

            return _FakeResponse(hashlib.sha256(asset_bytes).hexdigest().encode())
        if "/releases/latest" in url:
            return _FakeResponse(json.dumps(release).encode())
        if url.startswith("https://example.invalid/"):
            return _FakeResponse(asset_bytes)
        raise urllib.error.HTTPError(url, 404, "not found", None, None)

    return _open


# ------------------------------------------------------- сравнение версий
def test_parse_version():
    assert updater.parse_version("v1.10.0") == (1, 10, 0)
    assert updater.parse_version("1.2") == (1, 2)
    assert updater.parse_version("v1.1.1-beta1+build7") == (1, 1, 1)
    assert updater.parse_version("") == (0,)
    assert updater.parse_version("nightly") == (0,)


def test_is_newer_numeric_not_lexicographic():
    assert updater.is_newer("v1.10.0", "1.9.9")
    assert updater.is_newer("1.2.0", "1.1.1")
    assert updater.is_newer("2.0", "1.99.99")
    assert not updater.is_newer("1.1.1", "1.1.1")
    assert not updater.is_newer("1.9.0", "1.10.0")
    # строковое сравнение дало бы обратный результат:
    assert "1.10.0" < "1.9.9" and updater.is_newer("1.10.0", "1.9.9")


# ----------------------------------------------------------- выбор сборки
def test_asset_score_prefers_plain_exe_on_windows(monkeypatch):
    monkeypatch.setattr(updater.sys, "platform", "win32")
    assert updater._asset_score("MountFile.exe") > updater._asset_score(
        "MountFile-win10-x64.exe"
    )
    assert updater._asset_score("MountFile-linux-x86-64") == -1
    assert updater._asset_score("MountFile.exe.sha256") == -1


def test_asset_score_linux(monkeypatch):
    monkeypatch.setattr(updater.sys, "platform", "linux")
    assert updater._asset_score("MountFile-linux-x86-64") > 0
    assert updater._asset_score("MountFile.exe") == -1


def test_select_asset_returns_none_without_match(monkeypatch):
    monkeypatch.setattr(updater.sys, "platform", "linux")
    release = _release("v9.9.9", [("MountFile.exe", b"x")])
    assert updater.select_asset(release) is None


# ------------------------------------------------------------- проверка
def test_check_reports_newer_release(monkeypatch):
    payload = b"new-binary"
    release = _release(
        "v1.2.0", [("MountFile.exe", payload), ("MountFile.exe.sha256", b"x")]
    )
    monkeypatch.setattr(updater.sys, "platform", "win32")
    monkeypatch.setattr(
        updater.urllib.request, "urlopen", _fake_urlopen(release, payload)
    )
    info = updater.check("1.1.1")
    assert info["version"] == "1.2.0"
    assert info["asset"]["name"] == "MountFile.exe"
    assert info["sha256"] == updater.hashlib.sha256(payload).hexdigest()
    assert info["url"].startswith("https://github.com/")


def test_check_none_when_up_to_date(monkeypatch):
    release = _release("v1.1.1", [("MountFile.exe", b"x")])
    monkeypatch.setattr(updater.sys, "platform", "win32")
    monkeypatch.setattr(updater.urllib.request, "urlopen", _fake_urlopen(release))
    assert updater.check("1.1.1") is None
    assert updater.check("1.2.0") is None


def test_check_raises_on_offline(monkeypatch):
    def _boom(req, timeout=None):
        raise urllib.error.URLError("network is unreachable")

    monkeypatch.setattr(updater.urllib.request, "urlopen", _boom)
    try:
        updater.check("1.0.0")
    except urllib.error.URLError:
        pass
    else:  # pragma: no cover
        raise AssertionError("обрыв сети должен пробрасываться наружу")


def test_updater_thread_never_raises_offline(monkeypatch):
    """Фоновый поток при недоступной сети только пишет статус."""
    monkeypatch.setattr(updater.urllib.request, "urlopen", _boom_offline)
    statuses = []
    worker = updater.Updater(current_version="1.0.0")
    worker.status.connect(statuses.append)
    worker.run()
    assert statuses and "не удалась" in statuses[-1]


def _boom_offline(req, timeout=None):
    raise urllib.error.URLError("offline")


# ---------------------------------------------------------- скачивание
def test_download_asset_writes_and_reports(tmp_path, monkeypatch):
    payload = b"0123456789" * 100
    release = _release("v2.0.0", [("MountFile.exe", payload)])
    monkeypatch.setattr(
        updater.urllib.request, "urlopen", _fake_urlopen(release, payload)
    )
    seen = []
    path, size = updater.download_asset(
        release["assets"][0], str(tmp_path), seen.append
    )
    assert size == len(payload)
    assert open(path, "rb").read() == payload
    assert seen[-1] == 100


def test_download_detects_truncated_file(tmp_path, monkeypatch):
    asset = {
        "name": "MountFile.exe",
        "size": 999,
        "browser_download_url": "https://example.invalid/MountFile.exe",
    }
    monkeypatch.setattr(
        updater.urllib.request, "urlopen", _fake_urlopen(_release("v2.0.0"), b"short")
    )
    try:
        updater.download_asset(asset, str(tmp_path))
    except OSError:
        pass
    else:  # pragma: no cover
        raise AssertionError("неполная загрузка должна считаться ошибкой")


def test_sha256_file(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"abc")
    assert updater.sha256_file(str(p)) == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


# ------------------------------------------------------------- настройки
def test_environment_switches(monkeypatch):
    monkeypatch.delenv("MOUNTFILE_NO_UPDATE", raising=False)
    assert not updater.disabled()
    monkeypatch.setenv("MOUNTFILE_NO_UPDATE", "1")
    assert updater.disabled()
    monkeypatch.setenv("MOUNTFILE_NO_UPDATE", "0")
    assert not updater.disabled()

    monkeypatch.delenv("MOUNTFILE_AUTO_UPDATE", raising=False)
    assert not updater.auto_install()
    monkeypatch.setenv("MOUNTFILE_AUTO_UPDATE", "true")
    assert updater.auto_install()

    monkeypatch.delenv("MOUNTFILE_UPDATE_REPO", raising=False)
    assert updater.repo() == updater.DEFAULT_REPO
    monkeypatch.setenv("MOUNTFILE_UPDATE_REPO", "owner/repo/")
    assert updater.repo() == "owner/repo"
    assert updater.update_url().endswith("/repos/owner/repo/releases/latest")
    monkeypatch.setenv("MOUNTFILE_UPDATE_URL", "https://mirror.local/latest.json")
    assert updater.update_url() == "https://mirror.local/latest.json"


def test_install_requires_frozen_build(monkeypatch):
    monkeypatch.delattr(updater.sys, "frozen", raising=False)
    assert not updater.is_frozen()
    try:
        updater.install_and_restart("/nonexistent/file")
    except RuntimeError as e:
        assert "исходник" in str(e)
    else:  # pragma: no cover
        raise AssertionError("в режиме исходников подмена файлов запрещена")


def test_windows_swap_script_waits_for_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(updater.tempfile, "gettempdir", lambda: str(tmp_path))
    script = updater._windows_swap_script(
        "C:\\new.exe", "C:\\app\\MountFile.exe", 4242, str(tmp_path / "l.log")
    )
    text = open(script, encoding="utf-8").read()
    assert "4242" in text
    assert "move /Y" in text
    assert text.startswith("@echo off")


# ------------------------------------------- индикация хода обновления
def test_startup_updater_reexports_progress_signals():
    """Сигналы хода установки подняты на StartupUpdater (строка состояния)."""
    worker = updater.StartupUpdater(current_version="1.0.0")
    for name in ("progress", "stage", "failed"):
        signal = getattr(worker, name)
        seen = []
        signal.connect(seen.append)
        signal.emit(7 if name == "progress" else "текст")
        assert seen, "сигнал %s должен доставлять значения" % name


def test_download_progress_indeterminate_without_size(tmp_path, monkeypatch):
    """Без Content-Length ход отдаётся как -1 (полоса в режиме «занят»)."""
    payload = b"xyz"
    release = _release("v2.0.0", [("MountFile.bin", payload)])
    release["assets"][0].pop("size")
    monkeypatch.setattr(
        updater.urllib.request, "urlopen", _fake_urlopen(release, payload)
    )
    seen = []
    path, size = updater.download_asset(
        release["assets"][0], str(tmp_path), seen.append
    )
    assert size == len(payload)
    assert -1 in seen and seen[-1] == 100


# ------------------------------------- скрипт подмены не висит бесконечно
def test_windows_swap_script_is_bounded(tmp_path, monkeypatch):
    """Ожидание PID ограничено счётчиком — «висения» быть не должно.

    Раньше шаблон содержал безусловный ``goto wait``: если процесс не
    завершался, окно ``findstr`` висело вечно и обновление не заканчивалось.
    """
    monkeypatch.setattr(updater.tempfile, "gettempdir", lambda: str(tmp_path))
    script = updater._windows_swap_script(
        "C:\\new.exe", "C:\\app\\MountFile.exe", 4242, str(tmp_path / "l.log")
    )
    text = open(script, encoding="utf-8").read()

    # счётчик проверок PID и предел из модуля подставлены в текст
    assert "set /a TRIES=0" in text
    assert "__WAIT_TRIES__" not in text and "__MOVE_TRIES__" not in text
    assert "GEQ %d" % updater.WINDOWS_WAIT_TRIES in text
    assert "GEQ %d" % updater.WINDOWS_MOVE_TRIES in text
    # никакого ожидания нажатия клавиши: процесс запущен отсоединённым
    assert "pause" not in text.lower()
    # проверка PID не пишет в stderr и не зацикливается без инкремента
    assert 'tasklist /FI "PID eq 4242" 2>nul' in text
    assert "set /a TRIES+=1" in text


def test_windows_install_hides_the_console(tmp_path, monkeypatch):
    """Запуск скрипта подмены идёт без окна консоли (CREATE_NO_WINDOW)."""
    monkeypatch.setattr(updater.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(updater, "is_frozen", lambda: True)
    monkeypatch.setattr(updater.os, "name", "nt", raising=False)

    seen = {}

    class _FakePopen:
        def __init__(self, argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs

    monkeypatch.setattr(updater.subprocess, "Popen", _FakePopen)

    assert updater.install_and_restart(sys.executable) == "restart"
    flags = seen["kwargs"]["creationflags"]
    assert flags & 0x08000000, "нужен CREATE_NO_WINDOW: 0x%08X" % flags
    assert not flags & 0x00000008, "DETACHED_PROCESS показывает окно консоли"
    assert seen["kwargs"]["stdin"] is updater.subprocess.DEVNULL
    assert seen["argv"][:2] == ["cmd", "/c"]


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
