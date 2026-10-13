CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'warehouse', 'operations', 'delivery', 'accounts', 'viewer')),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    permissions_json TEXT,
    is_super_admin INTEGER NOT NULL DEFAULT 0 CHECK (is_super_admin IN (0, 1))
);

CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY,
    company_name TEXT NOT NULL,
    customer_code TEXT NOT NULL UNIQUE,
    delivery_city TEXT NOT NULL DEFAULT '',
    delivery_address TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    archived_at TEXT,
    archived_by TEXT
);

CREATE TABLE IF NOT EXISTS shipments (
    id TEXT PRIMARY KEY,
    tracking_number TEXT NOT NULL UNIQUE,
    customer_code TEXT NOT NULL REFERENCES customers(customer_code),
    shipping_mark TEXT NOT NULL DEFAULT '',
    supplier_name TEXT NOT NULL DEFAULT '',
    warehouse_receiving_date TEXT NOT NULL,
    goods_description TEXT NOT NULL,
    cartons INTEGER NOT NULL DEFAULT 0 CHECK (cartons >= 0),
    weight_kg REAL NOT NULL DEFAULT 0 CHECK (weight_kg >= 0),
    cbm REAL NOT NULL DEFAULT 0 CHECK (cbm >= 0),
    status TEXT NOT NULL,
    status_note TEXT NOT NULL DEFAULT '',
    container_number TEXT NOT NULL DEFAULT '',
    loading_date TEXT NOT NULL DEFAULT '',
    loaded_cartons INTEGER NOT NULL DEFAULT 0 CHECK (loaded_cartons >= 0),
    remaining_cartons INTEGER NOT NULL DEFAULT 0 CHECK (remaining_cartons >= 0),
    departure_port TEXT NOT NULL DEFAULT '',
    destination_port TEXT NOT NULL DEFAULT '',
    vessel TEXT NOT NULL DEFAULT '',
    voyage TEXT NOT NULL DEFAULT '',
    bl_reference TEXT NOT NULL DEFAULT '',
    expected_departure_date TEXT NOT NULL DEFAULT '',
    expected_arrival_date TEXT NOT NULL DEFAULT '',
    expected_delivery_date TEXT NOT NULL DEFAULT '',
    last_updated_at TEXT NOT NULL,
    archived_at TEXT,
    archived_by TEXT
);

CREATE TABLE IF NOT EXISTS shipment_status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shipment_id TEXT NOT NULL REFERENCES shipments(id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    date TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    remarks TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS shipment_photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shipment_id TEXT NOT NULL REFERENCES shipments(id) ON DELETE CASCADE,
    photo_url TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shipment_loadings (
    id TEXT PRIMARY KEY,
    shipment_id TEXT NOT NULL REFERENCES shipments(id) ON DELETE CASCADE,
    container_number TEXT NOT NULL,
    loading_date TEXT NOT NULL,
    loaded_cartons INTEGER NOT NULL CHECK (loaded_cartons > 0),
    departure_port TEXT NOT NULL DEFAULT '',
    destination_port TEXT NOT NULL DEFAULT '',
    vessel TEXT NOT NULL DEFAULT '',
    voyage TEXT NOT NULL DEFAULT '',
    bl_reference TEXT NOT NULL DEFAULT '',
    expected_departure_date TEXT NOT NULL DEFAULT '',
    expected_arrival_date TEXT NOT NULL DEFAULT '',
    expected_delivery_date TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'Loaded in container',
    status_updated_at TEXT NOT NULL DEFAULT '',
    status_updated_by TEXT NOT NULL DEFAULT '',
    status_note TEXT NOT NULL DEFAULT '',
    archived_at TEXT,
    archived_by TEXT
);

CREATE TABLE IF NOT EXISTS shipment_documents (
    id TEXT PRIMARY KEY,
    shipment_id TEXT NOT NULL REFERENCES shipments(id) ON DELETE CASCADE,
    document_type TEXT NOT NULL CHECK (document_type IN ('photo', 'packing_list')),
    file_name TEXT NOT NULL,
    stored_name TEXT NOT NULL UNIQUE,
    content_type TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    uploaded_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shipment_bills (
    shipment_id TEXT PRIMARY KEY REFERENCES shipments(id) ON DELETE CASCADE,
    invoice_number TEXT NOT NULL DEFAULT '',
    invoice_date TEXT NOT NULL DEFAULT '',
    total_amount REAL NOT NULL DEFAULT 0,
    received_payment REAL NOT NULL DEFAULT 0,
    outstanding_balance REAL NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    archived_at TEXT,
    archived_by TEXT
);

CREATE TABLE IF NOT EXISTS shipment_bill_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shipment_id TEXT NOT NULL REFERENCES shipments(id) ON DELETE CASCADE,
    invoice_number TEXT NOT NULL,
    invoice_date TEXT NOT NULL,
    total_amount REAL NOT NULL,
    received_payment REAL NOT NULL,
    outstanding_balance REAL NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL,
    superseded_at TEXT NOT NULL,
    superseded_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id TEXT,
    actor_username TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_shipments_customer_code ON shipments(customer_code);
CREATE INDEX IF NOT EXISTS idx_shipments_status ON shipments(status);
CREATE INDEX IF NOT EXISTS idx_status_history_shipment ON shipment_status_history(shipment_id);
CREATE INDEX IF NOT EXISTS idx_shipments_container ON shipments(container_number);
CREATE INDEX IF NOT EXISTS idx_loadings_shipment ON shipment_loadings(shipment_id);
CREATE INDEX IF NOT EXISTS idx_loadings_container ON shipment_loadings(container_number);
CREATE INDEX IF NOT EXISTS idx_audit_log_changed_at ON audit_log(changed_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_entity ON audit_log(entity_type, entity_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_shipment_bills_invoice_number
    ON shipment_bills(invoice_number) WHERE invoice_number != '';
