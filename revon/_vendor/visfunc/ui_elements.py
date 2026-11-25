"""List the UI elements of a window through Windows UI Automation, for agents.

OCR only sees text. Buttons that show an icon, checkbox states, disabled controls and what is
typed in a field are known to the application itself, and Windows exposes them through UI
Automation. This module asks for them in one cached call per window:

    elements = list_elements()                   # the foreground window
    elements = list_elements(title="Settings")   # the first visible window whose title contains it
    target = find_element(elements, "Refresh")[0]["center_px"]

Coordinates are physical screen pixels, the same space as `motionspec settle --screen` and the
screen-text reader when the screenshot covers the whole screen. Only Windows is supported, and
an application that does not implement UI Automation shows up as one opaque pane; fall back to
screen text for those.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Sequence
from typing import Any

Box = tuple[int, int, int, int]
MAX_ELEMENTS = 5000

# UI Automation property and control type ids (UIAutomationClient.h).
_PROPERTIES = {
    "process_id": 30002,
    "runtime_id": 30000,
    "name": 30005,
    "control_type": 30003,
    "automation_id": 30011,
    "class_name": 30012,
    "rect": 30001,
    "enabled": 30010,
    "offscreen": 30022,
    "focused": 30008,
    "help_text": 30013,
    "value": 30045,
    "password": 30019,
    "toggle_state": 30086,
    "toggle_available": 30041,
    "selection_item_available": 30036,
    "selected": 30079,
}
CONTROL_TYPES = {
    50000: "button",
    50001: "calendar",
    50002: "checkbox",
    50003: "combobox",
    50004: "edit",
    50005: "hyperlink",
    50006: "image",
    50007: "list_item",
    50008: "list",
    50009: "menu",
    50010: "menu_bar",
    50011: "menu_item",
    50012: "progress_bar",
    50013: "radio_button",
    50014: "scroll_bar",
    50015: "slider",
    50016: "spinner",
    50017: "status_bar",
    50018: "tab",
    50019: "tab_item",
    50020: "text",
    50021: "toolbar",
    50022: "tooltip",
    50023: "tree",
    50024: "tree_item",
    50025: "custom",
    50026: "group",
    50027: "thumb",
    50028: "data_grid",
    50029: "data_item",
    50030: "document",
    50031: "split_button",
    50032: "window",
    50033: "pane",
    50034: "header",
    50035: "header_item",
    50036: "table",
    50037: "title_bar",
    50038: "separator",
}
# Things an agent can act on; other elements are listed only when they carry a name.
INTERACTIVE = {
    "button",
    "checkbox",
    "combobox",
    "edit",
    "hyperlink",
    "list_item",
    "menu_item",
    "radio_button",
    "slider",
    "spinner",
    "tab_item",
    "tree_item",
    "data_item",
    "split_button",
    "header_item",
    "document",
}
TOGGLE = {0: "off", 1: "on", 2: "indeterminate"}


def _require_windows() -> None:
    if sys.platform != "win32":
        raise OSError("UI Automation is only available on Windows")


class _Client:
    """One UI Automation client per thread (COM objects are apartment-bound)."""

    def __init__(self) -> None:
        _require_windows()
        import ctypes

        import comtypes
        import comtypes.client

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            pass
        try:
            comtypes.CoInitialize()
            self.owns_com = True
        except OSError:
            self.owns_com = False  # Already initialised in another mode on this thread.
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as uia

        self.uia = uia
        self.client = comtypes.client.CreateObject(uia.CUIAutomation, interface=uia.IUIAutomation)
        self.cache = self.client.CreateCacheRequest()
        for prop in _PROPERTIES.values():
            self.cache.AddProperty(prop)
        self.cache.TreeScope = uia.TreeScope_Element | uia.TreeScope_Descendants
        self.everything = self.client.CreateTrueCondition()


_clients = threading.local()


def _client() -> _Client:
    client = getattr(_clients, "current", None)
    if client is None:
        client = _clients.current = _Client()
    return client


def release_thread_client() -> None:
    """Release interfaces on their owning worker before its COM apartment exits."""
    client = getattr(_clients, "current", None)
    if client is not None:
        owns_com = client.owns_com
        del _clients.current
        del client
        if owns_com:
            import comtypes

            comtypes.CoUninitialize()


def find_windows(title: str) -> list[int]:
    """Handles of all visible windows whose title contains `title`, topmost first."""
    _require_windows()
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    want = title.casefold()
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                if want in buffer.value.casefold():
                    found.append(hwnd)
        return True

    user32.EnumWindows(visit, 0)
    return found


def find_window(title: str) -> int:
    """Handle of the topmost visible window whose title contains `title` (case-insensitive).

    Uses plain Win32 calls: asking every top-level window for its name over UI Automation is slow
    and fails on windows that are busy or closing.
    """
    _require_windows()
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    want = title.casefold()
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                if want in buffer.value.casefold():
                    found.append(hwnd)
                    return False
        return True

    user32.EnumWindows(visit, 0)  # Visits windows in z-order, topmost first.
    if not found:
        raise ValueError(f"no visible window title contains {title!r}")
    return found[0]


def window_title(hwnd: int) -> str:
    import ctypes

    user32 = ctypes.windll.user32
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _window(client: _Client, title: str | None, hwnd: int | None) -> Any:
    import ctypes

    if hwnd is None:
        hwnd = (
            find_window(title) if title is not None else ctypes.windll.user32.GetForegroundWindow()
        )
        if not hwnd:
            raise ValueError("there is no foreground window")
    return client.client.ElementFromHandle(hwnd)


def _cached(element: Any, prop: int) -> Any:
    try:
        return element.GetCachedPropertyValue(prop)
    except Exception:  # noqa: BLE001 - a provider may fail on any single property
        return None


def _describe(element: Any) -> dict | None:
    rect = _cached(element, _PROPERTIES["rect"])
    if rect is None or len(rect) != 4:
        return None
    left, top, width, height = (round(v) for v in rect)
    if width <= 0 or height <= 0 or _cached(element, _PROPERTIES["offscreen"]):
        return None
    kind = CONTROL_TYPES.get(_cached(element, _PROPERTIES["control_type"]), "other")
    name = (_cached(element, _PROPERTIES["name"]) or "").strip()
    if not name and kind not in INTERACTIVE:
        return None
    box = [left, top, left + width, top + height]
    item: dict[str, Any] = {
        "name": name,
        "control_type": kind,
        "bbox_px": box,
        "center_px": [left + width // 2, top + height // 2],
        "enabled": bool(_cached(element, _PROPERTIES["enabled"])),
    }
    for key in ("automation_id", "class_name", "help_text"):
        value = _cached(element, _PROPERTIES[key])
        if value:
            item[key] = str(value)
    runtime_id = _cached(element, _PROPERTIES["runtime_id"])
    if isinstance(runtime_id, (tuple, list)) and runtime_id:
        item["element_ref"] = "uia:" + ".".join(map(str, runtime_id))
    else:
        item["element_ref"] = "box:" + ",".join(map(str, box))
    if _cached(element, _PROPERTIES["focused"]):
        item["focused"] = True
    if _cached(element, _PROPERTIES["selection_item_available"]):
        selected = _cached(element, _PROPERTIES["selected"])
        if selected is not None:
            item["selected"] = bool(selected)
    value = (
        None
        if _cached(element, _PROPERTIES["password"])
        else _cached(element, _PROPERTIES["value"])
    )
    if isinstance(value, str) and value and kind in ("edit", "combobox", "document", "spinner"):
        item["value"] = value
    if _cached(element, _PROPERTIES["toggle_available"]):
        toggle = _cached(element, _PROPERTIES["toggle_state"])
        if toggle in TOGGLE:
            item["toggle_state"] = TOGGLE[toggle]
    return item


def _intersects(box: Sequence[int], region: Sequence[int]) -> bool:
    return min(box[2], region[2]) > max(box[0], region[0]) and min(box[3], region[3]) > max(
        box[1], region[1]
    )


_woken: set[int] = set()


def _has_document(client: _Client, found: Any) -> bool:
    for i in range(found.Length):
        if _cached(found.GetElement(i), _PROPERTIES["control_type"]) == 50030:  # Document
            return True
    return False


def _descendants(client: _Client, window: Any) -> Any:
    """All elements of a window, waking Chromium's page accessibility on first contact.

    Chrome, Edge and Electron expose only their own frame until an accessibility client asks;
    the page (a Document element) appears in the tree a moment later. Without this, the first
    read of a browser window lists the toolbar and none of the page.
    """
    scope = client.uia.TreeScope_Element | client.uia.TreeScope_Descendants
    found = window.FindAllBuildCache(scope, client.everything, client.cache)
    try:
        hwnd = int(window.CurrentNativeWindowHandle or 0)
        chromium = (window.CurrentClassName or "").startswith("Chrome_WidgetWin")
    except Exception:  # noqa: BLE001 - a provider may refuse; treat as a plain window
        return found
    if not chromium or hwnd in _woken:
        return found
    _woken.add(hwnd)
    if _has_document(client, found):
        return found
    deadline = time.perf_counter() + 3.0
    while time.perf_counter() < deadline:
        time.sleep(0.25)
        found = window.FindAllBuildCache(scope, client.everything, client.cache)
        if _has_document(client, found):
            time.sleep(0.25)  # let the page's controls follow the document node
            return window.FindAllBuildCache(scope, client.everything, client.cache)
    return found


def list_elements(
    *,
    title: str | None = None,
    hwnd: int | None = None,
    region: Sequence[int] | None = None,
    max_elements: int = MAX_ELEMENTS,
    process_id: int | None = None,
) -> dict:
    """Describe the visible elements of one window, in reading order."""
    client = _client()
    start = time.perf_counter()
    window = _window(client, title, hwnd)
    found = _descendants(client, window)
    items = []
    truncated = found.Length > max_elements
    for i in range(min(found.Length, max_elements)):
        element = found.GetElement(i)
        if process_id is not None:
            if (_cached(element, _PROPERTIES["process_id"]) != process_id
                    or _cached(element, _PROPERTIES["password"])):
                continue
        item = _describe(element)
        if item is not None and (region is None or _intersects(item["bbox_px"], region)):
            items.append(item)
    items.sort(key=lambda e: (e["bbox_px"][1], e["bbox_px"][0]))
    rect = window.CurrentBoundingRectangle
    return {
        "version": "svsg.ui-elements/1",
        "coordinate_space": "screen_physical_pixels_xyxy",
        "window": {
            "title": window.CurrentName or "",
            "bbox_px": [rect.left, rect.top, rect.right, rect.bottom],
            "process_id": window.CurrentProcessId,
        },
        "elements": items,
        "truncated": truncated,
        "elapsed_ms": round((time.perf_counter() - start) * 1000, 1),
    }


def _plain(text: str) -> str:
    return "".join(text.split()).casefold()


def find_element(
    result: dict, query: str, *, control_type: str | None = None, exact: bool = False
) -> list[dict]:
    """Elements whose name, help text, id or value matches `query`; enabled ones rank first."""
    want = _plain(query)
    if not want:
        raise ValueError("query must contain text")
    hits = []
    for element in result["elements"]:
        if control_type is not None and element["control_type"] != control_type:
            continue
        names = [
            element["name"],
            element.get("help_text", ""),
            element.get("automation_id", ""),
            element.get("value", ""),
        ]
        plain = [_plain(n) for n in names if n]
        if any(p == want for p in plain):
            hits.append((0, element))
        elif not exact and any(want in p for p in plain):
            hits.append((1, element))
    hits.sort(
        key=lambda h: (
            h[0],
            not h[1]["enabled"],
            h[1]["control_type"] not in INTERACTIVE,
            len(h[1]["name"]),
        )
    )
    return [element for _, element in hits]


_PATTERNS = {"invoke": 10000, "value": 10002, "expand": 10005, "select": 10010, "toggle": 10015}


def _action_target(items, query, action, control_type, element_ref):
    if action == "set_text":
        items = [
            e for e in items if e["control_type"] in ("edit", "combobox", "document", "spinner")
        ]
    if element_ref is not None:
        items = [e for e in items if e.get("element_ref") == element_ref]
    listed = {"elements": items}
    ranked = find_element(listed, query, control_type=control_type, exact=True)
    if not ranked:
        ranked = find_element(listed, query, control_type=control_type)
    if not ranked:
        raise ValueError(f"no current element matches {query!r}; list controls again")
    enabled = [e for e in ranked if e["enabled"]]
    if not enabled:
        raise ValueError(f"{ranked[0]['name'] or query!r} is disabled")
    candidates = [e for e in enabled if e["control_type"] in INTERACTIVE] or enabled
    if len(candidates) != 1:
        choices = [
            {k: e.get(k) for k in ("name", "control_type", "element_ref", "center_px")}
            for e in candidates[:12]
        ]
        raise ValueError(f"ambiguous control {query!r}; choose an element_ref: {choices}")
    return candidates[0]


def perform(
    query: str,
    action: str,
    *,
    title: str | None = None,
    hwnd: int | None = None,
    text: str | None = None,
    control_type: str | None = None,
    element_ref: str | None = None,
) -> dict:
    """Act on the best element matching `query` through UI Automation patterns.

    ``click`` invokes a button or link, toggles a checkbox, selects a list or tab item, or
    expands/collapses a menu; ``set_text`` replaces a field's value. No mouse or keyboard input
    is sent, so the window does not need focus and the user's pointer does not move. Returns the
    element as described before and after the action.
    """
    if action not in ("click", "set_text"):
        raise ValueError("action must be click or set_text")
    if action == "set_text" and text is None:
        raise ValueError("set_text needs text")
    client = _client()
    window = _window(client, title, hwnd)
    found = _descendants(client, window)
    pairs = []
    for i in range(min(found.Length, MAX_ELEMENTS)):
        element = found.GetElement(i)
        item = _describe(element)
        if item is not None:
            pairs.append((item, element))
    target = _action_target([item for item, _ in pairs], query, action, control_type, element_ref)
    title_before_action = window.CurrentName or ""
    element = next(el for item, el in pairs if item is target)
    uia = client.uia

    def pattern(name, interface):
        raw = element.GetCurrentPattern(_PATTERNS[name])
        return raw.QueryInterface(interface) if raw else None

    done = None
    if action == "set_text":
        value = pattern("value", uia.IUIAutomationValuePattern)
        if value is None:
            raise ValueError(f"{target['name'] or query!r} does not accept text")
        if value.CurrentIsReadOnly:
            raise ValueError(f"{target['name'] or query!r} is read-only")
        value.SetValue(text)
        done = "set_value"
    else:
        for name, interface, call in (
            ("invoke", uia.IUIAutomationInvokePattern, lambda p: p.Invoke()),
            ("toggle", uia.IUIAutomationTogglePattern, lambda p: p.Toggle()),
            ("select", uia.IUIAutomationSelectionItemPattern, lambda p: p.Select()),
            (
                "expand",
                uia.IUIAutomationExpandCollapsePattern,
                lambda p: p.Collapse() if p.CurrentExpandCollapseState == 1 else p.Expand(),
            ),
        ):
            handle = pattern(name, interface)
            if handle is not None:
                call(handle)
                done = name
                break
        if done is None:
            raise ValueError(f"{target['name'] or query!r} cannot be clicked through UI Automation")
    after = None
    time.sleep(0.05)  # let the application process the action before reading it back
    try:
        refreshed = element.BuildUpdatedCache(client.cache)
        after = _describe(refreshed)
    except Exception:  # noqa: BLE001 - the element may be gone (a dialog closed)
        pass
    return {
        "action": action,
        "performed": done,
        "element": target,
        "after": after,
        "window": title_before_action,
    }


def post_click(root: int, x: int, y: int) -> dict:
    """Click at screen point (x, y) inside window `root` by posting mouse messages.

    The pointer does not move and the window does not need focus. The point must lie inside the
    window and must not be covered by another application's window. Applications that read the
    hardware mouse state instead of messages (some games, some custom controls) ignore it.
    """
    _require_windows()
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetAncestor.restype = wintypes.HWND
    rect = wintypes.RECT()
    user32.GetWindowRect(root, ctypes.byref(rect))
    if not (rect.left <= x < rect.right and rect.top <= y < rect.bottom):
        bounds = [rect.left, rect.top, rect.right, rect.bottom]
        raise ValueError(f"point ({x}, {y}) is outside the window {bounds}")
    point = wintypes.POINT(x, y)
    target = user32.WindowFromPoint(point)
    owner = wintypes.DWORD()
    mine = wintypes.DWORD()
    user32.GetWindowThreadProcessId(target, ctypes.byref(owner))
    user32.GetWindowThreadProcessId(root, ctypes.byref(mine))
    if not target or owner.value != mine.value:
        raise ValueError(f"point ({x}, {y}) is covered by another application's window")
    local = wintypes.POINT(x, y)
    if not user32.ScreenToClient(target, ctypes.byref(local)):
        raise OSError("could not map the screen point into the target window")
    lparam = (local.y & 0xFFFF) << 16 | (local.x & 0xFFFF)
    receipt = {
        "window": window_title(root),
        "target_window": window_title(target),
        "client_point": [local.x, local.y],
        "screen_point": [x, y],
        "dispatch_state": "completed",
    }
    for message, wparam in ((0x0200, 0), (0x0201, 0x0001), (0x0202, 0)):  # move, down, up
        if not user32.PostMessageW(target, message, wparam, lparam):
            if message == 0x0202:
                receipt["dispatch_state"] = "partial"
                break
            raise OSError("the target rejected the mouse message before button down")
        time.sleep(0.02)
    return receipt
