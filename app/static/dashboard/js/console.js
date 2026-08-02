/*
 * Sovereign Scents console — one page for everything the owner does.
 *
 * Deliberately a single view-swapping page rather than seven HTML files:
 * the catalog, the tester and the upload report are all about the same
 * 1,354 products, and moving between them should not lose what you were
 * looking at. State lives here; each view renders from it.
 *
 * No framework and no build step. The only external dependency in this
 * whole dashboard is supabase-js, loaded for auth alone (see common.js) —
 * everything below is small enough not to earn another.
 */

import { apiFetch, requireSession, getSupabase } from "/dashboard/js/common.js";

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, props = {}, ...kids) => {
  // dataset is a read-only DOMStringMap, so Object.assign silently drops it
  // — which cost the size inputs their size and posted prices keyed
  // "undefined". Split out and copied key by key.
  const { dataset, ...rest } = props;
  const node = Object.assign(document.createElement(tag), rest);
  if (dataset) for (const [k, v] of Object.entries(dataset)) node.dataset[k] = v;
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return node;
};
const money = (n) => "₹" + Number(n).toLocaleString("en-IN");

const PREF_KEYS = {
  theme: "ss.theme",
  density: "ss.density",
  view: "ss.view",
  groq: "ss.tester.groq",
  diag: "ss.tester.diag",
  pageSize: "ss.catalog.pageSize",
};
const pref = {
  get: (k, fallback) => localStorage.getItem(PREF_KEYS[k]) ?? fallback,
  set: (k, v) => localStorage.setItem(PREF_KEYS[k], v),
};

const state = {
  view: pref.get("view", "overview"),
  status: null,
  catalog: { items: [], total: 0, q: "", offset: 0, limit: Number(pref.get("pageSize", 50)) },
  selected: new Set(),
  brands: [],
  sizes: ["3ml", "5ml", "8ml", "10ml", "20ml", "30ml"],
  upload: null,
  uploadTab: "summary",
  messages: null,
  msgKey: "welcome",
  msgDraft: {},
  msgVerdict: {},
  thread: [],
  editing: null,
};

/* --- Chrome ------------------------------------------------------------- */

function toast(message, tone = "ok", ms = 4200) {
  const node = el("div", { className: `toast ${tone}` }, message);
  $("#toasts").append(node);
  setTimeout(() => node.remove(), ms);
}

function applyTheme() {
  const mode = pref.get("theme", "auto");
  document.documentElement.setAttribute("data-theme", mode === "auto" ? "" : mode);
  if (mode === "auto") document.documentElement.removeAttribute("data-theme");
  document.documentElement.setAttribute("data-density", pref.get("density", "normal"));
}

function renderStatusPills() {
  const s = state.status;
  const box = $("#status-pills");
  box.replaceChildren();
  if (!s) return;

  const pill = (label, ok, title) =>
    el("span", { className: `pill ${ok ? "ok" : "warn"}`, title }, el("span", { className: "dot" }), label);

  box.append(pill(`${s.catalog_size.toLocaleString()} perfumes`, true, "Products the bot can price"));
  box.append(pill("Groq", s.groq_configured, s.groq_configured ? "LLM layer active" : "No GROQ_API_KEY — the deterministic matcher is doing all the work"));
  box.append(pill("WhatsApp", s.whatsapp_configured, s.whatsapp_configured ? "Sovereign chat bot can send replies" : "No WhatsApp API token — the Sovereign chat bot cannot send anything"));
  const durable = s.handoff_pause?.durable;
  box.append(
    el(
      "span",
      { className: `pill ${durable ? "ok" : "bad"}`, title: s.handoff_pause?.detail || "" },
      el("span", { className: "dot" }),
      durable ? "Handoff pause" : "Handoff not durable"
    )
  );
  $("#nav-count").textContent = s.catalog_size.toLocaleString();
}

/* --- Views -------------------------------------------------------------- */

const TITLES = {
  overview: "Overview",
  catalog: "Catalog",
  add: "Add a perfume",
  upload: "Upload the sheet",
  messages: "Bot messages",
  tester: "Chat tester",
  settings: "Settings",
};

async function go(view) {
  state.view = view;
  pref.set("view", view);
  $("#view-title").textContent = TITLES[view] || view;
  for (const b of $("#nav").querySelectorAll("button")) {
    b.toggleAttribute("aria-current", b.dataset.view === view);
    if (b.dataset.view === view) b.setAttribute("aria-current", "page");
  }
  const content = $("#content");
  content.replaceChildren(el("div", { className: "empty" }, el("span", { className: "spinner" })));
  try {
    await VIEWS[view](content);
  } catch (err) {
    content.replaceChildren(
      el("div", { className: "note bad" }, `Could not load this view: ${err.message}`)
    );
  }
}

const VIEWS = {};

VIEWS.overview = async (root) => {
  const [overview, status] = await Promise.all([
    apiFetch("/api/admin/metrics/overview?days=30").catch(() => null),
    apiFetch("/api/admin/status"),
  ]);
  state.status = status;
  renderStatusPills();

  const tiles = el("div", { className: "grid tiles" });
  const tile = (n, l, tone = "") =>
    el("div", { className: `card stat ${tone}` }, el("span", { className: "n" }, n), el("span", { className: "l" }, l));

  tiles.append(tile(status.catalog_size.toLocaleString(), "Perfumes the bot can price"));
  if (overview) {
    tiles.append(tile(overview.total_queries?.toLocaleString() ?? "—", "Customer messages, 30 days"));
    tiles.append(tile(overview.match_rate != null ? overview.match_rate + "%" : "—", "Matched to a product", "good"));
    tiles.append(tile(overview.unmatched?.toLocaleString() ?? "—", "Unmatched — possible catalog gaps", overview.unmatched > 0 ? "warn" : ""));
  }
  root.replaceChildren(tiles);

  if (!overview) {
    root.append(
      el(
        "div",
        { className: "note info", style: "margin-top:.9rem" },
        "Analytics need Supabase configured. Everything else on this console works without it."
      )
    );
  }

  const checks = el("div", { className: "card", style: "margin-top:.9rem" }, el("h3", {}, "Is the bot actually working?"));
  const line = (ok, label, detail) =>
    el(
      "div",
      { className: "row-flex", style: "padding:.35rem 0;border-bottom:1px solid var(--rule)" },
      el("span", { className: `pill ${ok ? "ok" : "bad"}` }, el("span", { className: "dot" }), ok ? "yes" : "no"),
      el("strong", { style: "min-width:14rem" }, label),
      el("span", { className: "muted", style: "flex:1;min-width:12rem" }, detail)
    );
  checks.append(
    line(state.status.whatsapp_configured, "Sovereign chat bot can reply", state.status.whatsapp_configured ? "The WhatsApp API token is set" : "The WhatsApp API token is missing — no reply can leave the server"),
    line(state.status.groq_configured, "LLM layer", state.status.groq_configured ? "Groq is configured" : "No key — the deterministic matcher answers alone, which it is built to do"),
    line(state.status.webhook_secret_set, "Webhook signature check", state.status.webhook_secret_set ? "Inbound webhooks are verified" : "Unverified — fine locally, set it before go-live"),
    line(state.status.supabase_configured, "Analytics + history", state.status.supabase_configured ? "Supabase is connected" : "Not configured — analytics, versions and a durable handoff pause are all off"),
    line(state.status.handoff_pause?.durable, "Handoff pause survives a restart", state.status.handoff_pause?.detail || "")
  );
  root.append(checks);
};

VIEWS.catalog = async (root) => {
  root.replaceChildren();
  const toolbar = el("div", { className: "toolbar" });
  const search = el("input", {
    type: "search",
    className: "grow",
    placeholder: "Search by name or brand…",
    value: state.catalog.q,
  });
  let timer;
  search.oninput = () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      state.catalog.q = search.value;
      state.catalog.offset = 0;
      loadCatalog(body);
    }, 220);
  };
  const perPage = el("select", { style: "max-width:8rem" });
  for (const n of [25, 50, 100, 200]) {
    perPage.append(el("option", { value: String(n), selected: state.catalog.limit === n }, `${n} / page`));
  }
  perPage.onchange = () => {
    state.catalog.limit = Number(perPage.value);
    state.catalog.offset = 0;
    pref.set("pageSize", perPage.value);
    loadCatalog(body);
  };
  toolbar.append(search, perPage, el("button", { className: "btn", onclick: () => go("add") }, "＋ Add perfume"));
  root.append(toolbar);

  const selbar = el("div", { className: "selbar hidden" });
  root.append(selbar);

  const body = el("div");
  root.append(body);
  await loadCatalog(body, selbar);
};

// A column per size, so 1,354 rows can be compared down the page instead of
// read across. The columns are fixed — every decant tier the shop sells plus
// one for the bottle — because a column that appears and disappears with the
// page you happen to be on is not a column you can scan.
const SIZE_COLS = ["3ml", "5ml", "8ml", "10ml", "20ml", "30ml"];
const fullKeyOf = (prices) => Object.keys(prices || {}).find((k) => k.endsWith("_full"));

async function loadCatalog(body, selbar = $(".selbar")) {
  const { q, limit, offset } = state.catalog;
  body.replaceChildren(el("div", { className: "empty" }, el("span", { className: "spinner" })));
  const data = await apiFetch(
    `/api/admin/catalog?q=${encodeURIComponent(q)}&limit=${limit}&offset=${offset}`
  );
  state.catalog.items = data.items;
  state.catalog.total = data.total;

  const table = el("table");
  const head = el("tr");
  const all = el("input", { type: "checkbox" });
  all.onchange = () => {
    for (const item of data.items) {
      if (all.checked) state.selected.add(item.perfume_id);
      else state.selected.delete(item.perfume_id);
    }
    renderRows();
  };
  head.append(
    el("th", { style: "width:2rem" }, all),
    el("th", {}, "Perfume"),
    el("th", {}, "Brand"),
    ...SIZE_COLS.map((s) => el("th", { className: "num" }, s)),
    el("th", { className: "num" }, "Bottle"),
    el("th", { style: "width:4rem" }, "")
  );
  const tbody = el("tbody");
  table.append(el("thead", {}, head), tbody);

  function renderRows() {
    tbody.replaceChildren();
    for (const item of data.items) {
      const checked = state.selected.has(item.perfume_id);
      const box = el("input", { type: "checkbox", checked });
      box.onchange = () => {
        if (box.checked) state.selected.add(item.perfume_id);
        else state.selected.delete(item.perfume_id);
        renderRows();
      };
      const prices = item.prices || {};
      const fullKey = fullKeyOf(prices);
      const cells = SIZE_COLS.map((size) =>
        el(
          "td",
          { className: "num" + (prices[size] == null ? " muted" : "") },
          prices[size] == null ? "·" : money(prices[size])
        )
      );
      const bottle = fullKey
        ? el(
            "td",
            { className: "num" },
            money(prices[fullKey]),
            el("div", { className: "muted", style: "font-size:.68rem" }, fullKey.replace("ml_full", "ml") || "full")
          )
        : el("td", { className: "num muted" }, "·");

      tbody.append(
        el(
          "tr",
          { className: checked ? "selected" : "" },
          el("td", {}, box),
          el(
            "td",
            {},
            el("strong", {}, item.display_name),
            item.clone_of
              ? el("div", { className: "muted", style: "font-size:.72rem" }, `clone of ${item.clone_of}`)
              : null
          ),
          el("td", { className: "muted" }, item.brand || "—"),
          ...cells,
          bottle,
          el("td", {}, el("button", { className: "btn sm ghost", onclick: () => openEditor(item) }, "Edit"))
        )
      );
    }
    updateSelbar();
  }

  function updateSelbar() {
    if (!selbar) return;
    const n = state.selected.size;
    selbar.classList.toggle("hidden", n === 0);
    if (!n) return;
    selbar.replaceChildren(
      el("strong", {}, `${n} selected`),
      el("button", { className: "btn sm", onclick: () => { state.selected.clear(); renderRows(); } }, "Clear"),
      el("span", { style: "flex:1" }),
      el("button", { className: "btn sm", onclick: () => openCopyCard() }, "⧉ Copy card"),
      el("button", { className: "btn sm", onclick: () => openBulkEdit(body, selbar) }, "✎ Edit all selected"),
      el("button", { className: "btn sm danger", onclick: () => bulkDelete(body, selbar) }, "Remove selected")
    );
  }

  renderRows();

  const showing = `${offset + 1}–${Math.min(offset + limit, data.total)} of ${data.total.toLocaleString()}`;
  const pager = el(
    "div",
    { className: "toolbar", style: "margin-top:.6rem" },
    el("span", { className: "muted mono" }, data.total ? showing : "nothing matched"),
    el("span", { style: "flex:1" }),
    el("button", {
      className: "btn sm",
      disabled: offset === 0,
      onclick: () => { state.catalog.offset = Math.max(0, offset - limit); loadCatalog(body, selbar); },
    }, "← Previous"),
    el("button", {
      className: "btn sm",
      disabled: offset + limit >= data.total,
      onclick: () => { state.catalog.offset = offset + limit; loadCatalog(body, selbar); },
    }, "Next →")
  );

  body.replaceChildren(
    data.items.length
      ? el("div", { className: "table-wrap" }, table)
      : el("div", { className: "empty" }, "No perfume matches that."),
    pager
  );
}

/* --- Copy card ----------------------------------------------------------- */

function openCopyCard() {
  // Rendered by the server through the same formatter the bot uses, so what
  // gets pasted into a chat is character-for-character what a customer would
  // have received. Building it here would be a second formatter, and the
  // first anyone would hear of the drift is a customer quoted oddly.
  const dlg = $("#card-dialog");
  const sizeBox = $("#card-sizes");
  const out = $("#card-text");
  const meta = $("#card-meta");
  const chosen = new Set();

  sizeBox.replaceChildren(
    ...["3ml", "5ml", "8ml", "10ml", "20ml", "30ml", "bottle"].map((size) => {
      const key = size === "bottle" ? "100ml_full" : size;
      const cb = el("input", { type: "checkbox" });
      cb.onchange = () => {
        if (cb.checked) chosen.add(key);
        else chosen.delete(key);
        refresh();
      };
      return el("label", { className: "switch" }, cb, size);
    })
  );

  async function refresh() {
    out.textContent = "…";
    try {
      const res = await apiFetch("/api/admin/catalog/card", {
        method: "POST",
        body: JSON.stringify({
          perfume_ids: [...state.selected],
          sizes: chosen.size ? [...chosen] : null,
        }),
      });
      out.textContent = res.text;
      meta.replaceChildren(
        el("span", { className: "muted" }, `${res.perfumes} perfume(s)`),
        res.dropped?.length
          ? el("span", { className: "pill warn" }, `${res.dropped.length} left out — no such size`)
          : null
      );
    } catch (err) {
      out.textContent = "";
      meta.replaceChildren(el("span", { className: "pill bad" }, err.message));
    }
  }

  $("#card-copy").onclick = async () => {
    try {
      await navigator.clipboard.writeText(out.textContent);
      toast("Card copied — paste it straight into the chat.", "ok");
    } catch {
      // Clipboard permission can be refused; selecting the text is a fallback
      // that always works and needs no permission at all.
      const range = document.createRange();
      range.selectNodeContents(out);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      toast("Selected the card — press Ctrl+C to copy.", "warn", 6000);
    }
  };
  for (const b of dlg.querySelectorAll("[data-close]")) b.onclick = () => dlg.close();

  refresh();
  dlg.showModal();
}

/* --- Bulk edit ------------------------------------------------------------ */

function openBulkEdit(body, selbar) {
  // Operations, not field values. Nobody bulk-renames 40 perfumes; they put
  // the 3ml up by ten rupees, or stop selling 20ml. See catalog_edit.bulk_update.
  const dlg = $("#bulk-dialog");
  const n = state.selected.size;
  $("#bulk-count").textContent = `${n} perfume${n === 1 ? "" : "s"} selected`;

  const setBox = $("#bulk-set-prices");
  setBox.replaceChildren(
    ...["3ml", "5ml", "8ml", "10ml", "20ml", "30ml"].map((size) =>
      el(
        "div",
        { className: "size-box" },
        el("label", {}, size),
        el("input", { type: "number", min: "1", placeholder: "leave blank", dataset: { size } })
      )
    )
  );

  const removeBox = $("#bulk-remove-sizes");
  removeBox.replaceChildren(
    ...["3ml", "5ml", "8ml", "10ml", "20ml", "30ml"].map((size) =>
      el("label", { className: "switch" }, el("input", { type: "checkbox", dataset: { size } }), size)
    )
  );

  $("#bulk-pct").value = "";
  $("#bulk-flat").value = "";
  $("#bulk-brand").value = "";
  $("#bulk-result").replaceChildren();

  $("#bulk-apply").onclick = async () => {
    const ops = {};
    const setPrice = {};
    for (const input of setBox.querySelectorAll("input")) {
      if (input.value !== "") setPrice[input.dataset.size] = input.value;
    }
    if (Object.keys(setPrice).length) ops.set_price = setPrice;
    const remove = [...removeBox.querySelectorAll("input:checked")].map((i) => i.dataset.size);
    if (remove.length) ops.remove_sizes = remove;
    if ($("#bulk-pct").value) ops.adjust_pct = Number($("#bulk-pct").value);
    if ($("#bulk-flat").value) ops.adjust_flat = Number($("#bulk-flat").value);
    if ($("#bulk-brand").value.trim()) ops.set_brand = $("#bulk-brand").value.trim();

    const summary = [];
    if (ops.adjust_pct) summary.push(`every price ${ops.adjust_pct > 0 ? "up" : "down"} ${Math.abs(ops.adjust_pct)}%`);
    if (ops.adjust_flat) summary.push(`every price ${ops.adjust_flat > 0 ? "up" : "down"} ₹${Math.abs(ops.adjust_flat)}`);
    if (ops.set_price) summary.push(`set ${Object.entries(ops.set_price).map(([s, p]) => `${s}=₹${p}`).join(", ")}`);
    if (ops.remove_sizes) summary.push(`stop selling ${ops.remove_sizes.join(", ")}`);
    if (ops.set_brand) summary.push(`brand → ${ops.set_brand}`);

    if (!summary.length) return toast("Pick at least one change to apply.", "warn");
    if (!confirm(`Apply to ${n} perfume(s)?\n\n· ${summary.join("\n· ")}\n\nThis changes what customers are quoted straight away.`)) return;

    try {
      const res = await apiFetch("/api/admin/catalog/bulk-edit", {
        method: "POST",
        body: JSON.stringify({ perfume_ids: [...state.selected], ops }),
      });
      $("#bulk-result").replaceChildren(
        el(
          "div",
          { className: res.refused ? "note warn" : "note ok" },
          `${res.selected} selected · ${res.changed} changed · ${res.refused} left alone.`,
          res.refused
            ? el("ul", {}, ...res.refused_items.map((r) => el("li", {}, `${r.display_name} — ${r.reason}`)))
            : null
        )
      );
      toast(`${res.changed} perfume(s) updated.`, "ok");
      await loadCatalog(body, selbar);
    } catch (err) {
      $("#bulk-result").replaceChildren(el("div", { className: "note bad" }, err.message));
    }
  };
  for (const b of dlg.querySelectorAll("[data-close]")) b.onclick = () => dlg.close();
  dlg.showModal();
}


async function bulkDelete(body, selbar) {
  const ids = [...state.selected];
  const names = state.catalog.items
    .filter((i) => state.selected.has(i.perfume_id))
    .map((i) => i.display_name);
  const preview = names.slice(0, 5).join(", ") + (names.length > 5 ? `, and ${names.length - 5} more` : "");
  if (!confirm(`Remove ${ids.length} perfume(s) from the catalog?\n\n${preview}\n\nCustomers asking for these will get no reply.`)) return;
  try {
    const res = await apiFetch("/api/admin/catalog/delete", {
      method: "POST",
      body: JSON.stringify({ perfume_ids: ids }),
    });
    state.selected.clear();
    toast(`Removed ${res.removed} perfume(s).`, "ok");
    await refreshStatus();
    await loadCatalog(body, selbar);
  } catch (err) {
    toast(err.message, "bad", 8000);
  }
}

VIEWS.add = async (root) => {
  await ensureBrands();
  root.replaceChildren();
  root.append(
    el(
      "div",
      { className: "note info", style: "margin-bottom:.9rem" },
      "Added here, a perfume is matched by exactly the same rules as the other ",
      String(state.status?.catalog_size ?? ""),
      " — same keywords, same typo tolerance. It is sellable the moment you save."
    )
  );
  openEditor(null, () => go("catalog"));
};

function sizeInputs(container, prices = {}) {
  container.replaceChildren();
  for (const size of state.sizes) {
    const input = el("input", {
      type: "number",
      min: "1",
      placeholder: "—",
      value: prices[size] ?? "",
      dataset: { size },
    });
    container.append(el("div", { className: "size-box" }, el("label", {}, size), input));
  }
}

function openEditor(item, onDone) {
  const dlg = $("#edit-dialog");
  state.editing = item;
  $("#edit-title").textContent = item ? "Edit perfume" : "Add a perfume";
  $("#edit-warning").replaceChildren();

  const brand = $("#edit-brand");
  const name = $("#edit-name");
  const clone = $("#edit-clone");
  brand.value = item?.brand || "";
  let bare = item?.display_name || "";
  if (item?.brand && bare.startsWith(item.brand)) bare = bare.slice(item.brand.length).trim();
  name.value = bare;
  clone.value = item?.clone_of || "";

  const prices = { ...(item?.prices || {}) };
  const fullKey = Object.keys(prices).find((k) => k.endsWith("_full"));
  $("#edit-full-ml").value = fullKey ? fullKey.replace("ml_full", "") : "";
  $("#edit-full-price").value = fullKey ? prices[fullKey] : "";
  sizeInputs($("#edit-sizes"), prices);

  let checkTimer;
  const checkDuplicate = async () => {
    const display = `${brand.value} ${name.value}`.trim();
    const warn = $("#edit-warning");
    if (!display) return warn.replaceChildren();
    const res = await apiFetch("/api/admin/catalog/check-duplicate", {
      method: "POST",
      body: JSON.stringify({ display_name: display, ignore_id: item?.perfume_id ?? null }),
    }).catch(() => null);
    warn.replaceChildren(
      res?.duplicate
        ? el("div", { className: "note bad" }, `"${res.display_name}" is already in the catalog. Saving is blocked — edit that one instead if the prices changed.`)
        : ""
    );
    $("#edit-save").disabled = Boolean(res?.duplicate);
  };
  for (const input of [brand, name]) {
    input.oninput = () => {
      clearTimeout(checkTimer);
      checkTimer = setTimeout(checkDuplicate, 300);
    };
  }
  $("#edit-save").disabled = false;

  $("#edit-save").onclick = async () => {
    const payload = { brand: brand.value.trim(), name: name.value.trim(), clone_of: clone.value.trim() || null, prices: {} };
    for (const input of $("#edit-sizes").querySelectorAll("input")) {
      if (input.value !== "") payload.prices[input.dataset.size] = input.value;
    }
    const fullMl = $("#edit-full-ml").value.trim();
    const fullPrice = $("#edit-full-price").value.trim();
    if (fullPrice) payload.prices[(fullMl ? fullMl + "ml_full" : "ml_full")] = fullPrice;

    try {
      if (item) {
        await apiFetch(`/api/admin/catalog/perfume/${encodeURIComponent(item.perfume_id)}`, {
          method: "PUT",
          body: JSON.stringify(payload),
        });
        toast(`Saved ${payload.brand} ${payload.name}.`.trim(), "ok");
      } else {
        await apiFetch("/api/admin/catalog/perfume", { method: "POST", body: JSON.stringify(payload) });
        toast(`Added ${payload.brand} ${payload.name}`.trim() + " — it is sellable now.", "ok");
      }
      dlg.close();
      await refreshStatus();
      if (onDone) onDone();
      else if (state.view === "catalog") loadCatalog($("#content").lastChild);
    } catch (err) {
      toast(err.message, "bad", 9000);
    }
  };

  for (const b of dlg.querySelectorAll("[data-close]")) b.onclick = () => dlg.close();
  dlg.showModal();
}

async function ensureBrands() {
  if (state.brands.length) return;
  const data = await apiFetch("/api/admin/catalog/brands");
  state.brands = data.brands;
  state.sizes = data.sizes;
  $("#brand-list").replaceChildren(...data.brands.map((b) => el("option", { value: b })));
}

/* --- Upload -------------------------------------------------------------- */

VIEWS.upload = async (root) => {
  root.replaceChildren();

  const zone = el(
    "div",
    { className: "dropzone" },
    el("div", { style: "font-size:1.5rem" }, "↥"),
    el("div", { style: "margin-top:.4rem" }, "Drop the Decant Sheet here, or click to choose"),
    el("div", { className: "muted", style: "font-size:.78rem;margin-top:.3rem" }, ".xlsx keeps every tab — a .csv is only the one tab you exported")
  );
  const picker = el("input", { type: "file", accept: ".xlsx,.xlsm,.csv", className: "hidden" });
  zone.onclick = () => picker.click();
  zone.ondragover = (e) => { e.preventDefault(); zone.classList.add("over"); };
  zone.ondragleave = () => zone.classList.remove("over");
  zone.ondrop = (e) => {
    e.preventDefault();
    zone.classList.remove("over");
    if (e.dataTransfer.files[0]) doUpload(e.dataTransfer.files[0], root);
  };
  picker.onchange = () => picker.files[0] && doUpload(picker.files[0], root);

  root.append(
    el("div", { className: "note info", style: "margin-bottom:.9rem" },
      el("strong", {}, "Nothing goes live until you publish. "),
      "The upload is parsed and staged, and you see exactly what would change first."
    ),
    zone,
    picker,
    el("div", { id: "upload-result", style: "margin-top:1rem" })
  );

  if (state.upload) renderUploadReport($("#upload-result"));
};

async function doUpload(file, root) {
  const out = $("#upload-result");
  out.replaceChildren(el("div", { className: "empty" }, el("span", { className: "spinner" }), " Reading every tab…"));
  const form = new FormData();
  form.append("file", file);
  try {
    state.upload = await apiFetch("/api/admin/catalog/upload", { method: "POST", body: form });
    state.uploadTab = "summary";
    renderUploadReport(out);
  } catch (err) {
    out.replaceChildren(el("div", { className: "note bad" }, err.message));
  }
}

function renderUploadReport(out) {
  const v = state.upload;
  const diff = v.diff || {};
  const report = diff.sheet_report || {};
  out.replaceChildren();

  const tiles = el("div", { className: "grid tiles" });
  const tile = (n, l, tone = "") =>
    el("div", { className: `card stat ${tone}` }, el("span", { className: "n" }, n), el("span", { className: "l" }, l));
  tiles.append(
    tile(v.perfume_count?.toLocaleString() ?? "—", "Perfumes in this version"),
    tile(v.added_count ?? 0, "New", v.added_count ? "good" : ""),
    tile(v.updated_count ?? 0, "Price changes", v.updated_count ? "warn" : ""),
    tile(v.removed_count ?? 0, "Would be removed", v.removed_count ? "bad" : ""),
    tile(report.duplicate_rows_dropped ?? 0, "Duplicate rows skipped", report.duplicate_rows_dropped ? "warn" : "")
  );
  out.append(tiles);

  out.append(
    el(
      "div",
      { className: "note ok", style: "margin:.9rem 0" },
      `You gave me ${(report.products_after_merge ?? v.perfume_count ?? 0).toLocaleString()} perfumes across ${(report.sheets_read || []).length} tab(s). `,
      `${report.duplicate_rows_dropped ?? 0} were skipped for being listed twice, `,
      `${v.added_count ?? 0} are new, and ${v.updated_count ?? 0} have different prices to what the bot is quoting now.`
    )
  );

  if (v.removed_count > 10) {
    out.append(
      el("div", { className: "note bad", style: "margin-bottom:.9rem" },
        el("strong", {}, `This would delete ${v.removed_count} products. `),
        "That is usually a sign the file is missing a tab rather than that you stopped selling them. Check the Removed tab before publishing."
      )
    );
  }

  const tabs = el("div", { className: "tabs" });
  const panel = el("div");
  const TABS = [
    ["summary", "Tabs read"],
    ["added", `Added (${(diff.added || []).length})`],
    ["updated", `Price changes (${(diff.updated || []).length})`],
    ["removed", `Removed (${(diff.removed || []).length})`],
    ["dupes", `Duplicates skipped (${report.duplicate_rows_dropped ?? 0})`],
    ["orphans", `Full-bottle only (${report.full_bottle_unmatched ?? 0})`],
    ["warnings", `Warnings (${(v.parse_warnings || []).length})`],
  ];
  for (const [key, label] of TABS) {
    const b = el("button", { onclick: () => { state.uploadTab = key; draw(); } }, label);
    b.setAttribute("aria-selected", String(state.uploadTab === key));
    tabs.append(b);
  }

  function list(rows, render) {
    if (!rows?.length) return el("div", { className: "empty" }, "Nothing here.");
    const t = el("table");
    t.append(el("tbody", {}, ...rows.slice(0, 400).map(render)));
    return el("div", { className: "table-wrap" }, t);
  }

  function draw() {
    for (const b of tabs.querySelectorAll("button")) {
      b.setAttribute("aria-selected", String(b.textContent.startsWith(TABS.find(([k]) => k === state.uploadTab)[1].split(" (")[0])));
    }
    panel.replaceChildren();
    const k = state.uploadTab;
    if (k === "summary") {
      panel.append(
        list(report.sheets_read, (s) =>
          el("tr", {}, el("td", {}, el("strong", {}, s.sheet || "(single table)")), el("td", { className: "num" }, s.rows), el("td", { className: "muted" }, s.skipped || "read"))
        ),
        el("p", { className: "muted", style: "font-size:.8rem;margin-top:.6rem" },
          `Never read: ${(report.sheets_skipped || []).join(", ")} — Testers is stock-on-hand, not customer prices. `,
          `${report.full_bottle_prices_attached ?? 0} full-bottle prices were merged onto their decant products.`)
      );
    } else if (k === "added") {
      panel.append(list(diff.added, (r) => el("tr", {}, el("td", {}, r.display_name))));
    } else if (k === "updated") {
      panel.append(list(diff.updated, (r) =>
        el("tr", {}, el("td", {}, r.display_name), el("td", { className: "prices-cell" }, JSON.stringify(r.old_prices || {})), el("td", { className: "prices-cell" }, JSON.stringify(r.new_prices || {})))
      ));
    } else if (k === "removed") {
      panel.append(list(diff.removed, (r) => el("tr", {}, el("td", {}, r.display_name || r))));
    } else if (k === "dupes") {
      panel.append(list(report.duplicate_details, (d) =>
        el("tr", {},
          el("td", {}, el("strong", {}, d.display_name), el("div", { className: "muted", style: "font-size:.74rem" }, `kept ${d.kept_from}, dropped ${d.dropped_from}`)),
          el("td", { className: "prices-cell" }, JSON.stringify(d.kept_prices)),
          el("td", { className: "prices-cell" }, JSON.stringify(d.dropped_prices)))
      ));
    } else if (k === "orphans") {
      const rows = report.full_bottle_unmatched_details || [];
      panel.append(
        el("div", { className: "note warn", style: "margin-bottom:.6rem" },
          "These are Retail Packs rows that matched no decant product. Most are the same perfume written differently — adding them all would double part of your catalog — so pick the ones that are genuinely separate products."),
        rows.length ? el("div", { className: "row-flex", style: "margin-bottom:.5rem" },
          el("button", { className: "btn sm", onclick: () => addOrphans(rows) }, `Add all ${rows.length} as new products`)) : null,
        list(rows, (r) => el("tr", {},
          el("td", {}, r.display_name),
          el("td", { className: "prices-cell" }, Object.entries(r.prices).map(([s, p]) => `${s.replace("ml_full", "ml bottle")} ${money(p)}`).join(", ")),
          el("td", {}, el("button", { className: "btn sm ghost", onclick: () => addOrphans([r]) }, "Add"))))
      );
    } else {
      panel.append(list(v.parse_warnings, (w) => el("tr", {}, el("td", { className: "muted" }, w))));
    }
  }
  draw();

  const publish = el("button", { className: "btn primary" }, "Publish this version");
  publish.onclick = async () => {
    const many = v.removed_count > 10;
    if (many && !confirm(`This deletes ${v.removed_count} products from the live bot. Customers asking for them will get no reply.\n\nPublish anyway?`)) return;
    try {
      await apiFetch(`/api/admin/catalog/versions/${v.id}/publish?confirm_removals=${many}`, { method: "POST" });
      toast("Published — the bot is using this catalog now.", "ok");
      state.upload = null;
      await refreshStatus();
      go("catalog");
    } catch (err) {
      toast(err.message, "bad", 9000);
    }
  };

  out.append(tabs, panel, el("div", { className: "toolbar", style: "margin-top:.9rem" },
    publish,
    el("button", { className: "btn", onclick: () => { state.upload = null; go("upload"); } }, "Discard")));
}

async function addOrphans(rows) {
  const entries = rows.map((r) => ({
    brand: r.brand,
    name: r.name,
    display_name: r.display_name,
    prices: r.prices,
  }));
  try {
    const res = await apiFetch("/api/admin/catalog/add-many", { method: "POST", body: JSON.stringify({ entries }) });
    toast(`${res.submitted} submitted · ${res.added} added · ${res.skipped} skipped as already there.`, res.added ? "ok" : "warn", 7000);
    await refreshStatus();
  } catch (err) {
    toast(err.message, "bad", 9000);
  }
}

/* --- Messages ------------------------------------------------------------ */

VIEWS.messages = async (root) => {
  if (!state.messages) {
    state.messages = await apiFetch("/api/admin/messages");
    state.msgDraft = { ...state.messages.messages };
  }
  const { templates, defaults, max_chars, confirmed_emoji } = state.messages;
  root.replaceChildren();

  root.append(
    el("div", { className: "note warn", style: "margin-bottom:.9rem" },
      el("strong", {}, "A rejected message fails silently. "),
      "WhatsApp answers the Sovereign chat bot with success and then never delivers it, so a bad edit looks exactly like a working one. Asterisks were isolated as a real cause of that, which is why they are blocked here rather than warned about. Confirmed-safe emoji: ",
      el("span", { className: "mono" }, confirmed_emoji.join(" ")))
  );

  const tabs = el("div", { className: "tabs" });
  for (const [key, meta] of Object.entries(templates)) {
    const b = el("button", { onclick: () => { state.msgKey = key; go("messages"); } }, meta.label);
    b.setAttribute("aria-selected", String(state.msgKey === key));
    tabs.append(b);
  }
  root.append(tabs);

  const key = state.msgKey;
  const area = el("textarea", { value: state.msgDraft[key] ?? "", style: "min-height:18rem" });
  const counter = el("span", { className: "counter" });
  const verdictBox = el("div");
  const preview = el("div", { className: "preview" });

  const revalidate = async () => {
    state.msgDraft[key] = area.value;
    preview.textContent = area.value || "(empty — the built-in wording is used)";
    counter.textContent = `${area.value.length.toLocaleString()} / ${max_chars.toLocaleString()} characters`;
    counter.classList.toggle("over", area.value.length > max_chars);
    const res = await apiFetch("/api/admin/messages/validate", {
      method: "POST",
      body: JSON.stringify({ messages: { [key]: area.value } }),
    }).catch(() => null);
    const verdict = res?.results?.[key];
    state.msgVerdict[key] = verdict;
    verdictBox.replaceChildren();
    if (!verdict) return;
    if (verdict.errors.length) {
      verdictBox.append(el("div", { className: "note bad" },
        el("strong", {}, "This will not send. Fix before saving:"),
        el("ul", {}, ...verdict.errors.map((e) => el("li", {}, e)))));
    }
    if (verdict.warnings.length) {
      verdictBox.append(el("div", { className: "note warn", style: "margin-top:.5rem" },
        el("ul", {}, ...verdict.warnings.map((w) => el("li", {}, w)))));
    }
    if (!verdict.errors.length && !verdict.warnings.length) {
      verdictBox.append(el("div", { className: "note ok" }, "Safe to send."));
    }
    saveBtn.disabled = Boolean(verdict.errors.length);
  };

  let t;
  area.oninput = () => { clearTimeout(t); t = setTimeout(revalidate, 260); };

  const saveBtn = el("button", { className: "btn primary" }, "Save");
  saveBtn.onclick = async () => {
    try {
      const res = await apiFetch("/api/admin/messages", {
        method: "PUT",
        body: JSON.stringify({ messages: { [key]: area.value } }),
      });
      state.messages.messages = res.messages;
      toast(`${templates[key].label} saved — the next customer sees it.`, "ok");
    } catch (err) {
      toast(err.message, "bad", 9000);
    }
  };
  const resetBtn = el("button", { className: "btn" }, "Restore the built-in wording");
  resetBtn.onclick = async () => {
    area.value = defaults[key];
    await revalidate();
    try {
      await apiFetch("/api/admin/messages/reset", { method: "POST", body: JSON.stringify({ key }) });
      toast("Restored.", "ok");
    } catch (err) {
      toast(err.message, "bad");
    }
  };

  root.append(
    el("div", { className: "msg-editor" },
      el("div", { className: "stack" },
        el("p", { className: "muted", style: "margin:0;font-size:.85rem" }, templates[key].help),
        area,
        el("div", { className: "row-flex" }, counter, el("span", { style: "flex:1" }), resetBtn, saveBtn),
        verdictBox),
      el("div", { className: "card" }, el("h3", {}, "As the customer sees it"), preview))
  );
  await revalidate();
};

/* --- Chat tester ---------------------------------------------------------- */

async function copyReply(text, button) {
  // Clipboard permission can be refused, and a "Copy" button that silently
  // does nothing is worse than no button. The fallback selects the text so
  // Ctrl+C always works, and either way the button says what happened
  // rather than leaving you to guess whether it took.
  const label = button.textContent;
  try {
    await navigator.clipboard.writeText(text);
    button.textContent = "✓ Copied";
    toast("Reply copied — paste it straight into the chat.", "ok", 2200);
  } catch {
    const range = document.createRange();
    range.selectNodeContents(button.closest(".bubble"));
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    button.textContent = "press Ctrl+C";
    toast("Selected the reply — press Ctrl+C to copy.", "warn", 5000);
  }
  setTimeout(() => { button.textContent = label; }, 2200);
}

VIEWS.tester = async (root) => {
  root.replaceChildren();
  const thread = el("div", { className: "thread" });
  const input = el("input", { type: "text", placeholder: "Type what a customer would send…", className: "grow" });
  const send = el("button", { className: "btn primary" }, "Send");
  const diag = el("div", { className: "diag" });

  const useGroq = el("input", { type: "checkbox", checked: pref.get("groq", "1") === "1" });
  useGroq.onchange = () => pref.set("groq", useGroq.checked ? "1" : "0");
  const showDiag = el("input", { type: "checkbox", checked: pref.get("diag", "1") === "1" });
  showDiag.onchange = () => { pref.set("diag", showDiag.checked ? "1" : "0"); diagCard.classList.toggle("hidden", !showDiag.checked); };

  function draw() {
    thread.replaceChildren();
    for (const turn of state.thread) {
      if (turn.role === "out") {
        thread.append(el("div", { className: "bubble out" }, turn.text));
      } else if (turn.text) {
        // The reply carries its own copy button: the whole point of testing
        // a message here is often to get the card, and re-selecting text out
        // of a scrolling thread by hand loses a line as often as not.
        thread.append(
          el(
            "div",
            { className: "bubble in" },
            turn.text,
            el(
              "div",
              { className: "bubble-foot" },
              el("span", { className: "meta" }, turn.meta || ""),
              el(
                "button",
                { className: "btn sm ghost copy-reply", title: "Copy this reply", onclick: (e) => copyReply(turn.text, e.currentTarget) },
                "⧉ Copy"
              )
            )
          )
        );
      } else {
        thread.append(el("div", { className: "bubble silent" }, "the bot stayed silent — " + (turn.meta || "no product named")));
      }
    }
    thread.scrollTop = thread.scrollHeight;
  }

  async function submit() {
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    state.thread.push({ role: "out", text });
    draw();
    try {
      const res = await apiFetch("/api/admin/simulate", {
        method: "POST",
        body: JSON.stringify({ text, sender: "console", use_groq: useGroq.checked }),
      });
      state.thread.push({
        role: "in",
        text: res.reply,
        meta: [res.layer, res.matched.map((m) => m.display_name).join(", ")].filter(Boolean).join(" · "),
      });
      diag.replaceChildren(
        el("div", { className: "row" }, el("span", {}, "layer"), el("strong", {}, String(res.layer))),
        el("div", { className: "row" }, el("span", {}, "matched"), el("strong", {}, String(res.matched.length))),
        el("div", { className: "row" }, el("span", {}, "LLM"), el("strong", {}, res.llm_unavailable ? "unavailable" : useGroq.checked ? "used" : "off")),
        ...res.scores.map((s) => el("div", { className: "row" }, el("span", {}, s.display_name), el("strong", {}, s.score.toFixed(1))))
      );
      draw();
    } catch (err) {
      toast(err.message, "bad");
    }
  }
  send.onclick = submit;
  input.onkeydown = (e) => { if (e.key === "Enter") submit(); };

  const diagCard = el("div", { className: "card" }, el("h3", {}, "What the matcher saw"), diag);
  diagCard.classList.toggle("hidden", !showDiag.checked);

  root.append(
    el("div", { className: "note info", style: "margin-bottom:.9rem" },
      "This runs the real matching pipeline and shows what the Sovereign chat bot would reply. Nothing is sent to WhatsApp and no message credits are used."),
    el("div", { className: "chat-wrap" },
      el("div", { className: "phone" },
        el("div", { className: "head" }, "Sovereign Scents", el("span", { style: "flex:1" }),
          el("button", {
            className: "btn sm ghost",
            onclick: async () => {
              await apiFetch("/api/admin/simulate/reset", { method: "POST", body: JSON.stringify({ sender: "console" }) });
              state.thread = [];
              draw();
              toast("Conversation cleared — the next message starts fresh.", "ok");
            },
          }, "Reset chat")),
        thread,
        el("div", { className: "composer" }, input, send)),
      el("div", { className: "stack" },
        el("div", { className: "card" }, el("h3", {}, "Tester options"),
          el("label", { className: "switch" }, useGroq, "Use the LLM layer"),
          el("p", { className: "muted", style: "font-size:.78rem;margin:.3rem 0 .6rem" }, "Turn off to see what the bot does when Groq is unreachable — which is how it behaves during an outage."),
          el("label", { className: "switch" }, showDiag, "Show match diagnostics")),
        diagCard))
  );
  draw();
};

/* --- Settings -------------------------------------------------------------- */

VIEWS.settings = async (root) => {
  root.replaceChildren();
  const handoff = await apiFetch("/api/admin/settings/handoff").catch(() => null);

  const themeSel = el("select", {}, ...["auto", "light", "dark"].map((v) =>
    el("option", { value: v, selected: pref.get("theme", "auto") === v }, v === "auto" ? "Follow the system" : v[0].toUpperCase() + v.slice(1))));
  themeSel.onchange = () => { pref.set("theme", themeSel.value); applyTheme(); };

  const densitySel = el("select", {}, ...["compact", "normal", "roomy"].map((v) =>
    el("option", { value: v, selected: pref.get("density", "normal") === v }, v[0].toUpperCase() + v.slice(1))));
  densitySel.onchange = () => { pref.set("density", densitySel.value); applyTheme(); };

  const hours = el("input", { type: "number", min: "0.5", max: "720", step: "0.5", value: handoff?.pause_hours ?? 24 });
  const saveHandoff = el("button", { className: "btn primary" }, "Save");
  saveHandoff.onclick = async () => {
    try {
      await apiFetch("/api/admin/settings/handoff", { method: "PUT", body: JSON.stringify({ pause_hours: Number(hours.value) }) });
      toast("Saved.", "ok");
    } catch (err) { toast(err.message, "bad"); }
  };

  root.append(
    el("div", { className: "grid two" },
      el("div", { className: "card stack" },
        el("h3", {}, "Appearance"),
        el("div", { className: "field" }, el("label", {}, "Theme"), themeSel),
        el("div", { className: "field" }, el("label", {}, "Row density"), densitySel,
          el("span", { className: "hint" }, "Compact fits more of the catalog on screen.")),
        el("div", { className: "field" }, el("label", {}, "Catalog page size"),
          el("span", { className: "hint" }, "Set on the Catalog screen — it is remembered."))),
      el("div", { className: "card stack" },
        el("h3", {}, "Human handoff"),
        el("p", { className: "muted", style: "margin:0;font-size:.85rem" },
          "How long the bot stays out of a conversation after you message that customer yourself."),
        el("div", { className: "field" }, el("label", {}, "Pause for (hours)"), hours),
        saveHandoff,
        handoff ? null : el("div", { className: "note warn" }, "Needs Supabase configured to save."),
        state.status?.handoff_pause?.durable === false
          ? el("div", { className: "note bad" }, el("strong", {}, "Not durable. "), state.status.handoff_pause.detail)
          : null))
  );
};

/* --- Boot ------------------------------------------------------------------ */

async function refreshStatus() {
  try {
    state.status = await apiFetch("/api/admin/status");
    renderStatusPills();
  } catch { /* the header is not worth an error state */ }
}

applyTheme();
$("#theme-btn").onclick = () => {
  const order = ["auto", "light", "dark"];
  const next = order[(order.indexOf(pref.get("theme", "auto")) + 1) % order.length];
  pref.set("theme", next);
  applyTheme();
  toast(`Theme: ${next}`, "ok", 1600);
};
$("#signout-btn").onclick = async () => {
  const supabase = await getSupabase();
  if (supabase) await supabase.auth.signOut();
  window.location.href = "/dashboard/login.html";
};
for (const b of $("#nav").querySelectorAll("button")) b.onclick = () => go(b.dataset.view);

(async () => {
  if (!(await requireSession())) return;
  await refreshStatus();
  await go(state.view in VIEWS ? state.view : "overview");
})();
