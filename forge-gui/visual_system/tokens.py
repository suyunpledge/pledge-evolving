"""Semantic aliases to gui_theme, never a second palette or font registry."""
from types import MappingProxyType
import gui_theme as theme

COLOR_ROLES = MappingProxyType({
    'surface.root': 'bg', 'surface.navigation': 'sidebar', 'surface.panel': 'surface',
    'surface.editor': 'code_bg', 'surface.input': 'input_bg',
    'text.primary': 'text', 'text.body': 'body', 'text.secondary': 'subtext',
    'border.default': 'border', 'state.hover': 'hover', 'state.selected': 'accent_soft',
    'state.pressed': 'control_pressed', 'state.disabled': 'disabled_fg', 'state.focus': 'focus_ring',
    'action.primary': 'accent', 'action.primary-text': 'msg_user_fg',
    'status.info': 'info', 'status.success': 'ok', 'status.warning': 'warn', 'status.error': 'error',
})
FONT_ROLES = MappingProxyType({'body': 'FONT_UI', 'label': 'FONT_SMALL', 'title': 'FONT_TITLE',
                               'caption': 'FONT_CAPTION', 'code': 'FONT_MONO_SM'})


def color(role):
    return theme.C[COLOR_ROLES[role]]


def font(role):
    return getattr(theme, FONT_ROLES[role])


def px(widget, value):
    return theme.ui_px(widget, value)
