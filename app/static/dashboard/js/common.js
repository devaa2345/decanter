// Shared dashboard runtime: Supabase auth, API fetch helper, theme toggle,
// and small hand-rolled chart primitives (stat tiles, bar lists, a stacked
// daily chart) built to the dataviz skill's mark specs — no charting
// library, this dashboard has no other external dependency besides
// supabase-js (loaded from a CDN, needed for auth only).

const SUPABASE_JS_URL = "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/+esm";

let _supabasePromise = null;
let _configPromise = null;

function getConfig() {
  if (!_configPromise) {
    _configPromise = fetch("/api/config").then((r) => r.json());
  }
  return _configPromise;
}

export function getSupabase() {
  if (!_supabasePromise) {
    _supabasePromise = getConfig().then(async (config) => {
      if (!config.configured) return null;
      const { createClient } = await import(SUPABASE_JS_URL);
      return createClient(config.supabase_url, config.supabase_anon_key);
    });
  }
  return _supabasePromise;
}

export async function getSession() {
  const supabase = await getSupabase();
  if (!supabase) return null;
  const { data } = await supabase.auth.getSession();
  return data.session;
}

export async function requireSession() {
  const session = await getSession();
  if (!session) {
    window.location.href = "/dashboard/login.html";
    return null;
  }
  return session;
}

export async function apiFetch(path, options = {}) {
  const session = await requireSession();
  if (!session) throw new Error("not authenticated");

  const isFormData = options.body instanceof FormData;
  const res = await fetch(path, {
    ...options,
    headers: {
      ...(options.headers || {}),
      Authorization: `Bearer ${session.access_token}`,
      ...(options.body && !isFormData ? { "Content-Type": "application/json" } : {}),
    },
  });

  if (res.status === 401 || res.status === 403) {
    window.location.href = "/dashboard/login.html";
    throw new Error("Session expired — please log in again");
  }

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch (_e) {
      /* no JSON body */
    }
    throw new Error(detail);
  }

  return res.json();
}

export async function logout() {
  const supabase = await getSupabase();
  if (supabase) await supabase.auth.signOut();
  window.location.href = "/dashboard/login.html";
}

// --- Theme toggle (app-level, independent of OS setting) -------------------

export function initTheme() {
  const saved = localStorage.getItem("dashboard-theme");
  if (saved) document.documentElement.setAttribute("data-theme", saved);
}

export function toggleTheme() {
  const current = document.documentElement.getAttribute("data-theme");
  const next = current === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("dashboard-theme", next);
}

export async function wireShellChrome() {
  initTheme();
  const themeBtn = document.getElementById("theme-toggle-btn");
  if (themeBtn) themeBtn.addEventListener("click", toggleTheme);
  const logoutBtn = document.getElementById("logout-btn");
  if (logoutBtn) logoutBtn.addEventListener("click", logout);

  // Every dashboard page (besides login) requires a session, even ones that
  // don't fetch data immediately on load (e.g. upload.html, which only
  // calls the API once a file is chosen) — check here so that's uniform
  // instead of relying on each page's own first API call to catch it.
  await requireSession();
}

// --- Formatting helpers ----------------------------------------------------

export function escapeHtml(s) {
  return String(s ?? "").replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]
  );
}

export function formatCompact(n) {
  if (n === null || n === undefined) return "—";
  if (Math.abs(n) >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, "") + "k";
  return String(n);
}

export function formatRupees(n) {
  if (n === null || n === undefined) return "—";
  return "₹" + Number(n).toLocaleString("en-IN");
}

export function formatDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
}

export function formatDateTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

// --- Chart primitives (per the dataviz skill: thin marks, legend for 2+
// series, direct labels used sparingly, hover tooltip, table-view escape
// hatch is just "the underlying table already on the page") -------------

export const OUTCOME_COLORS = {
  exact: "var(--series-1)",
  fuzzy: "var(--series-2)",
  llm: "var(--series-3)",
  ambiguous: "var(--status-warning)",
  unmatched: "var(--status-critical)",
};

export const OUTCOME_LABELS = {
  exact: "Exact match",
  fuzzy: "Fuzzy match",
  llm: "AI match",
  ambiguous: "Ambiguous",
  unmatched: "Unmatched",
};

export function statTileHtml({ label, value, sub, subClass }) {
  return `
    <div class="stat-tile">
      <p class="stat-label">${escapeHtml(label)}</p>
      <div class="stat-value">${value}</div>
      ${sub ? `<div class="stat-delta ${subClass || ""}">${sub}</div>` : ""}
    </div>
  `;
}

export function barListHtml(items, { labelKey = "label", valueKey = "value", max = null, color = "var(--series-1)" } = {}) {
  if (!items || !items.length) return `<div class="empty-state">No data yet</div>`;
  const m = max || Math.max(...items.map((i) => i[valueKey]), 1);
  return items
    .map(
      (i) => `
      <div class="bar-row">
        <div class="bar-label" title="${escapeHtml(i[labelKey])}">${escapeHtml(i[labelKey])}</div>
        <div class="bar-track"><div class="bar-fill" style="width:${Math.max(2, (i[valueKey] / m) * 100)}%; background:${i.color || color}"></div></div>
        <div class="bar-value">${formatCompact(i[valueKey])}</div>
      </div>`
    )
    .join("");
}

export function legendHtml(keys) {
  return `<div class="chart-legend">${keys
    .map((k) => `<span class="key"><span class="swatch" style="background:${OUTCOME_COLORS[k]}"></span>${OUTCOME_LABELS[k]}</span>`)
    .join("")}</div>`;
}

export function stackedChartHtml(rows, keys) {
  if (!rows || !rows.length) return `<div class="empty-state">No queries logged yet</div>`;
  const max = Math.max(...rows.map((r) => keys.reduce((s, k) => s + (r[k] || 0), 0)), 1);
  const cols = rows
    .map((r) => {
      const segs = keys
        .filter((k) => r[k] > 0)
        .map((k) => {
          const pct = (r[k] / max) * 100;
          return `<div class="stacked-seg" data-tip="${escapeHtml(formatDate(r.date))} — ${escapeHtml(OUTCOME_LABELS[k])}: ${r[k]}" style="height:${pct}%; background:${OUTCOME_COLORS[k]}"></div>`;
        })
        .join("");
      return `<div class="stacked-col">${segs}</div>`;
    })
    .join("");
  return `<div class="stacked-chart" id="stacked-chart-root">${cols}</div>
    <div class="stacked-axis"><span>${formatDate(rows[0].date)}</span><span>${formatDate(rows[rows.length - 1].date)}</span></div>`;
}

export function wireTooltips(root) {
  let tip = document.querySelector(".tooltip");
  if (!tip) {
    tip = document.createElement("div");
    tip.className = "tooltip";
    document.body.appendChild(tip);
  }
  root.addEventListener("mousemove", (e) => {
    const seg = e.target.closest("[data-tip]");
    if (!seg) {
      tip.style.display = "none";
      return;
    }
    tip.textContent = seg.dataset.tip;
    tip.style.display = "block";
    tip.style.left = e.clientX + 12 + "px";
    tip.style.top = e.clientY + 12 + "px";
  });
  root.addEventListener("mouseleave", () => {
    tip.style.display = "none";
  });
}
