import * as VSCode from 'vscode';
import { randomBytes } from 'node:crypto';

export function viewHtml(webview: VSCode.Webview, root: VSCode.Uri): string {
  const nonce = randomBytes(24).toString('base64');
  const asset = (name: string) =>
    webview.asWebviewUri(VSCode.Uri.joinPath(root, 'media', name));
  return `<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource}; script-src 'nonce-${nonce}'; img-src ${webview.cspSource};">
<link rel="stylesheet" href="${asset('chat.css')}"><title>Forge</title></head><body>
<header><div class="brand">FORGE <span id="status" role="status" aria-live="polite"></span></div>
<div class="toolbar"><button id="connect" type="button"></button><button id="settings" type="button"></button></div></header>
<main id="messages" aria-label="Conversation" aria-live="polite" aria-relevant="additions">
<section id="empty"><h2 id="welcome"></h2><p id="intro"></p>
<div id="onboard" hidden>
  <ol id="onboardSteps">
    <li id="stepPython" class="todo"><span class="step-num">1</span><span class="step-body"></span></li>
    <li id="stepKey" class="todo"><span class="step-num">2</span><span class="step-body"></span></li>
    <li id="stepConnect" class="todo"><span class="step-num">3</span><span class="step-body"></span></li>
  </ol>
  <div id="pythonRow" hidden>
    <label id="pythonLabel" class="sr-only" for="pythonPath"></label>
    <input id="pythonPath" type="text" spellcheck="false" autocomplete="off">
    <button id="pythonScan" type="button" class="primary"></button>
    <span id="pythonStatus" class="hint" role="status" aria-live="polite"></span>
  </div>
  <div id="keyRow" hidden>
    <label id="keyLabel" class="sr-only" for="keyVendor"></label>
    <select id="keyVendor"></select>
    <label id="keyInputLabel" class="sr-only" for="keyValue"></label>
    <input id="keyValue" type="password" spellcheck="false" autocomplete="off" placeholder="sk-…">
    <button id="keySave" type="button" class="primary"></button>
    <span id="keyStatus" class="hint" role="status" aria-live="polite"></span>
  </div>
</div>
<div class="suggestions">
<button id="explain" type="button"></button><button id="review" type="button"></button></div><p id="trust"></p></section></main>
<div id="activity" role="status" aria-live="polite"></div>
<form id="composer"><div id="attachments"></div><label id="promptLabel" class="sr-only" for="prompt"></label>
<textarea id="prompt" rows="4" maxlength="16000"></textarea>
<div class="context-row"><button id="attach" type="button"></button><button id="selection" type="button"></button><button id="changes" type="button"></button></div>
<div class="options"><label><span id="modelLabel"></span><select id="model"></select></label><label><span id="modeLabel"></span><select id="mode"><option value="read-only"></option><option value="workspace-write"></option></select></label><label><span id="planningLabel"></span><select id="planning"><option value="high"></option><option value="medium"></option><option value="low"></option><option value="none"></option></select></label></div>
<details><summary id="phaseSettings"></summary><div class="options"><label><span id="planningModelLabel"></span><select id="planningModel"></select></label><label><span id="reviewModelLabel"></span><select id="reviewModel"></select></label><label><span id="reviewEnabledLabel"></span><select id="reviewEnabled"><option value="off"></option><option value="on"></option></select></label></div></details>
<div class="send-row"><span id="hint"></span><button id="stop" class="secondary" type="button" hidden></button><button id="send" class="primary" type="submit"></button></div>
<p id="boundary"></p></form><script nonce="${nonce}" src="${asset('chat.js')}"></script></body></html>`;
}
