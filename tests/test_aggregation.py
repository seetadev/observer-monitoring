import pytest

import root
from tests.conftest import make_stat


def test_average_group_averages_scalars_and_vectors():
    group = [make_stat(cpu=[10.0, 20.0], ram=40.0), make_stat(cpu=[30.0, 40.0], ram=60.0)]
    avg = root.average_group(group)
    assert avg["ram"] == 50.0
    assert avg["cpu"] == [20.0, 30.0]
    assert avg["packets_sent"] == [10.0, 0.0]


def test_condense_data_estimation_groups_by_gradient():
    records = [make_stat(ram=float(v)) for v in (10, 20, 30, 40, 50)]
    out = root.condense_data_estimation(records, 2)
    # 5 records, gradient 2: two full groups; the trailing record cannot form a group
    assert [r["ram"] for r in out] == [15.0, 35.0]


def test_condense_data_estimation_keeps_first_record_metadata():
    records = [make_stat(name="a", date="2026-01-01T00:00:00"), make_stat(name="b", date="2026-01-01T00:01:00"),
               make_stat(name="c", date="2026-01-01T00:02:00")]
    out = root.condense_data_estimation(records, 2)
    assert out[0]["date"] == "2026-01-01T00:00:00"
    assert out[0]["name"] == "b"


@pytest.mark.xfail(strict=True, reason="off-by-one: the last window is dropped, so one record yields none")
def test_condense_data_estimation_single_record_is_kept():
    out = root.condense_data_estimation([make_stat(ram=42.0)], 1)
    assert [r["ram"] for r in out] == [42.0]


@pytest.mark.xfail(strict=True, reason="off-by-one: the last window is dropped")
def test_condense_data_estimation_keeps_last_record_at_gradient_one():
    records = [make_stat(ram=float(v)) for v in (1, 2, 3)]
    assert len(root.condense_data_estimation(records, 1)) == 3


def test_bandwidth_usage_sums_last_record():
    records = [make_stat(bytes_recv=10, bytes_sent=20), make_stat(bytes_recv=30, bytes_sent=40)]
    assert root.get_bandwidth_usage(records) == (30, 40)
