const state = {
  currentRoute: "dashboard",
  page: 1,
  pageSize: 20,
  pages: 1,
  sort: "published_at",
  direction: "desc",
  timezone: "Asia/Kolkata",
  pollTimer: null,
  recipients: [],
  schedule: null,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function getAdminKey() {
  return sessionStorage.getItem("gp_admin_key") || "";
}

async function api(path, options = {}) {
  const adminKey = getAdminKey();
  const headers = { ...(options.headers || {}) };
  if (adminKey && !headers["X-Admin-Key"]) {
    headers["X-Admin-Key"] = adminKey;
  }
  if (options.body && typeof options.body === "object" && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.body);
  }
  options.headers = headers;

  const response = await fetch(path, options);
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const err = await response.json();
      message = err.detail || message;
    } catch (_) {
      /* no JSON */
    }
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function formatDate(value, options = {}) {
  if (!value) return "Never";
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: state.timezone,
    day: "2-digit",
    month: "short",
    year: "numeric",
    ...options,
  }).format(new Date(value));
}

function formatTime(value) {
  if (!value) return "";
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: state.timezone,
    hour: "2-digit",
    minute: "2-digit",
    hour12: true,
    timeZoneName: "short",
  }).format(new Date(value));
}

let toastTimer = null;
function toast(message, isError = false) {
  const element = $("#toast");
  if (!element) return;
  if (toastTimer) {
    window.clearTimeout(toastTimer);
    toastTimer = null;
  }
  element.innerHTML = "";
  const icon = document.createElement("span");
  icon.className = "toast-icon";
  icon.textContent = isError ? "⚠" : "✓";
  const text = document.createElement("span");
  text.className = "toast-text";
  text.textContent = message;
  element.append(icon, text);
  element.classList.toggle("error", isError);
  element.classList.add("show");
  toastTimer = window.setTimeout(() => {
    element.classList.remove("show");
    toastTimer = null;
  }, 4200);
}

// ==================== ROUTING ====================

const ROUTE_TITLES = {
  dashboard: {
    eyebrow: "MEDIA INTELLIGENCE / LIVE MONITOR",
    title: "Coverage, <em>clearly tracked.</em>",
    intro: "New coverage for Green Portfolio, Divam Sharma and Anuj Jain — discovered, verified and archived.",
  },
  articles: {
    eyebrow: "MEDIA INTELLIGENCE / COMPLETE DATASET",
    title: "All Coverage, <em>curated.</em>",
    intro: "Complete historical records and extracted article paragraphs stored by the application.",
  },
  email: {
    eyebrow: "MEDIA INTELLIGENCE / REPORT DISTRIBUTION",
    title: "Email <em>recipients.</em>",
    intro: "Manage who receives the weekly article intelligence report.",
  },
  automation: {
    eyebrow: "MEDIA INTELLIGENCE / SCHEDULER",
    title: "Automation <em>schedule.</em>",
    intro: "Configure the background scheduling engine for automated coverage discovery.",
  },
  "data-sync": {
    eyebrow: "MEDIA INTELLIGENCE / DATA INTEGRITY",
    title: "Data, <em>safely synchronized.</em>",
    intro: "Review differences between the permanent article database and Google Sheets.",
  },
};

function switchRoute(routeName) {
  const target = ROUTE_TITLES[routeName] ? routeName : "dashboard";
  state.currentRoute = target;

  // Update navigation items
  $$(".sidebar .nav-item").forEach((item) => {
    item.classList.toggle("active", item.dataset.route === target);
  });

  // Switch visible view
  $$(".cms-view").forEach((view) => {
    const isTarget = view.id === `view-${target}`;
    view.classList.toggle("active", isTarget);
    view.hidden = !isTarget;
  });

  // Update topbar copy
  const info = ROUTE_TITLES[target];
  if (info) {
    $("#topbar-eyebrow").textContent = info.eyebrow;
    $("#topbar-title").innerHTML = info.title;
    $("#topbar-intro").textContent = info.intro;
  }

  // Load data for specific route
  if (target === "dashboard") {
    loadOverview();
    loadHistory();
  } else if (target === "articles") {
    loadArticles();
    loadPublishers();
  } else if (target === "email") {
    loadRecipients();
  } else if (target === "automation") {
    loadAutomationSchedule();
  } else if (target === "data-sync") {
    loadDataSync();
  }
}

function handleHashChange() {
  const hash = window.location.hash.replace("#", "").trim();
  switchRoute(hash || "dashboard");
}

window.addEventListener("hashchange", handleHashChange);

$$(".sidebar .nav-item").forEach((link) => {
  link.addEventListener("click", (e) => {
    e.preventDefault();
    const route = link.dataset.route;
    window.location.hash = `#${route}`;
  });
});

// ==================== DASHBOARD OVERVIEW ====================

async function loadOverview() {
  try {
    const data = await api("/api/overview");
    state.timezone = data.timezone;
    state.schedule = data.automation_schedule;

    // Sidebar
    $("#sidebar-timezone").textContent = `${data.timezone} (IST)`;
    $("#scheduler-state").textContent = data.scheduler_enabled ? "Scheduler active" : "Scheduler disabled";
    $(".schedule-state i").style.background = data.scheduler_enabled ? "var(--lime)" : "#9aa9a7";

    // Dashboard Cards
    $("#dash-total-articles").textContent = data.total_articles.toLocaleString("en-IN");
    $("#dash-new-articles").textContent = data.last_fetch_new_article_count.toLocaleString("en-IN");
    $("#dash-recipients").textContent = (data.active_recipients_count ?? 0).toLocaleString("en-IN");

    // Articles page total count badge
    $("#articles-total-count").textContent = data.total_articles.toLocaleString("en-IN");

    // Latest fetch formatting
    if (data.last_successful_fetch) {
      const fetchDate = new Date(data.last_successful_fetch);
      $("#dash-latest-fetch").textContent = formatDate(fetchDate, {
        weekday: "short",
        day: "2-digit",
        month: "short",
      });
      $("#dash-latest-fetch-sub").textContent = formatTime(fetchDate);
    } else {
      $("#dash-latest-fetch").textContent = "Never";
      $("#dash-latest-fetch-sub").textContent = "Waiting for first run";
    }

    // Automation status pill and frequency
    const autoStatus = $("#dash-automation-status");
    if (autoStatus) {
      const isAutoOn = data.scheduler_enabled;
      autoStatus.textContent = isAutoOn ? "ON" : "OFF";
      autoStatus.className = `pill-badge ${isAutoOn ? "pill-on" : "pill-off"}`;
    }

    const sched = data.automation_schedule;
    const isDailyOrCustom = sched && (sched.frequency === "daily" || sched.frequency === "custom");
    if (sched) {
      const freqLabel = sched.frequency === "daily"
        ? "Daily"
        : sched.frequency === "custom"
        ? "Custom Cron"
        : `Every ${dayFullName(sched.day_of_week)}`;
      const timeLabel = formatHourMinute(sched.hour, sched.minute);
      $("#dash-automation-freq").textContent = `${freqLabel} at ${timeLabel}`;
      $("#sidebar-schedule-summary").textContent = isDailyOrCustom
        ? timeLabel
        : `${sched.day_of_week?.toUpperCase() || "MON"} · ${timeLabel}`;
    }

    // Next scheduled run
    if (data.next_scheduled_fetch) {
      const next = new Date(data.next_scheduled_fetch);
      const nextFormatted = data.next_scheduled_fetch_formatted || `${formatDate(next)} · ${formatTime(next)}`;
      $("#dash-next-run").textContent = isDailyOrCustom
        ? formatTime(next)
        : formatDate(next, { weekday: "short", day: "2-digit", month: "short" });
      $("#dash-next-run-sub").textContent = isDailyOrCustom ? (sched.frequency === "daily" ? "Daily run" : "Custom run") : formatTime(next);

      $("#next-run-date").textContent = isDailyOrCustom
        ? (sched.frequency === "daily" ? "Every Day" : "Custom Schedule")
        : formatDate(next, { weekday: "long" });
      $("#next-run-time").textContent = `${formatTime(next)} · ${data.timezone}`;
      $("#schedule-next-run-text").textContent = nextFormatted;

      const dayAbbr = new Intl.DateTimeFormat("en-IN", { timeZone: state.timezone, weekday: "short" }).format(next).toUpperCase();
      const dateDay = new Intl.DateTimeFormat("en-IN", { timeZone: state.timezone, day: "2-digit" }).format(next);
      $("#next-run-cal-day").textContent = dayAbbr;
      $("#next-run-cal-date").textContent = dateDay;
    } else {
      $("#dash-next-run").textContent = "Paused";
      $("#dash-next-run-sub").textContent = "Automation disabled";
      $("#next-run-date").textContent = "Paused";
      $("#next-run-time").textContent = "Enable in Automation Settings";
      $("#schedule-next-run-text").textContent = "Automation paused (OFF)";
    }

    // System Status
    const sheetReady = data.integrations.google_sheets.configured;
    const searchReady = data.search_providers.length > 0;
    const statusLabel = data.last_fetch_status === "running"
      ? "Fetching"
      : searchReady && sheetReady ? "Operational" : "Setup needed";
    $("#system-status").textContent = statusLabel;
    $(".system-pill > span").style.background = statusLabel === "Setup needed" ? "#d7a115" : "";
    const pendingSync = data.data_sync?.pending_reviews || 0;
    const syncBadge = $("#sync-nav-count");
    syncBadge.hidden = pendingSync === 0;
    syncBadge.textContent = pendingSync;
  } catch (error) {
    console.error("Failed to load overview", error);
  }
}

function dayFullName(dayAbbr) {
  const map = {
    mon: "Monday",
    tue: "Tuesday",
    wed: "Wednesday",
    thu: "Thursday",
    fri: "Friday",
    sat: "Saturday",
    sun: "Sunday",
  };
  return map[dayAbbr?.toLowerCase()] || "Monday";
}

function formatHourMinute(hour = 9, minute = 0) {
  const h = Number(hour);
  const m = Number(minute).toString().padStart(2, "0");
  const ampm = h >= 12 ? "PM" : "AM";
  const displayHour = h % 12 || 12;
  return `${displayHour}:${m} ${ampm}`;
}

// ==================== ARTICLES CMS VIEW ====================

function currentQuery() {
  const params = new URLSearchParams({
    page: state.page,
    page_size: state.pageSize,
    sort: state.sort,
    direction: state.direction,
  });
  const values = {
    search: $("#search-input").value.trim(),
    publisher: $("#publisher-filter").value,
    keyword: $("#keyword-filter").value,
    date_from: $("#date-from").value,
    date_to: $("#date-to").value,
  };
  Object.entries(values).forEach(([key, value]) => {
    if (value) params.set(key, value);
  });
  return params;
}

function initials(name) {
  return name.split(/\s+/).filter(Boolean).slice(0, 2).map((word) => word[0]).join("").toUpperCase();
}

function createDescription(text) {
  const cell = document.createElement("td");
  cell.className = "description-cell";
  if (!text) {
    cell.textContent = "Not reliably available";
    cell.style.fontStyle = "italic";
    return cell;
  }
  const shortened = text.length > 180;
  const span = document.createElement("span");
  span.textContent = shortened ? `${text.slice(0, 180).trim()}…` : text;
  cell.append(span);
  if (shortened) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Read full";
    let expanded = false;
    button.addEventListener("click", () => {
      expanded = !expanded;
      span.textContent = expanded ? text : `${text.slice(0, 180).trim()}…`;
      button.textContent = expanded ? "Collapse" : "Read full";
    });
    cell.append(button);
  }
  return cell;
}

function renderArticles(data) {
  const body = $("#article-rows");
  body.replaceChildren();
  state.pages = data.pages;
  $("#articles-total-count").textContent = data.total.toLocaleString("en-IN");
  $("#page-number").textContent = `${data.page} / ${data.pages}`;
  const first = data.total ? (data.page - 1) * data.page_size + 1 : 0;
  const last = Math.min(data.page * data.page_size, data.total);
  $("#page-summary").textContent = data.total ? `Showing ${first}–${last} of ${data.total}` : "Showing 0 articles";
  $("#prev-page").disabled = data.page <= 1;
  $("#next-page").disabled = data.page >= data.pages;
  $("#empty-state").hidden = data.items.length !== 0;

  data.items.forEach((article) => {
    const row = document.createElement("tr");

    const publisher = document.createElement("td");
    publisher.className = "publisher-cell";
    const avatar = document.createElement("span");
    avatar.className = "publisher-avatar";
    avatar.textContent = initials(article.publisher);
    const publisherName = document.createElement("span");
    publisherName.textContent = article.publisher;
    publisher.append(avatar, publisherName);

    const date = document.createElement("td");
    date.className = "date-cell";
    date.textContent = formatDate(article.published_at);

    const title = document.createElement("td");
    title.className = "title-cell";
    title.textContent = article.title;

    const linkCell = document.createElement("td");
    const link = document.createElement("a");
    link.className = "open-link";
    link.href = article.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = "Open ↗";
    link.setAttribute("aria-label", `Open ${article.title}`);
    linkCell.append(link);

    row.append(publisher, date, title, createDescription(article.description), linkCell);
    body.append(row);
  });
}

async function loadArticles() {
  try {
    renderArticles(await api(`/api/articles?${currentQuery()}`));
  } catch (error) {
    toast(error.message, true);
  }
}

async function loadPublishers() {
  try {
    const data = await api("/api/publishers");
    const select = $("#publisher-filter");
    const selected = select.value;
    [...select.options].slice(1).forEach((option) => option.remove());
    data.items.forEach((publisher) => {
      const option = document.createElement("option");
      option.value = option.textContent = publisher;
      select.append(option);
    });
    select.value = selected;
  } catch (error) {
    console.error("Failed to load publishers", error);
  }
}

function renderHistory(data) {
  const body = $("#history-rows");
  body.replaceChildren();
  if (!data.items.length) {
    const row = body.insertRow();
    const cell = row.insertCell();
    cell.colSpan = 5;
    cell.className = "loading-row";
    cell.textContent = "No fetches yet — click 'Fetch new articles' to run.";
    return;
  }
  data.items.forEach((fetch) => {
    const row = body.insertRow();
    const date = row.insertCell();
    date.textContent = `${formatDate(fetch.started_at)} · ${formatTime(fetch.started_at)}`;
    const statusCell = row.insertCell();
    const badge = document.createElement("span");
    badge.className = `status-badge ${fetch.status}`;
    badge.textContent = fetch.status;
    if (fetch.error) badge.title = fetch.error;
    statusCell.append(badge);
    row.insertCell().textContent = fetch.new_articles;
    row.insertCell().textContent = fetch.duplicates;
    row.insertCell().textContent = fetch.failed_articles;
  });
}

async function loadHistory() {
  try {
    renderHistory(await api("/api/fetches?limit=12"));
  } catch (error) {
    toast(error.message, true);
  }
}

// ==================== EMAIL SETTINGS CMS VIEW ====================

async function loadRecipients() {
  const body = $("#recipient-rows");
  try {
    const data = await api("/api/admin/recipients");
    state.recipients = data.items || [];
    renderRecipients(state.recipients);
  } catch (error) {
    if (error.status === 401) {
      showAdminDialog(() => loadRecipients());
    } else {
      toast(error.message, true);
    }
  }
}

function renderRecipients(items) {
  const body = $("#recipient-rows");
  body.replaceChildren();

  const activeCount = items.filter((r) => r.active).length;
  $("#recipients-active-count").textContent = activeCount;
  $("#recipients-empty").hidden = items.length > 0;
  $("#recipients-table").hidden = items.length === 0;

  items.forEach((item) => {
    const row = document.createElement("tr");

    // Email
    const emailCell = document.createElement("td");
    emailCell.style.fontWeight = "600";
    emailCell.textContent = item.email;

    // Status
    const statusCell = document.createElement("td");
    const pill = document.createElement("span");
    pill.className = `status-pill ${item.active ? "active" : "disabled"}`;
    pill.textContent = item.active ? "Active" : "Disabled";
    statusCell.append(pill);

    // Added Date
    const addedCell = document.createElement("td");
    addedCell.className = "date-cell";
    addedCell.textContent = formatDate(item.created_at);

    // Actions
    const actionsCell = document.createElement("td");
    actionsCell.style.textAlign = "right";
    const actionsWrap = document.createElement("div");
    actionsWrap.className = "table-actions";

    // Edit button
    const editBtn = document.createElement("button");
    editBtn.type = "button";
    editBtn.className = "btn-action btn-action-edit";
    editBtn.textContent = "Edit";
    editBtn.addEventListener("click", () => openRecipientModal(item));

    // Toggle Active button
    const toggleBtn = document.createElement("button");
    toggleBtn.type = "button";
    toggleBtn.className = "btn-action btn-action-toggle";
    toggleBtn.textContent = item.active ? "Disable" : "Enable";
    toggleBtn.addEventListener("click", () => toggleRecipientStatus(item.id, !item.active, toggleBtn));

    // Delete button
    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "btn-action btn-action-delete";
    delBtn.textContent = "Delete";
    delBtn.addEventListener("click", () => deleteRecipient(item.id, item.email, delBtn));

    actionsWrap.append(editBtn, toggleBtn, delBtn);
    actionsCell.append(actionsWrap);

    row.append(emailCell, statusCell, addedCell, actionsCell);
    body.append(row);
  });
}

function openRecipientModal(recipient = null) {
  const dialog = $("#recipient-dialog");
  const errorBox = $("#recipient-error-msg");
  errorBox.hidden = true;
  errorBox.textContent = "";

  if (recipient) {
    $("#recipient-dialog-title").textContent = "Edit recipient";
    $("#recipient-dialog-eyebrow").textContent = "UPDATE RECIPIENT";
    $("#recipient-edit-id").value = recipient.id;
    $("#recipient-email-input").value = recipient.email;
    $("#recipient-active-checkbox").checked = recipient.active;
  } else {
    $("#recipient-dialog-title").textContent = "Add recipient";
    $("#recipient-dialog-eyebrow").textContent = "NEW RECIPIENT";
    $("#recipient-edit-id").value = "";
    $("#recipient-email-input").value = "";
    $("#recipient-active-checkbox").checked = true;
  }

  dialog.showModal();
  $("#recipient-email-input").focus();
}

async function handleRecipientSubmit(event) {
  event.preventDefault();
  const id = $("#recipient-edit-id").value;
  const email = $("#recipient-email-input").value.trim().toLowerCase();
  const active = $("#recipient-active-checkbox").checked;
  const errorBox = $("#recipient-error-msg");

  // Email format validation
  const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
  if (!emailRegex.test(email)) {
    errorBox.textContent = "Please enter a valid email address.";
    errorBox.hidden = false;
    return;
  }

  // Client duplicate check
  const duplicate = state.recipients.find(
    (r) => r.email.toLowerCase() === email && r.id !== id
  );
  if (duplicate) {
    errorBox.textContent = `Recipient with email "${email}" is already configured.`;
    errorBox.hidden = false;
    return;
  }

  const saveBtn = $("#recipient-dialog-save");
  if (saveBtn) {
    saveBtn.disabled = true;
    saveBtn.textContent = "Saving…";
  }

  try {
    if (id) {
      await api(`/api/admin/recipients/${id}`, {
        method: "PUT",
        body: { email, active },
      });
    } else {
      await api("/api/admin/recipients", {
        method: "POST",
        body: { email, active },
      });
    }
    $("#recipient-dialog").close();
    toast("✓ Email recipients updated successfully.");
    await Promise.all([loadRecipients(), loadOverview()]);
  } catch (error) {
    if (error.status === 401) {
      showAdminDialog(() => handleRecipientSubmit(event));
    } else {
      errorBox.textContent = error.message;
      errorBox.hidden = false;
    }
  } finally {
    if (saveBtn) {
      saveBtn.disabled = false;
      saveBtn.textContent = "Save Recipient";
    }
  }
}

async function toggleRecipientStatus(recipientId, newActive, clickedBtn = null) {
  if (clickedBtn) {
    clickedBtn.disabled = true;
    clickedBtn.textContent = "Updating…";
  }
  try {
    await api(`/api/admin/recipients/${recipientId}`, {
      method: "PUT",
      body: { active: newActive },
    });
    toast(`✓ Email recipient ${newActive ? "enabled" : "disabled"} successfully.`);
    await Promise.all([loadRecipients(), loadOverview()]);
  } catch (error) {
    if (error.status === 401) {
      showAdminDialog(() => toggleRecipientStatus(recipientId, newActive, clickedBtn));
    } else {
      toast(error.message, true);
    }
  } finally {
    if (clickedBtn) {
      clickedBtn.disabled = false;
      clickedBtn.textContent = newActive ? "Disable" : "Enable";
    }
  }
}

async function deleteRecipient(recipientId, email, clickedBtn = null) {
  if (!confirm(`Are you sure you want to remove "${email}" from the report recipients?`)) {
    return;
  }
  if (clickedBtn) {
    clickedBtn.disabled = true;
    clickedBtn.textContent = "Deleting…";
  }
  try {
    await api(`/api/admin/recipients/${recipientId}`, { method: "DELETE" });
    toast("✓ Email recipient removed successfully.");
    await Promise.all([loadRecipients(), loadOverview()]);
  } catch (error) {
    if (error.status === 401) {
      showAdminDialog(() => deleteRecipient(recipientId, email, clickedBtn));
    } else {
      toast(error.message, true);
    }
  } finally {
    if (clickedBtn) {
      clickedBtn.disabled = false;
      clickedBtn.textContent = "Delete";
    }
  }
}

$("#btn-add-recipient").addEventListener("click", () => openRecipientModal());
$("#recipient-form").addEventListener("submit", handleRecipientSubmit);
$("#recipient-dialog-close").addEventListener("click", () => $("#recipient-dialog").close());
$("#recipient-dialog-cancel").addEventListener("click", () => $("#recipient-dialog").close());

// ==================== AUTOMATION SETTINGS CMS VIEW ====================

async function loadAutomationSchedule() {
  try {
    const data = await api("/api/admin/automation");
    const sched = data.schedule;
    state.schedule = sched;

    // Enabled toggle
    const enabledToggle = $("#auto-enabled-toggle");
    enabledToggle.checked = Boolean(sched.enabled);
    $("#auto-enabled-label").textContent = sched.enabled ? "Automation: ON" : "Automation: OFF";

    // Frequency
    $("#auto-frequency").value = sched.frequency || "weekly";
    handleFrequencyChange();

    // Day of week
    $("#auto-day-of-week").value = sched.day_of_week || "mon";

    // Hour and Minute (convert 24-hr to 12-hr)
    const hour24 = Number(sched.hour ?? 9);
    const minute = Number(sched.minute ?? 0);
    const ampm = hour24 >= 12 ? "PM" : "AM";
    const hour12 = hour24 % 12 || 12;

    $("#auto-hour").value = String(hour12);
    $("#auto-minute").value = String(minute).padStart(2, "0");
    $("#auto-ampm").value = ampm;

    if (sched.cron) {
      $("#auto-cron").value = sched.cron;
    }

    // Next scheduled run display
    const isDailyOrCustom = sched.frequency === "daily" || sched.frequency === "custom";
    const nextFormatted = data.next_scheduled_fetch_formatted;
    const timeOnlyText = `${formatHourMinute(sched.hour, sched.minute)} IST`;
    const fallbackText = isDailyOrCustom
      ? timeOnlyText
      : `${dayFullName(sched.day_of_week)}, ${timeOnlyText}`;
    $("#schedule-next-run-text").textContent = sched.enabled
      ? nextFormatted || fallbackText
      : "Automation paused (OFF)";
  } catch (error) {
    if (error.status === 401) {
      showAdminDialog(() => loadAutomationSchedule());
    } else {
      toast(error.message, true);
    }
  }
}

function updateScheduleBannerPreview() {
  const enabled = $("#auto-enabled-toggle").checked;
  if (!enabled) {
    $("#schedule-next-run-text").textContent = "Automation paused (OFF)";
    return;
  }
  const freq = $("#auto-frequency").value;
  const day = $("#auto-day-of-week").value;
  const hour12 = parseInt($("#auto-hour").value, 10);
  let minute = parseInt($("#auto-minute").value, 10);
  if (isNaN(minute) || minute < 0) minute = 0;
  if (minute > 59) minute = 59;
  const ampm = $("#auto-ampm").value;
  let hour24 = hour12 % 12;
  if (ampm === "PM") hour24 += 12;
  const timeStr = `${formatHourMinute(hour24, minute)} IST`;

  if (freq === "daily" || freq === "custom") {
    $("#schedule-next-run-text").textContent = timeStr;
  } else {
    $("#schedule-next-run-text").textContent = `${dayFullName(day)}, ${timeStr}`;
  }
}

function handleFrequencyChange() {
  const freq = $("#auto-frequency").value;
  const dayGroup = $("#group-day-of-week");
  const timeGroup = $("#group-time");
  const cronGroup = $("#group-custom-cron");

  if (freq === "weekly") {
    dayGroup.hidden = false;
    timeGroup.hidden = false;
    cronGroup.hidden = true;
  } else if (freq === "daily") {
    dayGroup.hidden = true;
    timeGroup.hidden = false;
    cronGroup.hidden = true;
  } else if (freq === "custom") {
    dayGroup.hidden = true;
    timeGroup.hidden = true;
    cronGroup.hidden = false;
  }
  updateScheduleBannerPreview();
}

$("#auto-frequency").addEventListener("change", handleFrequencyChange);
$("#auto-day-of-week").addEventListener("change", updateScheduleBannerPreview);
$("#auto-hour").addEventListener("change", updateScheduleBannerPreview);
$("#auto-minute").addEventListener("input", updateScheduleBannerPreview);
$("#auto-ampm").addEventListener("change", updateScheduleBannerPreview);

$("#auto-enabled-toggle").addEventListener("change", (e) => {
  $("#auto-enabled-label").textContent = e.target.checked ? "Automation: ON" : "Automation: OFF";
  updateScheduleBannerPreview();
});

async function handleSaveSchedule() {
  const enabled = $("#auto-enabled-toggle").checked;
  const frequency = $("#auto-frequency").value;
  const dayOfWeek = $("#auto-day-of-week").value;
  const hour12 = parseInt($("#auto-hour").value, 10);
  let minute = parseInt($("#auto-minute").value, 10);
  if (isNaN(minute) || minute < 0) minute = 0;
  if (minute > 59) minute = 59;
  $("#auto-minute").value = String(minute).padStart(2, "0");
  const ampm = $("#auto-ampm").value;
  const timezone = $("#auto-timezone").value || "Asia/Kolkata";
  const cron = $("#auto-cron").value.trim();

  // Convert 12-hr to 24-hr
  let hour24 = hour12 % 12;
  if (ampm === "PM") {
    hour24 += 12;
  }

  const payload = {
    enabled,
    frequency,
    day_of_week: dayOfWeek,
    hour: hour24,
    minute,
    timezone,
  };
  if (frequency === "custom" && cron) {
    payload.cron = cron;
  }

  const saveBtn = $("#btn-save-schedule");
  saveBtn.disabled = true;
  saveBtn.textContent = "Saving Schedule…";

  try {
    const res = await api("/api/admin/automation", {
      method: "POST",
      body: payload,
    });
    toast("✓ Automation schedule updated successfully.");
    const nextFormatted = res.next_scheduled_fetch_formatted;
    const isDailyOrCustom = frequency === "daily" || frequency === "custom";
    const timeOnlyText = `${formatHourMinute(hour24, minute)} IST`;
    const fallbackText = isDailyOrCustom
      ? timeOnlyText
      : `${dayFullName(dayOfWeek)}, ${timeOnlyText}`;
    $("#schedule-next-run-text").textContent = enabled
      ? nextFormatted || fallbackText
      : "Automation paused (OFF)";

    await loadOverview();
  } catch (error) {
    if (error.status === 401) {
      showAdminDialog(() => handleSaveSchedule());
    } else {
      toast(error.message, true);
    }
  } finally {
    saveBtn.disabled = false;
    saveBtn.textContent = "Save Schedule";
  }
}

$("#btn-save-schedule").addEventListener("click", handleSaveSchedule);
$("#auto-minute").addEventListener("blur", (e) => {
  let val = parseInt(e.target.value, 10);
  if (isNaN(val) || val < 0) val = 0;
  if (val > 59) val = 59;
  e.target.value = String(val).padStart(2, "0");
  updateScheduleBannerPreview();
});

// ==================== DATA SYNC REVIEW ====================

function renderDataSync(data) {
  const summary = data.summary || {};
  $("#sync-db-count").textContent = summary.database_articles ?? "—";
  $("#sync-sheet-count").textContent = summary.sheet_articles ?? "—";
  $("#sync-matching-count").textContent = summary.matching_articles ?? "—";
  $("#sync-db-only-count").textContent = summary.database_only ?? 0;
  $("#sync-sheet-only-count").textContent = summary.sheet_only ?? 0;
  $("#sync-duplicates-count").textContent = summary.duplicates ?? 0;
  const pending = summary.pending_reviews || 0;
  $("#sync-pending-count").textContent = `${pending} PENDING`;
  const alert = $("#sync-alert");
  alert.hidden = pending === 0;
  alert.textContent = pending
    ? `⚠ ${pending} article synchronization issue${pending === 1 ? "" : "s"} require review.`
    : "";

  const body = $("#sync-review-rows");
  body.replaceChildren();
  const items = data.items || [];
  if (!items.length) {
    const row = body.insertRow();
    const cell = row.insertCell();
    cell.colSpan = 5;
    cell.className = "loading-row";
    cell.textContent = "No pending synchronization reviews.";
    return;
  }

  const locationLabels = {
    database_only: "Database only",
    sheet_only: "Google Sheet only",
    duplicate_sheet_rows: "Duplicate Sheet rows",
  };
  items.forEach((review) => {
    const row = body.insertRow();
    const article = row.insertCell();
    const source = review.sheet_data || {};
    const title = review.article_title || source.title || source.url || review.normalized_url;
    article.textContent = title;
    article.className = "title-cell";
    row.insertCell().textContent = locationLabels[review.review_type] || review.review_type;
    const status = row.insertCell();
    status.innerHTML = '<span class="status-pill sync-review-required">Review required</span>';
    row.insertCell().textContent = (review.sheet_rows || []).join(", ") || "—";
    const actions = row.insertCell();
    actions.className = "table-actions";
    const choices = review.review_type === "database_only"
      ? [["Restore to Sheet", "restore_to_sheet"], ["Keep deleted", "confirm_deletion"]]
      : review.review_type === "sheet_only"
      ? [["Import to database", "import_to_database"]]
      : [["Consolidate rows", "consolidate_duplicates"]];
    choices.forEach(([label, action]) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "btn-action";
      button.textContent = label;
      button.addEventListener("click", () => resolveSyncReview(review.id, action, button));
      actions.append(button);
    });
  });
}

async function loadDataSync() {
  try {
    renderDataSync(await api("/api/admin/data-sync"));
  } catch (error) {
    if (error.status === 401) showAdminDialog(() => loadDataSync());
    else toast(error.message, true);
  }
}

async function runDataSync() {
  const button = $("#btn-run-sync");
  button.disabled = true;
  button.textContent = "Checking synchronization…";
  try {
    await api("/api/admin/sync-sheets", { method: "POST" });
    await Promise.all([loadDataSync(), loadOverview()]);
    toast("Synchronization check complete.");
  } catch (error) {
    if (error.status === 401) showAdminDialog(() => runDataSync());
    else toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Check synchronization";
  }
}

async function resolveSyncReview(reviewId, action, clickedButton = null) {
  if (action === "confirm_deletion" && !window.confirm("Keep this article in the database but mark it intentionally removed from Google Sheets?")) return;

  let rowButtons = [];
  let originalText = "";
  if (clickedButton) {
    const parent = clickedButton.closest(".table-actions") || clickedButton.parentElement;
    if (parent) {
      rowButtons = [...parent.querySelectorAll("button")];
      rowButtons.forEach((b) => (b.disabled = true));
    }
    originalText = clickedButton.textContent;
    clickedButton.textContent = action === "import_to_database"
      ? "Importing…"
      : action === "restore_to_sheet"
      ? "Restoring…"
      : action === "confirm_deletion"
      ? "Removing…"
      : "Consolidating…";
  }

  try {
    await api(`/api/admin/data-sync/${reviewId}/resolve`, {
      method: "POST",
      body: { action },
    });
    await Promise.all([loadDataSync(), loadOverview(), loadArticles()]);
    const successMsg = action === "import_to_database"
      ? "Article successfully imported to database."
      : action === "restore_to_sheet"
      ? "Article restored to Google Sheet."
      : action === "confirm_deletion"
      ? "Article marked intentionally removed from Google Sheet."
      : "Duplicate Sheet rows consolidated.";
    toast(successMsg);
  } catch (error) {
    if (error.status === 401) {
      showAdminDialog(() => resolveSyncReview(reviewId, action, clickedButton));
    } else {
      toast(error.message, true);
    }
  } finally {
    if (clickedButton) {
      rowButtons.forEach((b) => (b.disabled = false));
      clickedButton.textContent = originalText;
    }
  }
}

async function backfillDescriptions() {
  const button = $("#btn-backfill-descriptions");
  button.disabled = true;
  button.textContent = "Backfilling descriptions…";
  try {
    const result = await api("/api/admin/backfill-descriptions", { method: "POST" });
    await Promise.all([loadDataSync(), loadArticles()]);
    toast(`Description backfill: ${result.updated} updated, ${result.failed} unavailable.`);
  } catch (error) {
    if (error.status === 401) showAdminDialog(() => backfillDescriptions());
    else toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Backfill descriptions";
  }
}

$("#btn-run-sync").addEventListener("click", runDataSync);
$("#btn-backfill-descriptions").addEventListener("click", backfillDescriptions);

// ==================== FETCH ENGINE INTEGRATION ====================

const stages = ["Searching", "Collecting", "Extracting", "Deduplicating", "Saving", "Complete"];
function renderProgress(fetch) {
  const strip = $("#fetch-progress");
  strip.hidden = false;
  $("#progress-title").textContent = fetch.status === "failed" ? "Fetch failed" : fetch.status === "success" ? "Fetch complete" : "Fetch in progress";
  $("#progress-copy").textContent = fetch.status === "failed"
    ? fetch.error || "The existing dataset was left untouched."
    : `${fetch.stage} · ${fetch.new_articles} new · ${fetch.duplicates} duplicates · ${fetch.failed_articles} skipped`;
  const active = stages.indexOf(fetch.stage);
  $$("#progress-steps li").forEach((item, index) => {
    item.classList.toggle("done", index < active || fetch.status === "success");
    item.classList.toggle("active", index === active && fetch.status === "running");
  });
}

function resetFetchButton() {
  const btn = $("#fetch-button");
  if (!btn) return;
  btn.disabled = false;
  btn.innerHTML = '<span class="refresh-icon">↻</span> Fetch new articles';
}

function setFetchButtonLoading() {
  const btn = $("#fetch-button");
  if (!btn) return;
  btn.disabled = true;
  btn.innerHTML = '<span class="refresh-icon spinning">↻</span> Fetching articles…';
}

async function pollFetch(fetchId, attempts = 0) {
  window.clearTimeout(state.pollTimer);
  try {
    const fetch = await api(`/api/fetches/${fetchId}`);
    renderProgress(fetch);
    if (fetch.status === "running") {
      state.pollTimer = window.setTimeout(() => pollFetch(fetchId), 1400);
      return;
    }
    resetFetchButton();
    await Promise.all([loadOverview(), loadArticles(), loadPublishers(), loadHistory(), loadDataSync()]);
    toast(
      fetch.status === "success"
        ? `Fetch complete: ${fetch.new_articles} new article(s).`
        : `Fetch failed: ${fetch.error || "Existing data was preserved."}`,
      fetch.status !== "success"
    );
    window.setTimeout(() => {
      $("#fetch-progress").hidden = true;
    }, 7000);
  } catch (error) {
    if (error.status === 404 && attempts < 10) {
      state.pollTimer = window.setTimeout(() => pollFetch(fetchId, attempts + 1), 400);
      return;
    }
    resetFetchButton();
    $("#fetch-progress").hidden = true;
    toast(error.message, true);
  }
}

async function startFetch(adminKey = getAdminKey()) {
  setFetchButtonLoading();
  try {
    const data = await api("/api/fetch", {
      method: "POST",
      headers: adminKey ? { "X-Admin-Key": adminKey } : {},
    });
    $("#fetch-progress").hidden = false;
    $("#progress-title").textContent = "Fetch in progress";
    $("#progress-copy").textContent = "Preparing the incremental search window…";
    toast("Incremental fetch started.");
    pollFetch(data.fetch_id);
  } catch (error) {
    resetFetchButton();
    if (error.status === 401) {
      showAdminDialog((key) => startFetch(key));
    } else {
      toast(error.message, true);
    }
  }
}

function showAdminDialog(onSuccess) {
  const dialog = $("#admin-dialog");
  const errorBox = $("#admin-error-msg");
  if (errorBox) {
    errorBox.hidden = true;
    errorBox.textContent = "";
  }
  dialog.showModal();
  $("#admin-key").focus();
  state._pendingAdminCallback = onSuccess;
}

$("#fetch-button").addEventListener("click", () => startFetch());

$("#admin-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const key = $("#admin-key").value.trim();
  if (!key) return;
  sessionStorage.setItem("gp_admin_key", key);
  $("#admin-dialog").close();
  if (state._pendingAdminCallback) {
    const cb = state._pendingAdminCallback;
    state._pendingAdminCallback = null;
    cb(key);
  }
});

const adminCloseBtn = $("#admin-dialog-close");
if (adminCloseBtn) {
  adminCloseBtn.addEventListener("click", () => {
    $("#admin-dialog").close();
    state._pendingAdminCallback = null;
  });
}

const adminCancelBtn = $("#admin-dialog-cancel");
if (adminCancelBtn) {
  adminCancelBtn.addEventListener("click", () => {
    $("#admin-dialog").close();
    state._pendingAdminCallback = null;
  });
}

// ==================== FILTERS & SORTING ====================

let debounceTimer;
function filtersChanged() {
  state.page = 1;
  window.clearTimeout(debounceTimer);
  debounceTimer = window.setTimeout(loadArticles, 250);
}

$("#export-toggle").addEventListener("click", () => {
  const menu = $("#export-menu");
  menu.hidden = !menu.hidden;
  $("#export-toggle").setAttribute("aria-expanded", String(!menu.hidden));
});

document.addEventListener("click", (event) => {
  if (!event.target.closest(".export-wrap")) {
    $("#export-menu").hidden = true;
    $("#export-toggle").setAttribute("aria-expanded", "false");
  }
});

[$("#search-input"), $("#publisher-filter"), $("#keyword-filter"), $("#date-from"), $("#date-to")].forEach((input) => {
  if (input) input.addEventListener("input", filtersChanged);
});

$("#clear-filters").addEventListener("click", () => {
  $("#search-input").value = "";
  $("#publisher-filter").value = "";
  $("#keyword-filter").value = "";
  $("#date-from").value = "";
  $("#date-to").value = "";
  filtersChanged();
});

$$("th button[data-sort]").forEach((button) =>
  button.addEventListener("click", () => {
    const selected = button.dataset.sort;
    state.direction = state.sort === selected && state.direction === "desc" ? "asc" : "desc";
    state.sort = selected;
    state.page = 1;
    loadArticles();
  })
);

$("#prev-page").addEventListener("click", () => {
  if (state.page > 1) {
    state.page -= 1;
    loadArticles();
  }
});

$("#next-page").addEventListener("click", () => {
  if (state.page < state.pages) {
    state.page += 1;
    loadArticles();
  }
});

// ==================== BOOTSTRAP ====================

handleHashChange();
loadOverview();
loadHistory();
