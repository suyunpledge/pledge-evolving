"use strict";
const $ = (id) => document.getElementById(id);
const types = {
  document: ["W", "文档"],
  spreadsheet: ["X", "表格"],
  presentation: ["P", "演示"],
};
let api,
  doc,
  selected = "",
  section = 1,
  firstRow = 1,
  firstCol = 1;
let drafts = new Map(),
  sending = null,
  composing = false,
  needsSync = false,
  conflicted = false;
let refreshNumber = 0;
function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}
function btn(text, fn, cls) {
  const b = el("button", text, cls);
  b.type = "button";
  b.onclick = () => job(fn);
  return b;
}
function notice(text) {
  $("message").firstElementChild.textContent = text;
  $("message").hidden = !text;
}
async function job(fn) {
  try {
    await fn();
  } catch (error) {
    notice(error.message || String(error));
  }
}
function rpc(action, args = {}) {
  if (doc?.kind === "spreadsheet" && doc.id === args.id) args.chartField = chartKey();
  return new Promise((resolve, reject) =>
    api.request(JSON.stringify({ action, ...args }), (raw) => {
      try {
        const result = JSON.parse(raw);
        if (!result.ok) throw Error(result.error);
        resolve(result.data);
      } catch (error) {
        reject(error);
      }
    }),
  );
}
function colName(n) {
  let out = "";
  while (n) {
    n--;
    out = String.fromCharCode(65 + (n % 26)) + out;
    n = Math.floor(n / 26);
  }
  return out;
}
function parts(prefix) {
  const pattern = new RegExp("^" + prefix + "([1-9][0-9]*)(?:\\.|$)");
  return [
    ...new Set(
      Object.keys(doc.fields)
        .map((k) => pattern.exec(k))
        .filter(Boolean)
        .map((m) => Number(m[1])),
    ),
  ].sort((a, b) => a - b);
}
function fieldValue(key) {
  return drafts.get(key)?.value ?? doc.fields[key] ?? "";
}
function show(d) {
  refreshNumber++;
  drafts.clear();
  conflicted = false;
  doc = d;
  api.presentationOpened(d.kind === "presentation");
  firstRow = firstCol = 1;
  selected = "";
  section = 1;
  $("conflict").close();
  $("home").hidden = true;
  $("editor").hidden = false;
  $("editor").classList.toggle("without-ai", innerWidth < 720);
  $("collab").hidden = innerWidth < 720;
  redraw();
}
// Human-readable names for field addresses; raw keys stay internal.
const SLIDE_PARTS = {
  "": "标题", body: "正文", right: "右栏", notes: "备注", layout: "版式",
  hidden: "放映隐藏", transition: "切换效果", table: "表格",
};
function fieldLabel(key) {
  let m;
  if (key === "title") return "文件名";
  if (key === "slideTheme") return "演示主题";
  if ((m = /^P(\d+)(\.style)?$/.exec(key)))
    return "第 " + m[1] + " 段" + (m[2] ? "样式" : "");
  if ((m = /^S(\d+)(?:\.(\w+))?$/.exec(key)) && (m[2] ?? "") in SLIDE_PARTS) {
    // Slide ids are stable, not positions: number by the current deck order.
    const at = doc?.slideOrder?.indexOf("S" + m[1]) ?? -1;
    return "第 " + (at >= 0 ? at + 1 : m[1]) + " 页" + SLIDE_PARTS[m[2] ?? ""];
  }
  if ((m = /^T(\d+)\.name$/.exec(key))) return "工作表 " + m[1] + " 名称";
  if ((m = /^T(\d+)\.chart$/.exec(key))) return "工作表 " + m[1] + " 图表";
  if ((m = /^T(\d+)\.(.+)$/.exec(key))) return "工作表 " + m[1] + " · " + m[2];
  return "单元格 " + key;
}
// Per-field AI suggestions are labelled with the raw address (e.g. "B7").
function readableLabel(label) {
  return /^(?:title|slideTheme|P\d+(?:\.style)?|S\d+(?:\.\w+)?|T\d+\.(?:name|chart|[A-Z]{1,2}\d+)|[A-Z]{1,2}\d+)$/.test(label)
    ? "修改" + fieldLabel(label)
    : label;
}
function status() {
  if (!doc) return;
  $("saveState").textContent = drafts.size
    ? "编辑中…"
    : "本地已保存 · v" + doc.version;

  $("saveState").classList.toggle("dirty", drafts.size > 0);
  let detail = types[doc.kind][1];
  if (doc.kind === "document") {
    let chars = 0;
    for (const n of parts("P"))
      chars += fieldValue("P" + n).replace(/\s/g, "").length;
    detail = "字数 " + chars.toLocaleString("zh-CN");
  }
  if (doc.kind === "spreadsheet") {
    const raw = selected && selected !== "title" ? fieldValue(selected) : "";
    detail = selected && selected !== "title"
      ? selected.split(".").pop() + (raw ? " · " + raw : "")
      : "支持 SUM · AVERAGE · MIN · MAX · COUNT";
  }
  if (doc.kind === "presentation")
    detail =
      "第 " +
      (doc.slideOrder.indexOf("S" + section) + 1) +
      " / " +
      doc.slideOrder.length +
      " 页";
  $("detail").textContent = detail;
  $("selection").textContent =
    "上下文 · " + (selected ? fieldLabel(selected) : "整份文件");
}
// Coalesce typing into one saved transaction (and one undo step) per pause.
const SAVE_DELAY = 700;
let flushTimer = 0;
function scheduleFlush() {
  clearTimeout(flushTimer);
  flushTimer = setTimeout(() => {
    flushTimer = 0;
    if (!composing && !conflicted) job(flush);
  }, SAVE_DELAY);
}
function queueEdit(key, value) {
  if (!drafts.has(key))
    drafts.set(key, { field: key, before: doc.fields[key] ?? null, value });
  else drafts.get(key).value = value;
  status();
  if (!composing && !conflicted) scheduleFlush();
}
async function flush() {
  clearTimeout(flushTimer);
  flushTimer = 0;
  if (composing) throw Error("请先确认输入法候选词。");
  if (conflicted) throw Error("请先处理编辑冲突。");
  if (sending) {
    await sending;
    if (drafts.size) return flush();
    return;
  }
  if (!doc || !drafts.size) return;
  const id = doc.id,
    changes = [...drafts.values()].map((e) => ({ ...e }));
  const done = async () => {
    try {
      const submittedVersion = doc.version;
      const d = await rpc("apply", {
        id,
        args: {
          version: submittedVersion,
          changes,
          label:
            "编辑" +
            fieldLabel(changes[0].field) +
            (changes.length > 1 ? " 等 " + changes.length + " 处" : ""),
        },
      });
      if (doc?.id !== id) return;
      doc = d;
      for (const e of changes) {
        const newer = drafts.get(e.field);
        if (!newer) continue;
        if (newer.value === e.value) drafts.delete(e.field);
        else newer.before = e.value;
      }
      if (d.version > submittedVersion + 1 && !composing) redraw();
      else {
        updateMeta();
        updateCells();
        refreshChart();
      }
    } catch (error) {
      if (error.message.includes("字段已变化")) {
        conflicted = true;
        $("saveState").textContent = "草稿待处理";
        if (!$("conflict").open) $("conflict").showModal();
      }
      throw error;
    }
  };
  sending = done();
  try {
    await sending;
  } finally {
    sending = null;
  }
  if (drafts.size) return flush();
}
function editable(node, key) {
  node.dataset.field = key;
  node.value = fieldValue(key);
  node.onfocus = () => {
    if (doc.kind === "spreadsheet" && key !== "title") crosshair(node);
    selected = key;
    status();
    if (doc.kind === "spreadsheet" && key !== "title") {
      node.value = fieldValue(key);
      $("cellAddress").value = key.split(".").pop();
    }
    if (doc.kind === "document")
      $("style").value = doc.fields[key + ".style"] || "paragraph";
  };
  node.oncompositionstart = () => {
    composing = true;
  };
  node.oncompositionend = () => {
    composing = false;
    queueEdit(key, node.value);
    if (needsSync) {
      needsSync = false;
      job(sync);
    }
  };
  node.oninput = () => {
    if (node.tagName === "TEXTAREA" && doc.kind === "document") fit(node);
    queueEdit(key, node.value);
    if (doc.kind === "presentation") updateSlidePreview(key);
  };
  node.onblur = () => {
    if (!composing && !conflicted) job(flush);
    if (doc?.kind === "spreadsheet") queueMicrotask(updateCells);
  };
  return node;
}
function crosshair(node) {
  const cell = node.closest("td"),
    table = node.closest("table");
  if (!cell || !table) return;
  for (const th of table.querySelectorAll("th.active"))
    th.classList.remove("active");
  table.rows[0].cells[cell.cellIndex]?.classList.add("active");
  cell.parentElement.cells[0]?.classList.add("active");
}
function fit(node) {
  node.style.height = "auto";
  node.style.height = Math.max(node.scrollHeight, 32) + "px";
}
function ago(seconds) {
  const diff = Date.now() / 1000 - seconds;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return Math.floor(diff / 60) + " 分钟前";
  if (diff < 86400) return Math.floor(diff / 3600) + " 小时前";
  if (diff < 86400 * 7) return Math.floor(diff / 86400) + " 天前";
  return new Date(seconds * 1000).toLocaleDateString();
}
function typeBadge(kind) {
  return el("b", types[kind][0], "type " + kind);
}
async function home() {
  await flush();
  const ticket = ++refreshNumber;
  doc = null;
  api.presentationOpened(false);
  selected = "";
  $("editor").hidden = true;
  $("home").hidden = false;
  const rows = await rpc("list");
  if (ticket !== refreshNumber || doc) return;
  const root = $("recent");
  root.replaceChildren();
  for (const row of rows) {
    const b = btn(
      "",
      async () => show(await rpc("open", { id: row.id })),
      "recent",
    );
    b.append(
      typeBadge(row.kind),
      el("strong", row.title),
      row.pending
        ? el("small", row.pending + " 条待审建议", "pill")
        : el("small", "v" + row.version),
      el("small", ago(row.modified)),
    );
    root.append(b);
  }
  if (!rows.length) root.append(el("p", "当前工作区还没有办公文件。", "empty"));
}
async function action(kind, args) {
  await flush();
  const id = doc.id;
  const d = await rpc(kind, { id, args });
  if (doc?.id === id) {
    doc = d;
    redraw();
  }
}
async function settleAll(kind) {
  const ids = doc.pending.map((p) => p.id);
  for (const id of ids) {
    if (!doc.pending.some((p) => p.id === id)) continue;
    await action(kind, { id });
  }
}
// Scroll a field into view (switching slide/sheet when needed) and focus it.
async function reveal(field) {
  const slide = /^S(\d+)/.exec(field),
    sheet = /^T(\d+)\./.exec(field);
  const target = slide ? Number(slide[1]) : sheet ? Number(sheet[1]) : 1;
  if ((slide || doc.kind === "spreadsheet") && section !== target) {
    await flush();
    section = target;
    redraw();
  }
  const find = () =>
    $("canvas").querySelector('[data-field="' + CSS.escape(field) + '"]');
  let node = find();
  const ref = /([A-Z]{1,2})(\d+)$/.exec(field);
  if (!node && doc.kind === "spreadsheet" && ref) {
    // Outside the rendered window: scroll the grid to the cell.
    firstCol = [...ref[1]].reduce((v, ch) => v * 26 + ch.charCodeAt(0) - 64, 0);
    firstRow = Number(ref[2]);
    redraw();
    node = find();
  }
  node?.scrollIntoView({ block: "center", behavior: "smooth" });
  node?.focus({ preventScroll: true });
}
// One reviewed edit: before/after text, or a summary for structured values (charts).
function changeRow(e, withLabel) {
  const row = el("div", undefined, "change");
  if (withLabel) row.append(el("small", fieldLabel(e.field)));
  const summary = chartChangeText(e);
  if (summary !== null) row.append(el("p", summary, "summary"));
  else {
    if (e.before) row.append(el("del", e.before));
    row.append(e.value === null ? el("em", "删除此项") : el("ins", e.value));
  }
  return row;
}
function pendingFields() {
  const fields = new Map();
  for (const p of doc.pending)
    for (const e of p.changes) if (!fields.has(e.field)) fields.set(e.field, p);
  return fields;
}
function updateMeta() {
  const suggested = pendingFields();
  for (const item of $("outline").querySelectorAll("[data-outline]")) {
    const key = item.dataset.outline;
    const label = doc.fields[key] || item.dataset.empty;
    const heading =
      doc.kind === "document" && doc.fields[key + ".style"] === "heading";
    item.textContent = item.dataset.prefix + label.slice(0, 40);
    item.classList.toggle("heading", heading);
    item.classList.toggle(
      "suggested",
      suggested.has(key) || suggested.has(key + ".body"),
    );
  }
  for (const node of $("canvas").querySelectorAll("[data-field]")) {
    const p = suggested.get(node.dataset.field);
    node.classList.toggle("suggested", Boolean(p));
    const proposed = p?.changes.find((e) => e.field === node.dataset.field);
    node.title = p
      ? "revon 建议：" + (proposed?.value ?? "删除") + "（在右侧审阅）"
      : "";
    // Empty fields preview the proposed value as ghost text.
    if (node.dataset.hint === undefined) node.dataset.hint = node.placeholder;
    node.placeholder =
      p && !node.value && proposed?.value ? proposed.value : node.dataset.hint;
  }
  const list = $("proposals");
  list.replaceChildren();
  $("proposalCount").textContent = doc.pending.length
    ? String(doc.pending.length)
    : "";
  if (doc.pending.length > 1) {
    const bulk = el("div", undefined, "bulk");
    bulk.append(
      btn("全部接受", () => settleAll("accept"), "accept"),
      btn("全部拒绝", () => settleAll("reject")),
    );
    list.append(bulk);
  }
  for (const p of doc.pending) {
    const single =
      p.changes.length === 1 && readableLabel(p.label) !== p.label;
    const item = el("div", undefined, "proposal" + (single ? " single" : ""));
    if (single) {
      const e = p.changes[0],
        head = el("div", undefined, "single-head");
      const jump = btn(fieldLabel(e.field), () => reveal(e.field), "field");
      jump.title = "定位到" + fieldLabel(e.field);
      const yes = btn("✓", () => action("accept", { id: p.id }), "icon accept");
      const no = btn("✕", () => action("reject", { id: p.id }), "icon");
      yes.setAttribute("aria-label", "接受" + readableLabel(p.label));
      no.setAttribute("aria-label", "拒绝" + readableLabel(p.label));
      head.append(jump, yes, no);
      item.append(head, changeRow(e, false));
      list.append(item);
      continue;
    }
    item.append(el("strong", readableLabel(p.label)));
    for (const e of p.changes.slice(0, 5)) item.append(changeRow(e, true));
    if (p.changes.length > 5)
      item.append(el("small", "共 " + p.changes.length + " 处"));
    const acts = el("div", undefined, "actions");
    acts.append(
      btn("接受", () => action("accept", { id: p.id }), "accept"),
      btn("拒绝", () => action("reject", { id: p.id })),
    );
    item.append(acts);
    list.append(item);
  }
  if (!doc.pending.length) list.append(el("p", "暂无待处理建议", "quiet"));
  $("mode").value = doc.mode;
  for (const b of document.querySelectorAll("[data-mode]"))
    b.setAttribute("aria-checked", String(b.dataset.mode === doc.mode));
  $("history").replaceChildren(
    ...doc.history.map((h) => {
      const row = el("p", undefined, h.undone ? "undone" : "");
      row.append(
        el("b", h.author === "ai" ? "revon" : "你", h.author),
        el("span", readableLabel(h.label) + (h.undone ? "（已撤销）" : "")),
      );
      if (h.author === "ai" && !h.undone) {
        const undo = btn(
          "撤销",
          () => action("undo", { author: "ai", version: h.version }),
          "mini",
        );
        undo.setAttribute("aria-label", "撤销 " + readableLabel(h.label));
        row.append(undo);
      }
      return row;
    }),
  );
  $("undo").disabled = !doc.history.some(
    (h) => h.author === "user" && !h.undone,
  );
  $("undoAI").disabled = !doc.history.some(
    (h) => h.author === "ai" && !h.undone,
  );
  status();
}
function updateCells() {
  if (doc?.kind !== "spreadsheet") return;
  for (const node of $("canvas").querySelectorAll("input[data-field]")) {
    const k = node.dataset.field,
      raw = drafts.get(k)?.value ?? doc.fields[k] ?? "",
      shown = drafts.has(k) ? raw : (doc.computed[k] ?? raw);
    const numeric = /^-?\d+(?:\.\d+)?$/.test(shown);
    node.classList.toggle("num", numeric);
    node.classList.toggle("formula", raw.startsWith("="));
    node.classList.toggle("error", shown === "#ERROR!");
    // The focused cell is being edited: refresh it only while the window is
    // inactive, and then with the raw value so typing never saves "1,920.4".
    if (node !== document.activeElement)
      node.value = numeric ? formatNumber(shown) : shown;
    else if (!document.hasFocus()) node.value = raw;
  }
}
// Display-only rounding; the cell keeps its exact value and shows it on focus.
const numberFormats = [
  new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }),
  new Intl.NumberFormat("zh-CN", { maximumSignificantDigits: 4 }),
];
function formatNumber(text) {
  const n = Number(text);
  return numberFormats[n !== 0 && Math.abs(n) < 1 ? 1 : 0].format(n);
}
function redraw() {
  const active = document.activeElement,
    key = active?.dataset.field,
    offset = key ? active.selectionStart : null;
  const canvas = $("canvas"),
    oldScroll = canvas.scrollTop,
    oldX = canvas.scrollLeft,
    left = $("outline");
  editable($("title"), "title");
  const kind = doc.kind;
  $("kindIcon").className = "type " + kind;
  $("kindIcon").textContent = types[kind][0];
  $("style").hidden = kind !== "document";
  $("sheetAddress").hidden = kind !== "spreadsheet";
  $("chartDesign").hidden = kind !== "spreadsheet";
  $("play").hidden = kind !== "presentation";
  $("playStart").hidden = $("slideTools").hidden = kind !== "presentation";
  $("add").textContent = {
    document: "添加段落",
    spreadsheet: "添加工作表",
    presentation: "添加幻灯片",
  }[kind];
  const previousBoard = canvas.querySelector(".deck-board.edit");
  if (previousBoard) slideResize.unobserve(previousBoard);
  canvas.replaceChildren();
  left.replaceChildren();
  canvas.classList.toggle("grid", kind === "spreadsheet");
  left.className = "k-" + kind;
  $("editor").dataset.kind = kind;
  if (kind === "document") {
    left.append(el("h3", "文档大纲"));
    const paper = el("div", undefined, "paper");
    canvas.append(paper);
    for (const n of parts("P")) {
      const k = "P" + n,
        style = doc.fields[k + ".style"] || "paragraph",
        input = editable(el("textarea", undefined, style), k);
      input.rows = 1;
      input.placeholder = style === "heading" ? "标题" : "输入段落…";
      input.setAttribute("aria-label", "段落 " + n);
      paper.append(input);
      fit(input);
      const item = btn("", () => input.focus());
      Object.assign(item.dataset, {
        outline: k,
        empty: "段落 " + n,
        prefix: "",
      });
      left.append(item);
    }
  } else if (kind === "presentation") {
    renderDeck(left, canvas);
  } else {
    left.append(el("h3", "工作表"));
    const ids = [...new Set([1, ...parts("T")])];
    if (!ids.includes(section)) section = 1;
    for (const n of ids)
      left.append(
        btn(
          doc.fields["T" + n + ".name"] || "Sheet" + n,
          async () => {
            await flush();
            section = n;
            selected = "";
            firstCol = firstRow = 1;
            redraw();
          },
          n === section ? "on" : "",
        ),
      );
    const table = el("table"),
      head = table.insertRow();
    head.append(el("th", ""));
    for (let c = firstCol; c < firstCol + 12 && c <= 702; c++)
      head.append(el("th", colName(c)));
    for (let r = firstRow; r < firstRow + 35 && r <= 9999; r++) {
      const tr = table.insertRow();
      tr.append(el("th", String(r)));
      for (let c = firstCol; c < firstCol + 12 && c <= 702; c++) {
        const ref = colName(c) + r,
          k = (section === 1 ? "" : "T" + section + ".") + ref;
        const input = editable(el("input"), k);
        input.setAttribute("aria-label", ref);
        tr.insertCell().append(input);
      }
    }
    const chartHost = el("section");
    chartHost.id = "sheetChart";
    canvas.append(chartHost, table);
    refreshChart();
    updateCells();
  }
  updateMeta();
  canvas.scrollTop = oldScroll;
  canvas.scrollLeft = oldX;
  if (key) {
    const target = document.querySelector(
      '[data-field="' + CSS.escape(key) + '"]',
    );
    if (target) {
      target.focus({ preventScroll: true });
      if (offset !== null) target.setSelectionRange(offset, offset);
    }
  }
}
async function sync() {
  if (composing || conflicted) {
    needsSync = true;
    return;
  }
  if (!doc) return home();
  await flush();
  const id = doc.id;
  const d = await rpc("open", { id });
  if (doc?.id !== id || d.version <= doc.version) return;
  doc = d;
  redraw();
}
async function appendPart() {
  await flush();
  if (doc.kind === "presentation") return arrangeSlide("insert");
  const kind = doc.kind,
    prefix = { document: "P", spreadsheet: "T" }[kind];
  const n = Math.max(kind === "spreadsheet" ? 1 : 0, ...parts(prefix)) + 1;
  const key = prefix + n + (kind === "spreadsheet" ? ".name" : "");
  await action("apply", {
    version: doc.version,
    changes: [
      {
        field: key,
        before: null,
        value: kind === "spreadsheet" ? "Sheet" + n : "",
      },
    ],
    label: $("add").textContent,
  });
  section = n;
  selected = key;
  redraw();
  document.querySelector('[data-field="' + key + '"]')?.focus();
}
async function ask(form) {
  await flush();
  const input = form.querySelector("textarea,input"),
    prompt = input.value.trim();
  if (!prompt) return;
  const result = await new Promise((resolve) =>
    api.ask(doc?.id || "", selected || (doc?.kind === "presentation" ? "S" + section : ""), prompt, resolve),
  );
  if (result) throw Error(result);
  input.value = "";
  notice("已发送给 Work。");
}
function connect() {
  new QWebChannel(qt.webChannelTransport, (channel) => {
    api = channel.objects.office;
    api.updated.connect(() => job(sync));
    api.imported.connect((raw) => {
      const r = JSON.parse(raw);
      if (r.ok) {
        show(r.data);
        notice("已导入可编辑副本，原文件保留。");
      } else notice(r.error);
    });
    api.agentStatus.connect((s) => {
      $("agentState").textContent = s || "";
    });
    job(async () => {
      await home();
      api.initialized();
    });
  });
}
$("back").onclick = () => job(home);
$("dismiss").onclick = () => notice("");
$("pick").onclick = () => api.pickFile();
document.querySelectorAll("[data-create]").forEach(
  (b) =>
    (b.onclick = () =>
      job(async () => {
        const kind = b.dataset.create;
        show(await rpc("create", { kind, title: "未命名" + types[kind][1] }));
        $("title").focus();
        $("title").select();
      })),
);
$("add").onclick = () => job(appendPart);
bindChartDesign();
bindSlideControls();
$("style").onchange = () =>
  job(async () => {
    const key = /^P\d+$/.test(selected) ? selected : "P1";
    queueEdit(key + ".style", $("style").value);
    await flush();
    redraw();
  });
for (const b of document.querySelectorAll("[data-mode]"))
  b.onclick = () => {
    $("mode").value = b.dataset.mode;
    $("mode").onchange();
  };
// Leaving the page (workspace switch, hiding Office) saves pending typing.
document.addEventListener("visibilitychange", () => {
  if (document.hidden && drafts.size && !composing && !conflicted) job(flush);
});
$("mode").onchange = () =>
  job(() => action("mode", { value: $("mode").value }));
$("undo").onclick = () => job(() => action("undo", { author: "user" }));
$("undoAI").onclick = () => job(() => action("undo", { author: "ai" }));
$("aiToggle").onclick = () => {
  $("collab").hidden = !$("collab").hidden;
  $("editor").classList.toggle("without-ai", $("collab").hidden);
  $("aiToggle").setAttribute("aria-expanded", String(!$("collab").hidden));
};
$("export").onclick = () =>
  job(async () => {
    await flush();
    notice("已导出到 " + (await rpc("export", { id: doc.id })));
  });
for (const id of ["ask", "homeAsk"])
  $(id).onsubmit = (e) => {
    e.preventDefault();
    job(() => ask($(id)));
  };
$("goCell").onclick = () =>
  job(async () => {
    const ref = $("cellAddress").value.trim().toUpperCase(),
      m = /^([A-Z]{1,2})([1-9]\d{0,3})$/.exec(ref);
    if (!m) throw Error("请输入 A1 至 ZZ9999 的地址。");
    await flush();
    firstCol = [...m[1]].reduce((v, ch) => v * 26 + ch.charCodeAt(0) - 64, 0);
    firstRow = Number(m[2]);
    redraw();
    $("canvas").querySelector("input")?.focus();
  });
$("cellAddress").onkeydown = (e) => {
  if (e.key === "Enter") $("goCell").click();
};
$("useLatest").onclick = () =>
  job(async () => {
    const d = await rpc("open", { id: doc.id });
    show(d);
  });
$("useDraft").onclick = () =>
  job(async () => {
    doc = await rpc("open", { id: doc.id });
    for (const e of drafts.values()) e.before = doc.fields[e.field] ?? null;
    conflicted = false;
    $("conflict").close();
    await flush();
    redraw();
  });
$("conflict").oncancel = (e) => e.preventDefault();
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
    e.preventDefault();
    job(flush);
  }
});
connect();
