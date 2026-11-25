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
  firstCol = 1,
  slideIndex = 0;
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
function status() {
  if (!doc) return;
  $("saveState").textContent = drafts.size
    ? "正在保存…"
    : "本地已保存 · v" + doc.version;
  $("detail").textContent =
    doc.kind === "spreadsheet"
      ? "SUM · AVERAGE · MIN · MAX · COUNT"
      : types[doc.kind][1];
  $("selection").textContent = "当前选区 · " + (selected || "整份文件");
}
function queueEdit(key, value) {
  if (!drafts.has(key))
    drafts.set(key, { field: key, before: doc.fields[key] ?? null, value });
  else drafts.get(key).value = value;
  status();
  if (!composing && !conflicted) job(flush);
}
async function flush() {
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
          label: "编辑 " + changes[0].field,
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
    if (node.tagName === "TEXTAREA") fit(node);
    queueEdit(key, node.value);
  };
  node.onblur = () => {
    if (!composing && !conflicted) job(flush);
    if (doc?.kind === "spreadsheet") queueMicrotask(updateCells);
  };
  return node;
}
function fit(node) {
  node.style.height = "auto";
  node.style.height = Math.max(node.scrollHeight, 32) + "px";
}
function typeBadge(kind) {
  return el("b", types[kind][0], "type " + kind);
}
async function home() {
  await flush();
  const ticket = ++refreshNumber;
  doc = null;
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
      el("small", row.pending ? row.pending + " 条建议" : "v" + row.version),
      el("small", new Date(row.modified * 1000).toLocaleDateString()),
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
function updateMeta() {
  for (const item of $("outline").querySelectorAll("[data-outline]")) {
    const key = item.dataset.outline;
    const label = doc.fields[key] || item.dataset.empty;
    const prefix =
      doc.kind === "document" && doc.fields[key + ".style"] === "heading"
        ? "§ "
        : item.dataset.prefix;
    item.textContent = prefix + label.slice(0, 24);
  }
  const list = $("proposals");
  list.replaceChildren();
  for (const p of doc.pending) {
    const item = el("div", undefined, "proposal");
    item.append(el("strong", p.label));
    for (const e of p.changes.slice(0, 5))
      item.append(
        el(
          "p",
          e.field + "：" + (e.before ?? "空") + " → " + (e.value ?? "删除"),
        ),
      );
    if (p.changes.length > 5)
      item.append(el("small", "共 " + p.changes.length + " 处"));
    item.append(
      btn("接受", () => action("accept", { id: p.id })),
      btn("拒绝", () => action("reject", { id: p.id })),
    );
    list.append(item);
  }
  if (!doc.pending.length) list.append(el("small", "暂无建议"));
  $("mode").value = doc.mode;
  $("history").replaceChildren(
    ...doc.history.map((h) => {
      const row = el(
        "p",
        (h.author === "ai" ? "AI" : "你") +
          " · " +
          h.label +
          (h.undone ? "（已撤销）" : ""),
      );
      if (h.author === "ai" && !h.undone) {
        const undo = btn("撤销", () => action("undo", { author: "ai", version: h.version }));
        undo.setAttribute("aria-label", "撤销 " + h.label);
        row.append(" ", undo);
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
    if (node !== document.activeElement || !document.hasFocus()) {
      const k = node.dataset.field;
      node.value =
        drafts.get(k)?.value ?? doc.computed[k] ?? doc.fields[k] ?? "";
    }
  }
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
  $("play").hidden = kind !== "presentation";
  $("add").textContent = {
    document: "添加段落",
    spreadsheet: "添加工作表",
    presentation: "添加幻灯片",
  }[kind];
  canvas.replaceChildren();
  left.replaceChildren();
  canvas.classList.toggle("grid", kind === "spreadsheet");
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
    left.append(el("h3", "幻灯片"));
    const ids = parts("S");
    if (!ids.includes(section)) section = ids[0] || 1;
    ids.forEach((n, i) => {
      const item = btn(
        "",
        async () => {
          await flush();
          section = n;
          selected = "";
          redraw();
        },
        n === section ? "on" : "",
      );
      Object.assign(item.dataset, {
        outline: "S" + n,
        empty: "无标题",
        prefix: i + 1 + ". ",
      });
      left.append(item);
    });
    const slide = el("div", undefined, "slide");
    canvas.append(slide);
    for (const [k, cls, hint] of [
      ["S" + section, "slide-title", "幻灯片标题"],
      ["S" + section + ".body", "slide-body", "输入演示内容…"],
    ]) {
      const input = editable(el("textarea", undefined, cls), k);
      input.rows = 1;
      input.placeholder = hint;
      input.setAttribute("aria-label", hint);
      slide.append(input);
      fit(input);
    }
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
    canvas.append(table);
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
  const kind = doc.kind,
    prefix = { document: "P", spreadsheet: "T", presentation: "S" }[kind];
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
    api.ask(doc?.id || "", selected, prompt, resolve),
  );
  if (result) throw Error(result);
  input.value = "";
  notice("已发送给 Work。");
}
function present(delta) {
  const ids = parts("S");
  slideIndex = Math.max(0, Math.min(ids.length - 1, slideIndex + delta));
  const k = "S" + ids[slideIndex];
  $("screen").replaceChildren(
    el("h2", doc.fields[k] || ""),
    el("p", doc.fields[k + ".body"] || ""),
  );
  if (!$("presentation").open) $("presentation").showModal();
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
$("style").onchange = () =>
  job(async () => {
    const key = /^P\d+$/.test(selected) ? selected : "P1";
    queueEdit(key + ".style", $("style").value);
    await flush();
    redraw();
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
$("play").onclick = () =>
  job(async () => {
    await flush();
    slideIndex = parts("S").indexOf(section);
    present(0);
  });
$("prevSlide").onclick = () => present(-1);
$("nextSlide").onclick = () => present(1);
$("endShow").onclick = () => $("presentation").close();
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
  if ($("presentation").open && ["ArrowLeft", "ArrowRight"].includes(e.key))
    present(e.key === "ArrowLeft" ? -1 : 1);
});
connect();
