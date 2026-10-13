# MH Global Customs Solutions - Cargo Tracking System

ShippingFlow is a Flask and SQLite staff dashboard for tracking customer cargo from the China warehouse through Pakistan delivery. WhatsApp messaging, sender verification, chatbot replies, human takeover and outbound notifications are intentionally not implemented in this phase.

## Run locally

1. Install Python dependencies:
   ```bash
   python -m pip install -r requirements.txt
   ```
2. Start the app:
   ```bash
   python app.py
   ```
3. Open `http://localhost:3000/`.

On first startup, create the administrator account. Passwords must be at least 12 characters. The app creates `database/shippingflow.db` from `database/schema.sql`; startup migrations extend an existing database while preserving its users and cargo records. Existing customers and shipments in `data/store.json` are imported if the database has no customers.

For the isolated 10-customer sample and staff-training walkthrough, follow [TRAINING_GUIDE.md](./TRAINING_GUIDE.md) and run [seed_demo.py](./seed_demo.py). The training app selects its database with the `SHIPPINGFLOW_DATABASE` environment variable; do not point it at the live database when resetting demo data.

## Deploy the existing Flask app with persistent SQLite, and use Vercel

Vercel's serverless filesystem is temporary, so it cannot safely host this app's SQLite database or uploaded cargo documents. Keep the existing Flask app and SQLite design by running the API on a host with a persistent disk, then use Vercel to publish the dashboard and proxy its `/api/*` requests to the Flask service.

1. In Render, create a **Blueprint** and connect this GitHub repository. Render reads [render.yaml](./render.yaml), which starts the unchanged Flask app with Gunicorn, creates a persistent disk at `/var/data`, stores the database and uploads there, and generates a private Flask session key.
2. Use a paid Render instance plan that supports persistent disks. Do not remove or replace the disk; its contents are the live database, uploads and login/session configuration.
3. Deploy the Blueprint and wait for its health check at `/api/health` to pass. Copy the service's HTTPS URL, for example `https://shippingflow-api.onrender.com`.
4. In [vercel.json](./vercel.json), add an API rewrite before the static asset rewrite, replacing the example service hostname with the exact URL Render assigned:
   ```json
   {
     "source": "/api/:path*",
     "destination": "https://shippingflow-api.onrender.com/api/:path*"
   }
   ```
   Keep the existing `/static/:path*` rewrite. Commit and push `vercel.json`; Vercel will redeploy. Because API calls stay on the Vercel site origin and Vercel proxies them server-side, the browser's login cookie continues to work without enabling broad cross-origin access.
5. Before staff start using the new service, migrate the current `database/shippingflow.db` and `database/uploads/` to `/var/data/shippingflow.db` and `/var/data/uploads/` using a secure transfer path. These files are intentionally excluded from Git. Verify the existing accounts and cargo after transfer and back up both files; never put database files, uploads, or account secrets in the repository.

The Vercel project remains the staff-facing address, while Render runs the same Flask application and persists the original SQLite data. The app's database console only queries the configured Render database; it does not provide a data migration or restore-upload interface.

## Operational workflows

- **Customers:** admins register and correct customer/company and delivery destination details. Customer code remains the shipment ownership key.
- **China warehouse:** warehouse staff register each cargo lot with a unique tracking number, customer, shipping mark, supplier, received date, description, cartons, weight and CBM. JPEG/PNG photos and a PDF packing list can be uploaded to the lot. Documents are downloadable only to staff with shipment-read access.
- **Container loading:** each loading save records a separate container portion and its cartons, loading date, ports, vessel/voyage, BL reference, expected departure date from China, expected arrival date at the Pakistan port (ETA), and a separate estimated customer delivery date. These are estimates, not confirmed promises. Remaining cartons are computed from the received quantity and all portions; a lot with cartons remaining cannot be marked fully loaded.
- **Milestones:** the allowed stages are China warehouse received, awaiting loading, loaded in container, departed from China, in transit, arrived at Pakistan port, customs clearance in progress, customs cleared, transport dispatched and delivered. Updates retain the staff username, date and remarks. Container milestone updates affect only the lots/portions assigned to that container. A split lot shows its per-container status, while its overall status remains at the least-advanced portion (or partial loading while cartons remain). Delivery confirmation is per cargo lot, not a bulk container action.
- **Billing:** Accounts can create bills as drafts or approve/issue them. Payment and outstanding balance are calculated from entered accounting values. A revised bill archives the previous bill as superseded. Only the current approved/issued or paid bill can be downloaded as PDF; drafts cannot be downloaded.
- **Record corrections and archive:** Authorized staff can edit cargo, loading portions, customer details and bills. Archive is a soft-delete: customers, cargo lots, container portions and bills are hidden from active registers but remain in SQLite with their documents and history. Customers with active cargo cannot be archived. A super administrator can grant individual edit/archive permissions; matching archive permission is required to restore an item from Audit history.
- **Audit history:** Changes to customers, shipments, loading portions, invoices, uploaded documents and staff access are recorded with the actor, timestamp, action and before/after values. Archive and restore actions are included. The separate **Audit history** page is hidden unless the super administrator grants `audit:read`; ordinary administrators cannot grant or revoke audit, edit, archive or restore permissions. The log is append-only through the application and the database console remains read-only.
- **Search:** the dashboard searches by tracking number, customer code, supplier, shipping mark and container number. Arrival ETA and delivery estimate remain separate fields, and unknown dates remain blank/Pending rather than being guessed.

## Staff access

Administrators manage staff accounts and customer records. Warehouse users can receive cargo, attach documents and record loading. Operations users update Pakistan/container milestones. Delivery users can confirm delivery per cargo lot. Accounts users manage and approve bills. Read-only users can view operational records. Access is enforced by the API as well as by the dashboard.

In **User access**, a user with `users:manage` can change a staff member's role label, disable or reactivate the account, and grant or remove ordinary individual permissions. Permissions are saved per user and enforced from that saved list; changing a role does not change access. A new user's initial permissions are copied into their account once from the selected staff category, then can be changed individually. Edit, archive, restore and audit-history access can only be granted or revoked by the super administrator. Write permissions require their related read permission so the dashboard can show the forms and records those actions need. The `users:manage` and `database:manage` permissions independently control staff administration and database tools. Keep at least one active administrator account.

The protected super-administrator account is provisioned separately with `provision_super_admin.py`. It has full administrator access, can manage other administrator accounts, and cannot be edited, disabled, or demoted through the ordinary admin access manager or API. It is the only account that can access the database explorer or reset passwords for other users. If the matching username already exists, provisioning promotes that account and sets the supplied password without deleting its related data; it refuses to create a second super administrator or reset an already-provisioned one. Supply the initial password through the `SHIPPINGFLOW_SUPER_ADMIN_PASSWORD` environment variable; do not store it in source code or commit it. Change the password after first login.

## Database access and backups

The live SQLite database defaults to `database/shippingflow.db`. To keep it at a separate path, set `SHIPPINGFLOW_DATABASE` before starting the app. For example, in PowerShell:

```powershell
$env:SHIPPINGFLOW_DATABASE = 'D:\CargoData\shippingflow.db'
python app.py
```

The parent directory is created when needed. Cargo uploads are stored in an `uploads/` directory alongside the configured database; the session secret remains in `database/session.secret`. Restrict access to all of these files and include the database and uploads in protected backups.

Administrators can inspect tables and columns, run `SELECT`, `WITH` and `EXPLAIN` statements, and download a consistent SQLite snapshot from the **Database** page. The SQL console opens the configured database read-only, rejects writes and multiple statements, and caps results at 500 rows. Use the backup download or a separately controlled database tool for recovery and maintenance; this console cannot edit records or schema.

The app binds to localhost by default. To expose it on a trusted network, set `HOST=0.0.0.0` and run it behind HTTPS. Set `SESSION_COOKIE_SECURE=1` when HTTPS is enabled. Keep `database/session.secret` private and back it up with the database.

## API highlights

- `GET/POST /api/customers`; `PATCH /api/customers/<customer-code>` (admin correction)
- `GET/POST /api/shipments`; `PATCH /api/shipments/<id>` (cargo correction)
- `GET /api/shipments/<id>`
- `POST /api/shipments/<id>/loading` (adds a separately tracked container portion)
- `PATCH /api/container-portions/<loading-id>` (correct an existing loading portion)
- `POST /api/shipments/<id>/documents`; `GET /api/documents/<document-id>`
- `POST /api/shipments/<id>/status`
- `GET /api/containers`; `POST /api/containers/<container-number>/status`
- `POST /api/records/{customers|shipments|containers|invoices}/<id>/archive`
- `GET /api/audit`; `POST /api/audit/<event-id>/restore`
- `GET /api/dashboard`
- `GET/POST /api/invoices`; `PATCH /api/invoices/<shipment-id>` (accounting corrections)
- `GET /api/invoices/<shipment-id>/pdf`

The database file and `database/uploads/` contain operational records and cargo documents. Include both in encrypted, access-controlled backups and test restore procedures. Hosting, WhatsApp/API, AI usage, backup retention, staff training and maintenance costs need to be agreed and documented separately before deployment; this code does not set up external accounts, automatic off-site backups or a WhatsApp integration.

## Validation

Run the focused API suite with:

```bash
python -m unittest discover -s tests -v
```
