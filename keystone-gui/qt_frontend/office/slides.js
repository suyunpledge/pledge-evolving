"use strict";

let slideShow = null;
const slideHints = {title: "幻灯片标题", body: "正文", right: "右栏正文"};
const slideField = (page, role) => role === "title" ? page : page + "." + role;

function slideBoard(file, page, mode) {
  const fields = file.fields, design = file.slideDesign;
  const table = fields[page + ".table"];
  const variant = Number(!!fields[page + ".right"]) + 2 * Number(!!table);
  const scene = design.layouts[fields[page + ".layout"] || "content"][variant];
  const theme = design.themes[fields.slideTheme || "paper"];
  const board = el("div", undefined, "deck-board " + mode);
  board.style.backgroundColor = theme.background;
  board.style.color = theme.ink;
  function place(node, [x, y, w, h]) {
    Object.assign(node.style, {left: x / 9.6 + "%", top: y / 5.4 + "%",
      width: w / 9.6 + "%", height: h / 5.4 + "%"});
    board.append(node);
  }
  const accent = el("span", undefined, "deck-accent");
  accent.style.backgroundColor = theme.accent;
  place(accent, scene.accent);
  for (const [role, box] of Object.entries(scene.boxes)) {
    const key = slideField(page, role);
    let node;
    if (mode === "edit") {
      node = editable(el("textarea"), key);
      node.setAttribute("aria-label", slideHints[role]);
      node.placeholder = slideHints[role];
    } else {
      node = el("span", mode === "mini" ? (fields[key] || "").slice(0, 120) : fields[key] || "");
      if (mode === "mini") node.dataset.preview = key;
    }
    node.className = "deck-text deck-" + role;
    node.dataset.fontSize = box[4];
    node.style.fontSize = box[4] / 9.6 + "cqw";
    place(node, box);
  }
  if (table) {
    const rows = JSON.parse(table), grid = el("div", undefined, "deck-table");
    grid.style.gridTemplateColumns = "repeat(" + rows[0].length + ", minmax(0, 1fr))";
    grid.style.gridTemplateRows = "repeat(" + rows.length + ", minmax(0, 1fr))";
    grid.setAttribute("role", "table");
    for (const [i, row] of rows.entries()) {
      const line = el("div", undefined, "deck-row");
      line.setAttribute("role", "row");
      for (const value of row) {
        const cell = el("span", value);
        cell.dataset.fontSize = "20";
        cell.setAttribute("role", i ? "cell" : "columnheader");
        if (i === 0) Object.assign(cell.style, {color: theme.background, background: theme.accent});
        line.append(cell);
      }
      grid.append(line);
    }
    if (mode === "edit") {
      grid.title = "双击编辑表格";
      grid.onclick = () => { selected = page + ".table"; status(); };
      grid.ondblclick = () => $("editSlideTable").click();
    }
    place(grid, scene.table);
  }
  return board;
}

// Only measure the active full-sized canvas. Thumbnails never trigger layout fitting.
function fitSlideText(board) {
  if (!board?.isConnected || !board.clientWidth) return;
  for (const node of board.querySelectorAll("[data-font-size]")) {
    let size = Number(node.dataset.fontSize);
    node.style.fontSize = size / 9.6 + "cqw";
    while (size > 10 && (node.scrollHeight > node.clientHeight + 1 || node.scrollWidth > node.clientWidth + 1)) {
      size = Math.max(10, size * .9);
      node.style.fontSize = size / 9.6 + "cqw";
    }
  }
}
const slideResize = new ResizeObserver(entries => {
  for (const {target} of entries) fitSlideText(target);
});

function updateSlidePreview(key) {
  if (key.endsWith(".notes")) return;
  const node = $("outline").querySelector('[data-preview="' + CSS.escape(key) + '"]');
  if (node) node.textContent = fieldValue(key).slice(0, 120);
  const thumb = $("outline").querySelector('[data-slide="' + CSS.escape(key) + '"]');
  if (thumb) thumb.setAttribute("aria-label", thumb.firstChild.textContent + " " + fieldValue(key));
  fitSlideText($("canvas").querySelector(".deck-board"));
}

async function chooseSlide(page) {
  await flush();
  if (!doc.slideOrder.includes(page)) return;
  section = Number(page.slice(1));
  selected = page;
  redraw();
}

async function arrangeSlide(command, index, source) {
  await flush();
  const old = doc, key = source || "S" + section, at = old.slideOrder.indexOf(key);
  const result = await rpc("arrange", {id: old.id, args: {version: old.version, slide: key, command, index}});
  if (doc?.id !== old.id) return;
  let next = key;
  if (command === "insert" || command === "duplicate") next = result.slideOrder.find(k => !old.slideOrder.includes(k));
  if (command === "delete") next = result.slideOrder[Math.min(at, result.slideOrder.length - 1)];
  doc = result;
  section = Number(next.slice(1));
  selected = next;
  redraw();
  $("outline").querySelector(".on")?.scrollIntoView({block: "nearest"});
}

function renderDeck(outline, canvas) {
  const ids = doc.slideOrder;
  if (!ids.includes("S" + section)) section = ids.length ? Number(ids[0].slice(1)) : 1;
  const key = "S" + section, at = ids.indexOf(key);
  $("slideTheme").value = doc.fields.slideTheme || "paper";
  $("slideLayout").value = doc.fields[key + ".layout"] || "content";
  $("slideTransition").value = doc.fields[key + ".transition"] || "none";
  $("editSlideTable").textContent = doc.fields[key + ".table"] ? "编辑表格" : "插入表格";
  for (const id of ["slideLayout", "slideTransition", "editSlideTable", "play", "playStart"])
    $(id).disabled = !ids.length;
  outline.append(el("h3", "幻灯片"));
  if (!ids.length) { canvas.append(el("p", "添加第一张幻灯片。")); return; }
  const commands = el("div", undefined, "slide-commands");
  for (const [label, hint, fn, disabled] of [
    ["↑", "上移当前页", () => arrangeSlide("move", at - 1), at === 0],
    ["↓", "下移当前页", () => arrangeSlide("move", at + 1), at === ids.length - 1],
    ["复制", "复制当前页", () => arrangeSlide("duplicate"), false],
    ["删除", "删除当前页", () => arrangeSlide("delete"), ids.length === 1]
  ]) {
    const button = btn(label, fn);
    button.title = hint;
    button.setAttribute("aria-label", hint);
    button.disabled = disabled;
    commands.append(button);
  }
  outline.append(commands);
  for (const [i, id] of ids.entries()) {
    const hidden = doc.fields[id + ".hidden"] === "true";
    const label = "第 " + (i + 1) + " 页" + (hidden ? " · 已隐藏" : "");
    const thumb = btn("", () => chooseSlide(id), "slide-thumb" + (id === key ? " on" : ""));
    thumb.dataset.slide = id;
    thumb.setAttribute("aria-current", id === key ? "page" : "false");
    thumb.setAttribute("aria-label", label + " " + (doc.fields[id] || ""));
    thumb.draggable = true;
    thumb.ondragstart = event => event.dataTransfer.setData("application/x-keystone-slide", id);
    thumb.ondragover = event => {
      if (event.dataTransfer.types.includes("application/x-keystone-slide")) event.preventDefault();
    };
    thumb.ondrop = event => {
      event.preventDefault();
      const source = event.dataTransfer.getData("application/x-keystone-slide");
      if (ids.includes(source) && source !== id) job(() => arrangeSlide("move", i, source));
    };
    thumb.append(el("small", label), slideBoard(doc, id, "mini"));
    outline.append(thumb);
  }
  const board = slideBoard(doc, key, "edit");
  const panel = el("div", undefined, "slide-notes");
  const toggle = btn(doc.fields[key + ".hidden"] === "true" ? "恢复放映此页" : "放映时隐藏此页",
    () => setSlideOption(key + ".hidden", doc.fields[key + ".hidden"] === "true" ? "false" : "true"));
  const label = el("label", "演讲者备注");
  const notes = editable(el("textarea"), key + ".notes");
  notes.placeholder = "添加放映时仅供讲者参考的备注…";
  notes.rows = 2;
  label.append(notes);
  panel.append(label, toggle);
  canvas.append(board, panel);
  slideResize.observe(board);
}

async function setSlideOption(field, value) {
  await flush();
  await action("apply", {version: doc.version, label: "演示设计",
    changes: [{field, before: doc.fields[field] ?? null, value}]});
}

async function startSlideShow(current) {
  if (doc?.kind !== "presentation" || document.querySelector("dialog[open]")) return;
  await flush();
  const ids = doc.slideOrder.filter(k => doc.fields[k + ".hidden"] !== "true");
  if (!ids.length) throw Error("请先恢复至少一张幻灯片参与放映。");
  const active = "S" + section, order = doc.slideOrder;
  const index = current ? ids.findIndex(k => order.indexOf(k) >= order.indexOf(active)) : 0;
  slideShow = {file: doc, ids, index: index < 0 ? 0 : index};
  $("presentation").showModal();
  api.setSlideShow(true);
  displaySlide(slideShow.index);
}

function displaySlide(index) {
  if (!slideShow) return;
  const {file, ids} = slideShow;
  slideShow.index = Math.max(0, Math.min(ids.length - 1, index));
  const page = ids[slideShow.index], stage = $("screen");
  const board = slideBoard(file, page, "projected");
  if (file.fields[page + ".transition"] === "fade") board.classList.add("fade-in");
  const old = stage.firstElementChild;
  if (old) slideResize.unobserve(old);
  stage.replaceChildren(board);
  slideResize.observe(board);
  $("showPosition").textContent = (slideShow.index + 1) + " / " + ids.length;
  $("prevSlide").disabled = slideShow.index === 0;
  $("nextSlide").disabled = slideShow.index === ids.length - 1;
}
function bindSlideTable() {
  const dialog = $("slideTableDialog"), inputs = $("tableInputs");
  let target, rows;
  function draw() {
    inputs.replaceChildren();
    inputs.style.gridTemplateColumns = "repeat(" + rows[0].length + ", minmax(0, 1fr))";
    rows.forEach((row, i) => row.forEach((value, j) => {
      const node = el("textarea");
      node.value = value;
      node.rows = 2;
      node.maxLength = 200;
      node.setAttribute("aria-label", "第 " + (i + 1) + " 行，第 " + (j + 1) + " 列");
      node.oninput = () => { rows[i][j] = node.value; };
      inputs.append(node);
    }));
    $("rowPlus").disabled = rows.length === 8;
    $("rowMinus").disabled = rows.length === 1;
    $("colPlus").disabled = rows[0].length === 6;
    $("colMinus").disabled = rows[0].length === 1;
  }
  $("editSlideTable").onclick = () => job(async () => {
    await flush();
    const key = "S" + section + ".table";
    target = {id: doc.id, key, before: doc.fields[key] ?? null};
    rows = target.before ? JSON.parse(target.before) : [["", ""], ["", ""]];
    $("removeSlideTable").hidden = !target.before;
    $("tableError").hidden = true;
    draw();
    dialog.showModal();
    inputs.firstChild.focus();
  });
  $("rowPlus").onclick = () => { rows.push(Array(rows[0].length).fill("")); draw(); };
  $("rowMinus").onclick = () => { rows.pop(); draw(); };
  $("colPlus").onclick = () => { rows.forEach(r => r.push("")); draw(); };
  $("colMinus").onclick = () => { rows.forEach(r => r.pop()); draw(); };
  async function save(value) {
    try {
      if (doc?.id !== target.id || !doc.slideOrder.includes(target.key.split(".")[0]))
        throw Error("文件或页面已切换。");
      await action("apply", {version: doc.version, label: "编辑演示表格",
        changes: [{field: target.key, before: target.before, value}]});
      dialog.close();
    } catch (error) {
      $("tableError").hidden = false;
      $("tableError").textContent = error.message;
    }
  }
  $("saveSlideTable").onclick = () => save(JSON.stringify(rows));
  $("removeSlideTable").onclick = () => save(null);
  $("closeSlideTable").onclick = () => dialog.close();
}

function bindSlideControls() {
  bindSlideTable();
  for (const [id, suffix] of [["slideLayout", ".layout"], ["slideTransition", ".transition"], ["slideTheme", ""]])
    $(id).onchange = () => job(() => setSlideOption(suffix ? "S" + section + suffix : "slideTheme", $(id).value));
  $("play").onclick = () => job(() => startSlideShow(true));
  $("playStart").onclick = () => job(() => startSlideShow(false));
  $("prevSlide").onclick = () => displaySlide(slideShow.index - 1);
  $("nextSlide").onclick = () => displaySlide(slideShow.index + 1);
  $("endShow").onclick = () => $("presentation").close();
  $("presentation").onclose = () => {
    if ($("screen").firstChild) slideResize.unobserve($("screen").firstChild);
    $("screen").replaceChildren();
    slideShow = null;
    api.setSlideShow(false);
  };
  document.addEventListener("keydown", e => {
    if (doc?.kind !== "presentation" || e.isComposing) return;
    if (!slideShow) {
      if (e.key === "F5") { e.preventDefault(); job(() => startSlideShow(e.shiftKey)); }
      return;
    }
    const previous = ["ArrowLeft", "ArrowUp", "PageUp", "Backspace"];
    const next = ["ArrowRight", "ArrowDown", "PageDown", " "];
    if (e.key === "Escape") { e.preventDefault(); $("presentation").close(); }
    else if (previous.includes(e.key) || next.includes(e.key) || ["Home", "End"].includes(e.key)) {
      e.preventDefault();
      const target = e.key === "Home" ? 0 : e.key === "End" ? slideShow.ids.length - 1 :
        slideShow.index + (previous.includes(e.key) ? -1 : 1);
      displaySlide(target);
    }
  });
}
