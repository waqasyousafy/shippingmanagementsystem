const roleNames = {
  admin: 'Administrator',
  warehouse: 'Warehouse',
  operations: 'Operations',
  delivery: 'Delivery team',
  accounts: 'Accounts',
  viewer: 'Read only'
};

const permissionLabels = {
  'customers:read': 'View customers and addresses',
  'customers:write': 'Register customers',
  'customers:edit': 'Edit customer details',
  'customers:archive': 'Archive and restore customers',
  'shipments:read': 'View cargo and shipment records',
  'shipments:receive': 'Receive cargo and upload documents',
  'shipments:operate': 'Record container loading',
  'shipments:edit': 'Edit cargo and shipment details',
  'shipments:archive': 'Archive and restore cargo lots',
  'shipments:status': 'Update shipment milestones',
  'shipments:deliver': 'Confirm cargo delivery',
  'containers:operate': 'Update container milestones',
  'containers:edit': 'Edit container loading portions',
  'containers:archive': 'Archive and restore containers',
  'invoices:read': 'View bills and payment records',
  'invoices:write': 'Create and approve bills',
  'invoices:edit': 'Edit and revise bills',
  'invoices:archive': 'Archive and restore bills',
  'audit:read': 'View the immutable audit history',
  'users:manage': 'Manage staff accounts and access'
};

const superAdminAssignedPermissions = new Set([
  'audit:read'
]);

const permissionDependencies = {
  'customers:write': ['customers:read'],
  'customers:edit': ['customers:read'],
  'customers:archive': ['customers:read', 'shipments:read'],
  'shipments:receive': ['shipments:read'],
  'shipments:operate': ['shipments:read'],
  'shipments:edit': ['shipments:read'],
  'shipments:archive': ['shipments:read'],
  'shipments:status': ['shipments:read'],
  'shipments:deliver': ['shipments:read'],
  'containers:operate': ['shipments:read'],
  'containers:edit': ['shipments:read'],
  'containers:archive': ['shipments:read'],
  'invoices:write': ['invoices:read'],
  'invoices:edit': ['invoices:read', 'invoices:write'],
  'invoices:archive': ['invoices:read']
};

const api = {
  get: (url) => request(url),
  post: (url, body = {}) => request(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  }),
  patch: (url, body = {}) => request(url, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  })
};

let currentUser = null;
let customers = [];
let shipments = [];
let invoices = [];
let containers = [];
let auditEvents = [];
let permissionUser = null;
let passwordResetUser = null;
let shipmentEditTarget = null;
let loadingEditTarget = null;
let toastTimer;

function activatePage(page) {
  const links = [...document.querySelectorAll('[data-page-link]')];
  let activePage = page;
  const activeLink = links.find((link) => link.dataset.pageLink === page && !link.hidden);
  if (!activeLink) activePage = 'overview';

  document.querySelectorAll('[data-page-view]').forEach((view) => {
    view.hidden = view.dataset.pageView !== activePage;
  });
  links.forEach((link) => {
    const isActive = link.dataset.pageLink === activePage;
    link.classList.toggle('active', isActive);
    if (isActive) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
  return activePage;
}

function navigateToPage(page) {
  const activePage = activatePage(page);
  if (window.location.hash !== `#${activePage}`) {
    window.history.pushState(null, '', `#${activePage}`);
  }
}

async function request(url, options = {}) {
  const response = await fetch(url, { credentials: 'same-origin', ...options });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && !['/api/session', '/api/login', '/api/setup'].includes(url)) {
      showAuthentication(false);
    }
    throw new Error(data.error || 'The request could not be completed.');
  }
  return data;
}

function showAuthentication(setupRequired) {
  currentUser = null;
  document.getElementById('appShell').hidden = true;
  document.getElementById('authScreen').hidden = false;
  document.getElementById('authError').hidden = true;
  document.getElementById('loginForm').hidden = setupRequired;
  document.getElementById('setupForm').hidden = !setupRequired;
  document.getElementById('authEyebrow').textContent = setupRequired ? 'First-time setup' : 'Account access';
  document.getElementById('authTitle').textContent = setupRequired ? 'Create your admin account' : 'Sign in';
  document.getElementById('authDescription').textContent = setupRequired
    ? 'Set up the first administrator. You can create individual staff accounts after signing in.'
    : 'Use your assigned account to continue.';
}

function showApp(user) {
  currentUser = user;
  document.getElementById('authScreen').hidden = true;
  document.getElementById('appShell').hidden = false;
  document.getElementById('userName').textContent = user.username;
  document.getElementById('userRole').textContent = user.isSuperAdmin
    ? 'Super administrator'
    : (roleNames[user.role] || user.role);
  document.getElementById('userAvatar').textContent = user.username.slice(0, 1).toUpperCase();
  document.querySelectorAll('[data-permission]').forEach((element) => {
    element.hidden = !user.permissions.includes(element.dataset.permission);
  });
  activatePage(window.location.hash.slice(1) || 'overview');
  refreshWorkspace();
}

function showToast(message, isError = false) {
  const toast = document.getElementById('toast');
  toast.textContent = message;
  toast.classList.toggle('toast-error', isError);
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, 3200);
}

function showFormError(elementId, message) {
  const error = document.getElementById(elementId);
  error.textContent = message;
  error.hidden = false;
}

function addCell(row, value, className = '') {
  const cell = document.createElement('td');
  cell.textContent = value || '—';
  if (className) cell.className = className;
  row.append(cell);
}

function actionButton(text, className, onClick, label = text) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = className;
  button.textContent = text;
  button.setAttribute('aria-label', label);
  button.addEventListener('click', onClick);
  return button;
}

async function archiveRecord(entityType, entityId, label) {
  if (!window.confirm(`Archive ${label}? It will be hidden from active lists and can be restored later.`)) return;
  try {
    await api.post(`/api/records/${entityType}/${encodeURIComponent(entityId)}/archive`);
    await refreshWorkspace();
    showToast(`${label} archived; its data and audit history are retained.`);
  } catch (error) {
    showToast(error.message, true);
  }
}

function formatDate(value) {
  if (!value || value === 'Pending') return 'Pending';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(date);
}

function formatDateTime(value) {
  if (!value || value === 'Pending') return 'Pending';
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(date);
}

function formatSegmentDates(shipment, field, fallback) {
  const estimates = (shipment.loadingSegments || [])
    .filter((segment) => segment[field])
    .map((segment) => `${segment.containerNumber}: ${formatDate(segment[field])}`);
  return estimates.length ? estimates.join('; ') : formatDate(fallback);
}

function renderStatusSummary(counts) {
  const target = document.getElementById('statusSummary');
  target.replaceChildren();
  const entries = Object.entries(counts);
  if (!entries.length) {
    target.textContent = 'No shipment activity yet.';
    target.className = 'status-summary is-empty';
    return;
  }
  target.className = 'status-summary';
  const largest = Math.max(...entries.map(([, count]) => count), 1);
  entries.forEach(([status, count]) => {
    const item = document.createElement('div');
    item.className = 'status-row';
    const heading = document.createElement('div');
    heading.className = 'status-row-heading';
    const name = document.createElement('span');
    name.textContent = status;
    const total = document.createElement('strong');
    total.textContent = count;
    heading.append(name, total);
    const track = document.createElement('div');
    track.className = 'status-track';
    const bar = document.createElement('span');
    bar.style.width = `${Math.max(8, (count / largest) * 100)}%`;
    track.append(bar);
    item.append(heading, track);
    target.append(item);
  });
}

function renderShipments() {
  const query = document.getElementById('shipmentSearch').value.trim().toLowerCase();
  const selectedStatus = document.getElementById('statusFilter').value;
  const rows = document.getElementById('shipmentRows');
  rows.replaceChildren();
  const filtered = shipments.filter((shipment) => {
    const customer = customers.find((item) => item.customerCode === shipment.customerCode);
    const containerNumbers = (shipment.loadingSegments || []).map((segment) => (
      `${segment.containerNumber} ${segment.status || ''}`
    ));
    const searchText = [
      shipment.trackingNumber, shipment.customerCode, customer && customer.companyName, shipment.status,
      shipment.shippingMark, shipment.supplierName, ...containerNumbers
    ].join(' ').toLowerCase();
    return searchText.includes(query) && (!selectedStatus || shipment.status === selectedStatus);
  });
  filtered.forEach((shipment) => {
    const row = document.createElement('tr');
    addCell(row, shipment.trackingNumber, 'tracking-cell');
    addCell(row, shipment.customerCode);
    addCell(row, formatDate(shipment.warehouseReceivingDate));
    addCell(row, [shipment.supplierName, shipment.shippingMark].filter(Boolean).join(' · '));
    addCell(
      row,
      (shipment.loadingSegments || [])
        .map((segment) => `${segment.containerNumber} (${segment.status || 'Loaded'})`)
        .join(', '),
    );
    addCell(
      row,
      `${shipment.loadedCartons || 0} / ${shipment.cartons || 0} loaded · ${shipment.remainingCartons || 0} remaining`,
    );
    const statusCell = document.createElement('td');
    const badge = document.createElement('span');
    badge.className = 'status-badge';
    badge.textContent = shipment.status;
    statusCell.append(badge);
    row.append(statusCell);
    addCell(row, formatSegmentDates(shipment, 'expectedArrivalDate', shipment.expectedArrivalDate));
    addCell(row, formatSegmentDates(shipment, 'expectedDeliveryDate', shipment.expectedDeliveryDate));
    addCell(row, formatDateTime(shipment.lastUpdatedAt));
    const documentCell = document.createElement('td');
    (shipment.documents || []).forEach((item, index) => {
      if (index) documentCell.append(document.createTextNode(' · '));
      const link = document.createElement('a');
      link.href = item.downloadUrl;
      link.textContent = item.documentType === 'packing_list' ? 'Packing list' : 'Photo';
      link.setAttribute('aria-label', `Download ${item.fileName}`);
      documentCell.append(link);
    });
    if (!(shipment.documents || []).length) documentCell.textContent = '—';
    row.append(documentCell);
    const actionCell = document.createElement('td');
    actionCell.className = 'invoice-actions';
    if (currentUser.permissions.includes('shipments:edit')) {
      actionCell.append(actionButton(
        'Edit',
        'secondary-btn user-save-btn',
        () => openShipmentEditDialog(shipment),
        `Edit cargo ${shipment.trackingNumber}`
      ));
    }
    if (currentUser.permissions.includes('shipments:archive')) {
      actionCell.append(actionButton(
        'Archive',
        'secondary-btn user-save-btn',
        () => archiveRecord('shipments', shipment.id, `shipment ${shipment.trackingNumber}`),
        `Archive cargo ${shipment.trackingNumber}`
      ));
    }
    if (!actionCell.childElementCount) actionCell.textContent = '—';
    row.append(actionCell);
    rows.append(row);
  });
  document.getElementById('shipmentEmpty').hidden = filtered.length > 0;
  const filter = document.getElementById('statusFilter');
  const value = filter.value;
  filter.replaceChildren(new Option('All statuses', ''));
  [...new Set(shipments.map((shipment) => shipment.status))].sort().forEach((status) => {
    filter.add(new Option(status, status));
  });
  filter.value = value;
}

function populateSelect(selectId, items, labelForItem, valueForItem = (item) => item.id) {
  const select = document.getElementById(selectId);
  const prompt = select.options[0].textContent;
  select.replaceChildren(new Option(prompt, ''));
  items.forEach((item) => select.add(new Option(labelForItem(item), valueForItem(item))));
}

function renderCustomers() {
  const rows = document.getElementById('customerRows');
  rows.replaceChildren();
  customers.forEach((customer) => {
    const row = document.createElement('tr');
    addCell(row, customer.companyName, 'tracking-cell');
    addCell(row, customer.customerCode);
    addCell(row, [customer.deliveryCity, customer.deliveryAddress].filter(Boolean).join(', '));
    const activeShipments = shipments.filter((shipment) => shipment.customerCode === customer.customerCode);
    addCell(row, activeShipments.length);
    const actionCell = document.createElement('td');
    actionCell.className = 'invoice-actions';
    if (currentUser.permissions.includes('customers:archive') && activeShipments.length === 0) {
      actionCell.append(actionButton(
        'Archive',
        'secondary-btn user-save-btn',
        () => archiveRecord('customers', customer.customerCode, `customer ${customer.companyName}`),
        `Archive customer ${customer.companyName}`
      ));
    } else {
      actionCell.textContent = activeShipments.length ? 'Archive cargo first' : '—';
    }
    row.append(actionCell);
    rows.append(row);
  });
  populateSelect(
    'receivingCustomer',
    customers,
    (customer) => `${customer.companyName} · ${customer.customerCode}`,
    (customer) => customer.customerCode,
  );
  populateSelect(
    'customerEditSelect',
    customers,
    (customer) => `${customer.companyName} · ${customer.customerCode}`,
    (customer) => customer.customerCode,
  );
}

function renderUsers(users) {
  const rows = document.getElementById('userRows');
  rows.replaceChildren();
  users.forEach((user) => {
    const row = document.createElement('tr');
    addCell(row, user.isSuperAdmin ? `${user.username} · Super admin` : user.username, 'tracking-cell');
    const roleCell = document.createElement('td');
    const roleSelect = document.createElement('select');
    if (user.isSuperAdmin) {
      roleCell.textContent = 'Super administrator';
    } else {
      Object.entries(roleNames).forEach(([role, name]) => roleSelect.add(new Option(name, role)));
      roleSelect.value = user.role;
      roleSelect.setAttribute('aria-label', `Authority for ${user.username}`);
      roleCell.append(roleSelect);
    }
    row.append(roleCell);

    const statusCell = document.createElement('td');
    const statusSelect = document.createElement('select');
    if (user.isSuperAdmin) {
      statusCell.textContent = 'Active';
    } else {
      statusSelect.add(new Option('Active', 'active'));
      statusSelect.add(new Option('Disabled', 'disabled'));
      statusSelect.value = user.isActive ? 'active' : 'disabled';
      statusSelect.setAttribute('aria-label', `Account status for ${user.username}`);
      statusCell.append(statusSelect);
    }
    row.append(statusCell);

    const actionCell = document.createElement('td');
    actionCell.className = 'user-actions';
    const saveButton = document.createElement('button');
    saveButton.type = 'button';
    saveButton.className = 'secondary-btn user-save-btn';
    saveButton.textContent = 'Save';
    const isCurrentUser = user.id === currentUser.id;
    const protectedAccount = user.isSuperAdmin;
    roleSelect.disabled = isCurrentUser;
    statusSelect.disabled = isCurrentUser;
    saveButton.disabled = isCurrentUser || protectedAccount;
    saveButton.title = protectedAccount
      ? 'The super-administrator account is protected from access changes'
      : (isCurrentUser ? 'Your own access cannot be changed here' : `Save access for ${user.username}`);
    saveButton.addEventListener('click', async () => {
      saveButton.disabled = true;
      try {
        await api.patch(`/api/users/${encodeURIComponent(user.id)}`, {
          role: roleSelect.value,
          isActive: statusSelect.value === 'active'
        });
        showToast(`Access updated for ${user.username}.`);
        await refreshWorkspace();
      } catch (error) {
        showToast(error.message, true);
        saveButton.disabled = false;
      }
    });
    actionCell.append(saveButton);
    const permissionButton = document.createElement('button');
    permissionButton.type = 'button';
    permissionButton.className = 'secondary-btn user-save-btn';
    permissionButton.textContent = 'Permissions';
    permissionButton.disabled = protectedAccount;
    permissionButton.title = protectedAccount
      ? 'The super-administrator account is protected from access changes'
      : `Manage individual permissions for ${user.username}`;
    permissionButton.addEventListener('click', () => openPermissionsDialog(user));
    actionCell.append(permissionButton);
    if (currentUser.isSuperAdmin) {
      const resetPasswordButton = document.createElement('button');
      resetPasswordButton.type = 'button';
      resetPasswordButton.className = 'secondary-btn user-save-btn';
      resetPasswordButton.textContent = 'Set password';
      resetPasswordButton.setAttribute('aria-label', `Set password for ${user.username}`);
      resetPasswordButton.addEventListener('click', () => openPasswordResetDialog(user));
      actionCell.append(resetPasswordButton);
    }
    row.append(actionCell);
    rows.append(row);
  });
}

function openPermissionsDialog(user) {
  permissionUser = user;
  document.getElementById('permissionsTitle').textContent = `Permissions · ${user.username}`;
  document.getElementById('permissionsRoleNote').textContent =
    `These permissions apply only to ${user.username}. The ${roleNames[user.role]} role is a staff label and does not grant access.`;
  const options = document.getElementById('permissionOptions');
  options.replaceChildren();
  Object.entries(permissionLabels).forEach(([permission, label]) => {
    if (superAdminAssignedPermissions.has(permission) && !currentUser.isSuperAdmin) return;
    const wrapper = document.createElement('label');
    wrapper.className = 'permission-option';
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.name = 'permission';
    checkbox.value = permission;
    checkbox.checked = user.permissions.includes(permission);
    checkbox.addEventListener('change', () => {
      if (checkbox.checked) {
        (permissionDependencies[permission] || []).forEach((dependency) => {
          const required = options.querySelector(`input[value="${dependency}"]`);
          if (required) required.checked = true;
        });
      } else {
        Object.entries(permissionDependencies).forEach(([dependent, dependencies]) => {
          if (dependencies.includes(permission)) {
            const dependentCheckbox = options.querySelector(`input[value="${dependent}"]`);
            if (dependentCheckbox) dependentCheckbox.checked = false;
          }
        });
      }
    });
    wrapper.append(checkbox, document.createTextNode(label));
    options.append(wrapper);
  });
  document.getElementById('permissionsDialog').showModal();
}

function openPasswordResetDialog(user) {
  passwordResetUser = user;
  document.getElementById('passwordResetTitle').textContent = `Set password · ${user.username}`;
  document.getElementById('passwordResetForm').reset();
  document.getElementById('passwordResetError').hidden = true;
  document.getElementById('passwordResetDialog').showModal();
}

function formatInvoiceMoney(currency, amount) {
  const formatted = Number(amount || 0).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  });
  return `${currency} ${formatted}`;
}

function renderInvoices() {
  const rows = document.getElementById('invoiceRows');
  rows.replaceChildren();
  invoices.forEach((invoice) => {
    const row = document.createElement('tr');
    addCell(row, invoice.invoiceNumber, 'tracking-cell');
    addCell(row, formatDate(invoice.invoiceDate));
    addCell(row, invoice.trackingNumber);
    addCell(row, invoice.companyName);
    addCell(row, formatInvoiceMoney(invoice.currency, invoice.totalAmount));
    addCell(row, formatInvoiceMoney(invoice.currency, invoice.receivedPayment));
    addCell(row, formatInvoiceMoney(invoice.currency, invoice.outstandingBalance));

    const statusCell = document.createElement('td');
    const badge = document.createElement('span');
    badge.className = `status-badge invoice-status-${invoice.status}`;
    badge.textContent = invoice.status;
    statusCell.append(badge);
    row.append(statusCell);

    const actionCell = document.createElement('td');
    actionCell.className = 'invoice-actions';
    if (['issued', 'paid'].includes(invoice.status)) {
      const download = document.createElement('a');
      download.className = 'download-link';
      download.href = `/api/invoices/${encodeURIComponent(invoice.shipmentId)}/pdf`;
      download.textContent = 'PDF';
      download.setAttribute('aria-label', `Download latest approved bill for ${invoice.invoiceNumber}`);
      actionCell.append(download);
    } else if (invoice.status !== 'superseded') {
      actionCell.textContent = 'Approval pending';
    } else {
      actionCell.textContent = 'Archived';
    }

    if (currentUser.permissions.includes('invoices:edit') && invoice.status !== 'superseded') {
      const edit = document.createElement('button');
      edit.type = 'button';
      edit.className = 'secondary-btn invoice-edit-btn';
      edit.textContent = 'Revise';
      edit.addEventListener('click', () => editInvoice(invoice));
      actionCell.append(edit);
    }
    if (
      currentUser.permissions.includes('invoices:archive')
      && invoice.status !== 'superseded'
    ) {
      actionCell.append(actionButton(
        'Archive',
        'secondary-btn user-save-btn',
        () => archiveRecord('invoices', invoice.shipmentId, `invoice ${invoice.invoiceNumber}`),
        `Archive invoice ${invoice.invoiceNumber}`
      ));
    }
    row.append(actionCell);
    rows.append(row);
  });
  document.getElementById('invoiceEmpty').hidden = invoices.length > 0;
}

function renderContainers() {
  const rows = document.getElementById('containerRows');
  rows.replaceChildren();
  containers.forEach((container) => {
    const row = document.createElement('tr');
    addCell(row, container.container_number, 'tracking-cell');
    addCell(row, formatDate(container.loading_date));
    addCell(row, formatDate(container.expected_arrival_date));
    addCell(row, container.shipment_count);
    addCell(row, container.loaded_cartons);
    const actions = document.createElement('td');
    actions.className = 'invoice-actions';
    const portions = shipments.flatMap((shipment) => (
      (shipment.loadingSegments || [])
        .filter((portion) => portion.containerNumber === container.container_number && !portion.archivedAt)
        .map((portion) => ({ shipment, portion }))
    ));
    if (currentUser.permissions.includes('containers:edit')) {
      portions.forEach(({ shipment, portion }) => {
        actions.append(actionButton(
          `Edit ${shipment.trackingNumber}`,
          'secondary-btn user-save-btn',
          () => openContainerPortionDialog(portion),
          `Edit ${container.container_number} portion for ${shipment.trackingNumber}`
        ));
      });
    }
    if (currentUser.permissions.includes('containers:archive')) {
      actions.append(actionButton(
        'Archive container',
        'secondary-btn user-save-btn',
        () => archiveRecord('containers', container.container_number, `container ${container.container_number}`),
        `Archive container ${container.container_number}`
      ));
    }
    if (!actions.childElementCount) actions.textContent = '—';
    row.append(actions);
    rows.append(row);
  });
}

function renderAuditHistory() {
  const rows = document.getElementById('auditRows');
  rows.replaceChildren();
  auditEvents.forEach((event) => {
    const row = document.createElement('tr');
    addCell(row, formatDateTime(event.changedAt));
    addCell(row, event.actorUsername);
    addCell(row, `${event.entityType} · ${event.action}`);
    addCell(row, event.entityId, 'tracking-cell');
    const changesCell = document.createElement('td');
    const details = document.createElement('details');
    const summary = document.createElement('summary');
    summary.textContent = 'View snapshots';
    const beforeLabel = document.createElement('strong');
    beforeLabel.textContent = 'Before';
    const before = document.createElement('pre');
    before.textContent = event.before === null ? 'No previous record' : JSON.stringify(event.before, null, 2);
    const afterLabel = document.createElement('strong');
    afterLabel.textContent = 'After';
    const after = document.createElement('pre');
    after.textContent = event.after === null ? 'Record removed' : JSON.stringify(event.after, null, 2);
    details.append(summary, beforeLabel, before, afterLabel, after);
    changesCell.append(details);
    row.append(changesCell);
    const actions = document.createElement('td');
    if (event.canRestore) {
      actions.append(actionButton(
        'Restore',
        'secondary-btn user-save-btn',
        async () => {
          try {
            await api.post(`/api/audit/${event.id}/restore`);
            await refreshWorkspace();
            showToast(`${event.entityType} ${event.entityId} restored.`);
          } catch (error) {
            showToast(error.message, true);
          }
        },
        `Restore ${event.entityType} ${event.entityId}`
      ));
    } else {
      actions.textContent = '—';
    }
    row.append(actions);
    rows.append(row);
  });
  document.getElementById('auditEmpty').hidden = auditEvents.length > 0;
}

function openShipmentEditDialog(shipment) {
  shipmentEditTarget = shipment;
  const form = document.getElementById('shipmentEditForm');
  Object.entries({
    trackingNumber: shipment.trackingNumber,
    shippingMark: shipment.shippingMark,
    supplierName: shipment.supplierName,
    warehouseReceivingDate: shipment.warehouseReceivingDate,
    goodsDescription: shipment.goodsDescription,
    cartons: shipment.cartons,
    weightKg: shipment.weightKg,
    cbm: shipment.cbm
  }).forEach(([field, value]) => { form.elements[field].value = value ?? ''; });
  document.getElementById('shipmentEditDialog').showModal();
}

function openContainerPortionDialog(portion) {
  loadingEditTarget = portion;
  const form = document.getElementById('containerPortionEditForm');
  Object.entries({
    containerNumber: portion.containerNumber,
    loadingDate: portion.loadingDate,
    loadedCartons: portion.loadedCartons,
    departurePort: portion.departurePort,
    destinationPort: portion.destinationPort,
    vessel: portion.vessel,
    voyage: portion.voyage,
    blReference: portion.blReference,
    expectedDepartureDate: portion.expectedDepartureDate,
    expectedArrivalDate: portion.expectedArrivalDate,
    expectedDeliveryDate: portion.expectedDeliveryDate
  }).forEach(([field, value]) => { form.elements[field].value = value ?? ''; });
  document.getElementById('containerPortionEditDialog').showModal();
}

function editInvoice(invoice) {
  const form = document.getElementById('invoiceForm');
  populateSelect('invoiceShipment', shipments, (shipment) => `${shipment.trackingNumber} · ${shipment.customerCode}`);
  form.elements.shipmentId.value = invoice.shipmentId;
  form.elements.invoiceNumber.value = invoice.invoiceNumber;
  form.elements.invoiceDate.value = invoice.invoiceDate;
  form.elements.currency.value = invoice.currency;
  form.elements.totalAmount.value = invoice.totalAmount;
  form.elements.receivedPayment.value = invoice.receivedPayment;
  form.elements.status.value = invoice.status;
  form.dataset.editingShipment = invoice.shipmentId;
  document.getElementById('invoiceFormTitle').textContent = `Revise ${invoice.invoiceNumber}`;
  document.getElementById('saveInvoiceBtn').textContent = 'Save revised bill';
  document.getElementById('cancelInvoiceEdit').hidden = false;
  form.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function resetInvoiceForm() {
  const form = document.getElementById('invoiceForm');
  delete form.dataset.editingShipment;
  form.reset();
  form.elements.receivedPayment.value = '0';
  form.elements.status.value = 'draft';
  document.getElementById('invoiceFormTitle').textContent = 'Create invoice';
  document.getElementById('saveInvoiceBtn').textContent = 'Save invoice';
  document.getElementById('cancelInvoiceEdit').hidden = true;
  setDateDefaults();
}

async function refreshWorkspace() {
  try {
    const canReadCustomers = currentUser.permissions.includes('customers:read');
    const canReadShipments = currentUser.permissions.includes('shipments:read');
    const canReadInvoices = currentUser.permissions.includes('invoices:read');
    const canViewAudit = currentUser.permissions.includes('audit:read');
    const [dashboard, customerData, shipmentData, invoiceData, containerData, auditData] = await Promise.all([
      canReadShipments
        ? api.get('/api/dashboard')
        : Promise.resolve({ totals: { customers: 0, shipments: 0, activeShipments: 0, pendingLoading: 0 }, statusCounts: {} }),
      canReadCustomers ? api.get('/api/customers') : Promise.resolve([]),
      canReadShipments ? api.get('/api/shipments') : Promise.resolve([]),
      canReadInvoices ? api.get('/api/invoices') : Promise.resolve([]),
      canReadShipments ? api.get('/api/containers') : Promise.resolve([]),
      canViewAudit ? api.get('/api/audit') : Promise.resolve([])
    ]);
    customers = customerData;
    shipments = shipmentData;
    invoices = invoiceData;
    containers = containerData;
    auditEvents = auditData;
    document.getElementById('customerCount').textContent = dashboard.totals.customers;
    document.getElementById('shipmentCount').textContent = dashboard.totals.shipments;
    document.getElementById('activeCount').textContent = dashboard.totals.activeShipments;
    document.getElementById('pendingCount').textContent = dashboard.totals.pendingLoading;
    renderStatusSummary(dashboard.statusCounts);
    renderShipments();
    renderCustomers();
    populateSelect(
      'documentShipment',
      shipments,
      (shipment) => `${shipment.trackingNumber} · ${shipment.customerCode}`,
    );
    populateSelect(
      'statusContainer',
      containerData,
      (container) => `${container.container_number} · ${container.shipment_count} lots · ${container.loaded_cartons} cartons`,
      (container) => container.container_number,
    );
    populateSelect('loadingShipment', shipments, (shipment) => `${shipment.trackingNumber} · ${shipment.customerCode} · ${shipment.status}`);
    populateSelect('statusShipment', shipments, (shipment) => `${shipment.trackingNumber} · ${shipment.customerCode}`);
    const editingShipment = document.getElementById('invoiceForm').dataset.editingShipment;
    const availableShipments = shipments.filter((shipment) => (
      !invoices.some((invoice) => invoice.shipmentId === shipment.id) || shipment.id === editingShipment
    ));
    populateSelect('invoiceShipment', availableShipments, (shipment) => `${shipment.trackingNumber} · ${shipment.customerCode}`);
    renderInvoices();
    renderContainers();
    if (canViewAudit) renderAuditHistory();
    if (currentUser.permissions.includes('users:manage')) {
      renderUsers(await api.get('/api/users'));
    }
    if (currentUser.permissions.includes('database:manage')) await refreshDatabaseCatalog();
  } catch (error) {
    showToast(error.message, true);
  }
}

async function refreshDatabaseCatalog() {
  try {
    const catalog = await api.get('/api/admin/database');
    document.getElementById('databasePath').textContent = catalog.databasePath;
    const target = document.getElementById('databaseTables');
    target.replaceChildren();
    catalog.tables.forEach((table) => {
      const details = document.createElement('details');
      const summary = document.createElement('summary');
      summary.textContent = `${table.name} (${table.columns.length} columns)`;
      const columnList = document.createElement('ul');
      table.columns.forEach((column) => {
        const item = document.createElement('li');
        item.textContent = `${column.name} · ${column.type || 'any'}${column.primaryKey ? ' · primary key' : ''}`;
        columnList.append(item);
      });
      details.append(summary, columnList);
      target.append(details);
    });
  } catch (error) {
    document.getElementById('databasePath').textContent = error.message;
  }
}

function renderDatabaseQuery(result) {
  const head = document.getElementById('databaseResultHead');
  const rows = document.getElementById('databaseResultRows');
  head.replaceChildren();
  rows.replaceChildren();
  const headerRow = document.createElement('tr');
  result.columns.forEach((column) => {
    const cell = document.createElement('th');
    cell.textContent = column;
    headerRow.append(cell);
  });
  head.append(headerRow);
  result.rows.forEach((values) => {
    const row = document.createElement('tr');
    values.forEach((value) => {
      const cell = document.createElement('td');
      cell.textContent = value === null ? 'NULL' : String(value);
      row.append(cell);
    });
    rows.append(row);
  });
  document.getElementById('databaseQueryMessage').textContent = (
    `${result.returnedRows} row(s)${result.truncated ? ' shown (results capped at 500 rows)' : ''}.`
  );
}

function setDateDefaults() {
  const today = new Date().toISOString().slice(0, 10);
  document.querySelectorAll('input[type="date"][required]').forEach((input) => {
    if (!input.value) input.value = today;
  });
}

async function submitForm(event, url, payload, successMessage, afterSuccess = refreshWorkspace) {
  event.preventDefault();
  try {
    await api.post(url, payload);
    event.target.reset();
    setDateDefaults();
    await afterSuccess();
    showToast(successMessage);
  } catch (error) {
    showToast(error.message, true);
  }
}

document.addEventListener('DOMContentLoaded', async () => {
  document.getElementById('loginForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    try {
      const result = await api.post('/api/login', Object.fromEntries(form.entries()));
      event.target.reset();
      showApp(result.user);
    } catch (error) {
      showFormError('authError', error.message);
    }
  });

  document.getElementById('setupForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    const password = form.get('password');
    if (password !== form.get('confirmPassword')) {
      showFormError('authError', 'The passwords do not match.');
      return;
    }
    try {
      const result = await api.post('/api/setup', { username: form.get('username'), password });
      event.target.reset();
      showApp(result.user);
    } catch (error) {
      showFormError('authError', error.message);
    }
  });

  document.getElementById('logoutBtn').addEventListener('click', async () => {
    await api.post('/api/logout');
    showAuthentication(false);
  });
  document.getElementById('refreshBtn').addEventListener('click', refreshWorkspace);
  document.getElementById('shipmentSearch').addEventListener('input', renderShipments);
  document.getElementById('statusFilter').addEventListener('change', renderShipments);

  document.getElementById('customerForm').addEventListener('submit', (event) => {
    const form = new FormData(event.target);
    submitForm(event, '/api/customers', Object.fromEntries(form.entries()), 'Customer registered.');
  });
  document.getElementById('customerEditSelect').addEventListener('change', (event) => {
    const customer = customers.find((item) => item.customerCode === event.target.value);
    if (!customer) return;
    const form = document.getElementById('customerEditForm');
    form.elements.companyName.value = customer.companyName;
    form.elements.deliveryCity.value = customer.deliveryCity;
    form.elements.deliveryAddress.value = customer.deliveryAddress;
  });
  document.getElementById('customerEditForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form).entries());
    const customerCode = values.customerCode;
    delete values.customerCode;
    try {
      await api.patch(`/api/customers/${encodeURIComponent(customerCode)}`, values);
      await refreshWorkspace();
      showToast('Customer details updated.');
    } catch (error) {
      showToast(error.message, true);
    }
  });
  document.getElementById('receivingForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const formData = new FormData(form);
    const payload = Object.fromEntries(
      [...formData.entries()].filter(([, value]) => !(value instanceof File)),
    );
    payload.cartons = Number(payload.cartons || 0);
    payload.weightKg = Number(payload.weightKg || 0);
    payload.cbm = Number(payload.cbm || 0);
    let shipment;
    try {
      shipment = await api.post('/api/shipments', payload);
    } catch (error) {
      showToast(error.message, true);
      return;
    }
    const documents = new FormData();
    [...form.elements.photos.files].forEach((photo) => documents.append('photos', photo));
    if (form.elements.packingList.files[0]) {
      documents.append('packingList', form.elements.packingList.files[0]);
    }
    const hasDocuments = [...documents.keys()].length > 0;
    try {
      if (hasDocuments) {
        await request(`/api/shipments/${encodeURIComponent(shipment.id)}/documents`, {
          method: 'POST',
          body: documents
        });
      }
      form.reset();
      setDateDefaults();
      await refreshWorkspace();
      showToast(hasDocuments ? 'Cargo and documents received.' : 'Cargo received.');
    } catch (error) {
      form.reset();
      setDateDefaults();
      await refreshWorkspace();
      showToast(`Cargo ${shipment.trackingNumber} was saved, but document upload failed. Retry under Cargo documents: ${error.message}`, true);
    }
  });
  document.getElementById('documentForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const shipmentId = form.elements.shipmentId.value;
    const documents = new FormData();
    [...form.elements.photos.files].forEach((photo) => documents.append('photos', photo));
    if (form.elements.packingList.files[0]) {
      documents.append('packingList', form.elements.packingList.files[0]);
    }
    try {
      await request(`/api/shipments/${encodeURIComponent(shipmentId)}/documents`, {
        method: 'POST',
        body: documents
      });
      form.reset();
      await refreshWorkspace();
      showToast('Cargo documents uploaded.');
    } catch (error) {
      showToast(error.message, true);
    }
  });
  document.getElementById('loadingForm').addEventListener('submit', (event) => {
    const payload = Object.fromEntries(new FormData(event.target).entries());
    const shipmentId = payload.shipmentId;
    delete payload.shipmentId;
    payload.loadedCartons = Number(payload.loadedCartons || 0);
    submitForm(event, `/api/shipments/${encodeURIComponent(shipmentId)}/loading`, payload, 'Loading details saved.');
  });
  document.getElementById('statusForm').addEventListener('submit', (event) => {
    const payload = Object.fromEntries(new FormData(event.target).entries());
    const shipmentId = payload.shipmentId;
    delete payload.shipmentId;
    submitForm(event, `/api/shipments/${encodeURIComponent(shipmentId)}/status`, payload, 'Shipment status updated.');
  });
  document.getElementById('containerStatusForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(event.currentTarget).entries());
    const containerNumber = values.containerNumber;
    delete values.containerNumber;
    try {
      const result = await api.post(
        `/api/containers/${encodeURIComponent(containerNumber)}/status`,
        values,
      );
      await refreshWorkspace();
      showToast(`Milestone updated for ${result.updatedShipments} cargo lot(s).`);
    } catch (error) {
      showToast(error.message, true);
    }
  });
  document.getElementById('userForm').addEventListener('submit', (event) => {
    const payload = Object.fromEntries(new FormData(event.target).entries());
    submitForm(event, '/api/users', payload, 'Account created.');
  });
  document.getElementById('permissionsForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!permissionUser) return;
    const selectedPermissions = [...event.currentTarget.querySelectorAll('input[name="permission"]:checked')]
      .map((input) => input.value);
    const permissions = currentUser.isSuperAdmin
      ? selectedPermissions
      : [
        ...selectedPermissions,
        ...permissionUser.permissions.filter((permission) => superAdminAssignedPermissions.has(permission))
      ];
    try {
      await api.patch(`/api/users/${encodeURIComponent(permissionUser.id)}`, { permissions });
      document.getElementById('permissionsDialog').close();
      await refreshWorkspace();
      showToast(`Permissions updated for ${permissionUser.username}.`);
      permissionUser = null;
    } catch (error) {
      showToast(error.message, true);
    }
  });
  document.getElementById('shipmentEditForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!shipmentEditTarget) return;
    const form = event.currentTarget;
    const payload = Object.fromEntries(new FormData(form).entries());
    payload.cartons = Number(payload.cartons);
    payload.weightKg = Number(payload.weightKg);
    payload.cbm = Number(payload.cbm);
    try {
      await api.patch(`/api/shipments/${encodeURIComponent(shipmentEditTarget.id)}`, payload);
      document.getElementById('shipmentEditDialog').close();
      shipmentEditTarget = null;
      await refreshWorkspace();
      showToast('Shipment details updated.');
    } catch (error) {
      showToast(error.message, true);
    }
  });
  document.getElementById('closeShipmentEditBtn').addEventListener('click', () => {
    document.getElementById('shipmentEditDialog').close();
    shipmentEditTarget = null;
  });
  document.getElementById('containerPortionEditForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!loadingEditTarget) return;
    const form = event.currentTarget;
    const payload = Object.fromEntries(new FormData(form).entries());
    payload.loadedCartons = Number(payload.loadedCartons);
    try {
      await api.patch(
        `/api/container-portions/${encodeURIComponent(loadingEditTarget.id)}`,
        payload
      );
      document.getElementById('containerPortionEditDialog').close();
      loadingEditTarget = null;
      await refreshWorkspace();
      showToast('Container loading portion updated.');
    } catch (error) {
      showToast(error.message, true);
    }
  });
  document.getElementById('closeContainerPortionEditBtn').addEventListener('click', () => {
    document.getElementById('containerPortionEditDialog').close();
    loadingEditTarget = null;
  });
  document.getElementById('refreshAuditBtn').addEventListener('click', refreshWorkspace);
  document.getElementById('closePermissionsBtn').addEventListener('click', () => {
    document.getElementById('permissionsDialog').close();
    permissionUser = null;
  });
  document.getElementById('passwordResetForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!passwordResetUser) return;
    const form = event.currentTarget;
    const password = form.elements.password.value;
    const confirmation = form.elements.confirmPassword.value;
    const error = document.getElementById('passwordResetError');
    if (password !== confirmation) {
      error.textContent = 'The passwords do not match.';
      error.hidden = false;
      return;
    }
    try {
      await api.post(
        `/api/users/${encodeURIComponent(passwordResetUser.id)}/password`,
        { password },
      );
      document.getElementById('passwordResetDialog').close();
      showToast(`Password updated for ${passwordResetUser.username}.`);
      passwordResetUser = null;
    } catch (requestError) {
      error.textContent = requestError.message;
      error.hidden = false;
    }
  });
  document.getElementById('closePasswordResetBtn').addEventListener('click', () => {
    document.getElementById('passwordResetDialog').close();
    passwordResetUser = null;
  });
  document.getElementById('databaseQueryForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const message = document.getElementById('databaseQueryMessage');
    message.textContent = 'Running query…';
    try {
      const result = await api.post('/api/admin/database/query', {
        sql: document.getElementById('databaseSql').value
      });
      renderDatabaseQuery(result);
    } catch (error) {
      message.textContent = error.message;
      document.getElementById('databaseResultHead').replaceChildren();
      document.getElementById('databaseResultRows').replaceChildren();
    }
  });
  document.getElementById('invoiceForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.target;
    const payload = Object.fromEntries(new FormData(form).entries());
    const editingShipment = form.dataset.editingShipment;
    const button = document.getElementById('saveInvoiceBtn');
    button.disabled = true;
    try {
      await api.post('/api/invoices', payload);
      resetInvoiceForm();
      await refreshWorkspace();
      showToast(editingShipment ? 'Previous bill superseded; revised bill saved.' : 'Bill saved.');
    } catch (error) {
      showToast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
  document.getElementById('cancelInvoiceEdit').addEventListener('click', resetInvoiceForm);

  const passwordDialog = document.getElementById('passwordDialog');
  document.getElementById('changePasswordBtn').addEventListener('click', () => passwordDialog.showModal());
  document.getElementById('closePasswordBtn').addEventListener('click', () => passwordDialog.close());
  document.getElementById('passwordForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const payload = Object.fromEntries(new FormData(event.target).entries());
    try {
      await api.post('/api/password', payload);
      passwordDialog.close();
      event.target.reset();
      showToast('Password updated.');
    } catch (error) {
      showFormError('passwordError', error.message);
    }
  });

  document.querySelectorAll('[data-page-link]').forEach((link) => {
    link.addEventListener('click', (event) => {
      event.preventDefault();
      navigateToPage(link.dataset.pageLink);
    });
  });
  window.addEventListener('popstate', () => {
    activatePage(window.location.hash.slice(1) || 'overview');
  });

  setDateDefaults();
  try {
    const state = await api.get('/api/session');
    if (state.authenticated) showApp(state.user);
    else showAuthentication(state.setupRequired);
  } catch (error) {
    showAuthentication(false);
    showFormError('authError', error.message);
  }
});
