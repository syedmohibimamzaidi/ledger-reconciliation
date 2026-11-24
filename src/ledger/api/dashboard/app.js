const $ = (selector, root = document) => root.querySelector(selector);
const pages = {
  overview: "Overview", receipts: "Receipts", transactions: "Transactions",
  review: "Review", jobs: "Jobs", audit: "Audit history",
};
const statuses = {
  matched: "Matched", needs_review: "Needs review", unmatched: "Unmatched",
  queued: "Queued", retrying: "Retrying", running: "Running",
  succeeded: "Succeeded", failed: "Failed",
};
const reasons = {
  clear_unique_exact: "Unique exact match",
  human_confirmed: "Confirmed in review",
  amount_difference: "Amount differs",
  multiple_transactions_for_receipt: "Multiple possible transactions",
  multiple_receipts_for_transaction: "Transaction fits another receipt",
  transaction_already_matched: "Transaction already matched",
  human_review_required: "Human review required",
  all_candidates_rejected: "All candidates rejected",
  unsupported_receipt_amount: "Receipt amount is ineligible",
  no_plausible_candidates: "No suitable candidate",
};
const limit = 25;
let token = "";
let accounts = [];
let renderVersion = 0;
let sessionVersion = 0;
let activeLoad;
let poll;
const pending = new Set();

const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[char]);
const stamp = value => value ? new Date(value).toLocaleString() : "—";
const money = record => esc([record.currency, record.amount].filter(Boolean).join(" "));
const human = key => String(key ?? "").replaceAll("_", " ").replaceAll(".", " ");
const reasonText = keys => esc((keys ?? []).map(key => reasons[key] ?? human(key)).join(" · "));
const badge = state => `<span class="badge ${esc(state)}">${esc(statuses[state] ?? human(state))}</span>`;
const jobState = job => job.status === "queued" && job.attempt_count > 0 &&
  Date.parse(job.next_attempt_at) > Date.now() ? "retrying" : job.status;
const empty = message => `<div class="panel empty">${esc(message)}</div>`;

function route() {
  const params = new URLSearchParams(location.hash.slice(1));
  const requested = params.get("page");
  const page = Object.hasOwn(pages, requested) ? requested : "overview";
  const account = accounts.find(item => item.id === params.get("account")) ?? accounts[0];
  const offset = Number(params.get("offset"));
  return {
    page, account, offset: Number.isSafeInteger(offset) && offset > 0 ? Math.min(offset, 100000) : 0,
    filter: params.get("filter") ?? "", id: params.get("id") ?? "",
    kind: params.get("kind") ?? "", caseId: params.get("case") ?? "",
  };
}

function link(page, {account = route().account?.id, offset = 0, filter = "", id = "", kind = "", caseId = ""} = {}) {
  const params = new URLSearchParams({page});
  if (account) params.set("account", account);
  if (offset) params.set("offset", offset);
  if (filter) params.set("filter", filter);
  if (id) params.set("id", id);
  if (kind) params.set("kind", kind);
  if (caseId) params.set("case", caseId);
  return "#" + params.toString();
}

function pathFor(account) {
  return "/accounts/" + encodeURIComponent(account.id);
}

function table(headers, rows, label) {
  return `<div class="table-wrap" role="region" aria-label="${esc(label)}" tabindex="0">
    <table><thead><tr>${headers.map(([name, cls = ""]) =>
      `<th scope="col" class="${cls}">${esc(name)}</th>`).join("")}</tr></thead>
    <tbody>${rows.map(row => `<tr>${row.map((cell, index) =>
      `<td class="${headers[index][1] ?? ""}">${cell}</td>`).join("")}</tr>`).join("")}</tbody></table>
  </div>`;
}

function pager(state, items) {
  if (!state.offset && items.length < limit) return "";
  const href = offset => link(state.page, {...state, account: state.account?.id, offset, id: "", kind: "", caseId: ""});
  return `<div class="row" aria-label="Pagination">
    ${state.offset ? `<a class="button" href="${href(Math.max(0, state.offset - limit))}">Previous</a>` : ""}
    <span class="muted">${items.length ? `${state.offset + 1}–${state.offset + items.length}` : "No more records"}</span>
    ${items.length === limit && state.offset + limit <= 100000 ?
      `<a class="button" href="${href(state.offset + limit)}">Next</a>` : ""}
  </div>`;
}

function notice(message, error = false) {
  $("#notice").textContent = message;
  $("#notice").classList.toggle("error", error);
}

function disconnect(message = "") {
  sessionVersion++;
  token = "";
  accounts = [];
  jobKeys.clear();
  reviewKeys.clear();
  clearTimeout(poll);
  activeLoad?.abort();
  closeDialog();
  $("#workspace").hidden = true;
  $("#access").hidden = false;
  $("#view").replaceChildren();
  notice("");
  $("#connect-error").textContent = message;
  $("#connect").reset();
  $("#connect input").focus();
}

async function api(path, {body, signal} = {}) {
  const session = sessionVersion;
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST", signal, cache: "no-store",
    headers: {Authorization: "Bearer " + token, ...(body === undefined ? {} : {"Content-Type": "application/json"})},
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (session !== sessionVersion) throw new DOMException("Session ended", "AbortError");
  const data = await response.json().catch(error => {
    if (error.name === "AbortError") throw error;
    return null;
  });
  if (!response.ok) {
    const message = data?.error?.message ?? `Request failed (HTTP ${response.status}).`;
    if (response.status === 401) disconnect("The demo token was rejected. Enter a valid token to reconnect.");
    throw Object.assign(new Error(message), {status: response.status, ...data?.error});
  }
  if (data === null) throw new Error("The server did not return Ledger data.");
  return data;
}

function describeError(error) {
  if (error instanceof TypeError) return "Could not reach Ledger. Check that the server is running.";
  if (Array.isArray(error.details) && error.details.length) {
    // The CSV error's top-level message already summarizes the same rows.
    return error.details.map(detail => detail.row != null ? `Row ${detail.row}: ${detail.message}` :
      `${(detail.loc ?? []).slice(1).join(".") || "Input"}: ${detail.message}`).join("\n");
  }
  return error.message;
}

function closeDialog() {
  if ($("#dialog").open) $("#dialog").close();
  $("#dialog-content").replaceChildren();
}

function dialog(title, content) {
  closeDialog();
  $("#dialog-title").textContent = title;
  $("#dialog-content").innerHTML = content;
  $("#dialog").showModal();
}

function bindForm(work) {
  const form = $("#dialog form");
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (form.dataset.busy) return;
    const data = new FormData(form, event.submitter);
    const controls = [...form.elements].filter(control => !control.disabled);
    form.dataset.busy = "true";
    controls.forEach(control => { control.disabled = true; });
    const errorBox = $("[role=alert]", form);
    errorBox.textContent = "";
    try {
      await work(data);
    } catch (error) {
      if (error.name !== "AbortError" && form.isConnected) errorBox.textContent = describeError(error);
    } finally {
      delete form.dataset.busy;
      controls.forEach(control => { control.disabled = false; });
    }
  });
}

const formError = '<p class="error form-error" role="alert"></p>';
const fields = pairs => `<dl>${pairs.map(([label, value]) => `<dt>${esc(label)}</dt><dd>${value}</dd>`).join("")}</dl>`;

async function fetchAccounts() {
  const result = [];
  for (let offset = 0; offset <= 100000; offset += 100) {
    const {items} = await api(`/accounts?limit=100&offset=${offset}`);
    result.push(...items);
    if (items.length < 100) break;
  }
  return result;
}

function accountDialog() {
  dialog("Create account", `<form><label>Account name<input name="name" maxlength="255" required></label>
    <button class="primary" type="submit">Create account</button>${formError}</form>`);
  bindForm(async data => {
    const account = await api("/accounts", {body: {name: data.get("name")}});
    accounts.push(account);
    closeDialog();
    location.hash = link("overview", {account: account.id});
    notice("Account created.");
  });
}

async function runJob(state) {
  const key = state.account.id;
  if (pending.has(key)) return;
  pending.add(key);
  const button = $("#run");
  if (button) button.disabled = true;
  const session = sessionVersion;
  // Reuse a submission key after an ambiguous network or server failure.
  const retryKey = `ledger-job:${key}`;
  const requestKey = jobKeys.get(retryKey) ?? crypto.randomUUID();
  jobKeys.set(retryKey, requestKey);
  try {
    const job = await api(pathFor(state.account) + "/reconciliation-jobs", {body: {request_key: requestKey}});
    jobKeys.delete(retryKey);
    if (session !== sessionVersion || route().account?.id !== key) return;
    location.hash = link("jobs", {account: key, id: job.id});
    notice("Reconciliation job submitted.");
  } catch (error) {
    if (error.status && error.status < 500) jobKeys.delete(retryKey);
    if (error.name !== "AbortError" && session === sessionVersion && route().account?.id === key) {
      notice(describeError(error), true);
    }
  } finally {
    pending.delete(key);
    const current = $("#run");
    if (current && route().account?.id === key) current.disabled = false;
  }
}
const jobKeys = new Map();

function receiptDialog(state) {
  const name = esc(state.account.name);
  dialog("Add receipt", `<form>
    <label>Merchant<input name="merchant" maxlength="255" required></label>
    <div class="row"><label>Receipt date<input name="receipt_date" type="date" required></label>
    <label>Amount<input name="amount" inputmode="decimal" pattern="[0-9]{1,16}(\\.[0-9]{1,2})?" placeholder="12.30" required></label>
    <label>Currency<input name="currency" maxlength="3" pattern="[A-Za-z]{3}" value="CAD" required></label></div>
    <label>Source<input name="source" maxlength="100" value="manual" required></label>
    <label>Source receipt ID<input name="source_receipt_id" maxlength="255" value="${crypto.randomUUID()}" required></label>
    <label class="check"><input type="checkbox" name="assign" checked>Assign to ${name}</label>
    <button class="primary" type="submit">Save receipt</button>${formError}</form>`);
  bindForm(async data => {
    const body = Object.fromEntries(data);
    delete body.assign;
    body.currency = body.currency.toUpperCase();
    const result = await api("/receipts", {body});
    if (data.has("assign")) {
      try {
        await api(pathFor(state.account) + "/receipts/assign", {body: {receipt_ids: [result.receipt_id]}});
      } catch (error) {
        if (error.name === "AbortError" || error.status === 401) throw error;
        throw new Error("Receipt saved. Assignment failed. " + describeError(error));
      }
    }
    closeDialog();
    await refresh();
    notice(result.created ? "Receipt saved." : "Receipt already exists. The existing record was reused.");
  });
}

function importDialog(state) {
  dialog("Import bank CSV", `<form>
    <p>Import into <strong>${esc(state.account.name)}</strong>.</p>
    <label>Bank statement CSV<input type="file" name="csv" accept=".csv,text/csv" required></label>
    <p class="muted">Required columns: source_transaction_id, date, merchant, amount, currency.</p>
    <button class="primary" type="submit">Import transactions</button>${formError}</form>`);
  bindForm(async data => {
    const csv = data.get("csv");
    if (!csv.size) throw new Error("Choose a nonempty CSV file.");
    if (csv.size > 8_000_000) throw new Error("The CSV file is too large.");
    const result = await api(pathFor(state.account) + "/transactions/import", {body: {csv_text: await csv.text()}});
    closeDialog();
    await refresh();
    notice(`Import complete. ${result.inserted} inserted, ${result.skipped} skipped.`);
  });
}

function listToolbar(state, options, label) {
  return `<div class="toolbar"><label>${label}<select id="filter">
    ${options.map(([value, caption]) => `<option value="${value}" ${value === state.filter ? "selected" : ""}>${caption}</option>`).join("")}
    </select></label><button type="button" id="refresh">Refresh</button></div>`;
}

function listQuery(state, extras = {}) {
  return new URLSearchParams({limit, offset: state.offset, ...extras});
}

function receiptLink(id, label, state = route()) {
  return `<a href="${link(state.page, {...state, account: state.account.id, kind: "receipt", caseId: state.page === "review" ? state.caseId || state.id : "", id})}">${esc(label)}</a>`;
}

function bankLink(id, label, state = route()) {
  return `<a href="${link(state.page, {...state, account: state.account.id, kind: "transaction", caseId: state.page === "review" ? state.caseId || state.id : "", id})}">${esc(label)}</a>`;
}

function metrics(items) {
  return `<div class="cards">${items.map(([label, value, href, attention]) => {
    const tag = href ? "a" : "div";
    return `<${tag} class="metric${attention ? " attention" : ""}" ${href ? `href="${href}"` : ""}>
      ${esc(label)}<strong class="num" style="text-align:left">${esc(value)}</strong></${tag}>`;
  }).join("")}</div>`;
}

function overview(state, result) {
  const {receipts, transactions, latest_job: latest, active_jobs: active} = result;
  return `<section class="panel stack"><div class="toolbar"><h2>Receipt outcomes</h2>
      <span class="muted">${esc(receipts.total)} assigned receipts</span></div>
      ${metrics([
        ["Needs review", receipts.needs_review, link("review"), receipts.needs_review > 0],
        ["Matched", receipts.matched, link("receipts", {filter: "matched"})],
        ["Unmatched", receipts.unmatched, link("receipts", {filter: "unmatched"})],
        ["No decision", receipts.without_decision, link("receipts")],
      ])}
      <p class="muted">${result.last_reconciled_at ? `Last reconciliation: ${esc(stamp(result.last_reconciled_at))}` : "No reconciliation recorded yet."}</p>
      ${result.has_new_reconciliation_inputs ? `<p class="difference panel">${esc(receipts.assigned_since_last_run)} receipts assigned and
        ${esc(transactions.imported_since_last_run)} transactions imported ${result.last_reconciled_at ? "since the last run" : "before the first recorded run"}.</p>` : ""}
    </section>
    <section class="panel stack"><div class="toolbar"><h2>Bank transactions</h2>
      <a href="${link("transactions")}">${esc(transactions.total)} transactions</a></div>
      ${metrics([
        ["Debits without a match", transactions.unmatched_debits],
        ["Matched debits", transactions.matched_debits],
        ["Credits (ineligible)", transactions.credits_not_eligible],
        ["Zero amounts (ineligible)", transactions.zero_amount_not_eligible],
      ])}
    </section>
    <section class="panel stack"><div class="toolbar"><h2>Latest background job</h2><a href="${link("jobs")}">Job history</a></div>
      ${latest ? `<div class="row"><a href="${link("jobs", {id: latest.id})}">${esc(stamp(latest.created_at))}</a>
        ${badge(latest.display_status)}</div>` : '<p class="muted">No jobs submitted yet.</p>'}
      <p class="muted">${esc(active.queued)} queued · ${esc(active.retrying)} retrying · ${esc(active.running)} running</p>
    </section>`;
}

async function receiptView(state, signal) {
  const mode = state.filter;
  const isOutcome = ["matched", "needs_review", "unmatched"].includes(mode);
  const extras = mode === "unassigned" ? {unassigned: true} : {account_id: state.account.id};
  const query = listQuery(state, isOutcome ? {status: mode} : extras);
  const {items} = await api((isOutcome ? pathFor(state.account) + "/outcomes" : "/receipts") + "?" + query, {signal});
  const rows = items.map(row => {
    const record = isOutcome ? row.details.receipt : row;
    return [
      receiptLink(isOutcome ? row.receipt_id : row.id, record.merchant, state),
      esc(record.receipt_date ?? record.date), money(record),
      isOutcome ? `${badge(row.status)} <span class="muted">${reasonText(row.details.reasons)}</span>
        ${row.bank_transaction_id ? bankLink(row.bank_transaction_id, "Matched transaction", state) : ""}`
        : esc(record.source_receipt_id),
    ];
  });
  return listToolbar(state, [["", "Assigned receipts"], ["unassigned", "Unassigned receipts"],
    ["matched", "Matched outcomes"], ["needs_review", "Needs review outcomes"], ["unmatched", "Unmatched outcomes"]], "Show") +
    (rows.length ? table([["Merchant"], ["Receipt date"], ["Amount", "num"], [isOutcome ? "Outcome" : "Source receipt ID"]], rows, "Receipts") :
      empty("No receipts for this view.")) + pager(state, items);
}

async function transactionView(state, signal) {
  const {items} = await api(pathFor(state.account) + "/transactions?" + listQuery(state), {signal});
  return (items.length ? table([["Merchant"], ["Bank date"], ["Amount", "num"], ["Source transaction ID"]],
    items.map(row => [bankLink(row.id, row.merchant, state), esc(row.transaction_date), money(row),
      esc(row.source_transaction_id)]), "Bank transactions") : empty("No bank transactions. Import a bank CSV to get started.")) +
    pager(state, items);
}

function compare(receipt, candidate, state) {
  const differs = !/^-?0(?:\.0+)?$/.test(candidate.amount_difference);
  const rows = [
    ["Merchant", esc(receipt.merchant), esc(candidate.merchant)],
    ["Date", esc(receipt.date), esc(candidate.date)],
    ["Amount", money(receipt), differs ?
      `<span class="difference">${money(candidate)} · ${esc(candidate.amount_difference)} difference</span>` : money(candidate)],
    ["Currency", esc(receipt.currency), esc(candidate.currency)],
  ];
  return table([[""], ["Receipt"], ["Bank transaction"]], rows, "Receipt and candidate comparison") +
    `<div class="row">${bankLink(candidate.bank_transaction_id, "Transaction details", state)}
    <span class="muted">${esc(candidate.date_lag_days)} days after receipt</span></div>`;
}

function reviewDialog(state, current, candidate, action) {
  const confirming = action === "confirm";
  const mismatch = !/^-?0(?:\.0+)?$/.test(candidate.amount_difference);
  const label = confirming ? "Confirm match" : "Reject candidate";
  const key = JSON.stringify([state.account.id, current.receipt_id, current.revision, candidate.bank_transaction_id, action]);
  dialog(label, `<form><p><strong>${esc(state.account.name)}</strong> · ${esc(current.receipt.merchant)} · ${money(current.receipt)}</p>
    <p>Candidate: ${esc(candidate.merchant)} · ${money(candidate)} · ${esc(candidate.date)}</p>
    <label>Reason<textarea name="reason" rows="3" maxlength="2000" required></textarea></label>
    ${confirming && mismatch ? '<label class="check"><input type="checkbox" name="ack" required>I acknowledge the amount difference.</label>' : ""}
    <button class="${confirming ? "primary" : ""}" type="submit">${label}</button>${formError}
    <button id="reload-case" type="button" hidden>Reload review case</button></form>`);
  $("#reload-case").onclick = () => { closeDialog(); refresh(); };
  bindForm(async data => {
    const reason = data.get("reason").trim();
    if (!reason) throw new Error("Enter a reason for this decision.");
    const ack = data.has("ack");
    const fingerprint = JSON.stringify([reason, ack]);
    const prior = reviewKeys.get(key);
    const requestId = prior?.fingerprint === fingerprint ? prior.id : crypto.randomUUID();
    reviewKeys.set(key, {id: requestId, fingerprint});
    const body = {
      bank_transaction_id: candidate.bank_transaction_id, request_id: requestId,
      expected_revision: current.revision, reason,
    };
    if (confirming) body.acknowledge_amount_mismatch = ack;
    try {
      await api(pathFor(state.account) + "/review-cases/" + encodeURIComponent(current.receipt_id) + "/" + action, {body});
    } catch (error) {
      if (["stale_revision", "state_conflict"].includes(error.code)) {
        const reload = $("#reload-case");
        if (reload) reload.hidden = false;
        throw new Error("This case changed. Reload it and review the latest candidates before acting.");
      }
      throw error;
    }
    reviewKeys.delete(key);
    closeDialog();
    await refresh();
    notice(confirming ? "Match confirmed." : "Candidate rejected.");
  });
}
const reviewKeys = new Map();

async function reviewView(state, signal) {
  const {items} = await api(pathFor(state.account) + "/review-cases?" + listQuery(state), {signal});
  const current = items.find(item => item.receipt_id === (state.caseId || state.id)) ?? items[0];
  const queue = items.map(item => `<a href="${link("review", {account: state.account.id, offset: state.offset, id: item.receipt_id})}"
    ${current === item ? 'aria-current="true"' : ""}><strong>${esc(item.receipt.merchant)}</strong>
    <span class="muted">${esc(item.receipt.date)} · ${money(item.receipt)}</span></a>`).join("");
  const comparison = current ? `<div class="stack">
    <section class="panel stack"><div class="toolbar"><h2>${esc(current.receipt.merchant)}</h2>${badge("needs_review")}</div>
      <p>${reasonText(current.reasons)}</p>
      <p class="muted">Revision ${esc(current.revision)} · ${receiptLink(current.receipt_id, "Receipt details", {...state, id: current.receipt_id})}</p>
    </section>
    ${current.candidates.map((candidate, index) => `<section class="panel stack">
      <h3>${esc(candidate.merchant)} · ${money(candidate)}</h3>
      ${compare(current.receipt, candidate, {...state, id: current.receipt_id})}
      ${candidate.competing_receipt_ids?.length ? "<p class='muted'>This transaction also fits another receipt.</p>" : ""}
      <div class="row"><button class="primary" type="button" data-action="confirm" data-candidate="${index}">Confirm match</button>
        <button type="button" data-action="reject" data-candidate="${index}">Reject candidate</button></div>
    </section>`).join("")}
    ${current.rejected_candidates.length ? `<section class="panel"><h2>Rejected candidates</h2>
      ${current.rejected_candidates.map(item => `<p>${bankLink(item.bank_transaction_id, "Transaction", {...state, id: current.receipt_id})}
        · ${esc(item.reason)} · ${esc(item.reviewer)}</p>`).join("")}</section>` : ""}
    </div>` : "";
  return {
    html: `<div class="toolbar"><p class="muted">Compare the receipt and candidate before deciding.</p>
      <button type="button" id="refresh">Refresh cases</button></div>
      ${current ? `<div class="review"><section class="queue" aria-label="Review queue">${queue}${pager(state, items)}</section>${comparison}</div>`
        : empty("No review cases on this page.") + pager(state, items)}`,
    bind() {
      document.querySelectorAll("[data-candidate]").forEach(button => {
        button.onclick = () => reviewDialog(state, current, current.candidates[Number(button.dataset.candidate)], button.dataset.action);
      });
    },
  };
}

function jobDetails(job) {
  const state = job.display_status ?? jobState(job);
  return `<section class="panel stack"><div class="toolbar"><h2>Job details</h2>${badge(state)}</div>
    ${fields([
      ["Submitted", esc(stamp(job.created_at))],
      ["Attempts", `${esc(job.attempt_count)} of ${esc(job.max_attempts)}`],
      ["Next attempt", esc(stamp(job.next_attempt_at))],
      ["Next dispatch", esc(stamp(job.dispatch_after))],
      ["Finished", esc(stamp(job.finished_at))],
    ])}
    ${job.failure_details ? `<p class="${state === "failed" ? "error" : "muted"}">${esc(job.failure_details.message)}</p>` : ""}
    ${job.result_summary ? `<h3>Run outcomes</h3><div class="row">
      <a href="${link("receipts", {filter: "matched"})}">${esc(job.result_summary.matched)} matched</a>
      <a href="${link("review")}">${esc(job.result_summary.needs_review)} need review</a>
      <a href="${link("receipts", {filter: "unmatched"})}">${esc(job.result_summary.unmatched)} unmatched</a>
    </div>` : ""}
    <p class="muted">Job ID: ${esc(job.id)}</p>
  </section>`;
}

async function jobView(state, signal) {
  const path = pathFor(state.account) + "/reconciliation-jobs";
  const values = ["queued", "retrying", "running", "succeeded", "failed"];
  const extras = values.includes(state.filter) ? {status: state.filter} : {};
  const {items} = await api(path + "?" + listQuery(state, extras), {signal});
  const chosen = state.id ? await api(path + "/" + encodeURIComponent(state.id), {signal}) : null;
  const rows = items.map(job => [
    `<a href="${link("jobs", {account: state.account.id, offset: state.offset, filter: state.filter, id: job.id})}">${esc(stamp(job.created_at))}</a>`,
    badge(job.display_status), `${esc(job.attempt_count)} / ${esc(job.max_attempts)}`,
    esc(stamp(job.finished_at)), esc(job.failure_details?.message ?? "—"),
  ]);
  return {
    html: listToolbar(state, [["", "All statuses"], ...values.map(value => [value, statuses[value]])], "Status") +
      (rows.length ? table([["Submitted"], ["Status"], ["Attempts", "num"], ["Finished"], ["Message"]], rows, "Job history")
        : empty("No jobs for this view.")) + pager(state, items) +
      (chosen ? `<div id="job-detail">${jobDetails(chosen)}</div>` : ""),
    poll: chosen?.status === "queued" || chosen?.status === "running" ?
      async signal => {
        const job = await api(path + "/" + encodeURIComponent(chosen.id), {signal});
        if (signal.aborted) return false;
        $("#job-detail").innerHTML = jobDetails(job);
        const status = jobState(job);
        for (const row of $("#view").querySelectorAll("tbody tr")) {
          if (row.querySelector("a")?.hash === location.hash) {
            row.cells[1].innerHTML = badge(status);
            row.cells[2].textContent = `${job.attempt_count} / ${job.max_attempts}`;
            row.cells[3].textContent = stamp(job.finished_at);
            row.cells[4].textContent = job.failure_details?.message ?? "—";
          }
        }
        return job.status === "queued" || job.status === "running";
      } : null,
  };
}

async function auditView(state, signal) {
  const {items} = await api(pathFor(state.account) + "/audit-events?" + listQuery(state, {order: "newest"}), {signal});
  const groups = new Map();
  for (const event of items) {
    if (!groups.has(event.created_at)) groups.set(event.created_at, []);
    groups.get(event.created_at).push(event);
  }
  const eventNames = {
    "receipt.created": "Receipt created",
    "receipt.scoped": "Receipt assigned",
    "bank_transaction.created": "Bank transaction imported",
    "receipt.reconciliation_decided": "Reconciliation decision recorded",
    "receipt.reviewed": "Review decision recorded",
  };
  return `<div class="toolbar"><p class="muted">Events with the same timestamp are grouped.</p>
    <button type="button" id="refresh">Refresh history</button></div>` +
    ([...groups].map(([time, events]) => `<section class="panel"><h2>${esc(stamp(time))}</h2>
      ${events.map(event => `<details><summary>${esc(eventNames[event.event_type] ?? human(event.event_type))}
        ${event.payload.reviewer ? ` · ${esc(event.payload.reviewer)}` : ""}</summary>
        <div class="row">
        ${event.receipt_id ? receiptLink(event.receipt_id, "Receipt details", state) : ""}
        ${event.bank_transaction_id || event.payload.bank_transaction_id ?
          bankLink(event.bank_transaction_id ?? event.payload.bank_transaction_id, "Transaction details", state) : ""}
        </div><pre>${esc(JSON.stringify(event.payload, null, 2))}</pre></details>`).join("")}
      </section>`).join("") || empty("No audit events for this account.")) + pager(state, items);
}

async function recordDetail(state, signal) {
  const isReceipt = state.kind === "receipt";
  const path = (isReceipt ? "/receipts/" : pathFor(state.account) + "/transactions/") + encodeURIComponent(state.id);
  const record = await api(path, {signal});
  if (signal.aborted) return;
  if (isReceipt && record.account_id && record.account_id !== state.account.id) {
    throw new Error("This receipt belongs to a different account. Switch accounts to view it.");
  }
  const decision = record.decision;
  const fieldsMarkup = fields([
    ["Merchant", esc(record.merchant)], ["Date", esc(record.receipt_date ?? record.transaction_date)],
    ["Amount", money(record)],
    [isReceipt ? "Source receipt ID" : "Source transaction ID", esc(record.source_receipt_id ?? record.source_transaction_id)],
    ...(isReceipt ? [["Source", esc(record.source)], ["Decision", decision ? badge(decision.status) : "No decision"]] : []),
    ["Recorded", esc(stamp(record.created_at))],
  ]);
  let extra = "";
  if (decision) {
    extra = `<p>${reasonText(decision.details.reasons)}</p>
      ${decision.bank_transaction_id ? bankLink(decision.bank_transaction_id, "Matched transaction", state) : ""}`;
  } else if (isReceipt && !record.account_id) {
    extra = `<form><button class="primary" type="submit">Assign to ${esc(state.account.name)}</button>${formError}</form>`;
  }
  dialog(isReceipt ? "Receipt details" : "Transaction details", `<div class="stack">${fieldsMarkup}${extra}</div>`);
  if (isReceipt && !record.account_id) {
    bindForm(async () => {
      const {assigned} = await api(pathFor(state.account) + "/receipts/assign", {body: {receipt_ids: [record.id]}});
      closeRecord(state);
      notice(assigned ? "Receipt assigned." : "Receipt was already assigned.");
    });
  }
}

function closeRecord(state = route()) {
  closeDialog();
  if (state.kind) {
    const id = state.page === "review" ? state.caseId : "";
    location.hash = link(state.page, {...state, account: state.account?.id, kind: "", caseId: "", id});
  }
}

function shell(state) {
  const account = state.account;
  const select = $("#account");
  if (select.dataset.options !== JSON.stringify(accounts)) {
    select.replaceChildren(...(accounts.length ?
      accounts.map(item => new Option(item.name, item.id)) : [new Option("No accounts", "")]));
    select.dataset.options = JSON.stringify(accounts);
  }
  select.disabled = !account;
  select.value = account?.id ?? "";
  $("#navigation").innerHTML = Object.entries(pages).map(([key, label]) =>
    `<a href="${link(key)}" ${key === state.page ? 'aria-current="page"' : ""}>${label}</a>`).join("");
  const action = {
    overview: ["run", "Run reconciliation"], jobs: ["run", "Run reconciliation"],
    receipts: ["add-receipt", "Add receipt"], transactions: ["import", "Import CSV"],
  }[state.page];
  $("#heading").innerHTML = `<div><p class="context">${esc(account?.name ?? "Workspace")}</p><h1>${pages[state.page]}</h1></div>
    ${account && action ? `<button class="primary" type="button" id="${action[0]}">${action[1]}</button>` : ""}`;
  if ($("#run")) {
    $("#run").disabled = pending.has(account.id);
    $("#run").onclick = () => runJob(state);
  }
  if ($("#add-receipt")) $("#add-receipt").onclick = () => receiptDialog(state);
  if ($("#import")) $("#import").onclick = () => importDialog(state);
}

function schedulePoll(update, controller) {
  poll = setTimeout(async () => {
    if (controller.signal.aborted) return;
    try {
      if (document.hidden || $("#dialog").open || await update(controller.signal)) {
        if (!controller.signal.aborted) schedulePoll(update, controller);
      }
    } catch (error) {
      if (!controller.signal.aborted && error.name !== "AbortError") {
        notice(describeError(error) + " Refresh to check again.", true);
      }
    }
  }, 3000);
}

async function refresh() {
  if (!token) return;
  activeLoad?.abort();
  clearTimeout(poll);
  const controller = new AbortController();
  activeLoad = controller;
  const signal = controller.signal;
  const version = ++renderVersion;
  const state = route();
  shell(state);
  const view = $("#view");
  if (!state.account) {
    view.innerHTML = empty("Create an account to start importing receipts and bank transactions.");
    return;
  }
  view.setAttribute("aria-busy", "true");
  view.innerHTML = '<p class="muted" role="status">Loading…</p>';
  const path = pathFor(state.account);
  const loaders = {
    overview: async () => ({
      html: overview(state, await api(path + "/summary", {signal})),
      poll: async signal => {
        const result = await api(path + "/summary", {signal});
        if (!signal.aborted && !view.contains(document.activeElement)) {
          const markup = overview(state, result);
          if (view.dataset.overview !== markup) {
            view.innerHTML = markup;
            view.dataset.overview = markup;
          }
        }
        return true;
      },
    }),
    receipts: () => receiptView(state, signal),
    transactions: () => transactionView(state, signal),
    review: () => reviewView(state, signal),
    jobs: () => jobView(state, signal),
    audit: () => auditView(state, signal),
  };
  try {
    const result = await loaders[state.page]();
    if (signal.aborted || version !== renderVersion) return;
    view.innerHTML = typeof result === "string" ? result : result.html;
    if (state.page === "overview") view.dataset.overview = result.html;
    result.bind?.();
    if ($("#filter")) $("#filter").onchange = event => {
      location.hash = link(state.page, {account: state.account.id, filter: event.target.value});
    };
    if ($("#refresh")) $("#refresh").onclick = () => { notice(""); refresh(); };
    if (result.poll) schedulePoll(result.poll, controller);
    if (state.kind === "receipt" || state.kind === "transaction") {
      await recordDetail(state, signal);
    }
  } catch (error) {
    if (!signal.aborted && version === renderVersion && error.name !== "AbortError") {
      if (state.kind && view.querySelector("[role=status]") === null) {
        dialog("Record unavailable", `<p class="error" role="alert">${esc(describeError(error))}</p>`);
      } else {
        view.innerHTML = `<section class="panel stack"><p class="error" role="alert">${esc(describeError(error))}</p>
          <button type="button" id="retry-view">Try again</button></section>`;
        $("#retry-view").onclick = refresh;
      }
    }
  } finally {
    if (version === renderVersion) view.removeAttribute("aria-busy");
  }
}

$("#connect").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = $("button", form);
  if (button.disabled) return;
  button.disabled = true;
  $("#connect-error").textContent = "";
  token = new FormData(form).get("token").trim();
  try {
    accounts = await fetchAccounts();
    form.reset();
    $("#access").hidden = true;
    $("#workspace").hidden = false;
    await refresh();
    $("#content").focus();
  } catch (error) {
    token = "";
    if (error.status !== 401) $("#connect-error").textContent = describeError(error);
  } finally {
    button.disabled = false;
  }
});
$("#add-account").onclick = accountDialog;
$("#disconnect").onclick = () => disconnect();
$("#account").onchange = event => {
  notice("");
  location.hash = link(route().page, {account: event.target.value});
};
$("#workspace").addEventListener("click", event => {
  if (event.target.closest('a[href^="#page="]')) notice("");
});
$("#close").onclick = () => closeRecord();
$("#dialog").addEventListener("cancel", event => { event.preventDefault(); closeRecord(); });
$(".skip").onclick = event => { event.preventDefault(); $("#content").focus(); };
window.addEventListener("hashchange", () => {
  closeDialog();
  if (token && !$("#workspace").hidden) {
    refresh();
    if (!route().kind) $("#content").focus();
  }
});
