"""Window-scoped Windows accessibility actions that never request desktop input."""
from contextlib import contextmanager
import hashlib
import json
import time

from ._vendor.visfunc.ui_elements import CONTROL_TYPES


def _menu_action(role, available, state):
    # ExpandCollapseState: Collapsed, Expanded, PartiallyExpanded, LeafNode.
    if role == 'menu_item' and available is True:
        return {0: 'expand', 1: 'collapse', 2: 'expand'}.get(state)


def _set_value(handle, text, before_input, *, refresh=None, clock=time.monotonic, sleep=time.sleep):
    if handle.CurrentIsReadOnly:
        raise ValueError('文本框已变为只读。')
    before_input()
    handle.SetValue(text)
    # Chromium can acknowledge SetValue before its accessibility tree updates.
    # Poll only the same pattern on the same element; never repeat the write.
    start = clock()
    deadline = start + 1.5
    refresh_used = False
    while handle.CurrentValue != text:
        now = clock()
        if refresh is not None and now - start >= .25:
            refresh_used = refresh()
            refresh = None
            if refresh_used:
                continue
        remaining = deadline - now
        if remaining <= 0:
            raise RuntimeError('文本已派发，尚未读回预期值。请检查目标状态后再继续。')
        sleep(min(.05, remaining))
    return 'scroll_into_view' if refresh_used else None


@contextmanager
def window_controls(hwnd, pid, *, edge_page=False, verify_document=None):
    """Keep the exact observed COM elements alive until approval revalidation/dispatch finishes."""
    import comtypes

    # The Qt service runs each operation on a worker thread. Balance its COM
    # initialization and keep apartment-bound interfaces within this operation.
    comtypes.CoInitialize()
    try:
        yield from _snapshot(hwnd, pid, edge_page, verify_document)
    finally:
        comtypes.CoUninitialize()


def _edge_document(window, client, uia, pid):
    condition = client.CreateAndCondition(
        client.CreatePropertyCondition(uia.UIA_ProcessIdPropertyId, pid),
        client.CreatePropertyCondition(uia.UIA_ControlTypePropertyId, uia.UIA_DocumentControlTypeId))
    documents = window.FindAll(uia.TreeScope_Descendants, condition)
    title = window.CurrentName
    matches = []
    for i in range(documents.Length):
        document = documents.GetElement(i)
        name = document.CurrentName
        if name and (title == name or title.startswith(name + ' - ') or title.startswith(name + ' – ')):
            matches.append(document)
    if len(matches) != 1:
        raise ValueError('Edge 尚未提供当前网页的唯一控件树。请先选中目标标签页，待网页加载后再切回前台应用，并重新观察。')
    return matches[0]


def _snapshot(hwnd, pid, edge_page=False, verify_document=None):
    import comtypes.client
    comtypes.client.GetModule('UIAutomationCore.dll')
    from comtypes.gen import UIAutomationClient as uia

    targets = {}
    try:
        client = comtypes.client.CreateObject(uia.CUIAutomation8, interface=uia.IUIAutomation2)
        client.AutoSetFocus = False
        if client.AutoSetFocus:
            raise RuntimeError('Windows 未能关闭辅助功能自动聚焦。')
        client.ConnectionTimeout = 1500
        client.TransactionTimeout = 2500
        window = client.ElementFromHandle(hwnd)
        if window.CurrentProcessId != pid or window.CurrentNativeWindowHandle != hwnd:
            raise ValueError('所选窗口已变化，请重新选择。')
        if edge_page:
            browser = window
            window = _edge_document(window, client, uia, pid)
            document_id = tuple(window.GetRuntimeId())
            if verify_document:
                verify_document(document_id)
        properties = ('RuntimeId', 'Name', 'ControlType', 'IsEnabled', 'IsPassword',
                      'IsInvokePatternAvailable', 'IsTogglePatternAvailable',
                      'IsSelectionItemPatternAvailable', 'IsValuePatternAvailable',
                      'ToggleToggleState', 'SelectionItemIsSelected', 'ValueIsReadOnly', 'ValueValue',
                      'IsExpandCollapsePatternAvailable', 'ExpandCollapseExpandCollapseState')
        ids = {name: getattr(uia, 'UIA_' + name + 'PropertyId') for name in properties}
        cache = client.CreateCacheRequest()
        for prop in ids.values():
            cache.AddProperty(prop)
        own_process = client.CreatePropertyCondition(uia.UIA_ProcessIdPropertyId, pid)
        public_controls = client.CreateAndCondition(own_process, client.CreatePropertyCondition(ids['IsPassword'], False))
        patterns = [('toggle', 'IsTogglePatternAvailable'), ('select', 'IsSelectionItemPatternAvailable'),
                    ('invoke', 'IsInvokePatternAvailable'), ('value', 'IsValuePatternAvailable')]
        supported = [client.CreatePropertyCondition(ids[prop], True) for _, prop in patterns]
        menu = client.CreatePropertyCondition(ids['ControlType'], uia.UIA_MenuItemControlTypeId)
        expandable = client.CreatePropertyCondition(ids['IsExpandCollapsePatternAvailable'], True)
        supported.append(client.CreateAndCondition(menu, expandable))
        condition = supported[0]
        for other in supported[1:]:
            condition = client.CreateOrCondition(condition, other)
        found = window.FindAllBuildCache(uia.TreeScope_Descendants,
            client.CreateAndCondition(public_controls, condition), cache)
        elements, values = [], []
        for index in range(min(found.Length, 256)):
            element = found.GetElement(index)
            def get(name):
                return element.GetCachedPropertyValue(ids[name])
            runtime_id = get('RuntimeId')
            if (not isinstance(runtime_id, (list, tuple)) or not runtime_id
                    or get('IsPassword') != False or get('IsEnabled') != True):
                continue
            ref = 'uia:' + '.'.join(str(n) for n in runtime_id)
            if ref in targets:
                raise ValueError('窗口返回了重复控件标识，请重新读取控件。')
            role = CONTROL_TYPES.get(get('ControlType'), 'control')
            operations = {}
            row = {'ref': ref, 'class': role, 'name': str(get('Name') or '')[:256]}
            menu_action = _menu_action(role, get('IsExpandCollapsePatternAvailable'),
                                       get('ExpandCollapseExpandCollapseState'))
            if menu_action:
                operations[menu_action] = menu_action
            for op, available in patterns:
                if get(available) != True:
                    continue
                if op == 'value':
                    if role != 'title_bar' and get('ValueIsReadOnly') == False:
                        operations['type_text'] = op
                        # Keep the value out of the agent response and approval, but
                        # detect concurrent edits, including same-length replacements.
                        value = get('ValueValue')
                        if isinstance(value, str):
                            values.append((ref, hashlib.sha256(value.encode('utf-8', 'surrogatepass')).hexdigest()))
                else:
                    if not menu_action:
                        operations.setdefault('click', op)
                    if op == 'toggle':
                        row['checked'] = get('ToggleToggleState')
                    if op == 'select':
                        row['selected'] = get('SelectionItemIsSelected')
            if operations:
                row['actions'] = list(operations)
                elements.append(row)
                targets[ref] = (element, operations)
        state = {'elements': elements, 'truncated': found.Length > 256,
                 '_value_revision': hashlib.sha256(json.dumps(values).encode()).hexdigest()}
        if edge_page:
            state['document_ref'] = document_id

        def dispatch(ref, action, text, before_input):
            element, operations = targets[ref]
            if element.CurrentProcessId != pid or element.CurrentIsPassword or not element.CurrentIsEnabled:
                raise ValueError('后台控件已变化，请重新观察。')
            op = operations[action]
            pattern, method = {'toggle': ('Toggle', 'Toggle'), 'select': ('SelectionItem', 'Select'),
                               'invoke': ('Invoke', 'Invoke'), 'value': ('Value', 'SetValue'),
                               'expand': ('ExpandCollapse', 'Expand'), 'collapse': ('ExpandCollapse', 'Collapse')}[op]
            raw = element.GetCurrentPattern(getattr(uia, 'UIA_' + pattern + 'PatternId'))
            handle = raw.QueryInterface(getattr(uia, 'IUIAutomation' + pattern + 'Pattern'))
            if op == 'value':
                def refresh():
                    # Chromium can receive the edit while its occluded AX tree
                    # still advertises the old value. Refresh only its bound item.
                    current = _edge_document(browser, client, uia, pid)
                    if tuple(current.GetRuntimeId()) != document_id or element.CurrentIsPassword or not element.CurrentIsEnabled:
                        raise ValueError('网页或字段已变化，请核对写入结果。')
                    raw_scroll = element.GetCurrentPattern(uia.UIA_ScrollItemPatternId)
                    if not raw_scroll:
                        return False
                    scroll = raw_scroll.QueryInterface(uia.IUIAutomationScrollItemPattern)
                    before_input()
                    scroll.ScrollIntoView()
                    return True
                return _set_value(handle, text, before_input, refresh=refresh if edge_page else None)
            else:
                before_input()
                getattr(handle, method)()

        yield state, dispatch
    finally:
        targets.clear()
