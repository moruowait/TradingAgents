const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

let activeJobId = null;
let activeReportId = null;
let pollTimer = null;
let historyJobs = [];
let reportHistory = [];

const ACTIVE_JOB_STORAGE_KEY = "tradingagents.activeJobId";
const ACTIVE_REPORT_STORAGE_KEY = "tradingagents.activeReportId";
const ACTIVE_VIEW_STORAGE_KEY = "tradingagents.activeView";

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

function today() {
  return new Date().toISOString().slice(0, 10);
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
      <a class="download-button" href="${escapeHtml(report.download_url)}" download="${escapeHtml(report.file_name)}">下载报告</a>
    </div>
    <div class="meta">生成时间：${escapeHtml(formatTime(report.created_at))} · 文件大小：${escapeHtml(formatBytes(report.size_bytes))}</div>
    <div class="meta">报告路径：${escapeHtml(report.path)}</div>
    <article class="markdown-preview">${renderMarkdownPreview(report.content || "")}</article>
  `;
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
  const response = await fetch(`/api/jobs/${jobId}`);
  if (!response.ok) throw new Error("任务不存在");
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
  const response = await fetch("/api/jobs");
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
  const response = await fetch("/api/reports");
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
  const response = await fetch(`/api/reports/${reportId}`);
  if (!response.ok) {
    $("#report-detail-status").textContent = "读取失败";
    $("#report-detail").className = "empty";
    $("#report-detail").textContent = "报告不存在或已被移动。";
    return;
  }
  const report = await response.json();
  renderReportDetail(report);
}

async function loadSystemSettings() {
  const response = await fetch("/api/system-settings");
  if (!response.ok) {
    $("#system-settings").innerHTML = '<div class="empty">需要管理员令牌。</div>';
    return;
  }
  const data = await response.json();
  const settings = [
    ...data.settings.map((item) => ({ ...item, group: "配置" })),
    ...data.secrets.map((item) => ({ ...item, group: "密钥", value: item.configured ? "***已配置***" : "未配置" })),
    { key: "deployment", value: JSON.stringify(data.deployment, null, 2), source: "environment", group: "部署" },
  ];
  $("#system-settings").innerHTML = settings.map((item) => `
    <div class="setting">
      <div class="job-title"><span>${escapeHtml(item.group)} · ${escapeHtml(item.key)}</span><span class="badge">系统</span></div>
      <pre>${escapeHtml(typeof item.value === "string" ? item.value : JSON.stringify(item.value, null, 2))}</pre>
      <div class="meta">来源：${escapeHtml(item.source || "environment")}</div>
    </div>
  `).join("");
}

function bindTabs() {
  $$(".tab").forEach((button) => {
    button.addEventListener("click", () => {
      setActiveView(button.dataset.view);
    });
  });
}

function setActiveView(viewId) {
  localStorage.setItem(ACTIVE_VIEW_STORAGE_KEY, viewId);
  $$(".tab").forEach((item) => item.classList.toggle("active", item.dataset.view === viewId));
  $$(".view").forEach((view) => view.classList.toggle("active", view.id === viewId));
  if (viewId === "history") refreshJobs({ preserveSelection: true });
  if (viewId === "reports") refreshReports({ preserveSelection: true, autoSelect: !activeReportId });
  if (viewId === "system") loadSystemSettings();
}

function bindForm() {
  $("#analysis-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const response = await fetch("/api/jobs", {
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
  const initialView = localStorage.getItem(ACTIVE_VIEW_STORAGE_KEY) || "workbench";
  setActiveView(initialView);
  refreshJobs({ autoSelect: true });
});
