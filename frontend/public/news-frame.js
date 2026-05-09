const summaryText = document.getElementById("summary-text");
const alertStrip = document.getElementById("alert-strip");
const companyGrid = document.getElementById("company-grid");
const rumorList = document.getElementById("rumor-list");
const viewTabs = [...document.querySelectorAll("[data-view]")];
const viewPanels = [...document.querySelectorAll("[data-view-panel]")];
const viewLede = document.getElementById("view-lede");
const hotspotHeadline = document.getElementById("hotspot-headline");
const hotspotTime = document.getElementById("hotspot-time");
const hotspotSummary = document.getElementById("hotspot-summary");
const hotspotClusters = document.getElementById("hotspot-clusters");
const hotspotList = document.getElementById("hotspot-list");
const statusLabel = document.getElementById("status-label");
const lastRefreshLabel = document.getElementById("last-refresh-label");
const nextRefreshLabel = document.getElementById("next-refresh-label");
const statusPill = document.getElementById("status-pill");
const lastRefresh = document.getElementById("last-refresh");
const nextRefresh = document.getElementById("next-refresh");
const refreshButton = document.getElementById("refresh-button");
const markReadButton = document.getElementById("mark-read-button");

markReadButton.disabled = true;

let activeView = "ai";
let currentPayload = null;
let refreshSubmitting = false;

const setActiveView = (view) => {
  activeView = view;
  viewTabs.forEach((tab) => {
    const selected = tab.dataset.view === view;
    tab.classList.toggle("is-active", selected);
    tab.setAttribute("aria-pressed", selected ? "true" : "false");
  });
  viewPanels.forEach((panel) => {
    const selected = panel.dataset.viewPanel === view;
    panel.classList.toggle("is-active", selected);
    panel.hidden = !selected;
  });
  if (currentPayload) {
    updateStatus(currentPayload);
    updateReadButton(currentPayload);
  }
};

const countTrendItems = (payload) => ((payload.hotspot_digest || {}).top_items || []).length;

const countViewNewItems = (payload, view = activeView) => {
  const items =
    view === "trend"
      ? ((payload.hotspot_digest || {}).top_items || [])
      : [...(payload.companies || []), ...(payload.rumors || [])];
  return items.filter((item) => item && item.is_new).length;
};

const countNewItems = (payload) => countViewNewItems(payload, "ai") + countViewNewItems(payload, "trend");

const STATUS_LABELS = {
  idle: "待机",
  ready: "就绪",
  refreshing: "刷新中",
  error: "异常",
};

const parseDate = (value) => {
  if (!value) return null;
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    return new Date(`${value}T00:00:00Z`);
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
};

const isoFormat = (value) => {
  if (!value) return "未知";
  const date = parseDate(value);
  if (!date) return value || "未知";
  return date.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
};

const escapeHtml = (value = "") =>
  value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");

const setLoadingState = () => {
  companyGrid.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div>';
  rumorList.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>';
  hotspotClusters.innerHTML = '<div class="mini-skeleton"></div><div class="mini-skeleton"></div>';
  hotspotList.innerHTML = "";
};

const renderHotspotDigest = (panel = {}) => {
  const clusters = panel.clusters || [];
  const topItems = panel.top_items || [];
  hotspotHeadline.textContent = panel.headline || "全网热点 Top20";
  hotspotTime.textContent = isoFormat(panel.generated_at);
  hotspotSummary.textContent = panel.summary || "TrendRadar 暂时还没有导出可展示的热点摘要。";
  hotspotClusters.innerHTML = clusters.length
    ? clusters
        .map(
          (cluster) => `
            <article class="hotspot-cluster">
              <div class="cluster-top">
                <strong>${escapeHtml(cluster.title)}</strong>
                <span>${escapeHtml(cluster.mood || "热度观察")}</span>
              </div>
              <p>${escapeHtml(cluster.summary)}</p>
            </article>
          `,
        )
        .join("")
    : '<div class="empty-note">等待 TrendRadar 聚类摘要。</div>';
  hotspotList.innerHTML = topItems.length
    ? topItems
        .map(
          (item) => `
            <li class="${item.is_new ? "is-new" : ""}">
              <a href="${escapeHtml(item.url)}" target="_blank" rel="noreferrer">${escapeHtml(item.title)}</a>
              <span>${escapeHtml(item.platform)} #${escapeHtml(String(item.rank || ""))}</span>
            </li>
          `,
        )
        .join("")
    : "";
};

const renderTrendStatus = (payload) => {
  return {
    itemCount: countTrendItems(payload),
    isRunning: Boolean(payload.trend_refresh_in_progress),
    hasError: Boolean(payload.last_trend_error),
    incomplete: Boolean(payload.last_trend_started_at && !payload.last_trend_completed_at && !countTrendItems(payload)),
  };
};

const renderCompanies = (companies) => {
  if (!companies.length) {
    companyGrid.innerHTML = '<div class="company-story"><p class="company-dek">暂时还没有可展示的已确认动态。</p></div>';
    return;
  }
  companyGrid.innerHTML = companies
    .map((item) => {
      const bullets = (item.bullets || [])
        .map((bullet) => `<li>${escapeHtml(bullet)}</li>`)
        .join("");
      const sources = (item.sources || [])
        .map(
          (source) => `
            <li>
              <a href="${escapeHtml(source.url)}" target="_blank" rel="noreferrer">${escapeHtml(source.title)}</a>
              <span class="source-line">${escapeHtml(source.publisher)} • <time class="source-time">${escapeHtml(isoFormat(source.published_at))}</time> • ${escapeHtml(source.source_type)}</span>
            </li>
          `,
        )
        .join("");
      return `
        <article class="company-story${item.is_new ? " is-new" : ""}">
          <div class="story-topline">
            <div class="company-kicker">${escapeHtml(item.company)}</div>
            ${item.is_new ? '<span class="new-badge">NEW</span>' : ""}
          </div>
          <h3>${escapeHtml(item.headline)}</h3>
          <p class="company-dek">${escapeHtml(item.dek)}</p>
          <div class="story-meta">
            <span>${escapeHtml(item.theme || "当前动态")}</span>
            <span>${escapeHtml(item.confidence || "待确认")}</span>
          </div>
          <ul class="company-points">${bullets}</ul>
          <ul class="story-sources">${sources}</ul>
        </article>
      `;
    })
    .join("");
};

const renderRumors = (rumors) => {
  if (!rumors.length) {
    rumorList.innerHTML = '<div class="rumor-item"><p class="rumor-summary">当前还没有进入展示区的未证实高热帖子。</p></div>';
    return;
  }
  rumorList.innerHTML = rumors
    .map(
      (item) => `
        <article class="rumor-item${item.is_new ? " is-new" : ""}">
          <div class="rumor-meta">
            <span>${escapeHtml(item.display_name)} ${escapeHtml(item.handle)}</span>
            <time class="rumor-time">${escapeHtml(isoFormat(item.posted_at))}</time>
          </div>
          ${item.is_new ? '<div class="rumor-badge-row"><span class="new-badge">NEW SIGNAL</span></div>' : ""}
          <h3 class="rumor-title"><a href="${escapeHtml(item.url)}" target="_blank" rel="noreferrer">${escapeHtml(item.headline)}</a></h3>
          <p class="rumor-summary">${escapeHtml(item.summary)}</p>
          <p class="rumor-why">${escapeHtml(item.why_it_matters)}</p>
          <div class="story-meta">
            <span>${escapeHtml(item.engagement_hint)}</span>
            <span>${escapeHtml(item.verification_status)}</span>
          </div>
        </article>
      `,
    )
    .join("");
};

const updateStatus = (payload) => {
  const isTrend = activeView === "trend";
  const trend = renderTrendStatus(payload);
  const newsStatus = payload.status || "idle";
  const trendStatus = trend.isRunning
    ? "refreshing"
    : trend.hasError
      ? "error"
      : trend.itemCount
        ? "ready"
        : trend.incomplete
          ? "idle"
          : "idle";
  const status = isTrend ? trendStatus : newsStatus;

  viewLede.textContent = isTrend
    ? "Trend 全网热点按小时独立刷新，聚合多平台 Top20 并提炼时事主线。"
    : "X AI 热讯聚焦 OpenAI、Anthropic 和 X 上高热 AI 信号。";
  statusLabel.textContent = isTrend ? "Trend 状态" : "X AI 状态";
  lastRefreshLabel.textContent = isTrend ? "Trend 最近完成" : "最近刷新";
  nextRefreshLabel.textContent = isTrend ? "Trend 下次计划" : "下次计划";
  statusPill.textContent = isTrend && trend.incomplete ? "未完成" : STATUS_LABELS[status] || status;
  statusPill.classList.toggle("is-error", isTrend ? trend.hasError : newsStatus === "error");
  statusPill.classList.toggle("is-stale", isTrend ? trend.incomplete : Boolean(payload.is_stale));
  lastRefresh.textContent = isTrend
    ? isoFormat(payload.last_trend_completed_at || (payload.hotspot_digest || {}).generated_at)
    : isoFormat(payload.last_completed_at || payload.generated_at);
  nextRefresh.textContent = isTrend ? isoFormat(payload.next_trend_refresh_due_at) : isoFormat(payload.next_refresh_due_at);

  if (!refreshSubmitting) {
    const isRefreshing = isTrend ? trend.isRunning : newsStatus === "refreshing";
    refreshButton.disabled = isRefreshing;
    refreshButton.textContent = isTrend ? (isRefreshing ? "Trend 刷新中" : "刷新 Trend") : isRefreshing ? "刷新中" : "立即刷新";
  }
};

const updateReadButton = (payload) => {
  const newItems = countViewNewItems(payload);
  markReadButton.disabled = newItems === 0;
  const scope = activeView === "trend" ? "Trend" : "X AI";
  markReadButton.textContent = newItems ? `标记 ${newItems} 条为已读` : `${scope} 已读`;
};

const updateAlert = (payload) => {
  const parts = [];
  const newItems = countNewItems(payload);
  if (newItems) parts.push(`当前有 ${newItems} 条未读新条目，已用高亮标出，可右上角一键标记已读。`);
  if (payload.is_stale) parts.push("当前展示的内容已经偏旧，系统正在尝试刷新到更新的一版。");
  if (payload.background_note) parts.push(payload.background_note);
  if (payload.last_error) parts.push(`最近一次刷新失败：${payload.last_error}`);
  if (payload.last_trend_error) parts.push(`TrendRadar 热点刷新失败：${payload.last_trend_error}`);
  if (!parts.length) {
    alertStrip.hidden = true;
    alertStrip.textContent = "";
    return;
  }
  alertStrip.hidden = false;
  alertStrip.textContent = parts.join(" ");
};

const fetchNews = async () => {
  const response = await fetch("/api/news", { credentials: "same-origin", cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Failed to load news: ${response.status}`);
  }
  return response.json();
};

const render = (payload) => {
  currentPayload = payload;
  updateStatus(payload);
  updateReadButton(payload);
  updateAlert(payload);
  summaryText.textContent = payload.summary || "情报简报正在生成中。";
  renderTrendStatus(payload);
  renderHotspotDigest(payload.hotspot_digest || {});
  renderCompanies(payload.companies || []);
  renderRumors(payload.rumors || []);
};

const load = async () => {
  try {
    const payload = await fetchNews();
    render(payload);
  } catch (error) {
    summaryText.textContent = "情报页暂时无法拉取最新内容。";
    alertStrip.hidden = false;
    alertStrip.textContent = error instanceof Error ? error.message : String(error);
    companyGrid.innerHTML = "";
    rumorList.innerHTML = "";
    hotspotClusters.innerHTML = "";
    hotspotList.innerHTML = "";
    markReadButton.disabled = true;
    markReadButton.textContent = "标记当前已读";
  }
};

refreshButton.addEventListener("click", async () => {
  const isTrend = activeView === "trend";
  const endpoint = isTrend ? "/api/news/trend-refresh" : "/api/news/refresh";
  refreshButton.disabled = true;
  refreshSubmitting = true;
  refreshButton.textContent = isTrend ? "Trend 刷新中" : "刷新中";
  try {
    await fetch(endpoint, {
      method: "POST",
      credentials: "same-origin",
    });
    await load();
  } finally {
    refreshSubmitting = false;
    updateStatus(currentPayload || {});
  }
});

markReadButton.addEventListener("click", async () => {
  markReadButton.disabled = true;
  const previousText = markReadButton.textContent;
  markReadButton.textContent = "提交中";
  try {
    const response = await fetch("/api/news/mark-read", {
      method: "POST",
      credentials: "same-origin",
    });
    if (!response.ok) {
      throw new Error(`Failed to mark news read: ${response.status}`);
    }
    await load();
  } catch (error) {
    alertStrip.hidden = false;
    alertStrip.textContent = error instanceof Error ? error.message : String(error);
    markReadButton.textContent = previousText;
    markReadButton.disabled = false;
  }
});

viewTabs.forEach((tab) => {
  tab.addEventListener("click", () => {
    setActiveView(tab.dataset.view || "ai");
  });
});

setActiveView(activeView);
setLoadingState();
void load();
window.setInterval(() => {
  void load();
}, 60_000);
