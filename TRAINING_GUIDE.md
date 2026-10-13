# ShippingFlow staff training and 10-customer walkthrough

This is a hands-on training scenario for warehouse, Pakistan operations, delivery and accounts staff. It uses a separate database file, `database/demo_shippingflow.db`; it does not add sample records to the live `database/shippingflow.db`.

## 1. Prepare and open the training dashboard

Stop any already-running local ShippingFlow server first. From PowerShell at the project folder, seed the training database:

```powershell
.\.venv\Scripts\python.exe .\seed_demo.py
$env:SHIPPINGFLOW_DATABASE = (Resolve-Path .\database\demo_shippingflow.db).Path
.\.venv\Scripts\python.exe .\app.py
```

Open `
`. Keep that PowerShell window open while the server is running. The seed script can be rerun to reset the *demo* database to its original 10-cargo starting point; it never targets the live database.

Every account below is a separate login and has a different role. For this local training database only, the password is `DemoTraining123!`.

| Staff role | Login username | What to practice |
|---|---|---|
| Administrator | `demo.admin` | Customer registration/corrections, staff accounts and all operational areas |
| China warehouse | `china.warehouse` | Receive cargo, upload photos/packing lists, and record container portions |
| Pakistan operations | `pakistan.ops` | Update container and Pakistan milestone statuses |
| Delivery team | `delivery.team` | Confirm a selected cargo lot as Delivered |
| Accounts | `accounts.team` | Create/approve/revise bills, record payments and download approved PDFs |
| Read only | `audit.viewer` | Confirm that records are visible but write actions are unavailable |

These shared demo credentials are deliberately predictable for classroom use. Never use them on a public server or for real customer records. Create individually assigned accounts with strong unique passwords for a real deployment.

## 2. Starting scenario

The seed script creates ten fictional customers and one received cargo lot per customer. All have a warehouse receiving record and a status-history entry. No cargo is loaded yet, and no ETA is prefilled: staff should enter confirmed operational estimates rather than assume them.

| Customer | Code | Tracking number | Supplier | Received cartons |
|---|---|---|---|---:|
| Karachi Traders | `CUST-001` | `MH-DEMO-001` | Supplier 01, Yiwu | 10 |
| Lahore Home Store | `CUST-002` | `MH-DEMO-002` | Supplier 02, Yiwu | 6 |
| Islamabad Tech House | `CUST-003` | `MH-DEMO-003` | Supplier 03, Yiwu | 4 |
| Faisalabad Fabrics | `CUST-004` | `MH-DEMO-004` | Supplier 04, Yiwu | 12 |
| Peshawar Imports | `CUST-005` | `MH-DEMO-005` | Supplier 05, Yiwu | 8 |
| Multan Commercial | `CUST-006` | `MH-DEMO-006` | Supplier 06, Yiwu | 5 |
| Quetta Supply Co | `CUST-007` | `MH-DEMO-007` | Supplier 07, Yiwu | 7 |
| Hyderabad Wholesale | `CUST-008` | `MH-DEMO-008` | Supplier 08, Yiwu | 9 |
| Sialkot Sports Goods | `CUST-009` | `MH-DEMO-009` | Supplier 09, Yiwu | 11 |
| Rawalpindi Machinery | `CUST-010` | `MH-DEMO-010` | Supplier 10, Yiwu | 3 |

Training goal: place cargo for all ten customers into `CNTR-TRAIN-001` (ten customer cargo lots, not one combined customer record). Split Karachi Traders' ten cartons: put six in `CNTR-TRAIN-001` and the remaining four in `CNTR-TRAIN-002`. Each portion must remain separately visible.

## 3. Run the workflow by staff role

### A. Administrator: customer and account setup

1. Sign in as `demo.admin`.
2. Open **Customers** and confirm there are 10 unique customer codes. Use the correction form to update a delivery city/address, then refresh and verify the change.
3. Open **User access**. The training accounts already exist. For extra practice, create a temporary named account for a colleague and choose a role; sign out and sign in with that account to verify it.
4. Do not share one staff login in day-to-day work: each account's username is recorded with its updates.

### B. China warehouse: receiving and documents

1. Sign out; sign in as `china.warehouse`.
2. Open **Cargo receiving** and inspect the ten `MH-DEMO-*` records in the dashboard. These are the prepared warehouse-received starting lots, so you do not need to enter them again.
3. To practice a brand-new receipt, select one customer and enter a new unique tracking number, shipping mark, supplier, received date, goods, cartons, weight and CBM. Optionally upload JPEG/PNG cargo photos and a PDF packing list. Do not reuse one of the existing tracking numbers.
4. The ten prepared records have no documents. Use **Attach documents to an existing lot** to add a training photo/PDF to `MH-DEMO-001`; confirm links appear in the cargo register and download requires a logged-in staff account.
5. As an optional first milestone, update one lot to **Awaiting loading** using **Shipment status**. This records the staff member, date and remarks.

### C. China warehouse: load a shared container and a split lot

For each of `MH-DEMO-001` through `MH-DEMO-010`, use **Container loading**. Enter the portion quantity and loading date. Use `CNTR-TRAIN-001` for all portions except the second Karachi Traders portion:

| Tracking | Container | Cartons to load |
|---|---|---:|
| `MH-DEMO-001` | `CNTR-TRAIN-001` | 6 |
| `MH-DEMO-001` | `CNTR-TRAIN-002` | 4 |
| `MH-DEMO-002` | `CNTR-TRAIN-001` | 6 |
| `MH-DEMO-003` | `CNTR-TRAIN-001` | 4 |
| `MH-DEMO-004` | `CNTR-TRAIN-001` | 12 |
| `MH-DEMO-005` | `CNTR-TRAIN-001` | 8 |
| `MH-DEMO-006` | `CNTR-TRAIN-001` | 5 |
| `MH-DEMO-007` | `CNTR-TRAIN-001` | 7 |
| `MH-DEMO-008` | `CNTR-TRAIN-001` | 9 |
| `MH-DEMO-009` | `CNTR-TRAIN-001` | 11 |
| `MH-DEMO-010` | `CNTR-TRAIN-001` | 3 |

For the loading date, use the actual date of the training exercise. Add departure/destination ports, vessel/voyage, BL reference and expected dates only if they are part of the exercise. Keep expected Pakistan arrival and delivery estimate separate; blank means unknown, not a promise. For a partial load, the first Karachi save must display 6 loaded and 4 remaining. After the second save it must show 10 loaded and zero remaining. The system rejects loading more cartons than remain.

### D. Pakistan operations: update container and shipment milestones

1. Sign out and sign in as `pakistan.ops`.
2. In **Shipment status**, update `CNTR-TRAIN-001` through the container milestone form in sequence as the exercise progresses: **Departed from China** → **In transit** → **Arrived at Pakistan port** → **Customs clearance in progress** → **Customs cleared** → **Transport dispatched**. Add a truthful remark each time.
3. After each save, confirm nine fully loaded lots for the first container receive its milestone. The Karachi split lot remains **Partial loading** until its second portion is loaded; its portion display tells you which container has advanced.
4. Update `CNTR-TRAIN-002` independently for the four Karachi cartons. The cargo lot's overall milestone advances only as its slowest container portion advances. A container update must not update lots assigned only to the other container.
5. Search using `CNTR-TRAIN-001`, a customer code and a tracking number. Verify the cargo table displays individual customers, carton totals, container portions and the separate arrival/delivery estimates.

### E. Delivery team: per-customer confirmation

1. Sign in as `delivery.team`.
2. Confirm **Delivered** separately for the relevant tracking number only after the delivery is actually confirmed; include a concise remark.
3. Verify another customer's delivery remains unchanged. Delivery confirmation is intentionally not a bulk container update.

### F. Accounts: approved bills, payment and revisions

1. Sign in as `accounts.team` and open **Billing**.
2. Select a shipment and enter its invoice number/date, currency, total and received payment. Save as **Draft — awaiting approval** and confirm PDF download is unavailable.
3. Edit/revise it with a new invoice number and select **Approve and issue** (or Paid in full only when the full amount was received). Verify balance is total minus recorded payment and download the approved PDF.
4. Revise again and verify the former bill is shown as **superseded**, is not downloadable as the latest bill, and only the current approved copy is offered.
5. Payment figures here are for workflow demonstration only; use Accounts' verified accounting figures in live operations.

### G. Read-only and permission checks

Sign in as `audit.viewer`. The user can search/view operational records but cannot receive cargo, load containers, update milestones, manage users or access bills. Try opening a restricted page or submitting an API write if validating permissions; the server, not just the hidden dashboard controls, must reject unauthorized changes.

## 4. Training acceptance checklist

- [ ] Ten separate customers and tracking records appear; a shared container never merges customer cargo.
- [ ] `MH-DEMO-001` shows 6/10 after its first load and 10/10 after its second; both container numbers remain visible.
- [ ] Loading beyond the remaining quantity is rejected.
- [ ] Container 001 milestone affects only its own portions/lots; container 002 is independently progressed.
- [ ] The split lot does not claim full container progress while a portion lags.
- [ ] Arrival ETA and delivery estimate remain separate; missing estimates stay Pending/blank.
- [ ] Each status history item contains the actual user, date and remarks.
- [ ] Delivery status is confirmed customer-lot by customer-lot.
- [ ] Draft bills are not downloadable; revision supersedes the previous bill; latest approved PDF has the Accounts-entered balance.
- [ ] Warehouse, Operations, Delivery, Accounts and read-only permissions match their roles.

## 5. Reset or return to live data

To reset training, stop the server and run `.\.venv\Scripts\python.exe .\seed_demo.py` again. Start the demo server with `SHIPPINGFLOW_DATABASE` set to `database\demo_shippingflow.db` as in section 1. To use live data, stop it, clear that environment variable with `$env:SHIPPINGFLOW_DATABASE = $null`, then run `.\.venv\Scripts\python.exe .\app.py`; this selects `database\shippingflow.db`. Always check which database is selected before recording real work.

The sample data, addresses, cargo descriptions, staff users and bills are fictional training material. WhatsApp, customer phone authorization, chatbot replies, notifications and human handover are outside this training build.
