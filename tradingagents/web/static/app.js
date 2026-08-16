const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

let activeJobId = null;
let activeReportId = null;
let reportPreviewExpanded = false;
let pollTimer = null;
let historyJobs = [];
let reportHistory = [];
let currentUser = null;

const ACTIVE_JOB_STORAGE_KEY = "tradingagents.activeJobId";
const ACTIVE_REPORT_STORAGE_KEY = "tradingagents.activeReportId";
const ACTIVE_VIEW_STORAGE_KEY = "tradingagents.activeView";
const AUTH_TOKEN_STORAGE_KEY = "tradingagents.authToken";
const SYSTEM_SETTINGS_STORAGE_PREFIX = "tradingagents.systemSettings";
const DEFAULT_USER_ID = "admin";

const STATUS_TEXT = {
  queued: "排队中",
  running: "运行中",
  completed: "已完成",
  failed: "失败",
  idle: "空闲",
};

const ANALYST_TEXT = {
  market: "市场技术",
  social: "情绪舆情",
  news: "新闻宏观",
  fundamentals: "基本面",
};

const EVENT_TEXT = {
  queued: "已排队",
  system: "系统设置",
  user_behavior: "用户行为",
  completed: "已完成",
  failed: "失败",
};

const REPORT_SCOPE_TEXT = {
  report_artifact: "报告产物",
};

const CRYPTO_SUFFIXES = ["-USD", "-USDT", "-USDC", "-BTC", "-ETH"];
const SYSTEM_SETTINGS_GROUP_ORDER = [
  "llm_base",
  "business_runtime",
  "llm_credentials",
  "data_credentials",
  "deployment",
  "user_behavior",
];
const SYSTEM_SETTINGS_DEFAULT_OPEN = new Set(["llm_base", "business_runtime"]);
const SYSTEM_SETTINGS_GROUP_FALLBACK = {
  llm_base: {
    title: "LLM 基础配置",
    description: "模型供应商、模型名称、后端地址和调用稳定性参数。",
    scope: "system",
  },
  business_runtime: {
    title: "业务运行配置",
    description: "分析任务运行时使用的数据源、工具源、产物路径和新闻抓取限制。",
    scope: "system",
  },
  llm_credentials: {
    title: "LLM 密钥状态",
    description: "LLM 与兼容网关的 API Key，只显示是否已配置。",
    scope: "system",
  },
  data_credentials: {
    title: "业务数据密钥状态",
    description: "行情、宏观数据等业务数据服务的 API Key，只显示是否已配置。",
    scope: "system",
  },
  deployment: {
    title: "部署与服务配置",
    description: "登录开关、管理员账号、公开访问地址和服务端状态目录。",
    scope: "system",
  },
  user_behavior: {
    title: "用户行为边界",
    description: "用户提交的分析参数，仅用于说明隔离边界，不属于系统基础配置。",
    scope: "user_behavior",
  },
};

function today() {
  return new Date().toISOString().slice(0, 10);
}

function currentUserId() {
  return normalizeUserId(currentUser?.user_id || DEFAULT_USER_ID);
}

function normalizeUserId(value) {
  const safe = String(value || DEFAULT_USER_ID).trim().toLowerCase().replace(/[^a-z0-9_-]/g, "");
  return safe || DEFAULT_USER_ID;
}

function authToken() {
  return localStorage.getItem(AUTH_TOKEN_STORAGE_KEY) || "";
}

function authHeaders(extra = {}) {
  return { ...extra, "X-Auth-Token": authToken() };
}

function apiFetch(url, options = {}) {
  return fetch(url, {
    ...options,
    headers: authHeaders(options.headers || {}),
  });
}

async function responseDetail(response, fallback) {
  try {
    const data = await response.json();
    return data.detail || fallback;
  } catch (error) {
    return fallback;
  }
}

function badge(status, health) {
  const label = health === "stale" ? "疑似卡住" : STATUS_TEXT[status] || status;
  const classes = ["badge", status, health === "stale" ? "stale" : ""].filter(Boolean).join(" ");
  return `<span class="${classes}">${label}</span>`;
}

function formatTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return "-";
  const total = Math.max(0, Number(seconds) || 0);
  const minutes = Math.floor(total / 60);
  const remaining = total % 60;
  if (minutes < 1) return `${remaining} 秒`;
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  if (hours < 1) return `${minutes} 分 ${remaining} 秒`;
  return `${hours} 小时 ${mins} 分`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatBytes(bytes) {
  const value = Number(bytes) || 0;
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function collectForm(form) {
  const data = Object.fromEntries(new FormData(form).entries());
  data.selected_analysts = $$('input[name="selected_analysts"]:checked').map((node) => node.value);
  if (resolveAssetType(data.ticker, data.asset_type) === "crypto") {
    data.selected_analysts = data.selected_analysts.filter((name) => name !== "fundamentals");
  }
  for (const key of ["max_debate_rounds", "max_risk_discuss_rounds"]) {
    if (data[key]) data[key] = Number(data[key]);
  }
  return data;
}

function resolveAssetType(ticker, selectedAssetType) {
  if (selectedAssetType) return selectedAssetType;
  const normalized = String(ticker || "").trim().toUpperCase();
  return CRYPTO_SUFFIXES.some((suffix) => normalized.endsWith(suffix)) ? "crypto" : "stock";
}

function updateAssetHint() {
  const ticker = $('input[name="ticker"]').value;
  const assetType = resolveAssetType(ticker, $('select[name="asset_type"]').value);
  const fundamentals = $('input[name="selected_analysts"][value="fundamentals"]');
  const hint = $("#asset-hint");
  if (assetType === "crypto") {
    fundamentals.checked = false;
    fundamentals.disabled = true;
    hint.textContent = "已识别为加密货币，系统会跳过基本面分析师。";
  } else {
    fundamentals.disabled = false;
    hint.textContent = "";
  }
}

function renderJob(job) {
  const events = (job.events || [])
    .map((event) => `<div class="event"><strong>${escapeHtml(EVENT_TEXT[event.type] || event.type)}</strong><div class="meta">${escapeHtml(event.time)}</div><div>${escapeHtml(event.message)}</div></div>`)
    .join("");
  const result = job.result
    ? `<div class="report"><h2>最终决策</h2><pre>${escapeHtml(job.result.final_trade_decision || job.result.decision)}</pre><div class="meta">报告路径：${escapeHtml(job.result.report_path)}</div></div>`
    : "";
  const error = job.error ? `<div class="report"><h2>错误</h2><pre>${escapeHtml(job.error.message)}</pre></div>` : "";
  const analysts = (job.request.selected_analysts || []).map((name) => ANALYST_TEXT[name] || name).join(", ");
  return `
    <div class="job-title">
      <span>${escapeHtml(job.request.ticker)} · ${escapeHtml(job.request.trade_date)}</span>
      ${badge(job.status, job.health)}
    </div>
    <div class="meta">分析师：${escapeHtml(analysts)}</div>
    <div class="meta">创建时间：${escapeHtml(formatTime(job.created_at))} · 更新时间：${escapeHtml(formatTime(job.updated_at))}</div>
    ${renderRuntime(job)}
    ${renderRequestDetails(job)}
    ${events}
    ${result}
    ${error}
  `;
}

function renderRuntime(job) {
  if (!job.runtime) return "";
  const heartbeat = job.heartbeat_at || job.updated_at;
  const warning = job.health === "stale"
    ? `<div class="runtime-warning">${escapeHtml(job.health_message)}</div>`
    : "";
  return `
    <dl class="details-grid runtime-grid">
      <div><dt>运行时长</dt><dd>${escapeHtml(formatDuration(job.runtime.running_for_seconds))}</dd></div>
      <div><dt>最后心跳</dt><dd>${escapeHtml(formatTime(heartbeat))}</dd></div>
      <div><dt>无更新时长</dt><dd>${escapeHtml(job.status === "running" ? formatDuration(job.runtime.idle_for_seconds) : "-")}</dd></div>
      <div><dt>状态说明</dt><dd>${escapeHtml(job.health_message || "已结束")}</dd></div>
    </dl>
    ${warning}
  `;
}

function renderRequestDetails(job) {
  const request = job.request || {};
  const fields = [
    ["用户", job.user_id || DEFAULT_USER_ID],
    ["资产类型", request.asset_type === "crypto" ? "加密货币" : "股票"],
    ["输出语言", request.output_language || "系统默认"],
    ["研究辩论轮数", request.max_debate_rounds || "系统默认"],
    ["风险讨论轮数", request.max_risk_discuss_rounds || "系统默认"],
  ];
  if (request.note) fields.push(["备注", request.note]);
  return `
    <dl class="details-grid">
      ${fields.map(([key, value]) => `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("")}
    </dl>
  `;
}

function renderHistoryDetail(job) {
  $("#history-detail-status").innerHTML = badge(job.status, job.health);
  $("#history-detail").className = "";
  $("#history-detail").innerHTML = renderJob(job);
}

function renderReportList(reports) {
  $("#report-list").innerHTML = reports.length
    ? reports.map((report) => `
      <button class="job ${report.id === activeReportId ? "selected" : ""}" type="button" data-report-id="${report.id}">
        <span class="job-title"><span>${escapeHtml(report.ticker)} · ${escapeHtml(report.trade_date || "报告")}</span><span class="badge">${escapeHtml(REPORT_SCOPE_TEXT[report.scope] || "报告")}</span></span>
        <span class="meta">生成：${escapeHtml(formatTime(report.created_at))} · ${escapeHtml(formatBytes(report.size_bytes))}</span>
      </button>
    `).join("")
    : '<div class="empty">暂无可预览报告。</div>';
  $$("#report-list [data-report-id]").forEach((node) => {
    node.addEventListener("click", () => selectReport(node.dataset.reportId));
  });
  markSelectedReport(activeReportId);
}

function renderReportDetail(report) {
  $("#report-detail-status").innerHTML = '<span class="badge">可预览</span>';
  $("#report-detail").className = "";
  $("#report-detail").innerHTML = `
    <div class="job-title">
      <span>${escapeHtml(report.ticker)} · ${escapeHtml(report.trade_date || "报告")}</span>
      <span class="report-actions">
        <button id="toggle-report-preview" type="button">${reportPreviewExpanded ? "收起预览" : "全屏预览"}</button>
        <a class="download-button" href="${escapeHtml(report.download_url)}?auth_token=${encodeURIComponent(authToken())}" download="${escapeHtml(report.file_name)}">下载报告</a>
      </span>
    </div>
    <div class="meta">生成时间：${escapeHtml(formatTime(report.created_at))} · 文件大小：${escapeHtml(formatBytes(report.size_bytes))}</div>
    <div class="meta">报告路径：${escapeHtml(report.path)}</div>
    <article class="markdown-preview">${renderMarkdownPreview(report.content || "")}</article>
  `;
  $("#toggle-report-preview").addEventListener("click", () => setReportPreviewExpanded(!reportPreviewExpanded));
  applyReportPreviewState();
}

function setReportPreviewExpanded(expanded) {
  reportPreviewExpanded = expanded;
  applyReportPreviewState();
}

function applyReportPreviewState() {
  document.body.classList.toggle("report-preview-expanded", reportPreviewExpanded);
  $(".report-preview-panel")?.classList.toggle("expanded", reportPreviewExpanded);
  const button = $("#toggle-report-preview");
  if (button) button.textContent = reportPreviewExpanded ? "收起预览" : "全屏预览";
}

function renderMarkdownPreview(markdown) {
  const blocks = [];
  let listItems = [];
  const flushList = () => {
    if (!listItems.length) return;
    blocks.push(`<ul>${listItems.map((item) => `<li>${inlineMarkdown(item)}</li>`).join("")}</ul>`);
    listItems = [];
  };

  for (const rawLine of String(markdown).split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) {
      flushList();
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      flushList();
      const level = Math.min(heading[1].length + 1, 5);
      blocks.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
      continue;
    }
    const bullet = line.match(/^[-*]\s+(.+)$/);
    if (bullet) {
      listItems.push(bullet[1]);
      continue;
    }
    flushList();
    blocks.push(`<p>${inlineMarkdown(line)}</p>`);
  }
  flushList();
  return blocks.join("");
}

function inlineMarkdown(value) {
  return escapeHtml(value).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
}

async function fetchJob(jobId) {
  const response = await apiFetch(`/api/jobs/${jobId}`);
  if (!response.ok) throw new Error(response.status === 403 ? "当前用户未开通" : "任务不存在");
  return response.json();
}

async function selectJob(jobId) {
  activeJobId = jobId;
  localStorage.setItem(ACTIVE_JOB_STORAGE_KEY, jobId);
  const job = await fetchJob(jobId);
  $("#current-status").innerHTML = badge(job.status, job.health);
  $("#current-job").className = "";
  $("#current-job").innerHTML = renderJob(job);
  renderHistoryDetail(job);
  markSelectedJob(jobId);
  if (pollTimer) clearInterval(pollTimer);
  if (["queued", "running"].includes(job.status)) {
    pollTimer = setInterval(() => refreshSelectedJob(jobId), 3500);
  }
}

async function refreshSelectedJob(jobId) {
  try {
    const job = await fetchJob(jobId);
    $("#current-status").innerHTML = badge(job.status, job.health);
    $("#current-job").className = "";
    $("#current-job").innerHTML = renderJob(job);
    renderHistoryDetail(job);
    if (!["queued", "running"].includes(job.status) && pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    await refreshJobs({ preserveSelection: true });
  } catch (error) {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function refreshJobs(options = {}) {
  const { preserveSelection = false, autoSelect = false } = options;
  const response = await apiFetch("/api/jobs");
  if (!response.ok) {
    $("#job-list").innerHTML = '<div class="empty">当前用户未开通，请联系管理员。</div>';
    $("#history-detail-status").textContent = "不可用";
    $("#history-detail").className = "empty";
    $("#history-detail").textContent = "当前用户不在管理员维护的用户列表中。";
    return;
  }
  const data = await response.json();
  const jobs = data.jobs || [];
  historyJobs = jobs;
  $("#job-list").innerHTML = jobs.length
    ? jobs.map((job) => `
      <button class="job ${job.id === activeJobId ? "selected" : ""}" type="button" data-job-id="${job.id}">
        <span class="job-title"><span>${escapeHtml(job.request.ticker)} · ${escapeHtml(job.request.trade_date)}</span>${badge(job.status, job.health)}</span>
        <span class="meta">创建：${escapeHtml(formatTime(job.created_at))} · 更新：${escapeHtml(formatTime(job.updated_at))}</span>
      </button>
    `).join("")
    : '<div class="empty">暂无分析任务。</div>';
  $$("#job-list [data-job-id]").forEach((node) => {
    node.addEventListener("click", () => selectJob(node.dataset.jobId));
  });
  if (autoSelect && jobs.length) {
    const storedJobId = localStorage.getItem(ACTIVE_JOB_STORAGE_KEY);
    const selected = jobs.find((job) => job.id === storedJobId) || jobs[0];
    await selectJob(selected.id);
    return;
  }
  if (!preserveSelection && !jobs.length) {
    $("#history-detail-status").textContent = "未选择";
    $("#history-detail").className = "empty";
    $("#history-detail").textContent = "从左侧选择一条历史任务。";
  }
  markSelectedJob(activeJobId);
}

function markSelectedJob(jobId) {
  $$("#job-list [data-job-id]").forEach((node) => {
    node.classList.toggle("selected", node.dataset.jobId === jobId);
  });
}

function markSelectedReport(reportId) {
  $$("#report-list [data-report-id]").forEach((node) => {
    node.classList.toggle("selected", node.dataset.reportId === reportId);
  });
}

async function refreshReports(options = {}) {
  const { autoSelect = false, preserveSelection = false } = options;
  const response = await apiFetch("/api/reports");
  if (!response.ok) {
    $("#report-list").innerHTML = '<div class="empty">当前用户未开通，请联系管理员。</div>';
    $("#report-detail-status").textContent = "不可用";
    $("#report-detail").className = "empty";
    $("#report-detail").textContent = "当前用户不在管理员维护的用户列表中。";
    return;
  }
  const data = await response.json();
  const reports = data.reports || [];
  reportHistory = reports;
  renderReportList(reports);
  if (autoSelect && reports.length) {
    const storedReportId = localStorage.getItem(ACTIVE_REPORT_STORAGE_KEY);
    const selected = reports.find((report) => report.id === storedReportId) || reports[0];
    await selectReport(selected.id);
    return;
  }
  if (!preserveSelection && !reports.length) {
    $("#report-detail-status").textContent = "未选择";
    $("#report-detail").className = "empty";
    $("#report-detail").textContent = "从左侧选择一份报告。";
  }
  markSelectedReport(activeReportId);
}

async function selectReport(reportId) {
  activeReportId = reportId;
  localStorage.setItem(ACTIVE_REPORT_STORAGE_KEY, reportId);
  markSelectedReport(reportId);
  const response = await apiFetch(`/api/reports/${reportId}`);
  if (!response.ok) {
    $("#report-detail-status").textContent = "读取失败";
    $("#report-detail").className = "empty";
    $("#report-detail").textContent = "报告不存在或已被移动。";
    return;
  }
  const report = await response.json();
  renderReportDetail(report);
}

function systemSettingValue(value) {
  if (value === null || value === undefined || value === "") return "未配置";
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2);
}

function systemSettingSourceText(source) {
  if (source === "environment") return "环境变量";
  if (source === "default/config") return "默认/配置文件";
  if (source === "runtime schema") return "运行参数结构";
  return source || "服务端配置";
}

function systemSettingScopeText(scope) {
  if (scope === "user_behavior") return "用户行为";
  return "系统";
}

function systemSettingGroupIsOpen(groupId) {
  const stored = localStorage.getItem(`${SYSTEM_SETTINGS_STORAGE_PREFIX}.${groupId}`);
  if (stored) return stored === "open";
  return SYSTEM_SETTINGS_DEFAULT_OPEN.has(groupId);
}

function renderSystemSettingItem(item) {
  return `
    <div class="setting">
      <div class="job-title">
        <span>${escapeHtml(item.key)}</span>
        <span class="badge">${escapeHtml(systemSettingScopeText(item.scope))}</span>
      </div>
      <pre>${escapeHtml(systemSettingValue(item.value))}</pre>
      <div class="meta">来源：${escapeHtml(systemSettingSourceText(item.source))}</div>
    </div>
  `;
}

function renderSystemSettings(data) {
  const groupMeta = { ...SYSTEM_SETTINGS_GROUP_FALLBACK, ...(data.groups || {}) };
  const groups = new Map();
  const ensureGroup = (groupId) => {
    const id = groupId || "business_runtime";
    if (!groups.has(id)) {
      groups.set(id, {
        id,
        ...(groupMeta[id] || { title: id, description: "未分类配置。", scope: "system" }),
        items: [],
      });
    }
    return groups.get(id);
  };

  (data.settings || []).forEach((item) => {
    ensureGroup(item.group).items.push(item);
  });
  (data.secrets || []).forEach((item) => {
    ensureGroup(item.group).items.push({
      ...item,
      value: item.configured ? "***已配置***" : "未配置",
    });
  });
  if (data.deployment) {
    const { scope, group, ...deploymentValue } = data.deployment;
    ensureGroup(data.deployment.group || "deployment").items.push({
      key: "deployment",
      value: deploymentValue,
      source: "environment",
      scope: data.deployment.scope || "system",
    });
  }
  if (data.user_behavior_schema) {
    ensureGroup(data.user_behavior_schema.group || "user_behavior").items.push({
      key: "user_behavior_fields",
      value: data.user_behavior_schema.fields || [],
      source: "runtime schema",
      scope: data.user_behavior_schema.scope || "user_behavior",
    });
  }

  const orderedGroups = [
    ...SYSTEM_SETTINGS_GROUP_ORDER.map((groupId) => groups.get(groupId)).filter(Boolean),
    ...Array.from(groups.values()).filter((group) => !SYSTEM_SETTINGS_GROUP_ORDER.includes(group.id)),
  ].filter((group) => group.items.length);

  $("#system-settings").innerHTML = orderedGroups.map((group) => {
    const open = systemSettingGroupIsOpen(group.id) ? " open" : "";
    return `
      <details class="settings-group" data-group="${escapeHtml(group.id)}"${open}>
        <summary class="settings-group-header">
          <span class="settings-group-copy">
            <span class="settings-group-title">${escapeHtml(group.title)}</span>
            <span class="settings-group-description">${escapeHtml(group.description)}</span>
          </span>
          <span class="settings-group-info">
            <span>${escapeHtml(group.items.length)} 项</span>
            <span class="badge">${escapeHtml(systemSettingScopeText(group.scope))}</span>
            <span class="settings-group-chevron">⌄</span>
          </span>
        </summary>
        <div class="settings-group-body">
          ${group.items.map(renderSystemSettingItem).join("")}
        </div>
      </details>
    `;
  }).join("");

  $$(".settings-group").forEach((groupEl) => {
    groupEl.addEventListener("toggle", () => {
      localStorage.setItem(
        `${SYSTEM_SETTINGS_STORAGE_PREFIX}.${groupEl.dataset.group}`,
        groupEl.open ? "open" : "closed",
      );
    });
  });
}

async function loadSystemSettings() {
  const response = await apiFetch("/api/system-settings");
  if (!response.ok) {
    $("#system-settings").innerHTML = '<div class="empty">需要管理员账号登录。</div>';
    return;
  }
  const data = await response.json();
  renderSystemSettings(data);
}

function renderAdminUsers(payload) {
  const users = payload.users || [];
  $("#admin-status").innerHTML = '<span class="badge completed">已连接</span>';
  $("#admin-users-list").innerHTML = users.length
    ? users.map((user) => {
      const protectedUser = user.user_id === payload.default_user_id || user.user_id === payload.admin_user_id;
      const roleText = user.role === "admin" ? "管理员" : "用户";
      return `
        <div class="job user-row">
          <div>
            <div class="job-title">
              <span>${escapeHtml(user.display_name)} · ${escapeHtml(user.user_id)}</span>
              <span class="badge">${escapeHtml(roleText)}</span>
            </div>
            <div class="meta">创建：${escapeHtml(formatTime(user.created_at))} · 任务数：${escapeHtml(user.job_count || 0)}</div>
          </div>
          <div class="user-row-actions">
            <button class="danger" type="button" data-delete-user="${escapeHtml(user.user_id)}" ${protectedUser ? "disabled" : ""}>删除</button>
          </div>
        </div>
      `;
    }).join("")
    : '<div class="empty">暂无用户。</div>';
  $$("#admin-users-list [data-delete-user]").forEach((node) => {
    node.addEventListener("click", () => deleteManagedUser(node.dataset.deleteUser));
  });
}

async function loadAdminUsers() {
  const response = await apiFetch("/api/admin/users");
  if (!response.ok) {
    const detail = await responseDetail(response, "需要管理员账号");
    $("#admin-status").innerHTML = '<span class="badge failed">未连接</span>';
    $("#admin-session-detail").textContent = "当前账号没有用户管理权限。";
    $("#admin-users-list").innerHTML = `
      <div class="empty">
        ${escapeHtml(detail)}。请使用管理员账号登录。
      </div>
    `;
    return;
  }
  $("#admin-session-detail").textContent = `${currentUser.display_name} · ${currentUser.user_id}`;
  renderAdminUsers(await response.json());
}

async function createManagedUser(form) {
  if (currentUser?.role !== "admin") {
    alert("请使用管理员账号登录。");
    return;
  }
  const data = Object.fromEntries(new FormData(form).entries());
  const response = await apiFetch("/api/admin/users", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    alert(await responseDetail(response, "新增用户失败"));
    return;
  }
  form.reset();
  await loadAdminUsers();
}

async function deleteManagedUser(userId) {
  if (!confirm(`删除用户 ${userId}？该用户的任务历史会从数据库中移除。`)) return;
  const response = await apiFetch(`/api/admin/users/${encodeURIComponent(userId)}`, {
    method: "DELETE",
  });
  if (!response.ok) {
    const error = await response.json();
    alert(error.detail || "删除用户失败");
    return;
  }
  await loadAdminUsers();
}

function bindTabs() {
  $$(".tab").forEach((button) => {
    button.addEventListener("click", () => {
      setActiveView(button.dataset.view);
    });
  });
}

function setActiveView(viewId) {
  if (viewId !== "reports") setReportPreviewExpanded(false);
  localStorage.setItem(ACTIVE_VIEW_STORAGE_KEY, viewId);
  $$(".tab").forEach((item) => item.classList.toggle("active", item.dataset.view === viewId));
  $$(".view").forEach((view) => view.classList.toggle("active", view.id === viewId));
  if (viewId === "history") refreshJobs({ preserveSelection: true });
  if (viewId === "reports") refreshReports({ preserveSelection: true, autoSelect: !activeReportId });
  if (viewId === "system") loadSystemSettings();
  if (viewId === "admin-users") loadAdminUsers();
}

function bindForm() {
  $("#analysis-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const response = await apiFetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(collectForm(event.currentTarget)),
    });
    if (!response.ok) {
      const error = await response.json();
      alert(error.detail || "创建分析任务失败");
      return;
    }
    const job = await response.json();
    await selectJob(job.id);
    await refreshJobs({ preserveSelection: true });
    setActiveView("workbench");
  });
}

function bindRefreshButtons() {
  $("#refresh-jobs").addEventListener("click", () => refreshJobs({ preserveSelection: true }));
  $("#refresh-reports").addEventListener("click", () => refreshReports({ preserveSelection: true, autoSelect: !activeReportId }));
  $("#refresh-admin-users").addEventListener("click", () => loadAdminUsers());
}

function bindAdminForms() {
  $("#create-user-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    await createManagedUser(event.currentTarget);
  });
}

function bindAuthForms() {
  $("#login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
    const response = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      $("#login-error").textContent = await responseDetail(response, "登录失败");
      return;
    }
    const data = await response.json();
    localStorage.setItem(AUTH_TOKEN_STORAGE_KEY, data.token);
    $("#login-error").textContent = "";
    await applyAuthenticatedUser(data.user);
  });
  $("#logout-button").addEventListener("click", async () => {
    await apiFetch("/api/auth/logout", { method: "POST" }).catch(() => {});
    localStorage.removeItem(AUTH_TOKEN_STORAGE_KEY);
    currentUser = null;
    resetSessionState();
    showLogin();
  });
}

function bindKeyboardShortcuts() {
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && reportPreviewExpanded) {
      setReportPreviewExpanded(false);
    }
  });
}

function resetSessionState() {
  activeJobId = null;
  activeReportId = null;
  localStorage.removeItem(ACTIVE_JOB_STORAGE_KEY);
  localStorage.removeItem(ACTIVE_REPORT_STORAGE_KEY);
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  $("#current-status").textContent = "空闲";
  $("#current-job").className = "empty";
  $("#current-job").textContent = "尚未选择任务。";
  $("#history-detail-status").textContent = "未选择";
  $("#history-detail").className = "empty";
  $("#history-detail").textContent = "从左侧选择一条历史任务。";
  $("#report-detail-status").textContent = "未选择";
  $("#report-detail").className = "empty";
  $("#report-detail").textContent = "从左侧选择一份报告。";
}

function showLogin() {
  $("#login-screen").classList.remove("hidden");
  $(".shell").classList.add("hidden");
}

function showApp() {
  $("#login-screen").classList.add("hidden");
  $(".shell").classList.remove("hidden");
}

async function applyAuthenticatedUser(user) {
  currentUser = user;
  resetSessionState();
  $("#current-user-label").textContent = `${user.display_name} · ${user.user_id}`;
  showApp();
  const initialView = window.location.pathname === "/admin"
    ? "admin-users"
    : localStorage.getItem(ACTIVE_VIEW_STORAGE_KEY) || "workbench";
  setActiveView(initialView);
  await refreshJobs({ autoSelect: true });
}

async function restoreSession() {
  if (!authToken()) {
    showLogin();
    return;
  }
  const response = await apiFetch("/api/auth/me");
  if (!response.ok) {
    localStorage.removeItem(AUTH_TOKEN_STORAGE_KEY);
    showLogin();
    return;
  }
  const data = await response.json();
  await applyAuthenticatedUser(data.user);
}

document.addEventListener("DOMContentLoaded", () => {
  $('input[name="trade_date"]').value = today();
  $('input[name="ticker"]').value = "BTC-USD";
  updateAssetHint();
  $('input[name="ticker"]').addEventListener("input", updateAssetHint);
  $('select[name="asset_type"]').addEventListener("change", updateAssetHint);
  bindTabs();
  bindForm();
  bindRefreshButtons();
  bindAdminForms();
  bindAuthForms();
  bindKeyboardShortcuts();
  restoreSession();
});
