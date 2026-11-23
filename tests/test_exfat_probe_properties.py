"""Инварианты exFAT boot-чексуммы: детерминизм, маска 32 бит, зарезервированные
офсеты 106/107/112 не влияют на результат.

Крупные фиксированные регионы (11 секторов = 5632+ байт) генерируются
``random.Random`` с фиксированным seed вместо hypothesis: input по определению
крупный, и hypothesis на нём тратит больше времени на генерацию, чем сам тест.
Это делает тесты быстрыми, воспроизводимыми и безопасными для xdist.
"""

import random

import pytest

from mountfile.exfat_probe import ExfatProbeError, exfat_boot_checksum

pytestmark = pytest.mark.unit

SECTOR_SIZES = [512, 1024, 2048, 4096]
RESERVED_OFFSETS = (106, 107, 112)


def _random_region(sector_size: int, seed: int) -> bytes:
    rng = random.Random(seed)
    return bytes(rng.getrandbits(8) for _ in range(11 * sector_size))


@pytest.mark.parametrize("sector_size", SECTOR_SIZES)
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_checksum_is_deterministic(sector_size, seed):
    region = _random_region(sector_size, seed)
    assert exfat_boot_checksum(region, sector_size=sector_size) == exfat_boot_checksum(
        region, sector_size=sector_size
    )


@pytest.mark.parametrize("sector_size", SECTOR_SIZES)
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_reserved_offsets_do_not_affect_checksum(sector_size, seed):
    region = bytearray(_random_region(sector_size, seed))
    baseline = exfat_boot_checksum(bytes(region), sector_size=sector_size)
    for offset in RESERVED_OFFSETS:
        region[offset] = (region[offset] + 1) % 256
    assert exfat_boot_checksum(bytes(region), sector_size=sector_size) == baseline


@pytest.mark.parametrize("sector_size", SECTOR_SIZES)
@pytest.mark.parametrize("seed", [1, 2])
def test_checksum_fits_in_32_bits(sector_size, seed):
    checksum = exfat_boot_checksum(_random_region(sector_size, seed), sector_size=sector_size)
    assert 0 <= checksum <= 0xFFFFFFFF


@pytest.mark.parametrize("sector_size", SECTOR_SIZES)
def test_rejects_truncated_region(sector_size):
    with pytest.raises(ExfatProbeError):
        exfat_boot_checksum(b"\x00" * (11 * sector_size - 1), sector_size=sector_size)


@pytest.mark.parametrize("sector_size", SECTOR_SIZES)
def test_rejects_oversized_region(sector_size):
    with pytest.raises(ExfatProbeError):
        exfat_boot_checksum(b"\x00" * (11 * sector_size + 1), sector_size=sector_size)


def test_rejects_unsupported_sector_size():
    with pytest.raises(ExfatProbeError):
        exfat_boot_checksum(b"\x00" * (11 * 256), sector_size=256)
