import tempfile
import unittest
from io import BytesIO
from pathlib import Path

import app as shipping_app


class ShippingFlowApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database_directory = tempfile.TemporaryDirectory()
        cls.upload_directory = tempfile.TemporaryDirectory()
        cls.original_upload_directory = shipping_app.UPLOAD_DIR
        shipping_app.DATABASE_FILE = Path(cls.database_directory.name) / "test.db"
        shipping_app.UPLOAD_DIR = Path(cls.upload_directory.name)
        shipping_app.initialize_database()
        cls.client = shipping_app.app.test_client()

    @classmethod
    def tearDownClass(cls):
        shipping_app.UPLOAD_DIR = cls.original_upload_directory
        cls.upload_directory.cleanup()
        cls.database_directory.cleanup()

    def setUp(self):
        self.client = shipping_app.app.test_client()
        with shipping_app.database_connection() as connection:
            connection.execute("DELETE FROM shipment_status_history")
            connection.execute("DELETE FROM shipment_photos")
            connection.execute("DELETE FROM shipment_bills")
            connection.execute("DELETE FROM shipments")
            connection.execute("DELETE FROM customers")
            connection.execute("DELETE FROM users")
            connection.execute("DELETE FROM audit_log")
        shipping_app.save_data({"customers": [], "shipments": [], "invoices": []})
        setup = self.client.post(
            "/api/setup",
            json={"username": "admin", "password": "InitialAdminPass123"},
        )
        self.assertEqual(setup.status_code, 201)

    def create_super_admin_client(self):
        shipping_app.provision_super_admin(
            "testsuper",
            "TestSuperAdminPass123",
        )
        client = shipping_app.app.test_client()
        login = client.post(
            "/api/login",
            json={"username": "testsuper", "password": "TestSuperAdminPass123"},
        )
        self.assertEqual(login.status_code, 200, login.json)
        return client

    def grant_permissions(self, admin_client, permissions):
        super_client = self.create_super_admin_client()
        user_id = admin_client.get("/api/session").json["user"]["id"]
        existing = admin_client.get("/api/session").json["user"]["permissions"]
        response = super_client.patch(
            f"/api/users/{user_id}",
            json={"permissions": sorted(set(existing) | set(permissions))},
        )
        self.assertEqual(response.status_code, 200, response.json)
        return super_client

    def test_customers_do_not_require_phone_contacts(self):
        response = self.client.post(
            "/api/customers",
            json={"companyName": "Test Company", "customerCode": "TEST-1"},
        )

        self.assertEqual(response.status_code, 201)
        self.assertNotIn("registeredWhatsApp", response.json)
        self.assertNotIn("authorizedContacts", response.json)

    def test_cargo_receiving_accepts_customer_code(self):
        customer = self.client.post(
            "/api/customers",
            json={"companyName": "Test Company", "customerCode": "TEST-1"},
        )
        self.assertEqual(customer.status_code, 201)
        shipment = self.client.post(
            "/api/shipments",
            json={
                "customerCode": customer.json["customerCode"],
                "trackingNumber": "TEST-RECEIVING-1",
                "goodsDescription": "Cargo received",
            },
        )
        self.assertEqual(shipment.status_code, 201, shipment.json)
        self.assertEqual(shipment.json["customerCode"], "TEST-1")

    def test_invoice_create_update_pdf_and_role_access(self):
        self.client.post(
            "/api/customers",
            json={"companyName": "Test Company", "customerCode": "TEST-1"},
        )
        shipment = self.client.post(
            "/api/shipments",
            json={
                "customerCode": "TEST-1",
                "trackingNumber": "TEST-INVOICE-1",
                "goodsDescription": "Test cargo",
                "cartons": 5,
            },
        )
        shipment_id = shipment.json["id"]
        invoice = self.client.post(
            "/api/invoices",
            json={
                "shipmentId": shipment_id,
                "invoiceNumber": "INV-TEST-001",
                "invoiceDate": "2026-10-12",
                "currency": "USD",
                "totalAmount": "125.50",
                "receivedPayment": "25.25",
                "status": "issued",
            },
        )
        self.assertEqual(invoice.status_code, 201, invoice.json)
        self.assertEqual(invoice.json["outstandingBalance"], 100.25)
        self.assertEqual(len(self.client.get("/api/invoices").json), 1)

        invalid_payment = self.client.patch(
            f"/api/invoices/{shipment_id}",
            json={"receivedPayment": "200"},
        )
        self.assertEqual(invalid_payment.status_code, 400)

        with shipping_app.database_connection() as connection:
            connection.execute(
                "UPDATE shipment_bills SET status = 'approved' WHERE shipment_id = ?",
                (shipment_id,),
            )
        self.assertEqual(self.client.get("/api/invoices").json[0]["status"], "issued")

        paid = self.client.patch(
            f"/api/invoices/{shipment_id}",
            json={"receivedPayment": "125.50"},
        )
        self.assertEqual(paid.status_code, 200, paid.json)
        self.assertEqual(paid.json["status"], "paid")
        self.assertEqual(paid.json["outstandingBalance"], 0)

        pdf = self.client.get(f"/api/invoices/{shipment_id}/pdf")
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.mimetype, "application/pdf")
        self.assertTrue(pdf.data.startswith(b"%PDF-"))
        self.assertIn("INV-TEST-001.pdf", pdf.headers["Content-Disposition"])

        viewer = self.client.post(
            "/api/users",
            json={
                "username": "billingviewer",
                "password": "BillingViewerPass123",
                "role": "viewer",
            },
        )
        self.assertEqual(viewer.status_code, 201)
        viewer_client = shipping_app.app.test_client()
        viewer_client.post(
            "/api/login",
            json={"username": "billingviewer", "password": "BillingViewerPass123"},
        )
        self.assertEqual(viewer_client.get("/api/invoices").status_code, 403)
        self.assertEqual(viewer_client.get(f"/api/invoices/{shipment_id}/pdf").status_code, 403)
        self.assertIsNone(viewer_client.get(f"/api/shipments/{shipment_id}").json["bill"])

    def test_shipment_loading_and_status_are_persisted(self):
        self.client.post(
            "/api/customers",
            json={"companyName": "Test Company", "customerCode": "TEST-1"},
        )
        response = self.client.post(
            "/api/shipments",
            json={
                "customerCode": "TEST-1",
                "trackingNumber": "TEST-TRACK-1",
                "goodsDescription": "Test cargo",
                "cartons": 12,
            },
        )
        self.assertEqual(response.status_code, 201)
        shipment_id = response.json["id"]

        loading = self.client.post(
            f"/api/shipments/{shipment_id}/loading",
            json={"containerNumber": "TRACK-CNTR-1", "loadingDate": "2026-10-12", "loadedCartons": 5},
        )
        self.assertEqual(loading.status_code, 200)
        self.assertEqual(loading.json["status"], "Partial loading")

        blocked_status = self.client.post(
            f"/api/shipments/{shipment_id}/status",
            json={"stage": "In transit"},
        )
        self.assertEqual(blocked_status.status_code, 400)
        completed_loading = self.client.post(
            f"/api/shipments/{shipment_id}/loading",
            json={"containerNumber": "TRACK-CNTR-1", "loadingDate": "2026-10-12", "loadedCartons": 7},
        )
        self.assertEqual(completed_loading.status_code, 200)
        status = self.client.post(
            f"/api/shipments/{shipment_id}/status",
            json={"stage": "In transit", "updatedBy": "test"},
        )
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json["statusHistory"][-1]["stage"], "In transit")
        self.assertEqual(
            self.client.get(f"/api/shipments/{shipment_id}").json["status"],
            "In transit",
        )
        dashboard = self.client.get("/api/dashboard")
        self.assertEqual(dashboard.json["statusCounts"]["In transit"], 1)

    def test_partial_loading_across_containers_tracks_only_loaded_cartons(self):
        self.client.post(
            "/api/customers",
            json={"companyName": "Split Cargo", "customerCode": "SPLIT-1"},
        )
        shipment = self.client.post(
            "/api/shipments",
            json={
                "customerCode": "SPLIT-1",
                "trackingNumber": "SPLIT-TRACK-1",
                "goodsDescription": "Split load",
                "cartons": 10,
            },
        ).json
        first = self.client.post(
            f"/api/shipments/{shipment['id']}/loading",
            json={"containerNumber": "CNTR-1", "loadingDate": "2026-10-12", "loadedCartons": 4},
        )
        self.assertEqual(first.status_code, 200, first.json)
        self.assertEqual(first.json["status"], "Partial loading")
        self.assertEqual(first.json["loadedCartons"], 4)
        self.assertEqual(first.json["remainingCartons"], 6)
        self.assertEqual(first.json["loadingSegments"][0]["containerNumber"], "CNTR-1")

        overflow = self.client.post(
            f"/api/shipments/{shipment['id']}/loading",
            json={"containerNumber": "CNTR-2", "loadingDate": "2026-10-12", "loadedCartons": 7},
        )
        self.assertEqual(overflow.status_code, 400)

        second = self.client.post(
            f"/api/shipments/{shipment['id']}/loading",
            json={"containerNumber": "CNTR-2", "loadingDate": "2026-10-12", "loadedCartons": 6},
        )
        self.assertEqual(second.status_code, 200, second.json)
        self.assertEqual(second.json["status"], "Loaded in container")
        self.assertEqual(second.json["loadedCartons"], 10)
        self.assertEqual(second.json["remainingCartons"], 0)
        self.assertEqual(
            [segment["containerNumber"] for segment in second.json["loadingSegments"]],
            ["CNTR-1", "CNTR-2"],
        )

        container_update = self.client.post(
            "/api/containers/CNTR-1/status",
            json={"stage": "In transit", "remarks": "Vessel departed"},
        )
        self.assertEqual(container_update.status_code, 200, container_update.json)
        refreshed = self.client.get(f"/api/shipments/{shipment['id']}").json
        self.assertEqual(refreshed["status"], "Loaded in container")
        segment_statuses = {
            segment["containerNumber"]: segment["status"]
            for segment in refreshed["loadingSegments"]
        }
        self.assertEqual(segment_statuses["CNTR-1"], "In transit")
        self.assertEqual(segment_statuses["CNTR-2"], "Loaded in container")
        self.assertEqual(refreshed["statusHistory"][-1]["updatedBy"], "admin")
        self.assertEqual(
            refreshed["statusHistory"][-1]["remarks"],
            "Container CNTR-1: Vessel departed",
        )
        bulk_delivery = self.client.post(
            "/api/containers/CNTR-1/status",
            json={"stage": "Delivered"},
        )
        self.assertEqual(bulk_delivery.status_code, 400)
        self.client.post(
            "/api/containers/CNTR-2/status",
            json={"stage": "In transit", "remarks": "Second portion departed"},
        )
        self.assertEqual(
            self.client.get(f"/api/shipments/{shipment['id']}").json["status"],
            "In transit",
        )

    def test_document_uploads_are_attached_to_cargo_and_downloadable(self):
        self.client.post(
            "/api/customers",
            json={"companyName": "Docs Co", "customerCode": "DOCS-1"},
        )
        shipment = self.client.post(
            "/api/shipments",
            json={
                "customerCode": "DOCS-1",
                "trackingNumber": "DOCS-TRACK-1",
                "goodsDescription": "Cargo with documents",
            },
        ).json
        uploaded = self.client.post(
            f"/api/shipments/{shipment['id']}/documents",
            data={
                "photos": (BytesIO(b"\x89PNG\r\n\x1a\nphoto"), "cargo.png", "image/png"),
                "packingList": (BytesIO(b"%PDF-1.4 packing list"), "packing-list.pdf", "application/pdf"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.json)
        refreshed = self.client.get(f"/api/shipments/{shipment['id']}").json
        self.assertEqual(len(refreshed["documents"]), 2)
        for document in refreshed["documents"]:
            download = self.client.get(document["downloadUrl"])
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download.headers["Content-Disposition"].split(";")[0], "attachment")
            download.close()
        self.client.post(
            "/api/customers",
            json={"companyName": "Another Company", "customerCode": "DOCS-2"},
        )
        persisted = self.client.get(f"/api/shipments/{shipment['id']}").json
        self.assertEqual(len(persisted["documents"]), 2)

    def test_unapproved_bill_is_not_downloadable_and_revisions_supersede_prior_copy(self):
        self.client.post(
            "/api/customers",
            json={"companyName": "Invoice Co", "customerCode": "INV-CO"},
        )
        shipment = self.client.post(
            "/api/shipments",
            json={
                "customerCode": "INV-CO",
                "trackingNumber": "INV-TRACK",
                "goodsDescription": "Billable cargo",
            },
        ).json
        draft = {
            "shipmentId": shipment["id"],
            "invoiceNumber": "INV-DRAFT-1",
            "invoiceDate": "2026-10-12",
            "currency": "PKR",
            "totalAmount": "100",
            "receivedPayment": "0",
            "status": "draft",
        }
        created = self.client.post("/api/invoices", json=draft)
        self.assertEqual(created.status_code, 201, created.json)
        self.assertEqual(created.json["status"], "draft")
        self.assertEqual(
            self.client.get(f"/api/invoices/{shipment['id']}/pdf").status_code,
            409,
        )

        revised = {**draft, "invoiceNumber": "INV-APPROVED-2", "status": "issued"}
        response = self.client.post("/api/invoices", json=revised)
        self.assertEqual(response.status_code, 201, response.json)
        self.assertEqual(response.json["invoiceNumber"], "INV-APPROVED-2")
        register = self.client.get("/api/invoices").json
        self.assertEqual(
            [(invoice["invoiceNumber"], invoice["status"]) for invoice in register],
            [("INV-APPROVED-2", "issued"), ("INV-DRAFT-1", "superseded")],
        )
        pdf = self.client.get(f"/api/invoices/{shipment['id']}/pdf")
        self.assertEqual(pdf.status_code, 200)
        self.assertIn("INV-APPROVED-2.pdf", pdf.headers["Content-Disposition"])

    def test_delivery_and_accounts_roles_have_scoped_operations(self):
        self.client.post(
            "/api/customers",
            json={"companyName": "Role Co", "customerCode": "ROLE-1"},
        )
        shipment = self.client.post(
            "/api/shipments",
            json={
                "customerCode": "ROLE-1",
                "trackingNumber": "ROLE-TRACK",
                "goodsDescription": "Role test cargo",
            },
        ).json
        for username, role in (("delivery1", "delivery"), ("accounts1", "accounts")):
            created = self.client.post(
                "/api/users",
                json={
                    "username": username,
                    "password": "StaffRolePassword123",
                    "role": role,
                },
            )
            self.assertEqual(created.status_code, 201, created.json)

        delivery_client = shipping_app.app.test_client()
        delivery_client.post(
            "/api/login",
            json={"username": "delivery1", "password": "StaffRolePassword123"},
        )
        milestone = delivery_client.post(
            f"/api/shipments/{shipment['id']}/status",
            json={"stage": "In transit"},
        )
        self.assertEqual(milestone.status_code, 200, milestone.json)
        delivered = delivery_client.post(
            f"/api/shipments/{shipment['id']}/status",
            json={"stage": "Delivered", "remarks": "Received by consignee"},
        )
        self.assertEqual(delivered.status_code, 200, delivered.json)

        accounts_client = shipping_app.app.test_client()
        accounts_client.post(
            "/api/login",
            json={"username": "accounts1", "password": "StaffRolePassword123"},
        )
        self.assertEqual(accounts_client.get("/api/invoices").status_code, 200)
        self.assertEqual(
            accounts_client.post(
                f"/api/shipments/{shipment['id']}/status",
                json={"stage": "In transit"},
            ).status_code,
            403,
        )

    def test_user_can_change_password(self):
        response = self.client.post(
            "/api/password",
            json={
                "currentPassword": "InitialAdminPass123",
                "newPassword": "ChangedAdminPass456",
            },
        )
        self.assertEqual(response.status_code, 200, response.json)
        self.client.post("/api/logout")
        old_password = self.client.post(
            "/api/login",
            json={"username": "admin", "password": "InitialAdminPass123"},
        )
        self.assertEqual(old_password.status_code, 401)
        new_password = self.client.post(
            "/api/login",
            json={"username": "admin", "password": "ChangedAdminPass456"},
        )
        self.assertEqual(new_password.status_code, 200)

    def test_whatsapp_endpoint_is_removed(self):
        response = self.client.post("/api/whatsapp/inbound", json={})
        self.assertEqual(response.status_code, 404)

    def test_login_is_required_for_dashboard_and_shipments(self):
        anonymous = shipping_app.app.test_client()
        self.assertEqual(anonymous.get("/api/dashboard").status_code, 401)
        self.assertEqual(anonymous.get("/api/shipments").status_code, 401)
        self.assertEqual(
            anonymous.post(
                "/api/shipments",
                json={"customerCode": "NONE", "trackingNumber": "NONE", "goodsDescription": "None"},
            ).status_code,
            401,
        )

    def test_admin_can_create_accounts_and_warehouse_is_restricted(self):
        customer = self.client.post(
            "/api/customers",
            json={"companyName": "Test Company", "customerCode": "TEST-1"},
        )
        self.assertEqual(customer.status_code, 201)
        account = self.client.post(
            "/api/users",
            json={
                "username": "warehouse1",
                "password": "WarehousePass123",
                "role": "warehouse",
            },
        )
        self.assertEqual(account.status_code, 201, account.json)
        self.client.post("/api/logout")
        login = self.client.post(
            "/api/login",
            json={"username": "warehouse1", "password": "WarehousePass123"},
        )
        self.assertEqual(login.status_code, 200, login.json)
        self.assertEqual(login.json["user"]["role"], "warehouse")
        shipment = self.client.post(
            "/api/shipments",
            json={
                "customerCode": "TEST-1",
                "trackingNumber": "WH-TRACK-1",
                "goodsDescription": "Received cargo",
                "cartons": 8,
            },
        )
        self.assertEqual(shipment.status_code, 201, shipment.json)
        self.assertEqual(
            self.client.post(
                "/api/customers",
                json={"companyName": "Blocked", "customerCode": "BLOCKED"},
            ).status_code,
            403,
        )
        self.assertEqual(self.client.get("/api/users").status_code, 403)

        self.client.post("/api/logout")
        self.client.post(
            "/api/login",
            json={"username": "admin", "password": "InitialAdminPass123"},
        )
        operations = self.client.post(
            "/api/users",
            json={
                "username": "operations1",
                "password": "OperationsPass123",
                "role": "operations",
            },
        )
        self.assertEqual(operations.status_code, 201, operations.json)
        self.client.post("/api/logout")
        self.client.post(
            "/api/login",
            json={"username": "operations1", "password": "OperationsPass123"},
        )
        shipment_id = shipment.json["id"]
        loading = self.client.post(
            f"/api/shipments/{shipment_id}/loading",
            json={"containerNumber": "OPS-CNTR-1", "loadingDate": "2026-10-12", "loadedCartons": 3},
        )
        self.assertEqual(loading.status_code, 200, loading.json)
        blocked_status = self.client.post(
            f"/api/shipments/{shipment_id}/status",
            json={"stage": "In transit"},
        )
        self.assertEqual(blocked_status.status_code, 400)
        loaded_remainder = self.client.post(
            f"/api/shipments/{shipment_id}/loading",
            json={"containerNumber": "OPS-CNTR-1", "loadingDate": "2026-10-12", "loadedCartons": 5},
        )
        self.assertEqual(loaded_remainder.status_code, 200, loaded_remainder.json)
        status = self.client.post(
            f"/api/shipments/{shipment_id}/status",
            json={"stage": "In transit"},
        )
        self.assertEqual(status.status_code, 200, status.json)
        self.assertEqual(status.json["statusHistory"][-1]["updatedBy"], "operations1")
        self.assertEqual(
            self.client.post(
                "/api/customers",
                json={"companyName": "Blocked", "customerCode": "BLOCKED"},
            ).status_code,
            403,
        )

        self.client.post("/api/logout")
        self.client.post(
            "/api/login",
            json={"username": "admin", "password": "InitialAdminPass123"},
        )
        viewer = self.client.post(
            "/api/users",
            json={
                "username": "viewer1",
                "password": "ViewerAccountPass123",
                "role": "viewer",
            },
        )
        self.assertEqual(viewer.status_code, 201, viewer.json)
        self.client.post("/api/logout")
        self.client.post(
            "/api/login",
            json={"username": "viewer1", "password": "ViewerAccountPass123"},
        )
        self.assertEqual(self.client.get("/api/dashboard").status_code, 200)
        self.assertEqual(
            self.client.post(
                "/api/shipments",
                json={
                    "customerCode": "TEST-1",
                    "trackingNumber": "VIEW-TRACK-1",
                    "goodsDescription": "Blocked cargo",
                },
            ).status_code,
            403,
        )

    def test_setup_only_creates_one_admin_and_login_checks_password(self):
        second_setup = self.client.post(
            "/api/setup",
            json={"username": "second", "password": "AnotherAdminPass123"},
        )
        self.assertEqual(second_setup.status_code, 409)
        self.client.post("/api/logout")
        bad_login = self.client.post(
            "/api/login",
            json={"username": "admin", "password": "wrong-password"},
        )
        self.assertEqual(bad_login.status_code, 401)
        login = self.client.post(
            "/api/login",
            json={"username": "admin", "password": "InitialAdminPass123"},
        )
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json["user"]["role"], "admin")
        self.assertEqual(
            set(login.json["user"]["permissions"]),
            shipping_app.ROLE_PERMISSIONS["admin"],
        )
        self.assertNotIn("database:manage", login.json["user"]["permissions"])

    def test_admin_can_change_role_and_disable_user_without_self_lockout(self):
        created = self.client.post(
            "/api/users",
            json={
                "username": "staff1",
                "password": "StaffAccountPass123",
                "role": "warehouse",
            },
        )
        self.assertEqual(created.status_code, 201, created.json)
        user_id = created.json["id"]

        changed_role = self.client.patch(
            f"/api/users/{user_id}",
            json={"role": "operations", "isActive": True},
        )
        self.assertEqual(changed_role.status_code, 200, changed_role.json)
        self.assertEqual(changed_role.json["role"], "operations")

        disabled = self.client.patch(
            f"/api/users/{user_id}",
            json={"isActive": False},
        )
        self.assertEqual(disabled.status_code, 200, disabled.json)
        self.assertFalse(disabled.json["isActive"])

        staff_client = shipping_app.app.test_client()
        login = staff_client.post(
            "/api/login",
            json={"username": "staff1", "password": "StaffAccountPass123"},
        )
        self.assertEqual(login.status_code, 401)
        self.assertEqual(staff_client.get("/api/dashboard").status_code, 401)

        self_update = self.client.patch(
            f"/api/users/{self.client.get('/api/session').json['user']['id']}",
            json={"role": "viewer", "isActive": False},
        )
        self.assertEqual(self_update.status_code, 400)
        self.assertTrue(self.client.get("/api/session").json["authenticated"])

    def test_admin_can_grant_revoke_and_restore_role_permissions(self):
        created = self.client.post(
            "/api/users",
            json={
                "username": "customstaff",
                "password": "CustomStaffPass123",
                "role": "warehouse",
            },
        )
        self.assertEqual(created.status_code, 201, created.json)
        user_id = created.json["id"]
        granted = self.client.patch(
            f"/api/users/{user_id}",
            json={"permissions": ["shipments:read", "invoices:read", "invoices:write"]},
        )
        self.assertEqual(granted.status_code, 200, granted.json)
        self.assertEqual(
            set(granted.json["permissions"]),
            {"shipments:read", "invoices:read", "invoices:write"},
        )
        role_changed = self.client.patch(
            f"/api/users/{user_id}",
            json={"role": "viewer"},
        )
        self.assertEqual(role_changed.status_code, 200, role_changed.json)
        self.assertEqual(
            set(role_changed.json["permissions"]),
            {"shipments:read", "invoices:read", "invoices:write"},
        )

        customer = self.client.post(
            "/api/customers",
            json={"companyName": "Permission test", "customerCode": "PERMISSION-1"},
        )
        self.assertEqual(customer.status_code, 201)
        shipment = self.client.post(
            "/api/shipments",
            json={
                "customerCode": "PERMISSION-1",
                "trackingNumber": "PERMISSION-TRACK-1",
                "goodsDescription": "Permission test cargo",
                "cartons": 3,
            },
        )
        self.assertEqual(shipment.status_code, 201)

        staff_client = shipping_app.app.test_client()
        login = staff_client.post(
            "/api/login",
            json={"username": "customstaff", "password": "CustomStaffPass123"},
        )
        self.assertEqual(login.status_code, 200)
        self.assertEqual(staff_client.get("/api/invoices").status_code, 200)
        invoice = staff_client.post(
            "/api/invoices",
            json={
                "shipmentId": shipment.json["id"],
                "invoiceNumber": "PERMISSION-INV-1",
                "invoiceDate": "2026-10-12",
                "currency": "USD",
                "totalAmount": "25",
                "status": "draft",
            },
        )
        self.assertEqual(invoice.status_code, 201, invoice.json)

        revoked = self.client.patch(
            f"/api/users/{user_id}",
            json={"permissions": ["shipments:read", "invoices:read"]},
        )
        self.assertEqual(revoked.status_code, 200, revoked.json)
        self.assertEqual(staff_client.get("/api/invoices").status_code, 200)
        self.assertEqual(
            staff_client.patch(
                f"/api/invoices/{shipment.json['id']}",
                json={"receivedPayment": "5"},
            ).status_code,
            403,
        )

        revoked = self.client.patch(
            f"/api/users/{user_id}",
            json={"permissions": ["shipments:read"]},
        )
        self.assertEqual(revoked.status_code, 200, revoked.json)
        self.assertEqual(staff_client.get("/api/invoices").status_code, 403)

    def test_permission_dependencies_and_non_admin_access_management(self):
        created = self.client.post(
            "/api/users",
            json={
                "username": "limitedstaff",
                "password": "LimitedStaffPass123",
                "role": "viewer",
            },
        )
        self.assertEqual(created.status_code, 201, created.json)
        user_id = created.json["id"]
        invalid = self.client.patch(
            f"/api/users/{user_id}",
            json={"permissions": ["invoices:write"]},
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.json["requiredPermissions"], ["invoices:read"])

        staff_client = shipping_app.app.test_client()
        staff_client.post(
            "/api/login",
            json={"username": "limitedstaff", "password": "LimitedStaffPass123"},
        )
        self.assertEqual(staff_client.get("/api/users").status_code, 403)
        self.assertEqual(staff_client.get("/api/admin/database").status_code, 403)
        self.assertEqual(
            staff_client.post(
                "/api/admin/database/query",
                json={"sql": "SELECT 1"},
            ).status_code,
            403,
        )
        grant_database = self.client.patch(
            f"/api/users/{user_id}",
            json={
                "permissions": [
                    "customers:read", "shipments:read", "database:manage"
                ]
            },
        )
        self.assertEqual(grant_database.status_code, 403)
        manager_access = self.client.patch(
            f"/api/users/{user_id}",
            json={"permissions": ["customers:read", "shipments:read", "users:manage"]},
        )
        self.assertEqual(manager_access.status_code, 200, manager_access.json)
        self.assertEqual(staff_client.get("/api/users").status_code, 200)
        self.assertEqual(staff_client.get("/api/admin/database").status_code, 403)
        self.assertEqual(
            self.client.patch(
                f"/api/users/{user_id}",
                json={
                    "permissions": [
                        "customers:read", "shipments:read", "users:manage",
                        "database:manage",
                    ]
                },
            ).status_code,
            403,
        )
        self.assertEqual(staff_client.get("/api/admin/database").status_code, 403)

        admin_account = self.client.post(
            "/api/users",
            json={
                "username": "limitedadmin",
                "password": "LimitedAdminPass123",
                "role": "admin",
            },
        )
        self.assertEqual(admin_account.status_code, 201, admin_account.json)
        self.assertEqual(
            self.client.patch(
                f"/api/users/{admin_account.json['id']}",
                json={"permissions": ["invoices:read"]},
            ).status_code,
            200,
        )
        limited_admin = shipping_app.app.test_client()
        limited_admin.post(
            "/api/login",
            json={"username": "limitedadmin", "password": "LimitedAdminPass123"},
        )
        self.assertEqual(limited_admin.get("/api/invoices").status_code, 200)
        self.assertEqual(limited_admin.get("/api/dashboard").status_code, 403)
        self.assertEqual(limited_admin.get("/api/users").status_code, 403)
        self.assertEqual(limited_admin.get("/api/admin/database").status_code, 403)
        self.assertNotIn("database:manage", admin_account.json["permissions"])

    def test_edit_archive_restore_and_audit_permissions(self):
        self.assertEqual(self.client.get("/api/audit").status_code, 403)
        unprivileged = self.client.post(
            "/api/users",
            json={
                "username": "auditviewer",
                "password": "AuditViewerPass123",
                "role": "viewer",
            },
        )
        self.assertEqual(unprivileged.status_code, 201, unprivileged.json)
        unauthorized_grant = self.client.patch(
            f"/api/users/{unprivileged.json['id']}",
            json={"permissions": ["customers:read", "shipments:read", "audit:read"]},
        )
        self.assertEqual(unauthorized_grant.status_code, 403)

        super_client = self.grant_permissions(
            self.client,
            {
                "customers:edit", "customers:archive", "shipments:edit",
                "shipments:archive", "audit:read",
            },
        )
        customer = self.client.post(
            "/api/customers",
            json={"companyName": "Audit Cargo", "customerCode": "AUDIT-1"},
        )
        self.assertEqual(customer.status_code, 201)
        shipment = self.client.post(
            "/api/shipments",
            json={
                "customerCode": "AUDIT-1",
                "trackingNumber": "AUDIT-TRACK-1",
                "goodsDescription": "Original description",
                "cartons": 5,
            },
        )
        self.assertEqual(shipment.status_code, 201, shipment.json)

        edited = self.client.patch(
            f"/api/shipments/{shipment.json['id']}",
            json={"goodsDescription": "Corrected description", "cartons": 6},
        )
        self.assertEqual(edited.status_code, 200, edited.json)
        self.assertEqual(edited.json["remainingCartons"], 6)

        history = self.client.get("/api/audit")
        self.assertEqual(history.status_code, 200, history.json)
        edit_event = next(
            event for event in history.json
            if event["entityType"] == "shipment"
            and event["entityId"] == shipment.json["id"]
            and event["action"] == "updated"
        )
        self.assertEqual(edit_event["actorUsername"], "admin")
        self.assertEqual(edit_event["before"]["goods_description"], "Original description")
        self.assertEqual(edit_event["after"]["goods_description"], "Corrected description")
        self.assertTrue(edit_event["changedAt"])

        archived_shipment = self.client.post(
            f"/api/records/shipments/{shipment.json['id']}/archive",
        )
        self.assertEqual(archived_shipment.status_code, 200, archived_shipment.json)
        self.assertEqual(self.client.get("/api/shipments").json, [])
        history = self.client.get("/api/audit").json
        shipment_archive = next(
            event for event in history
            if event["entityType"] == "shipment"
            and event["entityId"] == shipment.json["id"]
            and event["action"] == "archived"
        )
        self.assertTrue(shipment_archive["canRestore"])
        restored_shipment = self.client.post(
            f"/api/audit/{shipment_archive['id']}/restore",
        )
        self.assertEqual(restored_shipment.status_code, 200, restored_shipment.json)
        self.assertEqual(len(self.client.get("/api/shipments").json), 1)

        archived_customer = self.client.post("/api/records/customers/AUDIT-1/archive")
        self.assertEqual(archived_customer.status_code, 409)
        self.client.post(f"/api/records/shipments/{shipment.json['id']}/archive")
        archived_customer = self.client.post("/api/records/customers/AUDIT-1/archive")
        self.assertEqual(archived_customer.status_code, 200, archived_customer.json)
        self.assertEqual(self.client.get("/api/customers").json, [])
        customer_archive = next(
            event for event in self.client.get("/api/audit").json
            if event["entityType"] == "customer" and event["action"] == "archived"
        )
        self.assertEqual(
            self.client.post(f"/api/audit/{customer_archive['id']}/restore").status_code,
            200,
        )
        self.assertEqual(len(self.client.get("/api/customers").json), 1)

        self.assertEqual(super_client.get("/api/audit").status_code, 200)

    def test_container_portion_and_invoice_archive_restore(self):
        self.grant_permissions(
            self.client,
            {"containers:edit", "containers:archive", "invoices:archive", "audit:read"},
        )
        customer = self.client.post(
            "/api/customers",
            json={"companyName": "Container Cargo", "customerCode": "CONT-1"},
        )
        self.assertEqual(customer.status_code, 201)
        shipment = self.client.post(
            "/api/shipments",
            json={
                "customerCode": "CONT-1",
                "trackingNumber": "CONT-TRACK-1",
                "goodsDescription": "Split load",
                "cartons": 5,
            },
        )
        self.assertEqual(shipment.status_code, 201, shipment.json)
        loaded = self.client.post(
            f"/api/shipments/{shipment.json['id']}/loading",
            json={
                "containerNumber": "TEST-CONT-1",
                "loadingDate": "2026-10-12",
                "loadedCartons": 3,
            },
        )
        self.assertEqual(loaded.status_code, 200, loaded.json)
        portion = loaded.json["loadingSegments"][0]

        edited = self.client.patch(
            f"/api/container-portions/{portion['id']}",
            json={"loadedCartons": 4},
        )
        self.assertEqual(edited.status_code, 200, edited.json)
        self.assertEqual(edited.json["shipment"]["remainingCartons"], 1)
        self.assertEqual(self.client.get("/api/containers").json[0]["loaded_cartons"], 4)

        archived_container = self.client.post(
            "/api/records/containers/TEST-CONT-1/archive",
        )
        self.assertEqual(archived_container.status_code, 200, archived_container.json)
        self.assertEqual(self.client.get("/api/containers").json, [])
        container_archive = next(
            event for event in self.client.get("/api/audit").json
            if event["entityType"] == "container"
            and event["entityId"] == "TEST-CONT-1"
            and event["action"] == "archived"
        )
        self.assertEqual(
            self.client.post(f"/api/audit/{container_archive['id']}/restore").status_code,
            200,
        )
        self.assertEqual(self.client.get("/api/containers").json[0]["loaded_cartons"], 4)

        invoice = self.client.post(
            "/api/invoices",
            json={
                "shipmentId": shipment.json["id"],
                "invoiceNumber": "INV-CONT-1",
                "invoiceDate": "2026-10-12",
                "totalAmount": 100,
                "receivedPayment": 0,
                "currency": "PKR",
                "status": "issued",
            },
        )
        self.assertEqual(invoice.status_code, 201, invoice.json)
        self.assertEqual(
            self.client.post(
                f"/api/records/invoices/{shipment.json['id']}/archive"
            ).status_code,
            200,
        )
        self.assertEqual(self.client.get("/api/invoices").json, [])
        invoice_archive = next(
            event for event in self.client.get("/api/audit").json
            if event["entityType"] == "invoice" and event["action"] == "archived"
        )
        self.assertEqual(
            self.client.post(f"/api/audit/{invoice_archive['id']}/restore").status_code,
            200,
        )
        self.assertEqual(len(self.client.get("/api/invoices").json), 1)

    def test_super_admin_is_protected_and_can_manage_other_admins(self):
        created = shipping_app.provision_super_admin(
            "waqasyousafy",
            "SuperAdminPass123",
        )
        self.assertTrue(created["isSuperAdmin"])

        super_client = shipping_app.app.test_client()
        login = super_client.post(
            "/api/login",
            json={"username": "waqasyousafy", "password": "SuperAdminPass123"},
        )
        self.assertEqual(login.status_code, 200, login.json)
        self.assertTrue(login.json["user"]["isSuperAdmin"])
        self.assertEqual(
            set(login.json["user"]["permissions"]),
            set(shipping_app.PERMISSION_LABELS),
        )

        admin_id = self.client.get("/api/session").json["user"]["id"]
        denied = self.client.patch(
            f"/api/users/{created['id']}",
            json={"role": "viewer", "isActive": False},
        )
        self.assertEqual(denied.status_code, 403)

        managed = super_client.patch(
            f"/api/users/{admin_id}",
            json={"role": "warehouse", "isActive": True},
        )
        self.assertEqual(managed.status_code, 200, managed.json)
        self.assertEqual(managed.json["role"], "warehouse")

        protected = super_client.patch(
            f"/api/users/{created['id']}",
            json={"role": "viewer"},
        )
        self.assertEqual(protected.status_code, 400)
        users = super_client.get("/api/users").json
        super_admin = next(user for user in users if user["id"] == created["id"])
        self.assertTrue(super_admin["isSuperAdmin"])

        with self.assertRaisesRegex(ValueError, "already provisioned"):
            shipping_app.provision_super_admin("waqasyousafy", "SuperAdminPass123")

    def test_only_super_admin_can_reset_any_user_password(self):
        created = self.client.post(
            "/api/users",
            json={
                "username": "passwordtarget",
                "password": "InitialTargetPass123",
                "role": "viewer",
            },
        )
        self.assertEqual(created.status_code, 201, created.json)
        user_id = created.json["id"]

        denied = self.client.post(
            f"/api/users/{user_id}/password",
            json={"password": "NewTargetPassword123"},
        )
        self.assertEqual(denied.status_code, 403)

        created_super = shipping_app.provision_super_admin(
            "passwordroot",
            "PasswordRootPass123",
        )
        self.assertTrue(created_super["isSuperAdmin"])
        super_client = shipping_app.app.test_client()
        login = super_client.post(
            "/api/login",
            json={"username": "passwordroot", "password": "PasswordRootPass123"},
        )
        self.assertEqual(login.status_code, 200)

        weak_password = super_client.post(
            f"/api/users/{user_id}/password",
            json={"password": "short"},
        )
        self.assertEqual(weak_password.status_code, 400)
        reset = super_client.post(
            f"/api/users/{user_id}/password",
            json={"password": "NewTargetPassword123"},
        )
        self.assertEqual(reset.status_code, 200, reset.json)

        target_client = shipping_app.app.test_client()
        old_login = target_client.post(
            "/api/login",
            json={"username": "passwordtarget", "password": "InitialTargetPass123"},
        )
        self.assertEqual(old_login.status_code, 401)
        new_login = target_client.post(
            "/api/login",
            json={"username": "passwordtarget", "password": "NewTargetPassword123"},
        )
        self.assertEqual(new_login.status_code, 200)

    def test_super_admin_provisioning_promotes_existing_account_and_preserves_data(self):
        customer = self.client.post(
            "/api/customers",
            json={"companyName": "Keep this record", "customerCode": "KEEP-1"},
        )
        self.assertEqual(customer.status_code, 201)
        original_admin_id = self.client.get("/api/session").json["user"]["id"]

        promoted = shipping_app.provision_super_admin(
            "admin",
            "PromotedAdminPass123",
        )
        self.assertTrue(promoted["promoted"])
        self.assertEqual(promoted["id"], original_admin_id)

        login_client = shipping_app.app.test_client()
        login = login_client.post(
            "/api/login",
            json={"username": "admin", "password": "PromotedAdminPass123"},
        )
        self.assertEqual(login.status_code, 200, login.json)
        self.assertTrue(login.json["user"]["isSuperAdmin"])
        self.assertEqual(
            [entry["customerCode"] for entry in login_client.get("/api/customers").json],
            ["KEEP-1"],
        )

    def test_admin_database_console_is_read_only_and_supports_sqlite_queries(self):
        shipping_app.provision_super_admin(
            "dbsuper",
            "DatabaseSuperPass123",
        )
        database_client = shipping_app.app.test_client()
        login = database_client.post(
            "/api/login",
            json={"username": "dbsuper", "password": "DatabaseSuperPass123"},
        )
        self.assertEqual(login.status_code, 200)
        self.assertTrue(login.json["user"]["isSuperAdmin"])

        catalog = database_client.get("/api/admin/database")
        self.assertEqual(catalog.status_code, 200, catalog.json)
        self.assertTrue(any(table["name"] == "shipments" for table in catalog.json["tables"]))
        self.assertEqual(Path(catalog.json["databasePath"]), shipping_app.DATABASE_FILE)

        query = database_client.post(
            "/api/admin/database/query",
            json={"sql": "SELECT 1 AS value"},
        )
        self.assertEqual(query.status_code, 200, query.json)
        self.assertEqual(query.json["columns"], ["value"])
        self.assertEqual(query.json["rows"], [[1]])
        self.assertTrue(query.json["readOnly"])

        with_semicolon = database_client.post(
            "/api/admin/database/query",
            json={"sql": "SELECT 2 AS value;"},
        )
        self.assertEqual(with_semicolon.status_code, 200, with_semicolon.json)
        self.assertEqual(with_semicolon.json["rows"], [[2]])

        with_clause = database_client.post(
            "/api/admin/database/query",
            json={"sql": "WITH valueset(value) AS (VALUES (3)) SELECT value FROM valueset"},
        )
        self.assertEqual(with_clause.status_code, 200, with_clause.json)
        self.assertEqual(with_clause.json["rows"], [[3]])

        write_attempt = database_client.post(
            "/api/admin/database/query",
            json={"sql": "WITH target AS (SELECT id FROM users) DELETE FROM users"},
        )
        self.assertEqual(write_attempt.status_code, 400)
        self.assertEqual(
            database_client.post(
                "/api/admin/database/query",
                json={"sql": "SELECT 1; SELECT 2"},
            ).status_code,
            400,
        )
        self.assertEqual(
            database_client.post(
                "/api/admin/database/query",
                json={"sql": "PRAGMA user_version"},
            ).status_code,
            400,
        )

        capped = database_client.post(
            "/api/admin/database/query",
            json={
                "sql": (
                    "WITH RECURSIVE numbers(value) AS (VALUES (1) "
                    "UNION ALL SELECT value + 1 FROM numbers WHERE value < 501) "
                    "SELECT value FROM numbers"
                )
            },
        )
        self.assertEqual(capped.status_code, 200, capped.json)
        self.assertEqual(capped.json["returnedRows"], 500)
        self.assertTrue(capped.json["truncated"])

        backup = database_client.get("/api/admin/database/backup")
        self.assertEqual(backup.status_code, 200)
        self.assertTrue(backup.data.startswith(b"SQLite format 3\x00"))

    def test_sqlite_schema_has_related_tables(self):
        with shipping_app.database_connection() as connection:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertTrue(
            {
                "customers",
                "users",
                "shipments",
                "shipment_status_history",
                "shipment_photos",
                "shipment_bills",
            }.issubset(tables)
        )
        with shipping_app.database_connection() as connection:
            user_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(users)")
            }
        self.assertIn("is_super_admin", user_columns)


if __name__ == "__main__":
    unittest.main()
