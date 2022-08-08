import pathlib

import pandas as pd
import pytest

from daft.dataframe import DataFrame
from tests.dataframe_cookbook.conftest import (
    IRIS_CSV,
    assert_df_equals,
    parametrize_partitioned_daft_df,
)


@parametrize_partitioned_daft_df()
def test_load_csv(daft_df, pd_df):
    """Loading data from a CSV works"""
    pd_slice = pd_df
    daft_slice = daft_df
    print(pd_slice)
    print(daft_slice.to_pandas())
    assert_df_equals(daft_slice, pd_slice)


def test_load_csv_no_headers(tmp_path: pathlib.Path):
    """Generate a default set of headers `col_0, col_1, ... col_{n}` when loading a CSV that has no headers"""
    csv = tmp_path / "headerless_iris.csv"
    csv.write_text("\n".join(pathlib.Path(IRIS_CSV).read_text().split("\n")[1:]))
    daft_df = DataFrame.from_csv(str(csv), headers=False)
    pd_df = pd.read_csv(csv, header=None)
    pd_df.columns = [f"col_{i}" for i in range(5)]
    assert_df_equals(daft_df, pd_df, assert_ordering=True)


def test_load_csv_tab_delimited(tmp_path: pathlib.Path):
    """Generate a default set of headers `col_0, col_1, ... col_{n}` when loading a CSV that has no headers"""
    csv = tmp_path / "headerless_iris.csv"
    csv.write_text(pathlib.Path(IRIS_CSV).read_text().replace(",", "\t"))
    daft_df = DataFrame.from_csv(str(csv), delimiter="\t")
    pd_df = pd.read_csv(csv, delimiter="\t")
    assert_df_equals(daft_df, pd_df, assert_ordering=True)


@pytest.mark.tdd_all
def test_load_pydict():
    data = {"foo": [1, 2, 3], "bar": [1.0, None, 3.0], "baz": ["a", "b", "c"]}
    daft_df = DataFrame.from_pydict(data)
    pd_df = pd.DataFrame.from_dict(data)
    assert_df_equals(daft_df, pd_df)


@pytest.mark.tdd_all
def test_load_pylist():
    data = [
        {"foo": 1, "bar": 1.0, "baz": "a"},
        {"foo": 2, "bar": 2.0, "baz": "b"},
        {"foo": 3, "bar": 3.0, "baz": "c"},
    ]
    daft_df = DataFrame.from_pylist(data)
    pd_df = pd.DataFrame.from_records(data)
    assert_df_equals(daft_df, pd_df)