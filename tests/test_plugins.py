# SPDX-License-Identifier: MIT
"""Focused tests for the opt-in plugin registry and example adapters."""

import importlib.util

import pytest

from mountfile.plugins import FileEvent, PluginRegistry


def test_registry_lifecycle_is_ordered_and_events_are_read_only():
    calls = []

    class Recorder:
        name = "recorder"

        def on_open(self, event):
            calls.append(("open", event.path, event.size))
            with pytest.raises(TypeError):
                event.metadata["changed"] = True

        def on_file(self, event):
            calls.append(("file", event.path, event.data))
            with pytest.raises(TypeError):
                event.metadata["changed"] = True

        def on_close(self):
            calls.append(("close",))

    registry = PluginRegistry([Recorder()])
    source = bytearray(b"immutable")
    registry.on_open({"path": "disk.img", "size": len(source), "metadata": {"kind": "raw"}})
    event = registry.on_file({"path": "a.txt", "data": source, "metadata": {"kind": "file"}})
    registry.on_close()

    assert calls == [("open", "disk.img", len(source)), ("file", "a.txt", b"immutable"), ("close",)]
    assert event.data == b"immutable"
    source[:] = b"unchanged"
    assert event.data == b"immutable"
    assert registry.active is False


def test_one_plugin_failure_does_not_stop_other_plugins_and_is_recorded():
    calls = []

    class Broken:
        name = "broken"

        def on_file(self, event):
            calls.append("broken")
            raise RuntimeError("bad plugin")

        def on_close(self):
            calls.append("broken-close")

    class Healthy:
        name = "healthy"

        def on_file(self, event):
            calls.append("healthy")

        def on_close(self):
            calls.append("healthy-close")

    registry = PluginRegistry([Broken(), Healthy()])
    registry.open("disk.img")
    registry.file({"path": "one", "data": b"1"})
    registry.file({"path": "two", "data": b"2"})
    registry.close()

    assert calls == ["broken", "healthy", "healthy", "broken-close", "healthy-close"]
    assert len(registry.errors) == 1
    assert registry.errors[0].plugin == "broken"
    assert registry.errors[0].hook == "on_file"
    assert registry.errors[0].exception_type == "RuntimeError"


def test_path_discovery_is_sorted_and_import_is_explicit(tmp_path):
    first = tmp_path / "z_plugin.py"
    second = tmp_path / "a_plugin.py"
    first.write_text(
        "class Z:\n"
        "    name = 'z'\n"
        "    def on_open(self, event): pass\n"
        "PLUGIN = Z()\n",
        encoding="utf-8",
    )
    second.write_text(
        "class A:\n"
        "    name = 'a'\n"
        "    def on_open(self, event): pass\n"
        "PLUGIN = A()\n",
        encoding="utf-8",
    )
    registry = PluginRegistry()
    assert registry.plugin_names == ()
    assert registry.discover(paths=[first, second]) == ("a", "z")
    assert registry.plugin_names == ("a", "z")


def test_optional_adapters_import_without_optional_dependencies():
    yara_path = importlib.util.spec_from_file_location("example_yara", "examples/plugins/yara.py")
    timeline_path = importlib.util.spec_from_file_location("example_timeline", "examples/plugins/timeline.py")
    yara_module = importlib.util.module_from_spec(yara_path)
    timeline_module = importlib.util.module_from_spec(timeline_path)
    yara_path.loader.exec_module(yara_module)
    timeline_path.loader.exec_module(timeline_module)

    yara = yara_module.YaraPlugin(rules="rule x { condition: true }", yara_module=None)
    timeline = timeline_module.TimelinePlugin(require_pandas=True, pandas_module=None)
    assert yara.status in ("available", "unavailable")
    assert timeline.status in ("available", "unavailable")
    assert hasattr(yara, "status_info")
    assert hasattr(timeline, "status_info")


def test_file_event_is_immutable_snapshot():
    mutable = bytearray(b"abc")
    event = FileEvent.from_value({"path": "x", "data": mutable, "metadata": {"n": [1]}})
    mutable[:] = b"xyz"
    assert event.data == b"abc"
    assert event.metadata["n"] == (1,)
    with pytest.raises(TypeError):
        event.metadata["new"] = 1
