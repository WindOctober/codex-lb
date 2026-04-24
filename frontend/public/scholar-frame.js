const summaryText = document.getElementById("summary-text");
const alertStrip = document.getElementById("alert-strip");
const topicGrid = document.getElementById("topic-grid");
const statusPill = document.getElementById("status-pill");
const lastRefresh = document.getElementById("last-refresh");
const nextRefresh = document.getElementById("next-refresh");
const refreshButton = document.getElementById("refresh-button");

const STATUS_LABELS = { idle: "待机", ready: "就绪", refreshing: "刷新中", error: "异常" };

const isoFormat = (value) => {
  if (!value) return "未知";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value || "未知";
  return date.toLocaleString("zh-CN", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
};

const escapeHtml = (value = "") =>
  String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");

const setLoadingState = () => {
  topicGrid.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>';
};

const renderSummaryParagraphs = (value) => {
  const paragraphs = String(value || "")
    .split(/\n{2,}/)
    .map((item) => item.trim())
    .filter(Boolean);
  if (!paragraphs.length) {
    return "";
  }
  return paragraphs
    .map((paragraph) => `<p class="paper-summary">${escapeHtml(paragraph)}</p>`)
    .join("");
};

const renderPaperList = (papers, emptyText) => {
  if (!papers.length) {
    return `<div class="paper-card"><p>${escapeHtml(emptyText)}</p></div>`;
  }
  return papers
    .map(
      (item) => `
        <article class="paper-card">
          <div class="paper-meta">
            <span>${escapeHtml(item.venue || item.source_type || "未知来源")}</span>
            <span>${escapeHtml(item.published_at || "日期未知")}</span>
          </div>
          <h4><a href="${escapeHtml(item.url)}" target="_blank" rel="noreferrer">${escapeHtml(item.title)}</a></h4>
          <p class="paper-authors">${escapeHtml(item.authors || "")}</p>
          <div class="paper-summary-block">
            ${renderSummaryParagraphs(item.summary || item.why_it_matters || "")}
          </div>
          ${
            (item.technical_points || []).length
              ? `<div class="paper-points-block">
                  <div class="paper-points-label">技术要点</div>
                  <ul class="paper-points">
                    ${(item.technical_points || [])
                      .map((point) => `<li>${escapeHtml(point)}</li>`)
                      .join("")}
                  </ul>
                </div>`
              : ""
          }
          <p class="paper-why">${escapeHtml(item.why_it_matters || "")}</p>
        </article>
      `,
    )
    .join("");
};

const renderTopics = (topics) => {
  if (!topics.length) {
    topicGrid.innerHTML = '<div class="paper-card"><p>暂时还没有可展示的 Scholar topic。</p></div>';
    return;
  }
  topicGrid.innerHTML = topics
    .map(
      (topic) => `
        <section class="topic-card">
          <div class="topic-card-header">
            <div>
              <div class="section-label">Topic</div>
              <h3>${escapeHtml(topic.label)}</h3>
              <p>${escapeHtml(topic.why_track || "")}</p>
            </div>
          </div>
          <div class="topic-columns">
            <div class="topic-column">
              <div class="column-kicker">CCF-A 已发表</div>
              <div class="paper-list">${renderPaperList(topic.published || [], "当前还没有进入展示区的已发表论文。")}</div>
            </div>
            <div class="topic-column">
              <div class="column-kicker">最新预印本</div>
              <div class="paper-list">${renderPaperList(topic.preprints || [], "当前还没有进入展示区的预印本。")}</div>
            </div>
          </div>
        </section>
      `,
    )
    .join("");
};

const updateStatus = (payload) => {
  const status = payload.status || "idle";
  statusPill.textContent = STATUS_LABELS[status] || status;
  statusPill.classList.toggle("is-error", status === "error");
  statusPill.classList.toggle("is-stale", Boolean(payload.is_stale));
  lastRefresh.textContent = isoFormat(payload.last_completed_at || payload.generated_at);
  nextRefresh.textContent = isoFormat(payload.next_refresh_due_at);
};

const updateAlert = (payload) => {
  const parts = [];
  if (payload.is_stale) parts.push("当前展示的学术列表已经偏旧，系统正在尝试刷新。");
  if (payload.background_note) parts.push(payload.background_note);
  if (payload.last_error) parts.push(`最近一次刷新失败：${payload.last_error}`);
  if (!parts.length) {
    alertStrip.hidden = true;
    alertStrip.textContent = "";
    return;
  }
  alertStrip.hidden = false;
  alertStrip.textContent = parts.join(" ");
};

const fetchScholar = async () => {
  const response = await fetch("/api/scholar", { credentials: "same-origin", cache: "no-store" });
  if (!response.ok) throw new Error(`Failed to load scholar: ${response.status}`);
  return response.json();
};

const render = (payload) => {
  updateStatus(payload);
  updateAlert(payload);
  summaryText.textContent = payload.summary || "Scholar 面板正在生成中。";
  renderTopics(payload.topics || []);
};

const load = async () => {
  try {
    render(await fetchScholar());
  } catch (error) {
    summaryText.textContent = "Scholar 面板暂时无法拉取最新内容。";
    alertStrip.hidden = false;
    alertStrip.textContent = error instanceof Error ? error.message : String(error);
    topicGrid.innerHTML = "";
  }
};

refreshButton.addEventListener("click", async () => {
  refreshButton.disabled = true;
  refreshButton.textContent = "刷新中";
  try {
    await fetch("/api/scholar/refresh", { method: "POST", credentials: "same-origin" });
    await load();
  } finally {
    refreshButton.disabled = false;
    refreshButton.textContent = "立即刷新";
  }
});

setLoadingState();
void load();
window.setInterval(() => void load(), 60_000);
