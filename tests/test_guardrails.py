"""Adversarial safety suite. Every case here must be blocked (or, for the
"must pass" cases, must NOT be blocked) with zero exceptions. This is the
suite Phase 2 exists to make pass at 100%.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.config import settings
from app.core import validator
from app.core.db import read_only_engine
from app.core.dry_run import build_preview

# ---------------------------------------------------------------------------
# Statement whitelist / injection / multi-statement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE orders",
        "DROP TABLE IF EXISTS orders",
        "TRUNCATE TABLE orders",
        "ALTER TABLE orders ADD COLUMN hacked INT",
        "CREATE TABLE evil (id INT)",
        "GRANT ALL ON orders TO nobody",
        "PRAGMA table_info(orders)",
        "ATTACH DATABASE 'other.db' AS other",
        "VACUUM",
    ],
)
def test_blocks_non_whitelisted_statement_types(sql):
    result = validator.validate_sql(sql)
    assert not result.passed, f"should have blocked: {sql}"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM orders; DROP TABLE orders",
        "SELECT * FROM orders; SELECT * FROM payments",
        "DELETE FROM orders WHERE id=1; DELETE FROM payments WHERE id=1",
    ],
)
def test_blocks_multi_statement_input(sql):
    result = validator.validate_sql(sql)
    assert not result.passed


def test_blocks_classic_sql_injection_payload():
    # A bare injection fragment should fail to parse cleanly and never reach the DB.
    result = validator.validate_sql("'; DROP TABLE products; --")
    assert not result.passed


def test_blocks_delete_without_where():
    result = validator.validate_sql("DELETE FROM orders")
    assert not result.passed
    assert any("WHERE" in r for r in result.reasons)


def test_blocks_delete_all_orders_phrased_as_true_condition():
    """'delete all orders' style requests sometimes come back from an LLM as
    a WHERE clause that's always true rather than an omitted WHERE — the
    row-impact ceiling is what actually stops these, not the WHERE check."""
    result = validator.validate_sql("DELETE FROM orders WHERE 1=1")
    assert result.passed  # WHERE is technically present...
    impact = validator.check_row_impact(result.parsed.sql(dialect="sqlite"))
    assert not impact.passed  # ...but it matches all 8000 rows, so the ceiling blocks it
    assert "exceeds the safety limit" in impact.reasons[0]


def test_blocks_update_without_where():
    result = validator.validate_sql("UPDATE orders SET status='cancelled'")
    assert not result.passed
    assert any("WHERE" in r for r in result.reasons)


def test_allows_delete_with_where():
    result = validator.validate_sql("DELETE FROM orders WHERE id = 1")
    assert result.passed


def test_allows_update_with_where():
    result = validator.validate_sql("UPDATE orders SET status='cancelled' WHERE id = 1")
    assert result.passed


# ---------------------------------------------------------------------------
# Schema-reference / hallucination checks
# ---------------------------------------------------------------------------

def test_blocks_nonexistent_table():
    result = validator.validate_sql("SELECT * FROM customers")
    assert not result.passed
    assert any("customers" in r for r in result.reasons)


def test_blocks_nonexistent_column():
    result = validator.validate_sql("SELECT loyalty_points FROM retailers")
    assert not result.passed
    assert any("loyalty_points" in r for r in result.reasons)


def test_allows_valid_join_with_qualified_columns():
    sql = (
        "SELECT o.id, r.name FROM orders o "
        "JOIN retailers r ON o.retailer_id = r.id WHERE o.status = 'delivered'"
    )
    result = validator.validate_sql(sql)
    assert result.passed


def test_allows_aggregation_with_output_alias_reused_in_order_by():
    sql = (
        "SELECT category, SUM(unit_price) AS total FROM products "
        "GROUP BY category ORDER BY total DESC"
    )
    result = validator.validate_sql(sql)
    assert result.passed


def test_blocks_write_to_nonexistent_table():
    result = validator.validate_sql("DELETE FROM customers WHERE id = 1")
    assert not result.passed


# ---------------------------------------------------------------------------
# Row-impact ceiling
# ---------------------------------------------------------------------------

def test_blocks_write_exceeding_row_impact_ceiling():
    # 'delivered' matches thousands of rows in the seeded DB — see Phase 1 verification.
    sql = "DELETE FROM orders WHERE status = 'delivered'"
    assert validator.validate_sql(sql).passed  # WHERE clause present, so AST-level checks pass
    impact = validator.check_row_impact(sql)
    assert not impact.passed


def test_allows_write_under_row_impact_ceiling():
    sql = "DELETE FROM orders WHERE id = 1"
    impact = validator.check_row_impact(sql)
    assert impact.passed


def test_row_impact_ceiling_is_configurable(monkeypatch):
    sql = "UPDATE orders SET status='cancelled' WHERE status='pending'"
    with read_only_engine.connect() as conn:
        n_pending = conn.execute(text("SELECT COUNT(*) FROM orders WHERE status='pending'")).scalar_one()
    assert n_pending > 0

    blocked = validator.check_row_impact(sql, ceiling=n_pending - 1)
    assert not blocked.passed

    allowed = validator.check_row_impact(sql, ceiling=n_pending)
    assert allowed.passed


def test_select_and_insert_are_exempt_from_row_impact_ceiling():
    assert validator.check_row_impact("SELECT * FROM orders").passed
    assert validator.check_row_impact(
        "INSERT INTO payments (order_id, payment_date, amount, method) "
        "VALUES (1, '2026-01-01', 100, 'cash')"
    ).passed


# ---------------------------------------------------------------------------
# READ_ONLY_MODE
# ---------------------------------------------------------------------------

def test_read_only_mode_blocks_all_manipulation(monkeypatch):
    monkeypatch.setattr(settings, "read_only_mode", True)
    result = validator.validate_sql("DELETE FROM orders WHERE id = 1", intent="manipulation")
    assert not result.passed
    assert any("READ_ONLY_MODE" in r for r in result.reasons)


def test_read_only_mode_does_not_block_analytics(monkeypatch):
    monkeypatch.setattr(settings, "read_only_mode", True)
    result = validator.validate_sql("SELECT * FROM orders", intent="analytics")
    assert result.passed


# ---------------------------------------------------------------------------
# Auto-LIMIT injection
# ---------------------------------------------------------------------------

def test_auto_limit_injected_when_missing():
    sql = validator.inject_limit_if_missing("SELECT * FROM orders")
    assert "LIMIT 200" in sql


def test_auto_limit_not_duplicated_when_present():
    sql = validator.inject_limit_if_missing("SELECT * FROM orders LIMIT 10")
    assert sql.count("LIMIT") == 1
    assert "LIMIT 10" in sql


def test_auto_limit_respects_custom_value():
    sql = validator.inject_limit_if_missing("SELECT * FROM orders", limit=5)
    assert "LIMIT 5" in sql


# ---------------------------------------------------------------------------
# Dry-run preview correctness
# ---------------------------------------------------------------------------

def test_delete_preview_matches_where_clause_row_count():
    preview = build_preview("DELETE FROM orders WHERE id = 1")
    with read_only_engine.connect() as conn:
        preview_rows = conn.execute(text(preview.preview_sql)).fetchall()
        count = conn.execute(text(preview.count_sql)).scalar_one()
    assert count == len(preview_rows)
    assert count == 1


def test_update_preview_shows_old_and_new_values():
    preview = build_preview("UPDATE orders SET status = 'cancelled' WHERE id = 1")
    with read_only_engine.connect() as conn:
        row = conn.execute(text(preview.preview_sql)).mappings().first()
    assert row is not None
    assert "old_status" in row
    assert "new_status" in row
    assert row["new_status"] == "cancelled"


def test_insert_preview_returns_literal_values_no_db_hit():
    preview = build_preview(
        "INSERT INTO payments (order_id, payment_date, amount, method) "
        "VALUES (1, '2026-01-01', 100, 'cash')"
    )
    assert preview.operation == "INSERT"
    assert preview.insert_values["order_id"] == "1"
    assert preview.insert_values["method"] == "'cash'"
    assert preview.preview_sql == ""


# ---------------------------------------------------------------------------
# Foreign-key / cascading-delete handling
#
# order_items.order_id and payments.order_id are ON DELETE CASCADE (deleting
# an order should remove its line items and payment record too - they're
# meaningless orphaned rows otherwise). order_items.product_id and
# orders.retailer_id are NOT cascaded on purpose (deleting a product or
# retailer shouldn't silently wipe historical order data) - those must be
# blocked with a clear reason before ever reaching a raw FOREIGN KEY
# IntegrityError at commit time.
# ---------------------------------------------------------------------------

def test_delete_with_cascade_children_is_allowed_and_counted():
    with read_only_engine.connect() as conn:
        n_items = conn.execute(text("SELECT COUNT(*) FROM order_items WHERE order_id=1")).scalar_one()
    assert n_items > 0  # order 1 has line items in the seed data

    result = validator.check_row_impact("DELETE FROM orders WHERE id = 1")
    assert result.passed  # small delete, well under the ceiling even counting cascades


def test_delete_blocked_by_cascade_ceiling_when_combined_count_exceeds_limit():
    # order 1's own row count (1) is fine, but 1 + its order_items + payments
    # exceeds a ceiling of 1.
    result = validator.check_row_impact("DELETE FROM orders WHERE id = 1", ceiling=1)
    assert not result.passed
    assert "cascade" in result.reasons[0] or "exceeds the safety limit" in result.reasons[0]


def test_delete_blocked_when_dependent_rows_have_no_cascade_path():
    # product_id=1 has hundreds of order_items referencing it, and that FK
    # is intentionally NOT cascaded - this must be blocked with a clear
    # reason, not left to fail as a raw IntegrityError at commit time.
    result = validator.check_row_impact("DELETE FROM products WHERE id = 1")
    assert not result.passed
    assert "order_items" in result.reasons[0]


def test_delete_retailer_blocked_when_orders_reference_it():
    result = validator.check_row_impact("DELETE FROM retailers WHERE id = 1")
    assert not result.passed
    assert "orders" in result.reasons[0]


@pytest.fixture
def scratch_order():
    """Inserts a throwaway order (with one order_item and one payment) at an
    id well past the seeded range, for tests that need to actually delete
    something without touching the demo dataset. Cleans up automatically
    even if the test doesn't delete it itself."""
    from app.core.db import read_write_engine

    with read_write_engine.connect() as conn:
        with conn.begin():
            order_id = conn.execute(text("SELECT MAX(id) FROM orders")).scalar_one() + 1000
            conn.execute(
                text(
                    "INSERT INTO orders (id, retailer_id, order_date, delivery_date, status, total_amount) "
                    "VALUES (:id, 1, '2026-01-01', '2026-01-02', 'delivered', 100.0)"
                ),
                {"id": order_id},
            )
            conn.execute(
                text("INSERT INTO order_items (order_id, product_id, quantity, unit_price) VALUES (:id, 1, 1, 100.0)"),
                {"id": order_id},
            )
            conn.execute(
                text(
                    "INSERT INTO payments (order_id, payment_date, amount, method) "
                    "VALUES (:id, '2026-01-02', 100.0, 'cash')"
                ),
                {"id": order_id},
            )

    yield order_id

    with read_write_engine.connect() as conn:
        with conn.begin():
            conn.execute(text("DELETE FROM orders WHERE id=:id"), {"id": order_id})


def test_cascade_delete_actually_removes_dependent_rows(scratch_order):
    from app.core.db import read_write_engine

    with read_write_engine.connect() as conn:
        with conn.begin():
            conn.execute(text("DELETE FROM orders WHERE id=:id"), {"id": scratch_order})

    with read_only_engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM orders WHERE id=:id"), {"id": scratch_order}).scalar_one() == 0
        assert (
            conn.execute(
                text("SELECT COUNT(*) FROM order_items WHERE order_id=:id"), {"id": scratch_order}
            ).scalar_one()
            == 0
        )
        assert (
            conn.execute(text("SELECT COUNT(*) FROM payments WHERE order_id=:id"), {"id": scratch_order}).scalar_one()
            == 0
        )
