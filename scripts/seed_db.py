"""Seed a synthetic SQLite database for a cold-drinks manufacturing &
wholesale distribution business based in Ratnagiri district.

Deterministic: fixed random seed (42) means re-running this script always
produces byte-identical row counts and the same seasonal distribution.

Usage:
    python -m scripts.seed_db [--force]
"""
from __future__ import annotations

import argparse
import random
import sqlite3
from datetime import date, timedelta

from app.config import settings

SEED = 42
START_DATE = date(2024, 1, 1)
END_DATE = date(2026, 7, 31)
N_ORDERS = 8000

DISTRICT = "Ratnagiri"
TOWNS = [
    "Ratnagiri", "Chiplun", "Guhagar", "Dapoli", "Khed",
    "Lanja", "Rajapur", "Sangameshwar", "Mandangad", "Ganpatipule",
]

RETAILER_TYPES = ["general store", "supermarket", "hotel"]
RETAILER_TYPE_WEIGHTS = [0.60, 0.25, 0.15]
RETAILER_TYPE_CREDIT_RANGE = {
    "general store": (5_000, 20_000),
    "supermarket": (20_000, 80_000),
    "hotel": (10_000, 40_000),
}
RETAILER_NAME_SUFFIX = {
    "general store": ["General Store", "Kirana Stores", "Provision Store", "Traders"],
    "supermarket": ["Supermarket", "Super Bazaar", "Mart", "Fresh Mart"],
    "hotel": ["Hotel", "Lodge & Restaurant", "Bhavan Restaurant", "Resort"],
}

PRODUCTS = [
    # (name, category, price_range, size_options)
    ("Ratna Cola Classic", "cola"), ("Ratna Cola Zero", "cola"), ("Konkan Cola", "cola"),
    ("Sahyadri Cola", "cola"), ("Coastal Cola", "cola"), ("Ratna Cola Cherry", "cola"),
    ("Malvan Cola", "cola"), ("Fizzo Cola", "cola"), ("Bubbly Cola", "cola"), ("Retro Cola", "cola"),

    ("Ratna Mango Juice", "juice"), ("Ratna Guava Juice", "juice"), ("Konkan Kokum Juice", "juice"),
    ("Sahyadri Pineapple Juice", "juice"), ("Ratna Orange Juice", "juice"), ("Alphonso Delight", "juice"),
    ("Ratna Mixed Fruit Juice", "juice"), ("Konkan Litchi Juice", "juice"), ("Ratna Apple Juice", "juice"),
    ("Sahyadri Grape Juice", "juice"),

    ("Sahyadri Springs Water", "water"), ("Ratna Pure Water", "water"), ("Konkan Aqua", "water"),
    ("HimJal Water", "water"), ("Crystal Konkan Water", "water"), ("Ratna Chilled Water", "water"),
    ("Coastal Aqua", "water"), ("PureFlow Water", "water"), ("Sahyadri Glacier Water", "water"),
    ("Ratna Aqua Plus", "water"),

    ("Ratna Power Energy", "energy"), ("Konkan Charge", "energy"), ("Sahyadri Boost", "energy"),
    ("Ratna Sting Energy", "energy"), ("TurboKonkan Energy", "energy"), ("Ratna Volt", "energy"),
    ("Coastal Rush Energy", "energy"), ("HighTide Energy", "energy"), ("Ratna Adrenaline", "energy"),
    ("Sahyadri Spark Energy", "energy"),
]

CATEGORY_PRICE_RANGE = {  # INR per bottle
    "cola": (20, 45),
    "juice": (25, 60),
    "water": (10, 30),
    "energy": (35, 90),
}
CATEGORY_SIZES = {
    "cola": [250, 300, 500, 750, 1000, 1500],
    "juice": [200, 250, 500, 1000],
    "water": [500, 1000, 2000],
    "energy": [250, 300],
}

PAYMENT_METHODS = ["cash", "upi", "credit"]
PAYMENT_METHOD_WEIGHTS = [0.35, 0.45, 0.20]

DDL = """
CREATE TABLE products (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL CHECK(category IN ('cola','juice','water','energy')),
    unit_price REAL NOT NULL,
    bottle_size_ml INTEGER NOT NULL
);

CREATE TABLE retailers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    town TEXT NOT NULL,
    district TEXT NOT NULL,
    retailer_type TEXT NOT NULL CHECK(retailer_type IN ('general store','supermarket','hotel')),
    credit_limit REAL NOT NULL
);

CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    retailer_id INTEGER NOT NULL REFERENCES retailers(id),
    order_date TEXT NOT NULL,
    delivery_date TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending','delivered','cancelled')),
    total_amount REAL NOT NULL
);

CREATE TABLE order_items (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL,
    unit_price REAL NOT NULL
);

CREATE TABLE payments (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    payment_date TEXT NOT NULL,
    amount REAL NOT NULL,
    method TEXT NOT NULL CHECK(method IN ('cash','upi','credit'))
);

CREATE TABLE inventory (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL UNIQUE REFERENCES products(id) ON DELETE CASCADE,
    stock_quantity INTEGER NOT NULL,
    last_restocked TEXT NOT NULL
);

CREATE INDEX idx_orders_retailer ON orders(retailer_id);
CREATE INDEX idx_orders_date ON orders(order_date);
CREATE INDEX idx_orders_status ON orders(status);
CREATE INDEX idx_order_items_order ON order_items(order_id);
CREATE INDEX idx_order_items_product ON order_items(product_id);
CREATE INDEX idx_payments_order ON payments(order_id);
"""


def month_weight(month: int) -> float:
    """Summer (Mar-Jun) spikes, monsoon (Jul-Sep) dips."""
    if month in (3, 4, 5, 6):
        return 1.8
    if month in (7, 8, 9):
        return 0.5
    return 1.0


def build_order_date_pool(rng: random.Random) -> list[date]:
    """Weighted list of all days in range, used for weighted sampling."""
    days = []
    d = START_DATE
    while d <= END_DATE:
        days.append(d)
        d += timedelta(days=1)
    weights = [month_weight(d.month) for d in days]
    return rng.choices(days, weights=weights, k=N_ORDERS)


def seed_products(rng: random.Random) -> list[dict]:
    rows = []
    for i, (name, category) in enumerate(PRODUCTS, start=1):
        lo, hi = CATEGORY_PRICE_RANGE[category]
        rows.append({
            "id": i,
            "name": name,
            "category": category,
            "unit_price": round(rng.uniform(lo, hi), 2),
            "bottle_size_ml": rng.choice(CATEGORY_SIZES[category]),
        })
    return rows


def seed_retailers(rng: random.Random, n: int = 150) -> list[dict]:
    rows = []
    for i in range(1, n + 1):
        town = rng.choice(TOWNS)
        rtype = rng.choices(RETAILER_TYPES, weights=RETAILER_TYPE_WEIGHTS, k=1)[0]
        suffix = rng.choice(RETAILER_NAME_SUFFIX[rtype])
        lo, hi = RETAILER_TYPE_CREDIT_RANGE[rtype]
        rows.append({
            "id": i,
            "name": f"{town} {suffix} {i}",
            "town": town,
            "district": DISTRICT,
            "retailer_type": rtype,
            "credit_limit": round(rng.uniform(lo, hi), -2),
        })
    return rows


def seed_orders_and_children(
    rng: random.Random, products: list[dict], retailers: list[dict]
) -> tuple[list[dict], list[dict], list[dict]]:
    order_dates = sorted(build_order_date_pool(rng))
    orders, order_items, payments = [], [], []
    order_item_id = 1
    payment_id = 1

    for order_id, order_date in enumerate(order_dates, start=1):
        retailer = rng.choice(retailers)
        status = rng.choices(
            ["delivered", "pending", "cancelled"], weights=[0.82, 0.08, 0.10], k=1
        )[0]

        delivery_date = None
        if status == "delivered":
            delivery_date = order_date + timedelta(days=rng.randint(1, 5))
        elif status == "pending":
            # order placed recently, not yet delivered
            delivery_date = None

        n_items = rng.randint(1, 6)
        chosen_products = rng.sample(products, k=min(n_items, len(products)))
        total_amount = 0.0
        for product in chosen_products:
            quantity = rng.randint(1, 40)
            unit_price = round(product["unit_price"] * rng.uniform(0.95, 1.05), 2)
            order_items.append({
                "id": order_item_id,
                "order_id": order_id,
                "product_id": product["id"],
                "quantity": quantity,
                "unit_price": unit_price,
            })
            total_amount += quantity * unit_price
            order_item_id += 1

        total_amount = round(total_amount, 2)
        orders.append({
            "id": order_id,
            "retailer_id": retailer["id"],
            "order_date": order_date.isoformat(),
            "delivery_date": delivery_date.isoformat() if delivery_date else None,
            "status": status,
            "total_amount": total_amount,
        })

        if status == "delivered":
            pay_roll = rng.random()
            if pay_roll < 0.70:  # fully paid
                pay_amount = total_amount
            elif pay_roll < 0.90:  # partially paid
                pay_amount = round(total_amount * rng.uniform(0.2, 0.8), 2)
            else:  # unpaid (credit, no payment row yet)
                pay_amount = None

            if pay_amount is not None:
                payment_date = delivery_date + timedelta(days=rng.randint(0, 15))
                method = rng.choices(PAYMENT_METHODS, weights=PAYMENT_METHOD_WEIGHTS, k=1)[0]
                payments.append({
                    "id": payment_id,
                    "order_id": order_id,
                    "payment_date": payment_date.isoformat(),
                    "amount": pay_amount,
                    "method": method,
                })
                payment_id += 1
        elif status == "pending" and rng.random() < 0.25:
            # occasional advance payment on a pending order
            payment_date = order_date + timedelta(days=rng.randint(0, 2))
            method = rng.choices(PAYMENT_METHODS, weights=PAYMENT_METHOD_WEIGHTS, k=1)[0]
            payments.append({
                "id": payment_id,
                "order_id": order_id,
                "payment_date": payment_date.isoformat(),
                "amount": round(total_amount * rng.uniform(0.1, 0.5), 2),
                "method": method,
            })
            payment_id += 1

    return orders, order_items, payments


def seed_inventory(rng: random.Random, products: list[dict]) -> list[dict]:
    rows = []
    for i, product in enumerate(products, start=1):
        stockout = rng.random() < 0.10
        stock_quantity = rng.randint(0, 5) if stockout else rng.randint(20, 500)
        restocked = END_DATE - timedelta(days=rng.randint(0, 30))
        rows.append({
            "id": i,
            "product_id": product["id"],
            "stock_quantity": stock_quantity,
            "last_restocked": restocked.isoformat(),
        })
    return rows


def main(force: bool) -> None:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)

    if settings.database_path.exists():
        if not force:
            print(f"{settings.database_path} already exists. Re-seeding (drop + recreate).")
        settings.database_path.unlink()

    conn = sqlite3.connect(settings.database_path)
    conn.executescript(DDL)

    rng = random.Random(SEED)
    products = seed_products(rng)
    retailers = seed_retailers(rng)
    orders, order_items, payments = seed_orders_and_children(rng, products, retailers)
    inventory = seed_inventory(rng, products)

    with conn:
        conn.executemany(
            "INSERT INTO products VALUES (:id, :name, :category, :unit_price, :bottle_size_ml)",
            products,
        )
        conn.executemany(
            "INSERT INTO retailers VALUES (:id, :name, :town, :district, :retailer_type, :credit_limit)",
            retailers,
        )
        conn.executemany(
            "INSERT INTO orders VALUES (:id, :retailer_id, :order_date, :delivery_date, :status, :total_amount)",
            orders,
        )
        conn.executemany(
            "INSERT INTO order_items VALUES (:id, :order_id, :product_id, :quantity, :unit_price)",
            order_items,
        )
        conn.executemany(
            "INSERT INTO payments VALUES (:id, :order_id, :payment_date, :amount, :method)",
            payments,
        )
        conn.executemany(
            "INSERT INTO inventory VALUES (:id, :product_id, :stock_quantity, :last_restocked)",
            inventory,
        )
        conn.execute("PRAGMA journal_mode=WAL")

    print(f"Seeded {settings.database_path}")
    print(f"  products:     {len(products)}")
    print(f"  retailers:    {len(retailers)}")
    print(f"  orders:       {len(orders)}")
    print(f"  order_items:  {len(order_items)}")
    print(f"  payments:     {len(payments)}")
    print(f"  inventory:    {len(inventory)}")
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="skip the existing-file notice")
    args = parser.parse_args()
    main(force=args.force)
