import pytest

from mountfile.olma.core import OlmaChain, OlmaIntegrityError


def test_chain_ending_exactly_at_budget_is_valid():
    links = {2: 3, 3: 0x0FFFFFF8}

    chain = list(OlmaChain(2, links.__getitem__, budget=2))

    assert [cluster.value for cluster in chain] == [2, 3]


def test_chain_with_another_cluster_after_budget_raises():
    links = {2: 3, 3: 4, 4: 0x0FFFFFF8}

    with pytest.raises(OlmaIntegrityError, match="chain budget exceeded"):
        list(OlmaChain(2, links.__getitem__, budget=2))
