from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TON_ADDRESS = "UQCZ10Eh8IJ5sFC6JKn-YralMF45Bk5fkNT6SqTHSIRGxfQv"
TON_VIEWER = f"https://tonviewer.com/{TON_ADDRESS}"
TON_LINK = f"ton://transfer/{TON_ADDRESS}?text=MountFile%20donation"


def test_donation_address_is_published_in_readme_and_docs():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    donations = (ROOT / "docs" / "donations.md").read_text(encoding="utf-8")

    assert readme.count(TON_ADDRESS) >= 3
    assert TON_ADDRESS in donations
    assert TON_VIEWER in donations
    assert TON_LINK in donations
    assert "seed-фразу" in donations


def test_github_funding_points_to_the_same_project_wallet():
    funding = (ROOT / ".github" / "FUNDING.yml").read_text(encoding="utf-8")

    assert "custom:" in funding
    assert TON_VIEWER in funding
    assert TON_ADDRESS in funding


def test_donation_qr_asset_is_present_and_non_empty():
    qr = ROOT / "docs" / "assets" / "ton-donation-qr.png"

    assert qr.is_file()
    assert qr.stat().st_size > 100
