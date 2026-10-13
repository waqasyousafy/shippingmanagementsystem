import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEMO_DATABASE = BASE_DIR / "database" / "demo_shippingflow.db"
LIVE_DATABASE = BASE_DIR / "database" / "shippingflow.db"

if DEMO_DATABASE.resolve() == LIVE_DATABASE.resolve():
    raise RuntimeError("Demo data must never be written to the live database.")

os.environ["SHIPPINGFLOW_DATABASE"] = str(DEMO_DATABASE)
os.environ["SHIPPINGFLOW_SKIP_LEGACY_IMPORT"] = "1"

import app as shippingflow  # noqa: E402


DEMO_PASSWORD = "DemoTraining123!"
CUSTOMERS = [
    ("CUST-001", "Karachi Traders", "Karachi", 10, "Kitchen appliances"),
    ("CUST-002", "Lahore Home Store", "Lahore", 6, "Home textiles"),
    ("CUST-003", "Islamabad Tech House", "Islamabad", 4, "Computer accessories"),
    ("CUST-004", "Faisalabad Fabrics", "Faisalabad", 12, "Fabric rolls"),
    ("CUST-005", "Peshawar Imports", "Peshawar", 8, "Lighting equipment"),
    ("CUST-006", "Multan Commercial", "Multan", 5, "Shop fittings"),
    ("CUST-007", "Quetta Supply Co", "Quetta", 7, "Solar accessories"),
    ("CUST-008", "Hyderabad Wholesale", "Hyderabad", 9, "Plastic housewares"),
    ("CUST-009", "Sialkot Sports Goods", "Sialkot", 11, "Sports equipment"),
    ("CUST-010", "Rawalpindi Machinery", "Rawalpindi", 3, "Machine parts"),
]
STAFF = [
    ("demo.admin", "admin"),
    ("china.warehouse", "warehouse"),
    ("pakistan.ops", "operations"),
    ("delivery.team", "delivery"),
    ("accounts.team", "accounts"),
    ("audit.viewer", "viewer"),
]


def seed_demo_data():
    with shippingflow.database_connection() as connection:
        connection.execute("DELETE FROM shipment_status_history")
        connection.execute("DELETE FROM shipment_photos")
        connection.execute("DELETE FROM shipment_loadings")
        connection.execute("DELETE FROM shipment_documents")
        connection.execute("DELETE FROM shipment_bills")
        connection.execute("DELETE FROM shipment_bill_revisions")
        connection.execute("DELETE FROM audit_log")
        connection.execute("DELETE FROM shipments")
        connection.execute("DELETE FROM customers")
        connection.execute("DELETE FROM users")

        for username, role in STAFF:
            shippingflow.create_user(connection, username, DEMO_PASSWORD, role)

        for index, (code, name, city, cartons, description) in enumerate(CUSTOMERS, start=1):
            connection.execute(
                """INSERT INTO customers
                   (id, company_name, customer_code, delivery_city, delivery_address, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    f"demo-customer-{index:03d}", name, code, city,
                    f"Training delivery address, {city}",
                    f"2026-10-{10 + (index % 3):02d}T08:00:00Z",
                ),
            )
            connection.execute(
                """INSERT INTO shipments (
                    id, tracking_number, customer_code, shipping_mark, supplier_name,
                    warehouse_receiving_date, goods_description, cartons, weight_kg, cbm,
                    status, status_note, remaining_cartons, last_updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"demo-shipment-{index:03d}", f"MH-DEMO-{index:03d}", code,
                    f"MH-{code}", f"Supplier {index:02d}, Yiwu",
                    f"2026-10-{10 + (index % 3):02d}", description, cartons,
                    float(cartons * 12), round(cartons * 0.08, 2),
                    "China warehouse received", "Training cargo received in China warehouse",
                    cartons, shippingflow.utc_now(),
                ),
            )
            connection.execute(
                """INSERT INTO shipment_status_history
                   (shipment_id, stage, date, updated_by, remarks)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    f"demo-shipment-{index:03d}", "China warehouse received",
                    f"2026-10-{10 + (index % 3):02d}", "china.warehouse",
                    "Received, counted and recorded for training",
                ),
            )

    return len(CUSTOMERS), len(STAFF)


if __name__ == "__main__":
    customer_count, staff_count = seed_demo_data()
    print(f"Training database ready: {DEMO_DATABASE}")
    print(f"Seeded {customer_count} customers/cargo lots and {staff_count} staff logins.")
    print(f"Demo-only password for every account: {DEMO_PASSWORD}")
