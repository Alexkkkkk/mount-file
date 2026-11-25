from __future__ import annotations

from mountfile import optional_backends


def test_inventory_exposes_existing_ext4_binding_without_claiming_ntfs_replay():
    report = optional_backends.inventory()

    assert report["libext2fs"].capability == "native-ext4-write-gated"
    assert report["pyfsntfs"].capability in {
        "read-only-independent-checker-candidate",
        "unavailable",
    }
    assert report["dfir_ntfs"].capability in {
        "read-only-secondary-parser",
        "unavailable",
    }
    assert all(status.capability != "native-ntfs-replay" for status in report.values())


def test_missing_optional_modules_are_typed_as_unavailable(monkeypatch):
    monkeypatch.setattr(optional_backends.importlib.util, "find_spec", lambda _: None)

    status = optional_backends.inventory()["pyfsntfs"]

    assert status.available is False
    assert status.capability == "unavailable"
    assert "not installed" in status.detail


def test_inventory_is_json_serializable():
    report = optional_backends.inventory_dict()

    assert report["ntfsfix"]["capability"] in {
        "secondary-ntfs-diagnostic",
        "unavailable",
    }
    assert report["ntfs-3g"]["license"] == "GPL-2.0-or-later"
