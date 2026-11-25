"use strict";
function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}
function renderOffice(state, page) {
  const root = document.querySelector("main");
  if (state.kind === "presentation") {
    const board = slideBoard(state, state.slideOrder[page - 1], "projected");
    root.replaceChildren(board);
    fitSlideText(board);
  } else {
    const paper = el("article", undefined, "paper");
    for (const key of Object.keys(state.fields).filter(k => /^P\d+$/.test(k)).sort((a,b) => Number(a.slice(1))-Number(b.slice(1)))) {
      paper.append(el("p", state.fields[key] || "\u00a0", state.fields[key + ".style"] || "paragraph"));
    }
    root.replaceChildren(paper);
  }
  return true;
}
