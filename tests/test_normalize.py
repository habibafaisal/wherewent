"""Tests for wherewent.normalize: normalize_sql() and group_key()."""

import pytest

from wherewent.normalize import group_key, normalize_sql


NORMALIZE_CASES = [
    pytest.param(
        "SELECT * FROM t WHERE id = 5",
        "SELECT * FROM t WHERE id = ?",
        id="integer-literal",
    ),
    pytest.param(
        "SELECT * FROM t WHERE x = 3.14",
        "SELECT * FROM t WHERE x = ?",
        id="float-literal",
    ),
    pytest.param(
        "SELECT * FROM t WHERE x = 1.5e10",
        "SELECT * FROM t WHERE x = ?",
        id="exponent-literal",
    ),
    pytest.param(
        "SELECT a FROM t WHERE x = 'hello'",
        "SELECT a FROM t WHERE x = ?",
        id="single-quoted-string",
    ),
    pytest.param(
        "SELECT a FROM t WHERE x = 'it''s'",
        "SELECT a FROM t WHERE x = ?",
        id="doubled-quote-escape",
    ),
    pytest.param(
        "SELECT a FROM t -- get all rows\nWHERE b = 1",
        "SELECT a FROM t WHERE b = ?",
        id="dash-dash-comment",
    ),
    pytest.param(
        "SELECT a /* block comment */ FROM t WHERE b = 1",
        "SELECT a FROM t WHERE b = ?",
        id="block-comment",
    ),
    pytest.param(
        "SELECT a FROM t WHERE x = %s",
        "SELECT a FROM t WHERE x = ?",
        id="percent-s-placeholder",
    ),
    pytest.param(
        "SELECT a FROM t WHERE x = %(name)s",
        "SELECT a FROM t WHERE x = ?",
        id="percent-named-placeholder",
    ),
    pytest.param(
        "SELECT a FROM t WHERE x = :x_1",
        "SELECT a FROM t WHERE x = ?",
        id="colon-named-placeholder",
    ),
    pytest.param(
        "SELECT * FROM t WHERE id IN (1, 2, 3)",
        "SELECT * FROM t WHERE id IN (?)",
        id="in-list-collapse",
    ),
    pytest.param(
        "SELECT * FROM t WHERE id IN (4)",
        "SELECT * FROM t WHERE id IN (?)",
        id="in-list-single",
    ),
    pytest.param(
        "INSERT INTO t VALUES (1, 'a'), (2, 'b'), (3, 'c')",
        "INSERT INTO t VALUES (?, ?)",
        id="multirow-values-collapse",
    ),
    pytest.param(
        "SELECT   a    FROM   t  WHERE   b  =  1",
        "SELECT a FROM t WHERE b = ?",
        id="whitespace-collapse",
    ),
    pytest.param(
        "SELECT\n\ta\t FROM\tt\nWHERE b = 1",
        "SELECT a FROM t WHERE b = ?",
        id="whitespace-collapse-tabs-newlines",
    ),
    pytest.param(
        "SELECT col1 FROM t1 WHERE id2 = 5",
        "SELECT col1 FROM t1 WHERE id2 = ?",
        id="identifiers-with-digits-untouched",
    ),
]


@pytest.mark.parametrize("raw_sql, expected", NORMALIZE_CASES)
def test_normalize_sql(raw_sql, expected):
    assert normalize_sql(raw_sql) == expected


def test_insert_values_pair_same_group():
    """INSERT ... VALUES (1,'a') and (2,'b') must normalize/group identically."""
    sql_a = normalize_sql("INSERT INTO events VALUES (1,'a')")
    sql_b = normalize_sql("INSERT INTO events VALUES (2,'b')")
    assert sql_a == sql_b
    assert group_key(sql_a) == group_key(sql_b)


def test_group_key_is_12_hex_chars():
    key = group_key(normalize_sql("SELECT 1"))
    assert len(key) == 12
    assert all(c in "0123456789abcdef" for c in key)


def test_group_key_differs_for_different_normalized_sql():
    key_a = group_key(normalize_sql("SELECT a FROM t"))
    key_b = group_key(normalize_sql("SELECT b FROM t"))
    assert key_a != key_b
