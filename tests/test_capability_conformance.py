from scripts.capability_conformance import validate_matrix


def test_capability_conformance_matches_callable_endpoints_and_refusal_cells():
    report = validate_matrix()
    assert report["schema"] == "mountfile.capability-conformance/v1"
    assert report["verdict"] == "pass"
    assert report["profiles"] == 12
    assert report["cells_checked"] == 120
    assert report["blockers"] == []
    assert report["parity"] == "not_claimed"


def test_capability_conformance_reports_every_canonical_operation():
    report = validate_matrix()
    assert report["operations"] == [
        "list",
        "stat",
        "read-file",
        "read-range",
        "extract",
        "write-file",
        "mkdir",
        "delete",
        "rename",
        "hard-link",
    ]
    assert all(cell["profile"] and cell["operation"] for cell in report["cells"])
