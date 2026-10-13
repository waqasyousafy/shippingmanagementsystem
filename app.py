import json
import math
import os
import re
import secrets
import sqlite3
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from io import BytesIO
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from xml.sax.saxutils import escape

from flask import Flask, g, has_request_context, jsonify, request, send_file, session
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
DATABASE_DIR = BASE_DIR / "database"
DATABASE_FILE = Path(
    os.environ.get("SHIPPINGFLOW_DATABASE", str(DATABASE_DIR / "shippingflow.db"))
).resolve()
SCHEMA_FILE = DATABASE_DIR / "schema.sql"
SECRET_FILE = DATABASE_DIR / "session.secret"
UPLOAD_DIR = DATABASE_FILE.parent / "uploads"
LEGACY_DATA_FILE = BASE_DIR / "data" / "store.json"

DATABASE_DIR.mkdir(parents=True, exist_ok=True)
if os.environ.get("FLASK_SECRET_KEY"):
    SECRET_KEY = os.environ["FLASK_SECRET_KEY"]
else:
    if not SECRET_FILE.exists():
        SECRET_FILE.write_text(secrets.token_urlsafe(48), encoding="utf-8")
    SECRET_KEY = SECRET_FILE.read_text(encoding="utf-8").strip()

app = Flask(__name__, static_folder="public", static_url_path="/static")
app.config.update(
    SECRET_KEY=SECRET_KEY,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE") == "1",
    MAX_CONTENT_LENGTH=25 * 1024 * 1024,
)
ROLE_PERMISSIONS = {
    "admin": {"customers:read", "customers:write", "customers:edit", "shipments:read", "shipments:receive", "shipments:operate", "shipments:status", "shipments:deliver", "containers:operate", "invoices:read", "invoices:write", "invoices:edit", "users:manage"},
    "warehouse": {"customers:read", "shipments:read", "shipments:receive", "shipments:operate", "shipments:status"},
    "operations": {"customers:read", "shipments:read", "shipments:operate", "shipments:status", "containers:operate"},
    "delivery": {"customers:read", "shipments:read", "shipments:deliver", "shipments:status"},
    "accounts": {"customers:read", "shipments:read", "invoices:read", "invoices:write", "invoices:edit"},
    "viewer": {"customers:read", "shipments:read"},
}

PERMISSION_LABELS = {
    "customers:read": "View customers and addresses",
    "customers:write": "Register customers",
    "customers:edit": "Edit customer details",
    "customers:archive": "Archive and restore customers",
    "shipments:read": "View cargo and shipment records",
    "shipments:receive": "Receive cargo and upload documents",
    "shipments:operate": "Record container loading",
    "shipments:edit": "Edit cargo and shipment details",
    "shipments:archive": "Archive and restore cargo lots",
    "shipments:status": "Update shipment milestones",
    "shipments:deliver": "Confirm cargo delivery",
    "containers:operate": "Update container milestones",
    "containers:edit": "Edit container loading portions",
    "containers:archive": "Archive and restore containers",
    "invoices:read": "View bills and payment records",
    "invoices:write": "Create and approve bills",
    "invoices:edit": "Edit and revise bills",
    "invoices:archive": "Archive and restore bills",
    "audit:read": "View the immutable audit history",
    "users:manage": "Manage staff accounts and access",
    "database:manage": "Inspect, query and back up the database",
}
SUPER_ADMIN_ASSIGNED_PERMISSIONS = {
    "audit:read",
}
PERMISSION_DEPENDENCIES = {
    "customers:write": {"customers:read"},
    "customers:edit": {"customers:read"},
    "customers:archive": {"customers:read", "shipments:read"},
    "shipments:receive": {"shipments:read"},
    "shipments:operate": {"shipments:read"},
    "shipments:edit": {"shipments:read"},
    "shipments:archive": {"shipments:read"},
    "shipments:status": {"shipments:read"},
    "shipments:deliver": {"shipments:read"},
    "containers:operate": {"shipments:read"},
    "containers:edit": {"shipments:read"},
    "containers:archive": {"shipments:read"},
    "invoices:write": {"invoices:read"},
    "invoices:edit": {"invoices:read", "invoices:write"},
    "invoices:archive": {"invoices:read"},
}

SHIPMENT_STAGES = (
    "China warehouse received",
    "Awaiting loading",
    "Loaded in container",
    "Departed from China",
    "In transit",
    "Arrived at Pakistan port",
    "Customs clearance in progress",
    "Customs cleared",
    "Transport dispatched",
    "Delivered",
)
STAGE_ORDER = {stage: index for index, stage in enumerate(SHIPMENT_STAGES)}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def today():
    return datetime.now(timezone.utc).date().isoformat()


def is_iso_date(value):
    if value is None or value == "":
        return True
    try:
        datetime.strptime(str(value), "%Y-%m-%d")
    except ValueError:
        return False
    return True


def connect_database():
    connection = sqlite3.connect(DATABASE_FILE)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def connect_database_readonly():
    database_uri = f"{DATABASE_FILE.as_uri()}?mode=ro"
    connection = sqlite3.connect(database_uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


@contextmanager
def readonly_database_connection():
    connection = connect_database_readonly()
    try:
        yield connection
    finally:
        connection.close()


def effective_permissions(user):
    if user["is_super_admin"]:
        return set(PERMISSION_LABELS)
    saved = user["permissions_json"]
    if saved is None:
        raise RuntimeError(f"Permissions were not initialized for user {user['id']}.")
    try:
        permissions = json.loads(saved)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Invalid stored permissions for user {user['id']}.") from error
    if not isinstance(permissions, list) or any(
        permission not in PERMISSION_LABELS for permission in permissions
    ):
        raise RuntimeError(f"Invalid stored permissions for user {user['id']}.")
    return set(permissions) - {"database:manage"}


@contextmanager
def database_connection():
    connection = connect_database()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def audit_snapshot(row):
    return dict(row) if row is not None else None


def append_audit(connection, entity_type, entity_id, action, before=None, after=None):
    actor = getattr(g, "current_user", None) if has_request_context() else None
    connection.execute(
        """INSERT INTO audit_log
           (actor_id, actor_username, entity_type, entity_id, action, changed_at,
            before_json, after_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            actor["id"] if actor else None,
            actor["username"] if actor else "system",
            entity_type,
            str(entity_id),
            action,
            utc_now(),
            json.dumps(before, ensure_ascii=False, sort_keys=True) if before is not None else None,
            json.dumps(after, ensure_ascii=False, sort_keys=True) if after is not None else None,
        ),
    )


def record_snapshot_changes(connection, before, after):
    labels = {
        "customers": "customer",
        "shipments": "shipment",
        "shipment_loadings": "container portion",
        "shipment_bills": "invoice",
        "shipment_documents": "shipment document",
    }
    identifiers = {
        "customers": "id",
        "shipments": "id",
        "shipment_loadings": "id",
        "shipment_bills": "shipment_id",
        "shipment_documents": "id",
    }
    for table, entity_type in labels.items():
        key = identifiers[table]
        old_rows = {str(row[key]): row for row in before[table]}
        new_rows = {str(row[key]): row for row in after[table]}
        for entity_id in sorted(old_rows.keys() | new_rows.keys()):
            old = old_rows.get(entity_id)
            new = new_rows.get(entity_id)
            if old == new:
                continue
            action = "created" if old is None else "updated" if new is not None else "deleted"
            append_audit(connection, entity_type, entity_id, action, old, new)


def setup_is_required():
    with database_connection() as connection:
        return not connection.execute("SELECT EXISTS(SELECT 1 FROM users)").fetchone()[0]


def public_user(user):
    permissions = user.get("permissions")
    if permissions is None:
        saved_permissions = user.get("permissions_json")
        if saved_permissions is not None:
            permissions = json.loads(saved_permissions)
        else:
            raise RuntimeError(f"Permissions were not initialized for user {user['id']}.")
    return {
        "id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "isSuperAdmin": bool(user.get("is_super_admin", user.get("isSuperAdmin", False))),
        "permissions": sorted(permissions),
    }


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    with database_connection() as connection:
        row = connection.execute(
            """SELECT id, username, role, permissions_json, is_super_admin
               FROM users WHERE id = ? AND is_active = 1""",
            (user_id,),
        ).fetchone()
        user = dict(row) if row is not None else None
        if user is not None:
            user["permissions"] = effective_permissions(row)
    if row is None:
        session.clear()
        return None
    return user


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            return jsonify({"error": "Login required."}), 401
        g.current_user = user
        return view(*args, **kwargs)
    return wrapped


def permissions_required(*permissions):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if not set(permissions).issubset(g.current_user["permissions"]):
                return jsonify({"error": "Your account does not have permission for this action."}), 403
            return view(*args, **kwargs)
        return wrapped
    return decorator


def valid_username(username):
    return bool(re.fullmatch(r"[a-zA-Z0-9._-]{3,64}", username))


def valid_password(password):
    return isinstance(password, str) and len(password) >= 12


def create_user(connection, username, password, role, is_super_admin=False):
    user_id = f"user-{uuid.uuid4().hex}"
    permissions = sorted(
        PERMISSION_LABELS if is_super_admin else ROLE_PERMISSIONS[role]
    )
    connection.execute(
        """INSERT INTO users
           (id, username, password_hash, role, created_at, permissions_json, is_super_admin)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            user_id, username, generate_password_hash(password), role, utc_now(),
            json.dumps(permissions), int(is_super_admin),
        ),
    )
    return {
        "id": user_id,
        "username": username,
        "role": role,
        "isSuperAdmin": is_super_admin,
        "permissions": permissions,
    }


def provision_super_admin(username, password):
    if not valid_username(username):
        raise ValueError("Super-admin username is invalid.")
    if not valid_password(password):
        raise ValueError("Super-admin password must be at least 12 characters.")
    with database_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            """SELECT id, role, is_super_admin FROM users
               WHERE username = ? COLLATE NOCASE""",
            (username,),
        ).fetchone()
        other_super_admin = connection.execute(
            """SELECT EXISTS(
                   SELECT 1 FROM users
                   WHERE is_super_admin = 1 AND id != ?
               )""",
            (existing["id"] if existing is not None else "",),
        ).fetchone()[0]
        if other_super_admin:
            raise ValueError("A super-admin account already exists; no account was changed.")
        if existing is not None:
            if existing["is_super_admin"]:
                raise ValueError("That super-admin account is already provisioned; no account was changed.")
            connection.execute(
                """UPDATE users SET password_hash = ?, role = 'admin', is_active = 1,
                          permissions_json = ?, is_super_admin = 1
                   WHERE id = ?""",
                (
                    generate_password_hash(password),
                    json.dumps(sorted(PERMISSION_LABELS)),
                    existing["id"],
                ),
            )
            return {
                "id": existing["id"],
                "username": username,
                "role": "admin",
                "isSuperAdmin": True,
                "promoted": True,
            }
        return create_user(
            connection,
            username,
            password,
            "admin",
            is_super_admin=True,
        )


def invoice_fields(payload, existing=None):
    values = {
        "invoiceNumber": payload.get("invoiceNumber", existing["invoice_number"] if existing else ""),
        "invoiceDate": payload.get("invoiceDate", existing["invoice_date"] if existing else today()),
        "totalAmount": payload.get("totalAmount", existing["total_amount"] if existing else None),
        "receivedPayment": payload.get("receivedPayment", existing["received_payment"] if existing else 0),
        "currency": payload.get("currency", existing["currency"] if existing else "PKR"),
        "status": payload.get("status", existing["status"] if existing else "draft"),
    }
    values["invoiceNumber"] = str(values["invoiceNumber"] or "").strip()
    values["invoiceDate"] = str(values["invoiceDate"] or "").strip()
    values["currency"] = str(values["currency"] or "").strip().upper()
    values["status"] = str(values["status"] or "").strip().lower()
    values["status"] = {"approved": "issued", "pending": "draft"}.get(values["status"], values["status"])
    if not values["invoiceNumber"] or len(values["invoiceNumber"]) > 64:
        raise ValueError("Invoice number is required and must be 64 characters or fewer.")
    if not values["invoiceDate"]:
        raise ValueError("Invoice date is required.")
    try:
        datetime.strptime(values["invoiceDate"], "%Y-%m-%d")
    except ValueError:
        raise ValueError("Invoice date must use YYYY-MM-DD format.") from None
    if not re.fullmatch(r"[A-Z]{3}", values["currency"]):
        raise ValueError("Currency must be a three-letter code, such as PKR or USD.")
    if values["status"] not in {"draft", "issued", "paid", "void"}:
        raise ValueError("Choose a valid invoice status.")
    try:
        total = Decimal(str(values["totalAmount"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        received = Decimal(str(values["receivedPayment"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Invoice total and received payment must be valid amounts.") from None
    if total <= 0 or received < 0 or received > total:
        raise ValueError("Invoice total must be positive and received payment must be between zero and the total.")
    if received == total and values["status"] in {"issued", "paid"}:
        values["status"] = "paid"
    elif values["status"] == "paid":
        raise ValueError("An invoice can only be marked paid when the full amount has been received.")
    values["totalAmount"] = float(total)
    values["receivedPayment"] = float(received)
    values["outstandingBalance"] = float(total - received)
    return values


def invoice_dict(row):
    return {
        "shipmentId": row["shipment_id"],
        "trackingNumber": row["tracking_number"],
        "customerCode": row["customer_code"],
        "companyName": row["company_name"],
        "deliveryCity": row["delivery_city"],
        "deliveryAddress": row["delivery_address"],
        "goodsDescription": row["goods_description"],
        "cartons": row["cartons"],
        "weightKg": row["weight_kg"],
        "cbm": row["cbm"],
        "invoiceNumber": row["invoice_number"],
        "invoiceDate": row["invoice_date"],
        "totalAmount": row["total_amount"],
        "receivedPayment": row["received_payment"],
        "outstandingBalance": row["outstanding_balance"],
        "currency": row["currency"],
        "status": {"approved": "issued", "pending": "draft"}.get(row["status"], row["status"]),
    }


INVOICE_QUERY = """SELECT
    shipment_bills.shipment_id, shipment_bills.invoice_number, shipment_bills.invoice_date,
    shipment_bills.total_amount, shipment_bills.received_payment,
    shipment_bills.outstanding_balance, shipment_bills.currency, shipment_bills.status,
    shipments.tracking_number, shipments.customer_code, shipments.goods_description,
    shipments.cartons, shipments.weight_kg, shipments.cbm,
    customers.company_name, customers.delivery_city, customers.delivery_address
FROM shipment_bills
JOIN shipments ON shipments.id = shipment_bills.shipment_id
JOIN customers ON customers.customer_code = shipments.customer_code
WHERE shipment_bills.archived_at IS NULL
  AND shipments.archived_at IS NULL
  AND customers.archived_at IS NULL
"""


def make_invoice_pdf(invoice):
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=f"Invoice {invoice['invoiceNumber']}",
        author="MH Global Customs Solutions",
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="InvoiceTitle", parent=styles["Title"], textColor=colors.HexColor("#174b39"), alignment=TA_RIGHT, fontSize=22, leading=26))
    styles.add(ParagraphStyle(name="SmallMuted", parent=styles["BodyText"], textColor=colors.HexColor("#63766b"), fontSize=8, leading=11))
    styles.add(ParagraphStyle(name="InvoiceSection", parent=styles["Heading2"], textColor=colors.HexColor("#174b39"), fontSize=11, leading=14, spaceAfter=5))
    story = [
        Table(
            [[
                Paragraph("<b>MH GLOBAL CUSTOMS SOLUTIONS</b><br/><font color='#63766b'>Shipping and freight services</font>", styles["BodyText"]),
                Paragraph("INVOICE", styles["InvoiceTitle"]),
            ]],
            colWidths=[105 * mm, 65 * mm],
            style=TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, -1), 1, colors.HexColor("#dce4dc")),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
            ]),
        ),
        Spacer(1, 9 * mm),
    ]
    invoice_meta = Table(
        [
            ["Invoice number", escape(invoice["invoiceNumber"]), "Invoice date", escape(invoice["invoiceDate"])],
            ["Status", escape(invoice["status"].title()), "Currency", escape(invoice["currency"])],
        ],
        colWidths=[32 * mm, 53 * mm, 28 * mm, 57 * mm],
        style=TableStyle([
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f1f4ee")),
            ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#f1f4ee")),
            ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#63766b")),
            ("TEXTCOLOR", (2, 0), (2, -1), colors.HexColor("#63766b")),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dce4dc")),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("PADDING", (0, 0), (-1, -1), 8),
        ]),
    )
    story.extend([
        invoice_meta,
        Spacer(1, 8 * mm),
        Paragraph("Bill to", styles["InvoiceSection"]),
        Paragraph(
            f"<b>{escape(invoice['companyName'])}</b><br/>Customer code: {escape(invoice['customerCode'])}<br/>{escape(invoice['deliveryAddress'])}<br/>{escape(invoice['deliveryCity'])}",
            styles["BodyText"],
        ),
        Spacer(1, 8 * mm),
        Paragraph("Shipment details", styles["InvoiceSection"]),
    ])
    shipment_table = Table(
        [
            ["Tracking number", "Goods description", "Cartons", "Weight", "Volume"],
            [
                escape(invoice["trackingNumber"]),
                Paragraph(escape(invoice["goodsDescription"]), styles["BodyText"]),
                str(invoice["cartons"]),
                f"{invoice['weightKg']:,.2f} kg",
                f"{invoice['cbm']:,.2f} CBM",
            ],
        ],
        colWidths=[32 * mm, 65 * mm, 20 * mm, 27 * mm, 26 * mm],
        repeatRows=1,
        style=TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#174b39")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dce4dc")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("PADDING", (0, 0), (-1, -1), 8),
        ]),
    )
    story.extend([shipment_table, Spacer(1, 8 * mm)])
    amount_rows = [
        ["Invoice total", f"{invoice['currency']} {invoice['totalAmount']:,.2f}"],
        ["Payment received", f"{invoice['currency']} {invoice['receivedPayment']:,.2f}"],
        ["Balance due", f"{invoice['currency']} {invoice['outstandingBalance']:,.2f}"],
    ]
    story.append(Table(
        amount_rows,
        colWidths=[115 * mm, 55 * mm],
        hAlign="RIGHT",
        style=TableStyle([
            ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ("LINEABOVE", (0, 0), (-1, 0), 0.75, colors.HexColor("#dce4dc")),
            ("LINEABOVE", (0, 2), (-1, 2), 1.25, colors.HexColor("#174b39")),
            ("FONTNAME", (0, 2), (-1, 2), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 10),
            ("PADDING", (0, 0), (-1, -1), 8),
        ]),
    ))
    story.extend([
        Spacer(1, 15 * mm),
        Paragraph("Thank you for your business.", styles["InvoiceSection"]),
        Paragraph("This invoice was generated by ShippingFlow.", styles["SmallMuted"]),
    ])
    document.build(story)
    buffer.seek(0)
    return buffer


def load_data():
    data = {"customers": [], "shipments": [], "invoices": []}
    with database_connection() as connection:
        data["customers"] = [
            {
                "id": row["id"],
                "companyName": row["company_name"],
                "customerCode": row["customer_code"],
                "deliveryCity": row["delivery_city"],
                "deliveryAddress": row["delivery_address"],
                "createdAt": row["created_at"],
                "archivedAt": row["archived_at"],
                "archivedBy": row["archived_by"],
            }
            for row in connection.execute("SELECT * FROM customers ORDER BY company_name")
        ]
        for row in connection.execute("SELECT * FROM shipments ORDER BY warehouse_receiving_date DESC"):
            shipment = {
                "id": row["id"],
                "trackingNumber": row["tracking_number"],
                "customerCode": row["customer_code"],
                "shippingMark": row["shipping_mark"],
                "supplierName": row["supplier_name"],
                "warehouseReceivingDate": row["warehouse_receiving_date"],
                "goodsDescription": row["goods_description"],
                "cartons": row["cartons"],
                "weightKg": row["weight_kg"],
                "cbm": row["cbm"],
                "status": row["status"],
                "statusNote": row["status_note"],
                "containerNumber": row["container_number"],
                "loadingDate": row["loading_date"],
                "loadedCartons": row["loaded_cartons"],
                "remainingCartons": row["remaining_cartons"],
                "departurePort": row["departure_port"],
                "destinationPort": row["destination_port"],
                "vessel": row["vessel"],
                "voyage": row["voyage"],
                "blReference": row["bl_reference"],
                "expectedDepartureDate": row["expected_departure_date"],
                "expectedArrivalDate": row["expected_arrival_date"],
                "expectedDeliveryDate": row["expected_delivery_date"],
                "lastUpdatedAt": row["last_updated_at"],
                "archivedAt": row["archived_at"],
                "archivedBy": row["archived_by"],
                "statusHistory": [],
                "photos": [],
                "documents": [],
                "loadingSegments": [],
                "bill": None,
            }
            shipment["documents"] = [
                {
                    "id": document["id"],
                    "documentType": document["document_type"],
                    "fileName": document["file_name"],
                    "downloadUrl": f"/api/documents/{document['id']}",
                    "uploadedAt": document["uploaded_at"],
                    "uploadedBy": document["uploaded_by"],
                }
                for document in connection.execute(
                    "SELECT * FROM shipment_documents WHERE shipment_id = ? ORDER BY uploaded_at, id",
                    (row["id"],),
                )
            ]
            shipment["loadingSegments"] = [
                {
                    "id": loading["id"],
                    "containerNumber": loading["container_number"],
                    "loadingDate": loading["loading_date"],
                    "loadedCartons": loading["loaded_cartons"],
                    "departurePort": loading["departure_port"],
                    "destinationPort": loading["destination_port"],
                    "vessel": loading["vessel"],
                    "voyage": loading["voyage"],
                    "blReference": loading["bl_reference"],
                    "expectedDepartureDate": loading["expected_departure_date"],
                    "expectedArrivalDate": loading["expected_arrival_date"],
                    "expectedDeliveryDate": loading["expected_delivery_date"],
                    "createdBy": loading["created_by"],
                    "status": loading["status"],
                    "statusUpdatedAt": loading["status_updated_at"],
                    "statusUpdatedBy": loading["status_updated_by"],
                    "statusNote": loading["status_note"],
                    "archivedAt": loading["archived_at"],
                    "archivedBy": loading["archived_by"],
                }
                for loading in connection.execute(
                    "SELECT * FROM shipment_loadings WHERE shipment_id = ? ORDER BY loading_date, id",
                    (row["id"],),
                )
            ]
            shipment["statusHistory"] = [
                {
                    "stage": history["stage"],
                    "date": history["date"],
                    "updatedBy": history["updated_by"],
                    "remarks": history["remarks"],
                }
                for history in connection.execute(
                    "SELECT * FROM shipment_status_history WHERE shipment_id = ? ORDER BY id",
                    (row["id"],),
                )
            ]
            shipment["photos"] = [
                photo["photo_url"]
                for photo in connection.execute(
                    "SELECT photo_url FROM shipment_photos WHERE shipment_id = ? ORDER BY id",
                    (row["id"],),
                )
            ]
            bill = connection.execute(
                "SELECT * FROM shipment_bills WHERE shipment_id = ?", (row["id"],)
            ).fetchone()
            if bill:
                shipment["bill"] = {
                    "invoiceNumber": bill["invoice_number"],
                    "invoiceDate": bill["invoice_date"],
                    "totalAmount": bill["total_amount"],
                    "receivedPayment": bill["received_payment"],
                    "outstandingBalance": bill["outstanding_balance"],
                    "currency": bill["currency"],
                    "status": bill["status"],
                    "archivedAt": bill["archived_at"],
                    "archivedBy": bill["archived_by"],
                }
            data["shipments"].append(shipment)
    return data


def save_data(data):
    with database_connection() as connection:
        audited_tables = (
            "customers", "shipments", "shipment_loadings", "shipment_bills",
            "shipment_documents",
        )
        before = {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in audited_tables
        }
        documents = connection.execute("SELECT * FROM shipment_documents").fetchall()
        invoice_revisions = connection.execute("SELECT * FROM shipment_bill_revisions").fetchall()
        connection.execute("DELETE FROM shipment_status_history")
        connection.execute("DELETE FROM shipment_photos")
        connection.execute("DELETE FROM shipment_loadings")
        connection.execute("DELETE FROM shipment_bills")
        connection.execute("DELETE FROM shipment_bill_revisions")
        connection.execute("DELETE FROM shipment_documents")
        connection.execute("DELETE FROM shipments")
        connection.execute("DELETE FROM customers")
        for customer in data["customers"]:
            connection.execute(
                """INSERT INTO customers
                   (id, company_name, customer_code, delivery_city, delivery_address, created_at,
                    archived_at, archived_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    customer["id"], customer["companyName"], customer["customerCode"],
                    customer.get("deliveryCity", ""), customer.get("deliveryAddress", ""),
                    customer.get("createdAt") or utc_now(),
                    customer.get("archivedAt"), customer.get("archivedBy"),
                ),
            )
        for shipment in data["shipments"]:
            connection.execute(
                """INSERT INTO shipments (
                    id, tracking_number, customer_code, shipping_mark, supplier_name,
                    warehouse_receiving_date, goods_description, cartons, weight_kg, cbm,
                    status, status_note, container_number, loading_date, loaded_cartons,
                    remaining_cartons, departure_port, destination_port, vessel, voyage,
                    bl_reference, expected_departure_date, expected_arrival_date,
                    expected_delivery_date, last_updated_at, archived_at, archived_by
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )""",
                (
                    shipment["id"], shipment["trackingNumber"], shipment["customerCode"],
                    shipment.get("shippingMark", ""), shipment.get("supplierName", ""),
                    shipment.get("warehouseReceivingDate", ""), shipment.get("goodsDescription", ""),
                    shipment.get("cartons", 0), shipment.get("weightKg", 0), shipment.get("cbm", 0),
                    shipment.get("status", ""), shipment.get("statusNote", ""),
                    shipment.get("containerNumber", ""), shipment.get("loadingDate", ""),
                    shipment.get("loadedCartons", 0), shipment.get("remainingCartons", 0),
                    shipment.get("departurePort", ""), shipment.get("destinationPort", ""),
                    shipment.get("vessel", ""), shipment.get("voyage", ""),
                    shipment.get("blReference", ""), shipment.get("expectedDepartureDate", ""),
                    shipment.get("expectedArrivalDate", ""), shipment.get("expectedDeliveryDate", ""),
                    shipment.get("lastUpdatedAt") or utc_now(),
                    shipment.get("archivedAt"), shipment.get("archivedBy"),
                ),
            )
            for history in shipment.get("statusHistory", []):
                connection.execute(
                    """INSERT INTO shipment_status_history
                       (shipment_id, stage, date, updated_by, remarks) VALUES (?, ?, ?, ?, ?)""",
                    (
                        shipment["id"], history.get("stage", ""), history.get("date", ""),
                        history.get("updatedBy", ""), history.get("remarks", ""),
                    ),
                )
            for photo_url in shipment.get("photos", []):
                connection.execute(
                    "INSERT INTO shipment_photos (shipment_id, photo_url) VALUES (?, ?)",
                    (shipment["id"], photo_url),
                )
            for loading in shipment.get("loadingSegments", []):
                connection.execute(
                    """INSERT INTO shipment_loadings
                       (id, shipment_id, container_number, loading_date, loaded_cartons,
                        departure_port, destination_port, vessel, voyage, bl_reference,
                        expected_departure_date, expected_arrival_date, expected_delivery_date,
                        created_by, status, status_updated_at, status_updated_by, status_note,
                        archived_at, archived_by)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        loading.get("id") or f"load-{uuid.uuid4().hex}", shipment["id"],
                        loading.get("containerNumber", ""), loading.get("loadingDate", ""),
                        loading.get("loadedCartons", 0), loading.get("departurePort", ""),
                        loading.get("destinationPort", ""), loading.get("vessel", ""),
                        loading.get("voyage", ""), loading.get("blReference", ""),
                        loading.get("expectedDepartureDate", ""), loading.get("expectedArrivalDate", ""),
                        loading.get("expectedDeliveryDate", ""), loading.get("createdBy", ""),
                        loading.get("status", "Loaded in container"),
                        loading.get("statusUpdatedAt", ""), loading.get("statusUpdatedBy", ""),
                        loading.get("statusNote", ""),
                        loading.get("archivedAt"), loading.get("archivedBy"),
                    ),
                )
            bill = shipment.get("bill") or {}
            if bill:
                connection.execute(
                    """INSERT INTO shipment_bills
                       (shipment_id, invoice_number, invoice_date, total_amount,
                        received_payment, outstanding_balance, currency, status, archived_at, archived_by)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        shipment["id"], bill.get("invoiceNumber", ""), bill.get("invoiceDate", ""),
                        bill.get("totalAmount", 0), bill.get("receivedPayment", 0),
                        bill.get("outstandingBalance", 0), bill.get("currency", ""),
                        bill.get("status", "pending"),
                        bill.get("archivedAt"), bill.get("archivedBy"),
                    ),
                )
        for document in documents:
            connection.execute(
                """INSERT INTO shipment_documents
                   (id, shipment_id, document_type, file_name, stored_name, content_type, uploaded_at, uploaded_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    document["id"], document["shipment_id"], document["document_type"],
                    document["file_name"], document["stored_name"], document["content_type"],
                    document["uploaded_at"], document["uploaded_by"],
                ),
            )
        for revision in invoice_revisions:
            connection.execute(
                """INSERT INTO shipment_bill_revisions
                   (shipment_id, invoice_number, invoice_date, total_amount, received_payment,
                    outstanding_balance, currency, status, superseded_at, superseded_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    revision["shipment_id"], revision["invoice_number"], revision["invoice_date"],
                    revision["total_amount"], revision["received_payment"],
                    revision["outstanding_balance"], revision["currency"], revision["status"],
                    revision["superseded_at"], revision["superseded_by"],
                ),
            )
        after = {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in audited_tables
        }
        record_snapshot_changes(connection, before, after)


def initialize_database():
    DATABASE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with database_connection() as connection:
        connection.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        for table, columns in {
            "customers": {
                "archived_at": "TEXT",
                "archived_by": "TEXT",
            },
            "shipments": {
                "archived_at": "TEXT",
                "archived_by": "TEXT",
            },
            "shipment_loadings": {
                "archived_at": "TEXT",
                "archived_by": "TEXT",
            },
            "shipment_bills": {
                "archived_at": "TEXT",
                "archived_by": "TEXT",
            },
        }.items():
            existing_columns = {
                column["name"]
                for column in connection.execute(f"PRAGMA table_info({table})")
            }
            for name, definition in columns.items():
                if name not in existing_columns:
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                    )
        loading_columns = {
            column["name"]
            for column in connection.execute("PRAGMA table_info(shipment_loadings)")
        }
        for name, definition in (
            ("status", "TEXT NOT NULL DEFAULT 'Loaded in container'"),
            ("status_updated_at", "TEXT NOT NULL DEFAULT ''"),
            ("status_updated_by", "TEXT NOT NULL DEFAULT ''"),
            ("status_note", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in loading_columns:
                connection.execute(
                    f"ALTER TABLE shipment_loadings ADD COLUMN {name} {definition}"
                )
        user_columns = {
            column["name"] for column in connection.execute("PRAGMA table_info(users)")
        }
        if "permissions_json" not in user_columns:
            connection.execute("ALTER TABLE users ADD COLUMN permissions_json TEXT")
        if "is_super_admin" not in user_columns:
            connection.execute(
                "ALTER TABLE users ADD COLUMN is_super_admin INTEGER NOT NULL DEFAULT 0"
            )
        user_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'"
        ).fetchone()["sql"]
        if "'delivery'" not in user_sql or "'accounts'" not in user_sql:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("ALTER TABLE users RENAME TO users_legacy")
            connection.execute(
                """CREATE TABLE users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN
                        ('admin', 'warehouse', 'operations', 'delivery', 'accounts', 'viewer')),
                    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
                    created_at TEXT NOT NULL,
                    permissions_json TEXT,
                    is_super_admin INTEGER NOT NULL DEFAULT 0 CHECK (is_super_admin IN (0, 1))
                )"""
            )
            connection.execute(
                """INSERT INTO users
                   (id, username, password_hash, role, is_active, created_at,
                    permissions_json, is_super_admin)
                   SELECT id, username, password_hash, role, is_active, created_at,
                          permissions_json, is_super_admin
                   FROM users_legacy"""
            )
            connection.execute("DROP TABLE users_legacy")
            connection.execute("PRAGMA foreign_keys = ON")
        for role, permissions in ROLE_PERMISSIONS.items():
            connection.execute(
                """UPDATE users SET permissions_json = ?
                   WHERE role = ? AND permissions_json IS NULL AND is_super_admin = 0""",
                (json.dumps(sorted(permissions)), role),
            )
        connection.execute(
            """UPDATE users SET permissions_json = ?
               WHERE permissions_json IS NULL AND is_super_admin = 1""",
            (json.dumps(sorted(PERMISSION_LABELS)),),
        )
        legacy_loadings = connection.execute(
            "SELECT id, container_number, loading_date, loaded_cartons FROM shipments "
            "WHERE container_number != '' AND loaded_cartons > 0 "
            "AND NOT EXISTS (SELECT 1 FROM shipment_loadings WHERE shipment_id = shipments.id)"
        ).fetchall()
        for row in legacy_loadings:
            connection.execute(
                """INSERT INTO shipment_loadings
                   (id, shipment_id, container_number, loading_date, loaded_cartons)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    f"load-{uuid.uuid4().hex}", row["id"], row["container_number"],
                    row["loading_date"] or "", row["loaded_cartons"],
                ),
            )
        has_customers = connection.execute("SELECT EXISTS(SELECT 1 FROM customers)").fetchone()[0]
    if (
        not has_customers
        and os.environ.get("SHIPPINGFLOW_SKIP_LEGACY_IMPORT") != "1"
        and LEGACY_DATA_FILE.exists()
    ):
        try:
            with LEGACY_DATA_FILE.open("r", encoding="utf-8") as legacy_file:
                legacy_data = json.load(legacy_file)
        except (OSError, json.JSONDecodeError):
            return
        legacy_data["customers"] = [
            {
                "id": customer.get("id") or f"cust-{uuid.uuid4().hex}",
                "companyName": customer.get("companyName", ""),
                "customerCode": customer.get("customerCode", ""),
                "deliveryCity": customer.get("deliveryCity", ""),
                "deliveryAddress": customer.get("deliveryAddress", ""),
                "createdAt": customer.get("createdAt") or utc_now(),
            }
            for customer in legacy_data.get("customers", [])
        ]
        known_codes = {customer["customerCode"] for customer in legacy_data["customers"]}
        legacy_data["shipments"] = [
            {
                **shipment,
                "id": shipment.get("id") or f"ship-{uuid.uuid4().hex}",
                "statusHistory": shipment.get("statusHistory", []),
                "photos": shipment.get("photos", []),
            }
            for shipment in legacy_data.get("shipments", [])
            if shipment.get("customerCode") in known_codes
        ]
        save_data(legacy_data)


def add_status_history(shipment, stage, updated_by, remarks):
    shipment.setdefault("statusHistory", []).append({
        "stage": stage,
        "date": today(),
        "updatedBy": updated_by,
        "remarks": remarks,
    })
    shipment["lastUpdatedAt"] = utc_now()


def find_shipment(value):
    return next(
        (shipment for shipment in load_data()["shipments"]
         if not shipment.get("archivedAt")
         and (shipment.get("id") == value or shipment.get("trackingNumber") == value)),
        None,
    )


def get_shipments_for_customer(customer_code):
    return [
        shipment for shipment in load_data()["shipments"]
        if shipment.get("customerCode") == customer_code and not shipment.get("archivedAt")
    ]


@app.get("/api/session")
def get_session():
    user = current_user()
    return jsonify({
        "authenticated": user is not None,
        "setupRequired": setup_is_required(),
        "user": public_user(user) if user else None,
    })


@app.post("/api/setup")
def setup_admin():
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username") or "").strip().lower()
    password = payload.get("password")
    if not valid_username(username):
        return jsonify({"error": "Username must be 3-64 characters using letters, numbers, dots, underscores, or hyphens."}), 400
    if not valid_password(password):
        return jsonify({"error": "Password must be at least 12 characters."}), 400

    with database_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT EXISTS(SELECT 1 FROM users)").fetchone()[0]:
            return jsonify({"error": "Initial setup has already been completed."}), 409
        user = create_user(connection, username, password, "admin")
    session.clear()
    session["user_id"] = user["id"]
    return jsonify({"user": public_user(user)}), 201


@app.post("/api/login")
def login():
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username") or "").strip()
    password = payload.get("password")
    with database_connection() as connection:
        user = connection.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE AND is_active = 1",
            (username,),
        ).fetchone()
    if user is None or not check_password_hash(user["password_hash"], str(password or "")):
        return jsonify({"error": "Username or password is incorrect."}), 401

    session.clear()
    session["user_id"] = user["id"]
    user_data = dict(user)
    user_data["permissions"] = effective_permissions(user)
    return jsonify({"user": public_user(user_data)})


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.post("/api/password")
@login_required
def change_password():
    payload = request.get_json(silent=True) or {}
    current_password = str(payload.get("currentPassword") or "")
    new_password = payload.get("newPassword")
    if not valid_password(new_password):
        return jsonify({"error": "New password must be at least 12 characters."}), 400

    with database_connection() as connection:
        user = connection.execute(
            "SELECT password_hash FROM users WHERE id = ?", (g.current_user["id"],)
        ).fetchone()
        if user is None or not check_password_hash(user["password_hash"], current_password):
            return jsonify({"error": "Current password is incorrect."}), 400
        connection.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash(new_password), g.current_user["id"]),
        )
    return jsonify({"ok": True})


@app.get("/api/users")
@permissions_required("users:manage")
def get_users():
    with database_connection() as connection:
        users = connection.execute(
            """SELECT id, username, role, is_active, created_at, permissions_json,
                      is_super_admin FROM users ORDER BY username"""
        ).fetchall()
    return jsonify([
        {
            "id": user["id"],
            "username": user["username"],
            "role": user["role"],
            "isSuperAdmin": bool(user["is_super_admin"]),
            "isActive": bool(user["is_active"]),
            "createdAt": user["created_at"],
            "permissions": sorted(effective_permissions(user)),
        }
        for user in users
    ])


@app.post("/api/users")
@permissions_required("users:manage")
def post_user():
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username") or "").strip().lower()
    password = payload.get("password")
    role = str(payload.get("role") or "")
    if not valid_username(username):
        return jsonify({"error": "Username must be 3-64 characters using letters, numbers, dots, underscores, or hyphens."}), 400
    if not valid_password(password):
        return jsonify({"error": "Password must be at least 12 characters."}), 400
    if role not in ROLE_PERMISSIONS:
        return jsonify({"error": "Choose a valid user role."}), 400

    with database_connection() as connection:
        try:
            user = create_user(connection, username, password, role)
            permissions = sorted(ROLE_PERMISSIONS[role])
            connection.execute(
                "UPDATE users SET permissions_json = ? WHERE id = ?",
                (json.dumps(permissions), user["id"]),
            )
            append_audit(
                connection,
                "user",
                user["id"],
                "created",
                None,
                {
                    "id": user["id"],
                    "username": user["username"],
                    "role": role,
                    "isActive": True,
                    "permissions": permissions,
                },
            )
        except sqlite3.IntegrityError:
            return jsonify({"error": "That username is already in use."}), 409
    return jsonify({
        **user,
        "isSuperAdmin": False,
        "isActive": True,
        "permissions": permissions,
    }), 201


@app.post("/api/users/<user_id>/password")
@login_required
def reset_user_password(user_id):
    if not g.current_user["is_super_admin"]:
        return jsonify({"error": "Only the super administrator can reset another user's password."}), 403
    payload = request.get_json(silent=True) or {}
    password = payload.get("password")
    if not valid_password(password):
        return jsonify({"error": "Password must be at least 12 characters."}), 400
    with database_connection() as connection:
        user = connection.execute(
            "SELECT id, username FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if user is None:
            return jsonify({"error": "User not found."}), 404
        before = {"userId": user["id"], "username": user["username"]}
        connection.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash(password), user_id),
        )
        append_audit(connection, "user", user_id, "password reset", before, before)
    return jsonify({"ok": True, "userId": user_id})


@app.patch("/api/users/<user_id>")
@permissions_required("users:manage")
def update_user_access(user_id):
    payload = request.get_json(silent=True) or {}
    with database_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        user = connection.execute(
            """SELECT id, username, role, is_active, permissions_json, is_super_admin
               FROM users WHERE id = ?""",
            (user_id,),
        ).fetchone()
        if user is None:
            return jsonify({"error": "User not found."}), 404
        if user["is_super_admin"] and not g.current_user["is_super_admin"]:
            return jsonify({"error": "Only the super administrator can manage this account."}), 403

        role = payload.get("role", user["role"])
        is_active = payload.get("isActive", bool(user["is_active"]))
        permissions = payload.get("permissions")
        if role not in ROLE_PERMISSIONS:
            return jsonify({"error": "Choose a valid user role."}), 400
        if not isinstance(is_active, bool):
            return jsonify({"error": "isActive must be true or false."}), 400
        if user["is_super_admin"] and (role != "admin" or not is_active):
            return jsonify({"error": "The super-administrator account must remain active with administrator access."}), 400
        before = {
            "id": user["id"],
            "username": user["username"],
            "role": user["role"],
            "isActive": bool(user["is_active"]),
            "permissions": sorted(effective_permissions(user)),
        }
        if permissions is not None:
            if not isinstance(permissions, list) or any(
                not isinstance(permission, str) or permission not in PERMISSION_LABELS
                for permission in permissions
            ):
                return jsonify({"error": "Choose permissions from the supported access list."}), 400
            permissions = sorted(set(permissions))
            if not g.current_user["is_super_admin"]:
                current_permissions = set(effective_permissions(user))
                if any(
                    (permission in current_permissions) != (permission in permissions)
                    for permission in SUPER_ADMIN_ASSIGNED_PERMISSIONS
                ):
                    return jsonify({
                        "error": "Only the super administrator can change audit-history access."
                    }), 403
            if "database:manage" in permissions and not g.current_user["is_super_admin"]:
                return jsonify({"error": "Database access is reserved for the super administrator."}), 403
            missing_dependencies = {
                dependency
                for permission in permissions
                for dependency in PERMISSION_DEPENDENCIES.get(permission, set())
                if dependency not in permissions
            }
            if missing_dependencies:
                return jsonify({
                    "error": "Some permissions require their related read access.",
                    "requiredPermissions": sorted(missing_dependencies),
                }), 400
        if user_id == g.current_user["id"] and (
            role != user["role"]
            or not is_active
            or (permissions is not None and set(permissions) != g.current_user["permissions"])
        ):
            return jsonify({"error": "You cannot change your own access or disable your account."}), 400

        removes_admin = user["role"] == "admin" and bool(user["is_active"]) and (
            role != "admin" or not is_active
        )
        if removes_admin:
            active_admins = connection.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1"
            ).fetchone()[0]
            if active_admins <= 1:
                return jsonify({"error": "At least one active administrator must remain."}), 409

        connection.execute(
            "UPDATE users SET role = ?, is_active = ?, permissions_json = ? WHERE id = ?",
            (
                role, int(is_active),
                json.dumps(permissions) if permissions is not None else user["permissions_json"],
                user_id,
            ),
        )
        updated_user = connection.execute(
            """SELECT id, username, role, is_active, permissions_json, is_super_admin
               FROM users WHERE id = ?""",
            (user_id,),
        ).fetchone()
        after = {
            "id": updated_user["id"],
            "username": updated_user["username"],
            "role": updated_user["role"],
            "isActive": bool(updated_user["is_active"]),
            "permissions": sorted(effective_permissions(updated_user)),
        }
        if before != after:
            append_audit(connection, "user", user_id, "access updated", before, after)
    if permissions is not None:
        updated_permissions = set(permissions)
    else:
        updated_permissions = effective_permissions(user)
    return jsonify({
        "id": user["id"],
        "username": user["username"],
        "role": role,
        "isSuperAdmin": bool(user["is_super_admin"]),
        "isActive": is_active,
        "permissions": sorted(updated_permissions),
    })


@app.get("/api/admin/database")
@permissions_required("database:manage")
def get_database_catalog():
    try:
        with readonly_database_connection() as connection:
            tables = connection.execute(
                """SELECT name FROM sqlite_master
                   WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                   ORDER BY name"""
            ).fetchall()
            catalog = []
            for table in tables:
                escaped_name = table["name"].replace('"', '""')
                columns = connection.execute(
                    f'PRAGMA table_info("{escaped_name}")'
                ).fetchall()
                catalog.append({
                    "name": table["name"],
                    "columns": [
                        {
                            "name": column["name"],
                            "type": column["type"],
                            "required": bool(column["notnull"]),
                            "primaryKey": bool(column["pk"]),
                        }
                        for column in columns
                    ],
                })
    except sqlite3.Error:
        app.logger.exception("Unable to inspect the configured SQLite database.")
        return jsonify({"error": "Could not inspect the configured database."}), 500
    return jsonify({"databasePath": str(DATABASE_FILE), "tables": catalog})


@app.post("/api/admin/database/query")
@permissions_required("database:manage")
def run_database_query():
    payload = request.get_json(silent=True) or {}
    sql = payload.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        return jsonify({"error": "Enter a read-only SQL query."}), 400
    statement = sql.strip()
    if not re.match(r"^(SELECT|WITH|EXPLAIN)\b", statement, re.IGNORECASE):
        return jsonify({"error": "Only SELECT, WITH and EXPLAIN queries are allowed."}), 400
    try:
        with readonly_database_connection() as connection:
            connection.set_progress_handler(lambda: 1, 250000)
            cursor = connection.execute(statement)
            if cursor.description is None:
                return jsonify({"error": "The query must return rows."}), 400
            columns = [description[0] for description in cursor.description]
            raw_rows = cursor.fetchmany(501)
            rows = [
                [
                    value.hex() if isinstance(value, bytes) else value
                    for value in tuple(row)
                ]
                for row in raw_rows[:500]
            ]
    except sqlite3.OperationalError as error:
        if "interrupted" in str(error).lower():
            return jsonify({"error": "Query exceeded its execution limit. Narrow the query and try again."}), 400
        return jsonify({"error": f"SQL query failed: {error}"}), 400
    except sqlite3.Error as error:
        return jsonify({"error": f"SQL query failed: {error}"}), 400
    return jsonify({
        "columns": columns,
        "rows": rows,
        "returnedRows": len(rows),
        "truncated": len(raw_rows) > 500,
        "readOnly": True,
    })


@app.get("/api/admin/database/backup")
@permissions_required("database:manage")
def download_database_backup():
    try:
        with readonly_database_connection() as source:
            snapshot = sqlite3.connect(":memory:")
            try:
                source.backup(snapshot)
                backup_bytes = snapshot.serialize()
            finally:
                snapshot.close()
    except sqlite3.Error:
        app.logger.exception("Unable to create a database snapshot.")
        return jsonify({"error": "Could not create the database backup."}), 500
    return send_file(
        BytesIO(backup_bytes),
        mimetype="application/vnd.sqlite3",
        as_attachment=True,
        download_name=f"{DATABASE_FILE.stem}-backup-{today()}.db",
    )


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "message": "Cargo tracker API is running"})


@app.get("/api/customers")
@permissions_required("customers:read")
def get_customers():
    return jsonify([
        customer for customer in load_data()["customers"]
        if not customer.get("archivedAt")
    ])


@app.post("/api/customers")
@permissions_required("customers:write")
def post_customer():
    payload = request.get_json(silent=True) or {}
    if not payload.get("companyName") or not payload.get("customerCode"):
        return jsonify({"error": "companyName and customerCode are required."}), 400
    company_name = str(payload["companyName"]).strip()
    customer_code = str(payload["customerCode"]).strip()
    if not company_name or not customer_code:
        return jsonify({"error": "companyName and customerCode cannot be blank."}), 400

    data = load_data()
    if any(customer.get("customerCode") == customer_code for customer in data["customers"]):
        return jsonify({"error": "Customer code already exists."}), 409

    customer = {
        "id": f"cust-{uuid.uuid4().hex}",
        "companyName": company_name,
        "customerCode": customer_code,
        "deliveryCity": str(payload.get("deliveryCity") or "").strip(),
        "deliveryAddress": str(payload.get("deliveryAddress") or "").strip(),
        "createdAt": utc_now(),
    }
    data["customers"].append(customer)
    save_data(data)
    return jsonify(customer), 201


@app.patch("/api/customers/<customer_code>")
@permissions_required("customers:edit")
def update_customer(customer_code):
    payload = request.get_json(silent=True) or {}
    if set(payload) - {"companyName", "deliveryCity", "deliveryAddress"}:
        return jsonify({"error": "Only customer name and delivery details can be edited here."}), 400
    with database_connection() as connection:
        customer = connection.execute(
            "SELECT * FROM customers WHERE customer_code = ? AND archived_at IS NULL",
            (customer_code,),
        ).fetchone()
        if customer is None:
            return jsonify({"error": "Customer not found."}), 404
        before = audit_snapshot(customer)
        company_name = str(payload.get("companyName", customer["company_name"])).strip()
        if not company_name:
            return jsonify({"error": "Company name is required."}), 400
        delivery_city = str(payload.get("deliveryCity", customer["delivery_city"]) or "").strip()
        delivery_address = str(payload.get("deliveryAddress", customer["delivery_address"]) or "").strip()
        connection.execute(
            "UPDATE customers SET company_name = ?, delivery_city = ?, delivery_address = ? WHERE customer_code = ?",
            (company_name, delivery_city, delivery_address, customer_code),
        )
        updated = connection.execute(
            "SELECT * FROM customers WHERE customer_code = ?", (customer_code,)
        ).fetchone()
        append_audit(connection, "customer", customer["id"], "updated", before, audit_snapshot(updated))
    return jsonify({
        "id": updated["id"],
        "companyName": updated["company_name"],
        "customerCode": updated["customer_code"],
        "deliveryCity": updated["delivery_city"],
        "deliveryAddress": updated["delivery_address"],
        "createdAt": updated["created_at"],
    })

@app.get("/api/shipments")
@permissions_required("shipments:read")
def get_shipments():
    shipments = [
        shipment for shipment in load_data()["shipments"]
        if not shipment.get("archivedAt")
    ]
    if "invoices:read" not in g.current_user["permissions"]:
        for shipment in shipments:
            shipment["bill"] = None
    else:
        for shipment in shipments:
            if shipment["bill"] and shipment["bill"].get("archivedAt"):
                shipment["bill"] = None
    return jsonify(shipments)


@app.get("/api/shipments/<shipment_id>")
@permissions_required("shipments:read")
def get_shipment(shipment_id):
    shipment = find_shipment(shipment_id)
    if not shipment:
        return jsonify({"error": "Shipment not found."}), 404
    if "invoices:read" not in g.current_user["permissions"]:
        shipment["bill"] = None
    elif shipment["bill"] and shipment["bill"].get("archivedAt"):
        shipment["bill"] = None
    return jsonify(shipment)


@app.post("/api/shipments/<shipment_id>/documents")
@permissions_required("shipments:receive")
def upload_shipment_documents(shipment_id):
    shipment = find_shipment(shipment_id)
    if shipment is None:
        return jsonify({"error": "Shipment not found."}), 404
    incoming = [
        ("photo", photo) for photo in request.files.getlist("photos")
    ]
    packing_list = request.files.get("packingList")
    if packing_list and packing_list.filename:
        incoming.append(("packing_list", packing_list))
    if not incoming:
        return jsonify({"error": "Choose at least one cargo photo or packing list."}), 400

    allowed = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".pdf": "application/pdf",
    }
    validated = []
    for document_type, upload in incoming:
        original_name = secure_filename(upload.filename or "")
        extension = Path(original_name).suffix.lower()
        if not original_name or extension not in allowed or upload.mimetype != allowed[extension]:
            return jsonify({"error": "Only JPEG/PNG photos and PDF packing lists are accepted."}), 400
        if document_type == "photo" and extension == ".pdf":
            return jsonify({"error": "Cargo photos must be JPEG or PNG images."}), 400
        if document_type == "packing_list" and extension != ".pdf":
            return jsonify({"error": "Packing lists must be PDF files."}), 400
        validated.append((document_type, upload, original_name, allowed[extension]))

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    saved = []
    with database_connection() as connection:
        for document_type, upload, original_name, content_type in validated:
            document_id = f"doc-{uuid.uuid4().hex}"
            stored_name = f"{uuid.uuid4().hex}{Path(original_name).suffix.lower()}"
            upload.save(UPLOAD_DIR / stored_name)
            connection.execute(
                """INSERT INTO shipment_documents
                   (id, shipment_id, document_type, file_name, stored_name, content_type, uploaded_at, uploaded_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    document_id, shipment["id"], document_type, original_name, stored_name,
                    content_type, utc_now(), g.current_user["username"],
                ),
            )
            document = connection.execute(
                "SELECT * FROM shipment_documents WHERE id = ?", (document_id,)
            ).fetchone()
            append_audit(
                connection, "shipment document", document_id, "created", None, audit_snapshot(document)
            )
            saved.append({
                "id": document_id,
                "documentType": document_type,
                "fileName": original_name,
                "downloadUrl": f"/api/documents/{document_id}",
            })
    return jsonify(saved), 201


@app.get("/api/documents/<document_id>")
@permissions_required("shipments:read")
def download_shipment_document(document_id):
    with database_connection() as connection:
        document = connection.execute(
            "SELECT stored_name, file_name, content_type FROM shipment_documents WHERE id = ?",
            (document_id,),
        ).fetchone()
    if document is None:
        return jsonify({"error": "Document not found."}), 404
    path = UPLOAD_DIR / document["stored_name"]
    if not path.is_file():
        return jsonify({"error": "The stored document is missing; contact an administrator."}), 404
    return send_file(
        path,
        mimetype=document["content_type"],
        as_attachment=True,
        download_name=document["file_name"],
    )


@app.get("/api/containers")
@permissions_required("shipments:read")
def get_containers():
    with database_connection() as connection:
        rows = connection.execute(
            """SELECT shipment_loadings.container_number,
                      MIN(shipment_loadings.loading_date) AS loading_date,
                      MAX(shipment_loadings.expected_arrival_date) AS expected_arrival_date,
                      COUNT(DISTINCT shipment_loadings.shipment_id) AS shipment_count,
                      SUM(shipment_loadings.loaded_cartons) AS loaded_cartons
               FROM shipment_loadings
               JOIN shipments ON shipments.id = shipment_loadings.shipment_id
               WHERE shipment_loadings.archived_at IS NULL
                 AND shipments.archived_at IS NULL
               GROUP BY shipment_loadings.container_number
               ORDER BY loading_date DESC"""
        ).fetchall()
    return jsonify([dict(row) for row in rows])


@app.patch("/api/container-portions/<loading_id>")
@permissions_required("containers:edit")
def edit_container_portion(loading_id):
    payload = request.get_json(silent=True) or {}
    allowed = {
        "containerNumber", "loadingDate", "loadedCartons", "departurePort",
        "destinationPort", "vessel", "voyage", "blReference",
        "expectedDepartureDate", "expectedArrivalDate", "expectedDeliveryDate",
    }
    if not payload or set(payload) - allowed:
        return jsonify({"error": "Only container portion details may be changed."}), 400
    data = load_data()
    shipment = next(
        (
            item for item in data["shipments"]
            if not item.get("archivedAt") and any(
                portion["id"] == loading_id and not portion.get("archivedAt")
                for portion in item.get("loadingSegments", [])
            )
        ),
        None,
    )
    if shipment is None:
        return jsonify({"error": "Active container portion not found."}), 404
    segment = next(
        portion for portion in shipment["loadingSegments"] if portion["id"] == loading_id
    )
    container_number = str(payload.get("containerNumber", segment["containerNumber"])).strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", container_number):
        return jsonify({"error": "A valid container number is required."}), 400
    loading_date = payload.get("loadingDate", segment["loadingDate"])
    date_fields = {
        "loadingDate": loading_date,
        "expectedDepartureDate": payload.get("expectedDepartureDate", segment["expectedDepartureDate"]),
        "expectedArrivalDate": payload.get("expectedArrivalDate", segment["expectedArrivalDate"]),
        "expectedDeliveryDate": payload.get("expectedDeliveryDate", segment["expectedDeliveryDate"]),
    }
    if not loading_date or any(not is_iso_date(value) for value in date_fields.values()):
        return jsonify({"error": "Loading and ETA dates must use YYYY-MM-DD format."}), 400
    if date_fields["expectedDepartureDate"] and date_fields["expectedArrivalDate"] and (
        date_fields["expectedArrivalDate"] < date_fields["expectedDepartureDate"]
    ):
        return jsonify({"error": "Expected arrival cannot be before expected departure."}), 400
    if date_fields["expectedArrivalDate"] and date_fields["expectedDeliveryDate"] and (
        date_fields["expectedDeliveryDate"] < date_fields["expectedArrivalDate"]
    ):
        return jsonify({"error": "Delivery estimate cannot be before the Pakistan arrival ETA."}), 400
    try:
        quantity_value = Decimal(str(payload.get("loadedCartons", segment["loadedCartons"])))
        if not quantity_value.is_finite() or quantity_value != quantity_value.to_integral_value():
            raise ValueError
        quantity = int(quantity_value)
    except (InvalidOperation, TypeError, ValueError):
        return jsonify({"error": "Loaded cartons must be a whole number."}), 400
    other_loaded = sum(
        int(portion["loadedCartons"] or 0)
        for portion in shipment["loadingSegments"]
        if portion["id"] != loading_id
    )
    if quantity <= 0 or other_loaded + quantity > int(shipment["cartons"] or 0):
        return jsonify({"error": "Loaded cartons must be positive and cannot exceed the received cargo."}), 409

    segment.update({
        "containerNumber": container_number,
        "loadingDate": loading_date,
        "loadedCartons": quantity,
        "departurePort": str(payload.get("departurePort", segment["departurePort"]) or "").strip(),
        "destinationPort": str(payload.get("destinationPort", segment["destinationPort"]) or "").strip(),
        "vessel": str(payload.get("vessel", segment["vessel"]) or "").strip(),
        "voyage": str(payload.get("voyage", segment["voyage"]) or "").strip(),
        "blReference": str(payload.get("blReference", segment["blReference"]) or "").strip(),
        "expectedDepartureDate": date_fields["expectedDepartureDate"] or "",
        "expectedArrivalDate": date_fields["expectedArrivalDate"] or "",
        "expectedDeliveryDate": date_fields["expectedDeliveryDate"] or "",
    })
    shipment["loadedCartons"] = other_loaded + quantity
    shipment["remainingCartons"] = int(shipment["cartons"]) - shipment["loadedCartons"]
    shipment["status"] = (
        "Partial loading" if shipment["remainingCartons"] > 0
        else min(
            (
                portion.get("status", "Loaded in container")
                for portion in shipment["loadingSegments"]
                if not portion.get("archivedAt")
            ),
            key=lambda value: STAGE_ORDER.get(value, 0),
        )
    )
    shipment["lastUpdatedAt"] = utc_now()
    save_data(data)
    return jsonify({"shipment": shipment, "portion": segment})


@app.post("/api/containers/<container_number>/status")
@permissions_required("containers:operate")
def update_container_status(container_number):
    payload = request.get_json(silent=True) or {}
    stage = str(payload.get("stage") or "").strip()
    if stage not in SHIPMENT_STAGES:
        return jsonify({"error": "Choose a valid shipment milestone."}), 400
    if stage == "Delivered":
        return jsonify({"error": "Delivery must be confirmed separately for each customer cargo lot."}), 400
    with database_connection() as connection:
        shipment_rows = connection.execute(
            """SELECT DISTINCT shipments.id, shipments.cartons, shipments.loaded_cartons FROM shipments
               JOIN shipment_loadings ON shipment_loadings.shipment_id = shipments.id
               WHERE shipment_loadings.container_number = ?
                 AND shipment_loadings.archived_at IS NULL
                 AND shipments.archived_at IS NULL""",
            (container_number,),
        ).fetchall()
        if not shipment_rows:
            return jsonify({"error": "Container not found."}), 404
        if stage == "Loaded in container" and any(
            row["cartons"] <= 0 or row["loaded_cartons"] < row["cartons"] for row in shipment_rows
        ):
            return jsonify({"error": "Cargo with remaining cartons cannot be marked fully loaded."}), 400
        before_portions = [
            dict(row) for row in connection.execute(
                """SELECT shipment_loadings.* FROM shipment_loadings
                   JOIN shipments ON shipments.id = shipment_loadings.shipment_id
                   WHERE shipment_loadings.container_number = ?
                     AND shipment_loadings.archived_at IS NULL
                     AND shipments.archived_at IS NULL""",
                (container_number,),
            )
        ]
        before_shipments = {
            row["id"]: audit_snapshot(connection.execute(
                "SELECT * FROM shipments WHERE id = ?", (row["id"],)
            ).fetchone())
            for row in shipment_rows
        }
        connection.execute(
            """UPDATE shipment_loadings SET status = ?, status_updated_at = ?,
               status_updated_by = ?, status_note = ?
               WHERE container_number = ? AND archived_at IS NULL
                 AND shipment_id IN (SELECT id FROM shipments WHERE archived_at IS NULL)""",
            (
                stage, utc_now(), g.current_user["username"],
                payload.get("remarks") or stage, container_number,
            ),
        )
        for row in shipment_rows:
            segments = connection.execute(
                "SELECT status FROM shipment_loadings WHERE shipment_id = ? AND archived_at IS NULL",
                (row["id"],)
            ).fetchall()
            if row["loaded_cartons"] < row["cartons"]:
                shipment_stage = "Partial loading" if row["loaded_cartons"] else "Awaiting loading"
            elif segments:
                shipment_stage = min(
                    (segment["status"] for segment in segments),
                    key=lambda value: STAGE_ORDER.get(value, 0),
                )
            else:
                shipment_stage = stage
            remarks = f"Container {container_number}: {payload.get('remarks') or stage}"
            connection.execute(
                "UPDATE shipments SET status = ?, status_note = ?, last_updated_at = ? WHERE id = ?",
                (shipment_stage, remarks, utc_now(), row["id"]),
            )
            connection.execute(
                """INSERT INTO shipment_status_history
                   (shipment_id, stage, date, updated_by, remarks) VALUES (?, ?, ?, ?, ?)""",
                (row["id"], stage, today(), g.current_user["username"], remarks),
            )
            updated_shipment = connection.execute(
                "SELECT * FROM shipments WHERE id = ?", (row["id"],)
            ).fetchone()
            append_audit(
                connection,
                "shipment",
                row["id"],
                "updated",
                before_shipments[row["id"]],
                audit_snapshot(updated_shipment),
            )
        for portion in before_portions:
            updated_portion = connection.execute(
                "SELECT * FROM shipment_loadings WHERE id = ?", (portion["id"],)
            ).fetchone()
            append_audit(
                connection,
                "container portion",
                portion["id"],
                "updated",
                portion,
                audit_snapshot(updated_portion),
            )
    return jsonify({"containerNumber": container_number, "status": stage, "updatedShipments": len(shipment_rows)})


ARCHIVE_PERMISSIONS = {
    "customers": "customers:archive",
    "shipments": "shipments:archive",
    "containers": "containers:archive",
    "invoices": "invoices:archive",
}


def archived_entity_state(connection, entity_type, entity_id):
    if entity_type == "customers":
        row = connection.execute(
            "SELECT * FROM customers WHERE id = ?", (entity_id,)
        ).fetchone()
        return [row] if row else []
    if entity_type == "shipments":
        row = connection.execute(
            "SELECT * FROM shipments WHERE id = ?", (entity_id,)
        ).fetchone()
        return [row] if row else []
    if entity_type == "invoices":
        row = connection.execute(
            "SELECT * FROM shipment_bills WHERE shipment_id = ?", (entity_id,)
        ).fetchone()
        return [row] if row else []
    if entity_type == "containers":
        return connection.execute(
            "SELECT * FROM shipment_loadings WHERE container_number = ? ORDER BY id",
            (entity_id,),
        ).fetchall()
    raise ValueError("Unsupported archived entity type.")


@app.post("/api/records/<entity_type>/<path:entity_identifier>/archive")
@login_required
def archive_record(entity_type, entity_identifier):
    permission = ARCHIVE_PERMISSIONS.get(entity_type)
    if permission is None:
        return jsonify({"error": "Unsupported record type."}), 404
    if permission not in g.current_user["permissions"]:
        return jsonify({"error": "Your account does not have permission for this action."}), 403
    with database_connection() as connection:
        if entity_type == "customers":
            entity = connection.execute(
                "SELECT * FROM customers WHERE customer_code = ? OR id = ?",
                (entity_identifier, entity_identifier),
            ).fetchone()
            if entity is None:
                return jsonify({"error": "Customer not found."}), 404
            if connection.execute(
                "SELECT EXISTS(SELECT 1 FROM shipments WHERE customer_code = ? AND archived_at IS NULL)",
                (entity["customer_code"],),
            ).fetchone()[0]:
                return jsonify({
                    "error": "Archive this customer's active cargo lots before archiving the customer."
                }), 409
            audit_id = entity["id"]
            before = audit_snapshot(entity)
            table = "customers"
            where = "id = ?"
        elif entity_type == "shipments":
            entity = connection.execute(
                "SELECT * FROM shipments WHERE id = ? OR tracking_number = ?",
                (entity_identifier, entity_identifier),
            ).fetchone()
            if entity is None:
                return jsonify({"error": "Shipment not found."}), 404
            audit_id = entity["id"]
            before = audit_snapshot(entity)
            table = "shipments"
            where = "id = ?"
        elif entity_type == "invoices":
            shipment = connection.execute(
                "SELECT id FROM shipments WHERE id = ? OR tracking_number = ?",
                (entity_identifier, entity_identifier),
            ).fetchone()
            entity = connection.execute(
                "SELECT * FROM shipment_bills WHERE shipment_id = ?",
                (shipment["id"],) if shipment else (entity_identifier,),
            ).fetchone()
            if entity is None:
                return jsonify({"error": "Invoice not found."}), 404
            audit_id = entity["shipment_id"]
            before = audit_snapshot(entity)
            table = "shipment_bills"
            where = "shipment_id = ?"
        else:
            entity_rows = connection.execute(
                "SELECT * FROM shipment_loadings WHERE container_number = ? ORDER BY id",
                (entity_identifier,),
            ).fetchall()
            if not entity_rows:
                return jsonify({"error": "Container not found."}), 404
            audit_id = entity_identifier
            before = [dict(row) for row in entity_rows]
            table = "shipment_loadings"
            where = "container_number = ?"

        rows = archived_entity_state(connection, entity_type, audit_id)
        if rows and all(row["archived_at"] for row in rows):
            return jsonify({"error": "This record is already archived."}), 409
        if entity_type == "containers":
            connection.execute(
                """UPDATE shipment_loadings SET archived_at = ?, archived_by = ?
                   WHERE container_number = ? AND archived_at IS NULL""",
                (utc_now(), g.current_user["username"], entity_identifier),
            )
            after = [
                dict(row) for row in connection.execute(
                    "SELECT * FROM shipment_loadings WHERE container_number = ? ORDER BY id",
                    (entity_identifier,),
                )
            ]
        else:
            connection.execute(
                f"UPDATE {table} SET archived_at = ?, archived_by = ? WHERE {where}",
                (utc_now(), g.current_user["username"], audit_id),
            )
            after_row = connection.execute(
                f"SELECT * FROM {table} WHERE {where}", (audit_id,)
            ).fetchone()
            after = audit_snapshot(after_row)
        append_audit(connection, entity_type[:-1] if entity_type != "containers" else "container", audit_id, "archived", before, after)
    return jsonify({"ok": True, "entityType": entity_type, "entityId": audit_id})


def entity_is_archived(connection, entity_type, entity_id):
    rows = archived_entity_state(connection, entity_type, entity_id)
    return bool(rows) and all(row["archived_at"] for row in rows)


@app.get("/api/audit")
@permissions_required("audit:read")
def get_audit_log():
    try:
        limit = min(max(int(request.args.get("limit", "200")), 1), 500)
    except ValueError:
        return jsonify({"error": "Audit limit must be a number from 1 to 500."}), 400
    with database_connection() as connection:
        rows = connection.execute(
            """SELECT id, actor_id, actor_username, entity_type, entity_id, action,
                      changed_at, before_json, after_json
               FROM audit_log ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        events = []
        for row in rows:
            entity_type = row["entity_type"]
            archive_type = {
                "customer": "customers",
                "shipment": "shipments",
                "container": "containers",
                "invoice": "invoices",
            }.get(entity_type)
            can_restore = bool(
                archive_type
                and row["action"] == "archived"
                and ARCHIVE_PERMISSIONS[archive_type] in g.current_user["permissions"]
                and entity_is_archived(connection, archive_type, row["entity_id"])
            )
            events.append({
                "id": row["id"],
                "actorId": row["actor_id"],
                "actorUsername": row["actor_username"],
                "entityType": entity_type,
                "entityId": row["entity_id"],
                "action": row["action"],
                "changedAt": row["changed_at"],
                "before": json.loads(row["before_json"]) if row["before_json"] else None,
                "after": json.loads(row["after_json"]) if row["after_json"] else None,
                "canRestore": can_restore,
            })
    return jsonify(events)


@app.post("/api/audit/<int:audit_id>/restore")
@login_required
def restore_archived_record(audit_id):
    with database_connection() as connection:
        event = connection.execute(
            """SELECT entity_type, entity_id, action FROM audit_log WHERE id = ?""",
            (audit_id,),
        ).fetchone()
        if event is None or event["action"] != "archived":
            return jsonify({"error": "Archive event not found."}), 404
        entity_type = event["entity_type"]
        archive_type = {
            "customer": "customers",
            "shipment": "shipments",
            "container": "containers",
            "invoice": "invoices",
        }.get(entity_type)
        if archive_type is None:
            return jsonify({"error": "This event cannot be restored."}), 400
        if ARCHIVE_PERMISSIONS[archive_type] not in g.current_user["permissions"]:
            return jsonify({"error": "Your account does not have permission for this action."}), 403
        entity_id = event["entity_id"]
        before_rows = archived_entity_state(connection, archive_type, entity_id)
        if not before_rows or not all(row["archived_at"] for row in before_rows):
            return jsonify({"error": "This record is no longer archived."}), 409
        if archive_type == "shipments":
            shipment_customer = connection.execute(
                """SELECT customers.archived_at FROM shipments
                   JOIN customers ON customers.customer_code = shipments.customer_code
                   WHERE shipments.id = ?""",
                (entity_id,),
            ).fetchone()
            if shipment_customer is None or shipment_customer["archived_at"]:
                return jsonify({
                    "error": "Restore the shipment's customer before restoring this cargo lot."
                }), 409
        before = [dict(row) for row in before_rows] if archive_type == "containers" else dict(before_rows[0])
        if archive_type == "containers":
            connection.execute(
                """UPDATE shipment_loadings SET archived_at = NULL, archived_by = NULL
                   WHERE container_number = ?""",
                (entity_id,),
            )
            after = [
                dict(row) for row in connection.execute(
                    "SELECT * FROM shipment_loadings WHERE container_number = ? ORDER BY id",
                    (entity_id,),
                )
            ]
        else:
            table = {
                "customers": "customers",
                "shipments": "shipments",
                "invoices": "shipment_bills",
            }[archive_type]
            key = "shipment_id" if archive_type == "invoices" else "id"
            connection.execute(
                f"UPDATE {table} SET archived_at = NULL, archived_by = NULL WHERE {key} = ?",
                (entity_id,),
            )
            after_row = connection.execute(
                f"SELECT * FROM {table} WHERE {key} = ?", (entity_id,)
            ).fetchone()
            after = audit_snapshot(after_row)
        append_audit(
            connection,
            entity_type,
            entity_id,
            "restored",
            before,
            after,
        )
    return jsonify({"ok": True, "entityType": archive_type, "entityId": entity_id})


@app.get("/api/invoices")
@permissions_required("invoices:read")
def get_invoices():
    with database_connection() as connection:
        rows = connection.execute(INVOICE_QUERY + " ORDER BY shipment_bills.invoice_date DESC, shipment_bills.invoice_number").fetchall()
        revisions = connection.execute(
            """SELECT shipment_bill_revisions.shipment_id, shipment_bill_revisions.invoice_number,
                      shipment_bill_revisions.invoice_date, shipment_bill_revisions.total_amount,
                      shipment_bill_revisions.received_payment, shipment_bill_revisions.outstanding_balance,
                      shipment_bill_revisions.currency, shipment_bill_revisions.status,
                      shipments.tracking_number, shipments.customer_code, shipments.goods_description,
                      shipments.cartons, shipments.weight_kg, shipments.cbm,
                      customers.company_name, customers.delivery_city, customers.delivery_address,
                      shipment_bill_revisions.superseded_at, shipment_bill_revisions.superseded_by
               FROM shipment_bill_revisions
               JOIN shipment_bills ON shipment_bills.shipment_id = shipment_bill_revisions.shipment_id
               JOIN shipments ON shipments.id = shipment_bill_revisions.shipment_id
               JOIN customers ON customers.customer_code = shipments.customer_code
               WHERE shipment_bills.archived_at IS NULL
                 AND shipments.archived_at IS NULL
                 AND customers.archived_at IS NULL
               ORDER BY shipment_bill_revisions.superseded_at DESC"""
        ).fetchall()
    return jsonify(
        [invoice_dict(row) for row in rows]
        + [
            {
                **invoice_dict(row),
                "status": "superseded",
                "supersededAt": row["superseded_at"],
                "supersededBy": row["superseded_by"],
            }
            for row in revisions
        ]
    )


@app.post("/api/invoices")
@permissions_required("invoices:write")
def post_invoice():
    payload = request.get_json(silent=True) or {}
    shipment_identifier = str(payload.get("shipmentId") or "").strip()
    if not shipment_identifier:
        return jsonify({"error": "Select a shipment for this invoice."}), 400
    try:
        invoice = invoice_fields(payload)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    with database_connection() as connection:
        shipment = connection.execute(
            "SELECT id FROM shipments WHERE (id = ? OR tracking_number = ?) AND archived_at IS NULL",
            (shipment_identifier, shipment_identifier),
        ).fetchone()
        if shipment is None:
            return jsonify({"error": "Shipment not found."}), 404
        duplicate = connection.execute(
            "SELECT shipment_id FROM shipment_bills WHERE invoice_number = ?",
            (invoice["invoiceNumber"],),
        ).fetchone()
        if duplicate and duplicate["shipment_id"] != shipment["id"]:
            return jsonify({"error": "That invoice number is already in use."}), 409
        existing = connection.execute(
            "SELECT * FROM shipment_bills WHERE shipment_id = ?", (shipment["id"],)
        ).fetchone()
        if existing:
            if existing["archived_at"]:
                return jsonify({"error": "Restore the archived bill before creating a revision."}), 409
            if "invoices:edit" not in g.current_user["permissions"]:
                return jsonify({"error": "Your account cannot revise an existing bill."}), 403
            before = audit_snapshot(existing)
            connection.execute(
                """INSERT INTO shipment_bill_revisions
                   (shipment_id, invoice_number, invoice_date, total_amount, received_payment,
                    outstanding_balance, currency, status, superseded_at, superseded_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    existing["shipment_id"], existing["invoice_number"], existing["invoice_date"],
                    existing["total_amount"], existing["received_payment"],
                    existing["outstanding_balance"], existing["currency"], "superseded",
                    utc_now(), g.current_user["username"],
                ),
            )
            try:
                connection.execute(
                    """UPDATE shipment_bills SET invoice_number = ?, invoice_date = ?, total_amount = ?,
                       received_payment = ?, outstanding_balance = ?, currency = ?, status = ?
                       WHERE shipment_id = ?""",
                    (
                        invoice["invoiceNumber"], invoice["invoiceDate"], invoice["totalAmount"],
                        invoice["receivedPayment"], invoice["outstandingBalance"], invoice["currency"],
                        invoice["status"], shipment["id"],
                    ),
                )
            except sqlite3.IntegrityError:
                return jsonify({"error": "That invoice number is already in use."}), 409
        else:
            before = None
            try:
                connection.execute(
                    """INSERT INTO shipment_bills
                       (shipment_id, invoice_number, invoice_date, total_amount,
                        received_payment, outstanding_balance, currency, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        shipment["id"], invoice["invoiceNumber"], invoice["invoiceDate"],
                        invoice["totalAmount"], invoice["receivedPayment"],
                        invoice["outstandingBalance"], invoice["currency"], invoice["status"],
                    ),
                )
            except sqlite3.IntegrityError:
                return jsonify({"error": "That invoice number is already in use."}), 409
        row = connection.execute(
            INVOICE_QUERY + " AND shipment_bills.shipment_id = ?", (shipment["id"],)
        ).fetchone()
        after = connection.execute(
            "SELECT * FROM shipment_bills WHERE shipment_id = ?", (shipment["id"],)
        ).fetchone()
        append_audit(
            connection,
            "invoice",
            shipment["id"],
            "revised" if before is not None else "created",
            before,
            audit_snapshot(after),
        )
    return jsonify(invoice_dict(row)), 201


@app.patch("/api/invoices/<shipment_identifier>")
@permissions_required("invoices:edit")
def update_invoice(shipment_identifier):
    payload = request.get_json(silent=True) or {}
    if set(payload) - {"receivedPayment", "status"}:
        return jsonify({"error": "To change bill details, create a revised bill instead."}), 400
    with database_connection() as connection:
        shipment = connection.execute(
            "SELECT id FROM shipments WHERE (id = ? OR tracking_number = ?) AND archived_at IS NULL",
            (shipment_identifier, shipment_identifier),
        ).fetchone()
        if shipment is None:
            return jsonify({"error": "Shipment not found."}), 404
        existing = connection.execute(
            "SELECT * FROM shipment_bills WHERE shipment_id = ?", (shipment["id"],)
        ).fetchone()
        if existing is None or existing["archived_at"]:
            return jsonify({"error": "Create an invoice for this shipment before editing it."}), 404
        before = audit_snapshot(existing)
        try:
            invoice = invoice_fields(payload, existing)
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
        try:
            connection.execute(
                """UPDATE shipment_bills SET invoice_number = ?, invoice_date = ?,
                   total_amount = ?, received_payment = ?, outstanding_balance = ?,
                   currency = ?, status = ? WHERE shipment_id = ?""",
                (
                    invoice["invoiceNumber"], invoice["invoiceDate"], invoice["totalAmount"],
                    invoice["receivedPayment"], invoice["outstandingBalance"],
                    invoice["currency"], invoice["status"], shipment["id"],
                ),
            )
        except sqlite3.IntegrityError:
            return jsonify({"error": "That invoice number is already in use."}), 409
        row = connection.execute(
            INVOICE_QUERY + " AND shipment_bills.shipment_id = ?", (shipment["id"],)
        ).fetchone()
        after = connection.execute(
            "SELECT * FROM shipment_bills WHERE shipment_id = ?", (shipment["id"],)
        ).fetchone()
        append_audit(connection, "invoice", shipment["id"], "updated", before, audit_snapshot(after))
    return jsonify(invoice_dict(row))


@app.get("/api/invoices/<shipment_identifier>/pdf")
@permissions_required("invoices:read")
def download_invoice_pdf(shipment_identifier):
    if "invoices:read" not in g.current_user["permissions"]:
        return jsonify({"error": "Your account does not have permission for this action."}), 403
    with database_connection() as connection:
        row = connection.execute(
            INVOICE_QUERY + " AND (shipment_bills.shipment_id = ? OR shipments.tracking_number = ?)",
            (shipment_identifier, shipment_identifier),
        ).fetchone()
    if row is None:
        return jsonify({"error": "Invoice not found."}), 404
    invoice = invoice_dict(row)
    if invoice["status"] not in {"issued", "paid"}:
        return jsonify({"error": "This bill has not been approved by Accounts yet."}), 409
    pdf = make_invoice_pdf(invoice)
    filename = re.sub(r"[^A-Za-z0-9._-]+", "-", invoice["invoiceNumber"]).strip(".-") or "invoice"
    return send_file(
        pdf,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"{filename}.pdf",
    )


@app.post("/api/shipments")
@permissions_required("shipments:receive")
def post_shipment():
    payload = request.get_json(silent=True) or {}
    customer_code = str(payload.get("customerCode") or "").strip()
    tracking_number = str(payload.get("trackingNumber") or "").strip()
    goods_description = str(payload.get("goodsDescription") or "").strip()
    if not customer_code or not tracking_number or not goods_description:
        return jsonify({"error": "customerCode, trackingNumber and goodsDescription are required."}), 400

    data = load_data()
    if not any(
        customer.get("customerCode") == customer_code and not customer.get("archivedAt")
        for customer in data["customers"]
    ):
        return jsonify({"error": "Customer code not found."}), 404

    try:
        cartons_value = Decimal(str(payload.get("cartons") or 0))
        if not cartons_value.is_finite() or cartons_value != cartons_value.to_integral_value():
            raise ValueError
        cartons = int(cartons_value)
        weight_kg = float(payload.get("weightKg") or 0)
        cbm = float(payload.get("cbm") or 0)
    except (InvalidOperation, TypeError, ValueError):
        return jsonify({"error": "Cartons, weightKg and cbm must be valid numbers."}), 400

    date_received = payload.get("warehouseReceivingDate") or today()
    if not is_iso_date(date_received):
        return jsonify({"error": "warehouseReceivingDate must use YYYY-MM-DD format."}), 400
    if cartons < 0 or not math.isfinite(weight_kg) or not math.isfinite(cbm) or weight_kg < 0 or cbm < 0:
        return jsonify({"error": "Cartons, weight and CBM must be valid nonnegative numbers."}), 400
    shipment = {
        "id": f"ship-{uuid.uuid4().hex}",
        "trackingNumber": tracking_number,
        "customerCode": customer_code,
        "shippingMark": payload.get("shippingMark") or "",
        "supplierName": payload.get("supplierName") or "",
        "warehouseReceivingDate": date_received,
        "goodsDescription": goods_description,
        "cartons": cartons,
        "weightKg": weight_kg,
        "cbm": cbm,
        "status": "China warehouse received",
        "statusNote": "Cargo received in China warehouse",
        "containerNumber": "",
        "loadingDate": "",
        "loadedCartons": 0,
        "remainingCartons": cartons,
        "departurePort": "",
        "destinationPort": "",
        "vessel": "",
        "voyage": "",
        "blReference": "",
        "expectedDepartureDate": "",
        "expectedArrivalDate": "",
        "expectedDeliveryDate": "",
        "lastUpdatedAt": utc_now(),
        "statusHistory": [{
            "stage": "China warehouse received",
            "date": date_received,
            "updatedBy": g.current_user["username"],
            "remarks": "Cargo received and recorded",
        }],
        "photos": payload.get("photos") or [],
        "bill": None,
    }
    if any(item.get("trackingNumber", "").casefold() == shipment["trackingNumber"].casefold() for item in data["shipments"]):
        return jsonify({"error": "Tracking number already exists."}), 409
    data["shipments"].append(shipment)
    save_data(data)
    return jsonify(shipment), 201


@app.patch("/api/shipments/<shipment_identifier>")
@permissions_required("shipments:edit")
def edit_shipment(shipment_identifier):
    payload = request.get_json(silent=True) or {}
    allowed_fields = {
        "trackingNumber", "shippingMark", "supplierName", "warehouseReceivingDate",
        "goodsDescription", "cartons", "weightKg", "cbm",
    }
    if not payload or set(payload) - allowed_fields:
        return jsonify({"error": "Only the editable cargo details may be changed."}), 400
    data = load_data()
    shipment = next(
        (
            row for row in data["shipments"]
            if not row.get("archivedAt")
            and (row["id"] == shipment_identifier or row["trackingNumber"] == shipment_identifier)
        ),
        None,
    )
    if shipment is None:
        return jsonify({"error": "Shipment not found."}), 404

    tracking_number = str(payload.get("trackingNumber", shipment["trackingNumber"])).strip()
    goods_description = str(payload.get("goodsDescription", shipment["goodsDescription"])).strip()
    receiving_date = payload.get("warehouseReceivingDate", shipment["warehouseReceivingDate"])
    if not tracking_number or not goods_description:
        return jsonify({"error": "Tracking number and goods description are required."}), 400
    if not is_iso_date(receiving_date) or not receiving_date:
        return jsonify({"error": "Warehouse receiving date must use YYYY-MM-DD format."}), 400
    if any(
        row["id"] != shipment["id"]
        and row["trackingNumber"].casefold() == tracking_number.casefold()
        for row in data["shipments"]
    ):
        return jsonify({"error": "Tracking number already exists."}), 409
    try:
        cartons_value = Decimal(str(payload.get("cartons", shipment["cartons"])))
        weight_kg = float(payload.get("weightKg", shipment["weightKg"]))
        cbm = float(payload.get("cbm", shipment["cbm"]))
        if not cartons_value.is_finite() or cartons_value != cartons_value.to_integral_value():
            raise ValueError
        cartons = int(cartons_value)
    except (InvalidOperation, TypeError, ValueError):
        return jsonify({"error": "Cartons, weight and CBM must be valid numbers."}), 400
    if cartons < 0 or not math.isfinite(weight_kg) or not math.isfinite(cbm) or weight_kg < 0 or cbm < 0:
        return jsonify({"error": "Cartons, weight and CBM must be valid nonnegative numbers."}), 400
    loaded_cartons = max(
        sum(int(segment.get("loadedCartons") or 0) for segment in shipment.get("loadingSegments", [])),
        int(shipment.get("loadedCartons") or 0),
    )
    if cartons < loaded_cartons:
        return jsonify({"error": "Received cartons cannot be reduced below the quantity already loaded."}), 409

    shipment.update({
        "trackingNumber": tracking_number,
        "shippingMark": str(payload.get("shippingMark", shipment["shippingMark"]) or "").strip(),
        "supplierName": str(payload.get("supplierName", shipment["supplierName"]) or "").strip(),
        "warehouseReceivingDate": receiving_date,
        "goodsDescription": goods_description,
        "cartons": cartons,
        "weightKg": weight_kg,
        "cbm": cbm,
        "loadedCartons": loaded_cartons,
        "remainingCartons": cartons - loaded_cartons,
        "lastUpdatedAt": utc_now(),
    })
    if shipment["remainingCartons"] > 0:
        shipment["status"] = "Partial loading"
        shipment["statusNote"] = "Cargo quantity updated; cartons remain awaiting loading."
    elif shipment["status"] == "Partial loading":
        active_statuses = [
            portion.get("status", "Loaded in container")
            for portion in shipment.get("loadingSegments", [])
            if not portion.get("archivedAt")
        ]
        shipment["status"] = min(
            active_statuses,
            key=lambda value: STAGE_ORDER.get(value, 0),
        ) if active_statuses else "Awaiting loading"
        shipment["statusNote"] = "Cargo quantity updated."
    save_data(data)
    return jsonify(shipment)


@app.post("/api/shipments/<shipment_id>/loading")
@permissions_required("shipments:operate")
def update_loading(shipment_id):
    payload = request.get_json(silent=True) or {}
    data = load_data()
    shipment = next(
        (item for item in data["shipments"]
         if not item.get("archivedAt")
         and (item.get("id") == shipment_id or item.get("trackingNumber") == shipment_id)),
        None,
    )
    if not shipment:
        return jsonify({"error": "Shipment not found."}), 404

    container_number = str(payload.get("containerNumber") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", container_number):
        return jsonify({"error": "A valid container number is required for each loading portion."}), 400
    with database_connection() as connection:
        archived_container = connection.execute(
            """SELECT EXISTS(
                   SELECT 1 FROM shipment_loadings WHERE container_number = ? AND archived_at IS NOT NULL
               ) AND NOT EXISTS(
                   SELECT 1 FROM shipment_loadings WHERE container_number = ? AND archived_at IS NULL
               )""",
            (container_number, container_number),
        ).fetchone()[0]
    if archived_container:
        return jsonify({"error": "This container is archived. Restore it or use a different container number."}), 409
    if not payload.get("loadingDate"):
        return jsonify({"error": "Loading date is required for a container portion."}), 400
    date_fields = (
        "loadingDate", "expectedDepartureDate", "expectedArrivalDate", "expectedDeliveryDate",
    )
    if any(not is_iso_date(payload.get(field)) for field in date_fields):
        return jsonify({"error": "Loading and ETA dates must use YYYY-MM-DD format."}), 400
    if (
        payload.get("expectedDepartureDate")
        and payload.get("expectedArrivalDate")
        and payload["expectedArrivalDate"] < payload["expectedDepartureDate"]
    ):
        return jsonify({"error": "Expected arrival cannot be before expected departure."}), 400
    if (
        payload.get("expectedArrivalDate")
        and payload.get("expectedDeliveryDate")
        and payload["expectedDeliveryDate"] < payload["expectedArrivalDate"]
    ):
        return jsonify({"error": "Delivery estimate cannot be before the Pakistan arrival ETA."}), 400

    try:
        quantity_value = Decimal(str(payload.get("loadedCartons") or 0))
        if not quantity_value.is_finite() or quantity_value != quantity_value.to_integral_value():
            raise ValueError
        quantity = int(quantity_value)
    except (InvalidOperation, TypeError, ValueError):
        return jsonify({"error": "Loaded cartons must be a whole number."}), 400
    cartons = int(shipment.get("cartons") or 0)
    previously_loaded = max(sum(
        int(segment.get("loadedCartons") or 0) for segment in shipment.get("loadingSegments", [])
    ), int(shipment.get("loadedCartons") or 0))
    if quantity <= 0 or quantity > cartons - previously_loaded:
        return jsonify({"error": "Loaded cartons must be positive and cannot exceed the remaining cargo."}), 400

    segment = {
        "id": f"load-{uuid.uuid4().hex}",
        "containerNumber": container_number,
        "loadingDate": payload["loadingDate"],
        "loadedCartons": quantity,
        "departurePort": payload.get("departurePort") or "",
        "destinationPort": payload.get("destinationPort") or "",
        "vessel": payload.get("vessel") or "",
        "voyage": payload.get("voyage") or "",
        "blReference": payload.get("blReference") or "",
        "expectedDepartureDate": payload.get("expectedDepartureDate") or "",
        "expectedArrivalDate": payload.get("expectedArrivalDate") or "",
        "expectedDeliveryDate": payload.get("expectedDeliveryDate") or "",
        "createdBy": g.current_user["username"],
        "status": "Loaded in container",
        "statusUpdatedAt": utc_now(),
        "statusUpdatedBy": g.current_user["username"],
        "statusNote": "Cargo loaded into container",
    }
    shipment.setdefault("loadingSegments", []).append(segment)
    shipment["loadedCartons"] = previously_loaded + quantity
    shipment["remainingCartons"] = cartons - shipment["loadedCartons"]
    if shipment["loadedCartons"] >= cartons:
        segment_statuses = [
            item.get("status", "Loaded in container")
            for item in shipment.get("loadingSegments", [])
        ]
        shipment["status"] = min(
            segment_statuses, key=lambda value: STAGE_ORDER.get(value, 0)
        ) if segment_statuses else "Loaded in container"
        shipment["statusNote"] = "Full cargo loaded into container"
    else:
        shipment["status"] = "Partial loading"
        shipment["statusNote"] = "Partially loaded into container"
    shipment["containerNumber"] = container_number
    shipment["loadingDate"] = segment["loadingDate"]
    for field in (
        "departurePort", "destinationPort", "vessel", "voyage", "blReference",
        "expectedDepartureDate", "expectedArrivalDate", "expectedDeliveryDate",
    ):
        shipment[field] = segment[{
            "departurePort": "departurePort", "destinationPort": "destinationPort",
            "vessel": "vessel", "voyage": "voyage", "blReference": "blReference",
            "expectedDepartureDate": "expectedDepartureDate",
            "expectedArrivalDate": "expectedArrivalDate",
            "expectedDeliveryDate": "expectedDeliveryDate",
        }[field]]
    add_status_history(shipment, shipment["status"], g.current_user["username"], shipment["statusNote"])
    save_data(data)
    return jsonify(shipment)


@app.post("/api/shipments/<shipment_id>/status")
@permissions_required("shipments:status")
def update_status(shipment_id):
    payload = request.get_json(silent=True) or {}
    data = load_data()
    shipment = next(
        (item for item in data["shipments"]
         if not item.get("archivedAt")
         and (item.get("id") == shipment_id or item.get("trackingNumber") == shipment_id)),
        None,
    )
    if not shipment:
        return jsonify({"error": "Shipment not found."}), 404

    stage = str(payload.get("stage") or "").strip()
    if stage not in SHIPMENT_STAGES:
        return jsonify({"error": "Choose a valid shipment milestone."}), 400
    if stage == "Loaded in container" and (
        int(shipment.get("cartons") or 0) <= 0
        or int(shipment.get("remainingCartons") or 0) > 0
    ):
        return jsonify({"error": "Cargo with remaining cartons cannot be marked fully loaded."}), 400
    if (
        STAGE_ORDER[stage] > STAGE_ORDER["Loaded in container"]
        and int(shipment.get("remainingCartons") or 0) > 0
    ):
        return jsonify({"error": "Update the relevant container; this cargo lot still has cartons awaiting loading."}), 400

    shipment["status"] = stage
    shipment["statusNote"] = payload.get("remarks") or stage
    for segment in shipment.get("loadingSegments", []):
        if segment.get("archivedAt"):
            continue
        segment["status"] = stage
        segment["statusUpdatedAt"] = utc_now()
        segment["statusUpdatedBy"] = g.current_user["username"]
        segment["statusNote"] = shipment["statusNote"]
    add_status_history(shipment, stage, g.current_user["username"], payload.get("remarks") or stage)
    save_data(data)
    return jsonify(shipment)


@app.get("/api/dashboard")
@permissions_required("shipments:read")
def dashboard():
    data = load_data()
    shipments = [
        shipment for shipment in data["shipments"]
        if not shipment.get("archivedAt")
    ]
    return jsonify({
        "totals": {
            "customers": sum(1 for customer in data["customers"] if not customer.get("archivedAt")),
            "shipments": len(shipments),
            "activeShipments": sum(1 for shipment in shipments if shipment.get("status") != "Delivered"),
            "pendingLoading": sum(
                1 for shipment in shipments
                if shipment.get("status") in {"Awaiting loading", "China warehouse received", "Partial loading"}
            ),
        },
        "statusCounts": {
            status: sum(1 for shipment in shipments if shipment.get("status") == status)
            for status in sorted({shipment.get("status", "Unknown") for shipment in shipments})
        },
        "shipments": [
            {
                "id": shipment.get("id"),
                "trackingNumber": shipment.get("trackingNumber"),
                "customerCode": shipment.get("customerCode"),
                "status": shipment.get("status"),
                "expectedArrivalDate": shipment.get("expectedArrivalDate") or "Pending",
                "lastUpdatedAt": shipment.get("lastUpdatedAt"),
            }
            for shipment in shipments
        ],
    })


@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.errorhandler(413)
def request_too_large(_error):
    return jsonify({"error": "Uploads must fit within the 25 MB request limit."}), 413


initialize_database()


if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "3000")),
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
