"use strict";

function chartKey() { return "T" + section + ".chart"; }

function chartChangeText(edit) {
  if (!/^T\d+\.chart$/.test(edit.field)) return null;
  const describe = raw => {
    if (!raw) return "无图表";
    const c = JSON.parse(raw);
    return [c.title, c.expression ? "y = " + c.expression : c.xRange + " / " + c.yRange,
      c.xTitle, c.yTitle, c.style === "scatter" ? "散点" : "连线", c.color,
      c.labels ? "显示数值" : "隐藏数值", c.expression ? (c.min ?? -5) + " 至 " + (c.max ?? 5) : ""]
      .filter(Boolean).join(" · ");
  };
  return describe(edit.before) + " → " + describe(edit.value);
}

function svgNode(tag, attributes = {}) {
  const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attributes)) n.setAttribute(k, v);
  return n;
}

function refreshChart() {
  const host = $("sheetChart");
  if (!host || doc?.kind !== "spreadsheet") return;
  const field = chartKey(), id = doc.id, version = doc.version;
  $("chartDesign").textContent = doc.fields[field] ? "图表设计" : "插入图表";
  if (!doc.fields[field]) {
    host.replaceChildren();
    delete host.dataset.version;
    return;
  }
  if (host.dataset.version === String(version)) return;
  job(async () => {
    const scene = doc.chartScene?.field === field ? doc.chartScene : await rpc("chart", { id, field });
    if (!host.isConnected || doc?.id !== id || field !== chartKey() || doc.version !== scene.version) return;
    doc.chartScene = scene;
    const {spec, plot, texts, points} = scene;
    const color = spec.color || "#3679b8";
    const svg = svgNode("svg", {viewBox: scene.viewBox.join(" "), role: "img", tabindex: "0",
      "aria-label": spec.title || "工作表图表"});
    const [left, top, right, bottom] = plot;
    svg.append(svgNode("path", {d: "M" + left + " " + top + "V" + bottom + "H" + right,
      fill: "none", stroke: "#93a1b0"}));
    let path = "", start = true;
    for (const p of points) {
      if (p.cx === undefined) { start = true; continue; }
      path += (start ? "M" : "L") + p.cx + " " + p.cy;
      start = false;
    }
    if (spec.style !== "scatter" && path) svg.append(svgNode("path",
      {d: path, fill: "none", stroke: color, "stroke-width": 2}));
    for (const p of points) {
      if (p.cx === undefined) continue;
      const dot = svgNode("circle", {cx: p.cx, cy: p.cy, r: 3, fill: color, "data-mark": p.id});
      const hint = svgNode("title");
      hint.textContent = [p.xCell, p.x, p.yCell, p.yFormula, p.y].filter(v => v != null).join(" · ");
      dot.append(hint);
      svg.append(dot);
    }
    for (const t of texts) {
      const node = svgNode("text", {x: t.cx, y: t.cy, "text-anchor": t.align, "data-mark": t.id});
      if (t.rotation) node.setAttribute("transform", "rotate(" + t.rotation + " " + t.cx + " " + t.cy + ")");
      node.textContent = t.text;
      svg.append(node);
    }
    svg.onfocus = () => { selected = field; status(); };
    svg.onclick = e => {
      const mark = e.target.closest("[data-mark]")?.dataset.mark;
      selected = field + (mark ? " / " + mark : "");
      status();
    };
    host.replaceChildren(svg);
    host.dataset.version = String(scene.version);
  });
}

// Called on demand by the host. CSS pixels are relative to this editor viewport.
window.inspectOfficeChart = function(id, version, field, offset) {
  const host = $("sheetChart"), canvas = $("canvas");
  if (document.hidden || !host || doc?.id !== id || field !== chartKey() || doc.version !== version ||
      host.dataset.version !== String(version) || !host.firstChild || sending || drafts.size ||
      composing || document.querySelector("dialog[open]")) return null;
  const clip = canvas.getBoundingClientRect(), rects = {}, inCanvas = [];
  const box = r => [r.x, r.y, r.width, r.height].map(n => Math.round(n * 100) / 100);
  for (const node of host.querySelectorAll("[data-mark]")) {
    const key = node.dataset.mark, m = /^[pv](\d+)$/.exec(key);
    if (m && (+m[1] < offset || +m[1] >= offset + 12)) continue;
    const rect = node.getBoundingClientRect();
    rects[key] = box(rect);
    if (rect.right > clip.left && rect.left < clip.left + canvas.clientWidth &&
        rect.bottom > clip.top && rect.top < clip.top + canvas.clientHeight) inCanvas.push(key);
  }
  return {unit: "CSS px", origin: "Office viewport top-left", rect_format: "x,y,width,height",
    measurement: "editor layout; desktop window occlusion is not measured",
    canvas_clip: [clip.left, clip.top, canvas.clientWidth, canvas.clientHeight],
    chart: box(host.firstChild.getBoundingClientRect()), marks: rects, intersects_canvas: inCanvas};
};

function bindChartDesign() {
  const dialog = $("chartDialog"), form = $("chartForm"), controls = form.elements;
  let edit;
  function toggleSource() {
    const fn = controls.source.value === "function";
    for (const [id, hidden] of [["rangeSource", fn], ["functionSource", !fn]]) {
      $(id).hidden = hidden;
      $(id).disabled = hidden;
    }
  }
  controls.source.onchange = toggleSource;
  $("chartDesign").onclick = () => job(async () => {
    await flush();
    const field = chartKey(), raw = doc.fields[field] ?? null;
    edit = {id: doc.id, field, raw};
    const spec = raw ? JSON.parse(raw) : {};
    for (const [name, fallback] of Object.entries({title: "", xTitle: "", yTitle: "", expression: "x^2",
      min: -5, max: 5, xRange: "A2:A12", yRange: "B2:B12", style: "line", color: "#3679b8"}))
      controls.namedItem(name).value = spec[name] ?? fallback;
    controls.labels.checked = !!spec.labels;
    controls.source.value = spec.expression ? "function" : "cells";
    toggleSource();
    $("removeChart").hidden = !raw;
    $("chartError").hidden = true;
    dialog.showModal();
  });
  async function save(value) {
    try {
      if (doc?.id !== edit.id) throw Error("当前文件已切换。");
      await action("apply", {version: doc.version, label: value ? "图表设计" : "移除图表",
        changes: [{field: edit.field, before: edit.raw, value}]});
      dialog.close();
    } catch (error) {
      $("chartError").textContent = error.message;
      $("chartError").hidden = false;
    }
  }
  form.onsubmit = e => {
    e.preventDefault();
    const spec = {};
    for (const k of ["title", "xTitle", "yTitle", "style", "color"]) spec[k] = controls.namedItem(k).value;
    spec.labels = controls.labels.checked;
    if (controls.source.value === "function") {
      spec.expression = controls.expression.value.trim();
      spec.min = controls.min.valueAsNumber;
      spec.max = controls.max.valueAsNumber;
    } else {
      spec.xRange = controls.xRange.value.trim().toUpperCase();
      spec.yRange = controls.yRange.value.trim().toUpperCase();
    }
    save(JSON.stringify(spec));
  };
  $("removeChart").onclick = () => save(null);
  $("closeChart").onclick = () => dialog.close();
}
