## Сводка трекера — сгенерировано `scripts/tracker_status.py`

Срез манифеста: `5a3c48c` (v1.103.2), сверка `2026-11-25`. HEAD: `5a3c48c` — Merge remote-tracking branch 'origin/main' into fix/issue-510-evidence-completion.

⚠️ Ветка опережает `origin/main` на 3 коммит(ов): манифест может отставать.

✅ 23 · ⚠️ 8 · ⬜ 0 · 🚫 1 · всего 32

> ✅ код и доказательство · ⚠️ код есть, независимое доказательство не воспроизведено · ⬜ не начато · 🚫 ждёт мержа или отказ по проекту

### A. Общее и release-база

| № | Пункт | Состояние | Доказательство |
|---|---|---|---|
| A1 | Небезопасный replay-подход удалён из runtime | ✅ | `tests/test_docs_isolation.py` |
| A2 | Релиз содержит безопасный код | ✅ | `pyproject.toml`, `mountfile/__init__.py` |
| A3 | Черновики из docs/proposals/ не подключены к runtime | ✅ | `docs/proposals`, `tests/test_docs_isolation.py` |
| A4 | README сохраняет ext3/ext4 и NTFS в read-only до принятия replay | ✅ | `README.md`, `scripts/product_status_contract.py`, `tests/test_readme_status_gate.py`, `scripts/verify_generated_status.py` |

### B. ext4 / JBD2 — полный transaction replay (#385)

| № | Пункт | Состояние | Доказательство |
|---|---|---|---|
| B1 | Journal gate на RECOVER + отказ вместо тихого сброса | ✅ | `mountfile/ext4_journal_gate.py` |
| B2 | Recovery через независимый чекер на копии | ✅ | `mountfile/ext4_journal_gate.py`, `tests/test_checker_acceptance.py` |
| B3 | Descriptor/data/commit-записи и ring-планировщик | ✅ | `tests/test_jbd2_writer.py` |
| B4 | Revoke-записи | ✅ | `tests/test_jbd2_writer.py` |
| B5 | Sequence wrap / rollover | ✅ | `tests/test_ext3_jbd2_sequence_rollover.py`, `tests/test_jbd2_checkpoint.py` |
| B6 | Transaction checksums (JBD2 csum) end-to-end | ✅ | `tests/test_jbd2_csum_replay.py`, `tests/test_ext4_jbd2_csum.py` |
| B7 | Checkpointing внутри writer-цикла | ✅ | `mountfile/ext4_native_writer.py`, `mountfile/jbd2_checkpoint.py` |
| B8 | Process-kill на каждой фазе записи JBD2-транзакции в ring | ✅ | `mountfile/jbd2_ring_writer.py`, `mountfile/jbd2_checkpoint.py`, `mountfile/native_write.py`, `tests/test_jbd2_ring_phase_kill.py`, `tests/test_jbd2_checkpoint_fault_points.py`, `tests/test_ext4_writer_fault_coverage.py` |
| B9 | Прерванный commit и malformed-journal | ✅ | `tests/test_jbd2_interrupted_commit.py`, `tests/test_jbd2_malformed_journal_refusal.py` |
| B10 | e2fsck -f -n после каждой операции writer'а | ✅ | `mountfile/ext4_native_writer.py`, `tests/test_checker_acceptance.py` |

### C. NTFS / `$LogFile` — redo/undo replay (#386)

| № | Пункт | Состояние | Доказательство |
|---|---|---|---|
| C1 | Честный fail-closed по умолчанию | ✅ | `mountfile/ntfs_replay_executor.py`, `mountfile/ntfs_native_writer.py`, `tests/test_ntfs_acceptance_independent_checker.py` |
| C2 | Парсер $LogFile | ✅ | `mountfile/ntfs_logfile.py`, `tests/test_ntfs_logfile_parser.py`, `tests/test_ntfs_logfile_draft_golden.py` |
| C3 | Применение redo/undo к $MFT, $BITMAP, индексам | ⚠️ | `mountfile/ntfs_replay.py`, `mountfile/ntfs_replay_executor.py`, `tests/test_ntfs_bitmap_handler.py` |
| C4 | $ATTRIBUTE_LIST | 🚫 | `mountfile/ntfs_replay.py` |
| C5 | Safe MFT record allocation/reuse и $MFTMirr | ⚠️ | `mountfile/ntfs_mft_allocation.py`, `tests/test_ntfs_mft_allocation.py` |
| C6 | Synthetic NTFS crash fixtures with auxiliary ntfsfix -n checks | ⚠️ | `tests/ntfs_crash_fixture.py`, `tests/test_ntfs_replay.py`, `tests/test_ntfs_writer_fault_coverage.py`, `tests/test_ntfs_acceptance_independent_checker.py`, `scripts/windows_ntfs_acceptance.ps1`, `.github/workflows/ntfs-windows-acceptance.yml`, `tests/test_windows_ntfs_acceptance_contract.py`, `tests/ntfs_oracle.py`, `tests/test_ntfs_replay_oracle.py`, `scripts/ntfs_oracle_evidence.py` |
| C7 | Quarantine при недоказанном replay | ✅ | `mountfile/ntfs_native_writer.py` |
| C8 | MIT-совместимая граница с внешним dfir_ntfs | ✅ | `mountfile/experimental/dfir_ntfs_adapter.py` |

### D. Общая acceptance-матрица (#387)

| № | Пункт | Состояние | Доказательство |
|---|---|---|---|
| D1 | Failure injection после каждого шага (data, bitmap, directory, inode, journal, commit) | ⚠️ | `mountfile/native_write.py`, `tests/test_ext4_write_phase_injection.py`, `tests/test_ntfs_native_write_phase_injection.py`, `tests/test_ntfs_write_phase_injection.py`, `tests/test_writer_fault_injection_acceptance.py` |
| D2 | Process-kill at selected staged-writer boundaries | ✅ | `tests/test_ext4_writer_fault_coverage.py`, `tests/test_ntfs_writer_fault_coverage.py`, `tests/test_native_journal_writers.py`, `.github/workflows/replay-nightly.yml`, `scripts/replay_nightly_evidence.py`, `tests/test_replay_nightly_evidence.py` |
| D3 | Hash preservation on mapped refusal and failure cases | ⚠️ | `mountfile/native_write.py`, `mountfile/ntfs_native_writer.py`, `tests/test_jbd2_malformed_journal_refusal.py`, `tests/test_ntfs_native_write_phase_injection.py`, `tests/test_ntfs_write_phase_injection.py`, `tests/test_ntfs_writer_fault_coverage.py`, `tests/test_writer_fault_injection_acceptance.py`, `tests/test_hash_preservation_matrix.py` |
| D4 | Source identity / concurrent modification | ✅ | `mountfile/native_write.py`, `tests/test_ntfs_acceptance_independent_checker.py`, `tests/test_ext2_native.py` |
| D5 | Independent checker + read-back после каждой поддержанной операции | ⚠️ | `tests/_independent_checkers.py`, `tests/test_checker_acceptance.py`, `mountfile/ntfs_native_writer.py`, `mountfile/native_write.py`, `tests/test_ntfs_transactional_readback.py`, `tests/test_native_journal_writers.py`, `tests/test_writer_fault_coverage.py` |
| D6 | JUnit-backed profile × operation × failure-state artifact | ⚠️ | `.github/workflows/ci.yml`, `.github/workflows/failure-matrix.yml`, `.github/workflows/replay-nightly.yml`, `scripts/failure_matrix_artifact.py`, `scripts/replay_nightly_evidence.py`, `tests/test_failure_matrix_artifact.py`, `tests/test_writer_fault_injection_acceptance.py` |
| D7 | Unsupported-ячейки как typed refusals, а не скрытые skip | ✅ | `scripts/capability_conformance.py`, `tests/test_capability_conformance.py`, `tests/test_vfs_refusal_routing.py`, `tests/test_fat_capability_identity.py` |

### E. Паритет, платформы, статус

| № | Пункт | Состояние | Доказательство |
|---|---|---|---|
| E1 | #389: Windows/macOS evidence, unavailable vs pass как typed outcome | ⚠️ | `scripts/native_read_evidence.py`, `scripts/cross_platform_evidence.py`, `tests/test_cross_platform_evidence.py`, `.github/workflows/ci.yml` |
| E2 | #390: CI-гейт на расхождение README / CLI / runtime | ✅ | `scripts/product_status_contract.py`, `scripts/verify_generated_status.py`, `tests/test_readme_status_gate.py`, `.github/workflows/ci.yml` |
| E3 | #388: единый capability/refusal-контракт GUI/CLI/VFS | ✅ | `mountfile/capabilities.py`, `mountfile/safety.py` |

**Критерий закрытия:** replay нельзя объявлять supported, пока не закрыты блоки B, C и D, а README и CI-гейт статуса (#390) не отражают фактическое поведение

Расхождений манифеста с деревом нет.
