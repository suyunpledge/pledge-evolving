// Forge 图标 v2 —— 24 网格，活动区 3–21，1.75 圆头描边，圆角 2–2.5。
// 只用 line / polyline / polygon / circle / rect / arc，可无损落回 ui_icons.ICONS。
(function () {
  function gear() {
    const pts = [];
    for (let i = 0; i < 6; i++) {
      const a = (Math.PI * 2 * i) / 6 - Math.PI / 2;
      for (const [r, o] of [[7.1, -0.4], [9.4, -0.22], [9.4, 0.22], [7.1, 0.4]]) {
        pts.push((12 + r * Math.cos(a + o)).toFixed(2) + ',' + (12 + r * Math.sin(a + o)).toFixed(2));
      }
    }
    return `<polygon points="${pts.join(' ')}"/><circle cx="12" cy="12" r="2.9"/>`;
  }
  const spark = (cx, cy, r) => {
    const k = r * 0.24;
    return `<path d="M${cx} ${cy - r}L${cx + k} ${cy - k}L${cx + r} ${cy}L${cx + k} ${cy + k}L${cx} ${cy + r}L${cx - k} ${cy + k}L${cx - r} ${cy}L${cx - k} ${cy - k}Z"/>`;
  };
  const I = {
    chat: '<path d="M6 4.5h12A2.5 2.5 0 0 1 20.5 7v8a2.5 2.5 0 0 1-2.5 2.5h-6.5L7 21v-3.5H6A2.5 2.5 0 0 1 3.5 15V7A2.5 2.5 0 0 1 6 4.5z"/>',
    task: '<rect x="5" y="4.5" width="14" height="16.5" rx="2.5"/><rect x="9" y="2.75" width="6" height="3.5" rx="1.25"/><path d="M9 13l2.2 2.2L15.5 11"/>',
    agents: '<rect x="4.5" y="8" width="15" height="11.5" rx="3.5"/><path d="M12 8V5.4"/><circle class="f" cx="12" cy="4.2" r="1.2"/><circle class="f" cx="9.4" cy="13.6" r="1.15"/><circle class="f" cx="14.6" cy="13.6" r="1.15"/><path d="M2.5 12.5v3M21.5 12.5v3"/>',
    connector: '<path d="M9 3v4M15 3v4M6.5 7h11v3.5a5.5 5.5 0 0 1-11 0z M12 16v5"/>',
    knowledge: '<path d="M3 5.5c3-.8 6-.5 9 1.5v13c-3-2-6-2.3-9-1.5z"/><path d="M21 5.5c-3-.8-6-.5-9 1.5v13c3-2 6-2.3 9-1.5z"/>',
    evolution: '<path d="M12 21v-8.5"/><path d="M12 14.5c-4 0-6.8-2.5-6.8-6.8 4 0 6.8 2.7 6.8 6.8z"/><path d="M12 12c0-4.3 2.7-7.3 6.8-7.3 0 4.3-2.7 7.3-6.8 7.3z"/>',
    tools: '<path d="M4 7h4M12 7h8M4 17h8M16 17h4"/><circle cx="10" cy="7" r="2"/><circle cx="14" cy="17" r="2"/>',
    files: '<path d="M3.5 7a2 2 0 0 1 2-2h3.8l2 2.2h7.2a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/>',
    file: '<path d="M7 3h7l5 5v11a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/><path d="M14 3v5h5M9 13h6M9 17h4"/>',
    code: '<path d="M8.5 7.5 4 12l4.5 4.5M15.5 7.5 20 12l-4.5 4.5M13.5 5l-3 14"/>',
    config: gear(),
    search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.4 15.4 20.5 20.5"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    minus: '<path d="M5 12h14"/>',
    close: '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
    more: '<circle class="f" cx="5.5" cy="12" r="1.5"/><circle class="f" cx="12" cy="12" r="1.5"/><circle class="f" cx="18.5" cy="12" r="1.5"/>',
    chevron_right: '<path d="M9.5 6l6 6-6 6"/>',
    chevron_left: '<path d="M14.5 6l-6 6 6 6"/>',
    chevron_down: '<path d="M6 9.5l6 6 6-6"/>',
    chevron_up: '<path d="M6 14.5l6-6 6 6"/>',
    refresh: '<path d="M20 12a8 8 0 1 1-2.34-5.66L20 8.5"/><path d="M20 4v4.5h-4.5"/>',
    external: '<path d="M14 4h6v6M20 4l-8.5 8.5M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4"/>',
    sidebar: '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="M9.5 4.5v15"/>',
    inspector: '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="M14.5 4.5v15M17 8.5h1M17 11.5h1"/>',
    workspace: '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="M3.5 9.5h17M10 9.5v10"/>',
    branch: '<circle cx="6.5" cy="5.5" r="2"/><circle cx="6.5" cy="18.5" r="2"/><circle cx="17.5" cy="8" r="2"/><path d="M6.5 7.5v9M17.5 10c0 3-2.5 4-6 4H6.5"/>',
    diff: '<path d="M7 3h7l5 5v11a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/><path d="M12 8.5v5M9.5 11h5M9.5 17h5"/>',
    send: '<path d="M12 19V5.5M6 11.5l6-6 6 6"/>',
    play: '<path class="f" d="M8 5.6v12.8a1 1 0 0 0 1.53.85l10.1-6.4a1 1 0 0 0 0-1.7L9.53 4.75A1 1 0 0 0 8 5.6z"/>',
    stop: '<rect class="f" x="6.5" y="6.5" width="11" height="11" rx="2.2"/>',
    check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    check_circle: '<circle cx="12" cy="12" r="9"/><path d="M8 12.3l2.7 2.7L16 9.7"/>',
    warning: '<path d="M10.27 4.3a2 2 0 0 1 3.46 0l7.2 12.7a2 2 0 0 1-1.73 3H4.8a2 2 0 0 1-1.73-3z"/><path d="M12 9.5v4"/><circle class="f" cx="12" cy="16.8" r="1.1"/>',
    error: '<circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5"/><circle class="f" cx="12" cy="7.8" r="1.1"/>',
    copy: '<rect x="8.5" y="8.5" width="12" height="12" rx="2.5"/><path d="M15.5 8.5V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v7.5a2 2 0 0 0 2 2h2.5"/>',
    thumb_up: '<path d="M8 10.5 11.3 3.6a2.1 2.1 0 0 1 2.5 2.4l-.6 3.5h5a2 2 0 0 1 2 2.4l-1.3 6.4a2 2 0 0 1-2 1.7H8z"/><rect x="3.5" y="10.5" width="4.5" height="9.5" rx="1.5"/>',
    thumb_down: '<g transform="matrix(1 0 0 -1 0 24)"><path d="M8 10.5 11.3 3.6a2.1 2.1 0 0 1 2.5 2.4l-.6 3.5h5a2 2 0 0 1 2 2.4l-1.3 6.4a2 2 0 0 1-2 1.7H8z"/><rect x="3.5" y="10.5" width="4.5" height="9.5" rx="1.5"/></g>',
    paperclip: '<path d="M20 11.5l-7.8 7.8a5 5 0 0 1-7.1-7.1l8.2-8.2a3.3 3.3 0 0 1 4.7 4.7l-8.2 8.2a1.7 1.7 0 0 1-2.4-2.4L15 6.9"/>',
    key: '<circle cx="8" cy="15.5" r="4.5"/><path d="M11.2 12.3 20 3.5M16.5 7l2.5 2.5M14 9.5l2 2"/>',
    model: '<path d="M12 3 20 7.5v9L12 21l-8-4.5v-9z"/><path d="M4 7.5l8 4.5 8-4.5M12 12v9"/>',
    terminal: '<rect x="3" y="4.5" width="18" height="15" rx="2.5"/><path d="M7 9.5l3 2.5-3 2.5M12.5 15H17"/>',
    preview: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>',
    image: '<rect x="3.5" y="4" width="17" height="16" rx="2.5"/><circle cx="9" cy="9.5" r="1.6"/><path d="M20.5 15.5 16 11l-9 9"/>',
    user: '<circle cx="12" cy="8" r="4"/><path d="M4.5 20.5a7.5 7.5 0 0 1 15 0"/>',
    edit: '<path d="M15.5 4.5l4 4L9 19H5v-4z M13.2 6.8l4 4"/>',
    folder_open: '<path d="M3.5 18V7a2 2 0 0 1 2-2h3.8l2 2.2h6.2a2 2 0 0 1 2 2V11"/><path d="M3.6 18.4 6.1 12a1.5 1.5 0 0 1 1.4-1H20a1 1 0 0 1 .94 1.35L18.6 18.5a1.5 1.5 0 0 1-1.4 1H4.5a1 1 0 0 1-.9-1.1z"/>',
    save: '<path d="M6 3.5h10l4 4v11a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-13a2 2 0 0 1 2-2z"/><path d="M8 3.5v4h7v-4M8 20.5v-6h8v6"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7.5V12l3 2"/>',
    history: '<path d="M3.6 12A8.5 8.5 0 1 0 6 6"/><path d="M3.5 3.8V8h4.2M12 8v4l2.8 1.8"/>',
    pin: '<path d="M9 3.5h6M10 3.5V9l-3.5 4h11L14 9V3.5M12 13v7.5"/>',
    spark: spark(12, 12, 8.5) ,
    sparkles: spark(10, 13, 7) + spark(18.5, 5.5, 3),
    bolt: '<path d="M13 3 5 13.5h6L10.5 21 19 10.5h-6z"/>',
    cpu: '<rect x="6.5" y="6.5" width="11" height="11" rx="2"/><rect x="9.5" y="9.5" width="5" height="5" rx="1"/><path d="M9.5 3.5v3M14.5 3.5v3M9.5 17.5v3M14.5 17.5v3M3.5 9.5h3M3.5 14.5h3M17.5 9.5h3M17.5 14.5h3"/>',
    globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 2.6 3.8 5.6 3.8 9s-1.3 6.4-3.8 9c-2.5-2.6-3.8-5.6-3.8-9S9.5 5.6 12 3z"/>',
    cursor: '<path d="M5 4l5.5 15 2.3-6.2L19 10.5z"/><path d="M13.2 13.2 18.5 18.5"/>',
    timeline: '<circle cx="6" cy="5.5" r="1.9"/><circle cx="6" cy="12" r="1.9"/><circle cx="6" cy="18.5" r="1.9"/><path d="M6 7.4v2.7M6 13.9v2.7M10.5 5.5h9M10.5 12h6.5M10.5 18.5h8"/>',
    mic: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21"/>',
    at: '<circle cx="12" cy="12" r="3.5"/><path d="M15.5 12v1.5a2.5 2.5 0 0 0 5 0V12a8.5 8.5 0 1 0-3.5 6.9"/>',
    command: '<rect x="3.5" y="3.5" width="17" height="17" rx="4"/><path d="M14 8l-4 8"/>',
    layers: '<path d="M12 3.5 21 8l-9 4.5L3 8z"/><path d="M3 12.5l9 4.5 9-4.5M3 16.5l9 4.5 9-4.5"/>',
    hexagram: '<path d="M5 6h14M5 12h5.5M13.5 12H19M5 18h14"/>',
    flame: '<path d="M12 21c-3.9 0-6.5-2.6-6.5-6.2 0-3.6 2.7-5.6 4-8.8.6 1.8 1.6 2.8 2.8 3.4C12.6 6.6 13.6 4.4 15.5 3c-.3 3 3 5.9 3 11.3 0 4-2.7 6.7-6.5 6.7z"/><path d="M12 21c-1.7 0-2.8-1.2-2.8-2.8 0-1.8 1.4-2.7 2.3-4.2.9 1.5 3.3 2.3 3.3 4.4 0 1.5-1.1 2.6-2.8 2.6z"/>',
    anvil: '<path d="M3.5 7.5h11.5c.3 2.6 2.2 4.1 5.5 4.3v1.7h-5.3L12.8 16v2h2.7v2.5H6.5V18h2.7v-2L6.8 13.6C4.7 12.4 3.5 10.3 3.5 7.5z"/>' + spark(19, 4.5, 2.6),
    pause: '<rect class="f" x="6.5" y="5.5" width="3.8" height="13" rx="1.3"/><rect class="f" x="13.7" y="5.5" width="3.8" height="13" rx="1.3"/>',
    retry: '<path d="M4 12a8 8 0 0 1 13.66-5.66L20 8.5"/><path d="M20 4v4.5h-4.5"/><path d="M20 12a8 8 0 0 1-13.66 5.66L4 15.5"/><path d="M4 20v-4.5h4.5"/>',
    keyboard: '<rect x="2.5" y="6" width="19" height="12" rx="2.5"/><path d="M6.5 10h.01M10 10h.01M13.5 10h.01M17 10h.01M7.5 14.5h9"/>',
  };
  window.ICONS_V2 = I;
  window.NEW_ICON_NAMES = ['inspector', 'branch', 'history', 'pin', 'spark', 'sparkles', 'bolt', 'cpu', 'globe', 'cursor', 'timeline', 'mic', 'at', 'command', 'layers', 'anvil', 'pause', 'retry', 'keyboard'];
  window.svgIcon = function (name, size, set) {
    const body = (set || I)[name] || '';
    return `<svg class="ico" width="${size}" height="${size}" viewBox="0 0 24 24" aria-hidden="true">${body}</svg>`;
  };
  window.hydrateIcons = function (root) {
    (root || document).querySelectorAll('i[data-i]').forEach((el) => {
      el.outerHTML = window.svgIcon(el.dataset.i, +(el.dataset.s || 18)).replace('class="ico"', `class="ico ${el.className}"`);
    });
  };
})();
