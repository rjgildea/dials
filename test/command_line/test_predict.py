from __future__ import absolute_import, division, print_function

import os

import procrunner
from dials.array_family import flex  # import dependency


def plausible(table):
    # Check the reflection IDs
    assert "id" in table
    assert "miller_index" in table
    assert "s1" in table
    assert "xyzcal.px" in table
    assert "xyzcal.mm" in table
    for row in table:
        assert row["id"] == 0
    return True


def test_static_prediction(dials_regression, run_in_tmpdir):
    result = procrunner.run(
        [
            "dials.predict",
            os.path.join(
                dials_regression,
                "prediction_test_data",
                "experiments_scan_static_crystal.json",
            ),
        ]
    )
    assert result["exitcode"] == 0
    assert result["stderr"] == ""

    table = flex.reflection_table.from_msgpack_file("predicted.mpack")
    assert len(table) == 1996
    assert plausible(table)


def test_scan_varying_prediction(dials_regression, run_in_tmpdir):
    result = procrunner.run(
        [
            "dials.predict",
            os.path.join(
                dials_regression,
                "prediction_test_data",
                "experiments_scan_varying_crystal.json",
            ),
        ]
    )
    assert result["exitcode"] == 0
    assert result["stderr"] == ""

    table = flex.reflection_table.from_msgpack_file("predicted.mpack")
    assert len(table) == 1934
    assert plausible(table)


def test_force_static_prediction(dials_regression, run_in_tmpdir):
    result = procrunner.run(
        [
            "dials.predict",
            os.path.join(
                dials_regression,
                "prediction_test_data",
                "experiments_scan_varying_crystal.json",
            ),
            "force_static=True",
        ]
    )
    assert result["exitcode"] == 0
    assert result["stderr"] == ""

    table = flex.reflection_table.from_msgpack_file("predicted.mpack")
    assert len(table) == 1996
    assert plausible(table)
