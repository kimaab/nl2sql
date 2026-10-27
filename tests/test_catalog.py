import pytest

from catalog import schemas, table_parts
from models import ApiException, SystemInput

PG = {"driver": "postgresql", "db_name": "dev", "db_schemas": [], "username": "app"}


def test_default_schema_when_none_listed():
    assert schemas(PG) == ["public"]
    assert schemas({**PG, "driver": "oracle"}) == ["APP"]
    assert schemas({**PG, "driver": "mysql", "db_schemas": ["x"]}) == ["dev"]


def test_oracle_needs_schema_or_account():
    with pytest.raises(ApiException):
        schemas({**PG, "driver": "oracle", "username": ""})


def test_first_listed_schema_is_default():
    row = {**PG, "db_schemas": ["app", "sales"]}
    assert schemas(row) == ["app", "sales"]
    assert table_parts(row, {"name": "orders"}) == ("app", "orders")
    assert table_parts(row, {"name": "sales.orders", "schema": "sales"}) == ("sales", "orders")


def test_system_input_cleans_schemas():
    data = SystemInput(code="a", name="A", driver="postgresql", host="h", db_name="d",
                       db_schemas=[" app ", "", "APP", "sales"])
    assert data.db_schemas == ["app", "sales"]
    with pytest.raises(ValueError):
        SystemInput(code="a", name="A", driver="postgresql", host="h", db_name="d", db_schemas=["a.b"])
