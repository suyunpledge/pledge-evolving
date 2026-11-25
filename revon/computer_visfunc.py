"""Local VisFunc recognition for a single, already captured Windows client area."""
from ._vendor.visfunc import screen_text, ui_elements


def _label(item):
    return (item.get('name') or item.get('automation_id') or item.get('help_text') or '')[:160]


def _inside(point, rect):
    return rect[0] <= point[0] < rect[2] and rect[1] <= point[1] < rect[3]


def inspect_window(driver, frame, request):
    """Runs in the cancellable capture worker. All returned points are screenshot pixels."""
    client = frame['client_rect']
    origin_x, origin_y = frame['rect'][:2]
    query = request.get('target')
    role = request.get('role')
    try:
        tree = ui_elements.list_elements(hwnd=driver.window, process_id=driver.process_id,
                                         region=client, max_elements=512)
    except Exception:
        # An accessibility provider may throw COMError or return a malformed
        # property. The captured client image is still valid for local OCR.
        tree = None
    if tree is not None and tree['window']['process_id'] != driver.process_id:
        raise ValueError('Selected window changed during UI Automation')
    elements = []
    for item in (tree or {}).get('elements', []):
        if _inside(item['center_px'], client) and item['control_type'] not in ('window', 'title_bar'):
            # Do not send field values to the model, or use them as selectors.
            elements.append({k: v for k, v in item.items() if k != 'value'})

    def describe(item):
        result = {'label': _label(item), 'role': item['control_type'], 'enabled': item['enabled']}
        if item.get('automation_id'):
            # Qt UIA IDs end in the object's own name, after its ancestor path.
            result['id'] = item['automation_id'][-120:]
        if 'toggle_state' in item:
            result['checked'] = item['toggle_state']
        if item.get('focused'):
            result['focused'] = True
        if 'selected' in item:
            result['selected'] = item['selected']
        return result

    if not query:
        if len(elements) > 64:
            # A long page can put its active form below dozens of static labels.
            elements.sort(key=lambda item: (not item.get('focused'), not item.get('selected'),
                          item['control_type'] not in ui_elements.INTERACTIVE, not item['enabled']))
        return {'source': 'visfunc_uia', 'status': 'ok' if tree else 'unavailable',
                'controls': [describe(item) for item in elements[:64]],
                'truncated': len(elements) > 64 or bool(tree and tree['truncated'])}

    data = {'elements': elements}
    hits = (ui_elements.find_element(data, query, control_type=role, exact=True)
            or ui_elements.find_element(data, query, control_type=role))
    if hits:
        interactive = [item for item in hits if item['control_type'] in ui_elements.INTERACTIVE]
        candidates = interactive or hits
        if len(candidates) != 1 or tree['truncated']:
            return {'source': 'visfunc_uia', 'status': 'partial' if tree['truncated'] else 'ambiguous',
                    'candidates': [describe(item) for item in candidates[:8]]}
        item = candidates[0]
        if not item['enabled']:
            return {'source': 'visfunc_uia', 'status': 'disabled', 'control': describe(item)}
        x, y = item['center_px']
        return {'source': 'visfunc_uia', 'status': 'found', 'control': describe(item), 'point': {
            'found': True, 'x': x - origin_x, 'y': y - origin_y, 'label': _label(item)}}
    if role:
        return {'source': 'visfunc_uia', 'status': 'not_found' if tree else 'unavailable'}

    # The OCR input is the client crop of the exact PNG that backs this observation.
    left, top, right, bottom = client
    dx, dy = left - origin_x, top - origin_y
    try:
        bgr = screen_text.load_bgr(frame['png'])
        result = screen_text.ScreenTextReader().read(bgr[dy:bottom-origin_y, dx:right-origin_x])
    except ImportError:
        return {'source': 'visfunc_ocr', 'status': 'unavailable'}
    matches = screen_text.find_text(result, query, exact=True)
    unique = {tuple(hit['bbox_px']): hit for hit in matches}
    if len(unique) != 1:
        return {'source': 'visfunc_ocr', 'status': 'ambiguous' if unique else 'not_found',
                'match_count': len(unique)}
    hit = next(iter(unique.values()))
    if hit['low_confidence']:
        return {'source': 'visfunc_ocr', 'status': 'low_confidence'}
    x, y = hit['center_px']
    return {'source': 'visfunc_ocr', 'status': 'found', 'point': {
        'found': True, 'x': dx + x, 'y': dy + y, 'label': hit['text'][:160]}}
