# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Тесты самообучающихся слоёв: tests/test_selflearning_layers.py.

Run:  python -m pytest tests/test_selflearning_layers.py -v
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mountfile.layers import (
    EWMA,
    BetaLearner,
    KnowledgeStore,
    LogisticWeights,
    SelfLearningStack,
)


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    return tmp_path / "proj"


# --------------------------------------------------------------- store
class TestKnowledgeStore:
    def test_roundtrip(self, root: Path) -> None:
        store = KnowledgeStore(root)
        state = store.layer("volume")
        state["ranker"] = {"dim": 5, "w": [0.1, 0, 0, 0, 0.2]}
        store.save()

        fresh = KnowledgeStore(root)
        raw = json.loads((root / ".mountfile" / "layers.json").read_text("utf-8"))
        assert raw["version"] == 1
        assert fresh.layer("volume")["ranker"]["w"][0] == 0.1

    def test_corrupt_file_resets(self, root: Path) -> None:
        (root / ".mountfile").mkdir(parents=True)
        (root / ".mountfile" / "layers.json").write_text("{not json", "utf-8")
        store = KnowledgeStore(root)
        assert store.layer("volume") == {}

    def test_wrong_version_resets(self, root: Path) -> None:
        (root / ".mountfile").mkdir(parents=True)
        (root / ".mountfile" / "layers.json").write_text(
            json.dumps({"version": 999, "layers": {}}), "utf-8"
        )
        store = KnowledgeStore(root)
        assert store.layer("volume") == {}


# ---------------------------------------------------------- primitives
class TestPrimitives:
    def test_beta_no_knowledge(self) -> None:
        learner = BetaLearner()
        assert learner.observations == 0.0

    def test_beta_updates(self) -> None:
        learner = BetaLearner()
        for _ in range(8):
            learner.update(True)
        learner.update(False)
        assert learner.beta() > 0.7
        assert learner.observations == pytest.approx(9.0)

    def test_logistic_zero_weights_neutral(self) -> None:
        model = LogisticWeights(5)
        assert model.score([0.9, 1.0, 0.0, 1.0, 1.0]) == 0.0

    def test_logistic_learns(self) -> None:
        model = LogisticWeights(2)
        for _ in range(50):
            model.update([1.0, 0.0], 1.0)
            model.update([0.0, 1.0], 0.0)
        assert model.score([1.0, 0.0]) > model.score([0.0, 1.0])

    def test_ewma(self) -> None:
        ewma = EWMA(0.5)
        assert ewma.update(1.0) == 1.0
        assert ewma.update(0.0) == pytest.approx(0.5)


# ------------------------------------------------------------- volume
class TestVolumeLayer:
    class Cand:
        def __init__(self, score, offset=0, label="", size=1, backend="tsk"):
            self.score = score
            self.offset = offset
            self.label = label
            self.name = ""
            self.size = size
            self.backend = backend
            self.fstype = "ntfs"

    def test_fallback_without_knowledge(self, root: Path) -> None:
        stack = SelfLearningStack(str(root))
        a, b = self.Cand(100, offset=0), self.Cand(50, offset=4096)
        assert stack.volume.rerank([a, b]) == [a, b]

    def test_learning_changes_order(self, root: Path) -> None:
        stack = SelfLearningStack(str(root))
        loser = self.Cand(100, offset=0)
        winner = self.Cand(10, offset=1 << 20)
        for _ in range(30):
            stack.volume.feedback(winner, accepted=True)
            stack.volume.feedback(loser, accepted=False)
        ranked = stack.volume.rerank([loser, winner])
        assert ranked[0] is winner

    def test_persistence(self, root: Path) -> None:
        stack = SelfLearningStack(str(root))
        cand = self.Cand(10)
        for _ in range(5):
            stack.volume.feedback(cand, accepted=True)
        fresh = SelfLearningStack(str(root))
        assert fresh.volume.observations == 5


# ---------------------------------------------------------- preflight
class TestPreflightLayer:
    class Report:
        family = "ntfs"
        refusal_code = None
        refusals = []

    def test_hint_none_without_knowledge(self, root: Path) -> None:
        stack = SelfLearningStack(str(root))
        assert stack.preflight.pass_hint("ntfs") is None

    def test_hint_after_feedback(self, root: Path) -> None:
        stack = SelfLearningStack(str(root))
        for _ in range(5):
            stack.preflight.observe(self.Report(), True)
        hint = stack.preflight.pass_hint("ntfs")
        assert hint is not None and hint > 0.8

    def test_preflight_image_learns_from_actual_payload(self, root: Path, monkeypatch) -> None:
        from mountfile import fs_preflight

        monkeypatch.setattr(
            fs_preflight,
            "preflight_image",
            lambda *_args, **_kwargs: {
                "volumes": [
                    {"filesystem": "NTFS", "eligible_for_read": False},
                    {"filesystem": "ext4", "eligible_for_read": True},
                ]
            },
        )
        stack = SelfLearningStack(str(root))
        payload = stack.preflight.preflight_image("image.img")
        assert payload["volumes"][0]["eligible_for_read"] is False
        assert stack.preflight.observations == 2
        assert stack.preflight.families["ntfs"].beta() < 0.5
        assert stack.preflight.families["ext"].beta() > 0.5

    def test_display_name_is_normalized_and_learned(self, root: Path) -> None:
        stack = SelfLearningStack(str(root))
        for _ in range(4):
            stack.preflight.observe_dict(
                {"filesystem": "NTFS", "eligible_for_read": True}
            )
        assert stack.preflight.pass_hint("ntfs") is not None
        assert stack.preflight.observations == 4

    def test_unknown_family_ignored(self, root: Path) -> None:
        stack = SelfLearningStack(str(root))
        report = self.Report()
        report.family = "hfs"
        stack.preflight.observe(report, True)
        assert stack.preflight.pass_hint("hfs") is None


# -------------------------------------------------------------- cache
class TestCacheLayer:
    def test_factor_none_without_knowledge(self, root: Path) -> None:
        stack = SelfLearningStack(str(root))
        assert stack.cache.ttl_factor is None

    def test_factor_grows_with_misses(self, root: Path) -> None:
        stack = SelfLearningStack(str(root))
        for _ in range(30):
            stack.cache.observe_hit(False)
        factor = stack.cache.ttl_factor
        assert factor is not None and factor > 2.0

    def test_wrap_without_knowledge_uses_default(self, root: Path) -> None:
        from mountfile import turbo

        class FS:
            def list_dir(self, path):
                return []

        stack = SelfLearningStack(str(root))
        cached = stack.cache.wrap(FS())
        assert isinstance(cached, turbo.CachedFS)

    def test_live_wrapper_observes_hits_and_flushes_state(self, root: Path) -> None:
        from types import SimpleNamespace

        class Entry:
            info = SimpleNamespace(
                name=SimpleNamespace(name=b"file.bin"),
                meta=SimpleNamespace(size=4, type=1, addr=2, mtime=0),
            )

        class FS:
            def open_dir(self, _path):
                return [Entry()]

        stack = SelfLearningStack(str(root))
        cached = stack.cache.wrap(FS())
        assert cached.list_dir("/") == cached.list_dir("/")
        assert cached.stats()["misses"] == 1
        assert cached.stats()["hits"] == 1
        assert stack.cache.miss_rate.n == 2
        stack.cache.flush()
        restored = SelfLearningStack(str(root))
        assert restored.cache.miss_rate.n == 2


# ------------------------------------------------------------ advisor
class TestAdvisorLayer:
    HYP = [{"format": "ntfs", "confidence": 0.8}, {"format": "exfat", "confidence": 0.6}]

    def test_calibrate_none_without_knowledge(self, root: Path) -> None:
        stack = SelfLearningStack(str(root))
        assert stack.advisor.calibrate(self.HYP) is None

    def test_calibration_boosts_good_format(self, root: Path) -> None:
        stack = SelfLearningStack(str(root))
        for _ in range(10):
            stack.advisor.feedback("exfat", True)
            stack.advisor.feedback("ntfs", False)
        calibrated = stack.advisor.calibrate(self.HYP)
        assert calibrated is not None
        assert calibrated[0]["format"] == "exfat"

    def test_best_format(self, root: Path) -> None:
        stack = SelfLearningStack(str(root))
        assert stack.advisor.best_format(self.HYP) is None
        for _ in range(6):
            stack.advisor.feedback("ntfs", True)
        assert stack.advisor.best_format(self.HYP) == "ntfs"


# ------------------------------------------------------------ pipeline
class TestStack:
    def test_summary(self, root: Path) -> None:
        stack = SelfLearningStack(str(root))
        assert stack.summary() == {
            "volume": 0,
            "preflight": 0,
            "cache": 0,
            "advisor": 0,
            "operations": 0,
        }

    def test_summary_does_not_double_count_volume_feedback(self, root: Path) -> None:
        stack = SelfLearningStack(str(root))
        candidate = TestVolumeLayer.Cand(10)
        stack.volume.feedback(candidate, accepted=True)
        assert stack.summary()["volume"] == 1
