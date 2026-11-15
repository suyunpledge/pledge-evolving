"""Explicit, display-only localization. Never translate user or plugin payloads.

Messages retain the source string for IDs/comparisons/persistence. Widgets render
them using their owning Tk interpreter; switching never rebuilds the UI tree.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
from string import Formatter
import sys
import tkinter as tk
from tkinter import messagebox as _messagebox
import weakref

LANGUAGES = {
    "zh-CN": "简体中文", "zh-TW": "繁體中文", "en": "English",
    "ja": "日本語", "ko": "한국어", "de": "Deutsch", "fr": "Français",
    "es": "Español", "pt": "Português", "ru": "Русский",
}
DIRECTORY = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "locales"


def normalize_language(value):
    if not isinstance(value, str):
        return "zh-CN"
    value = value.replace("_", "-")
    aliases = {"zh": "zh-CN", "zh-Hans": "zh-CN", "zh-Hant": "zh-TW"}
    value = aliases.get(value, value)
    return value if value in LANGUAGES else value.split("-")[0] if value.split("-")[0] in LANGUAGES else "zh-CN"


def fields(value):
    names = []
    for _, name, spec, conversion in Formatter().parse(value):
        if name is not None:
            if not re.fullmatch(r"[a-zA-Z_][a-zA-Z_0-9]*", name) or "{" in spec or conversion:
                raise ValueError("Unsafe translation placeholder")
            names.append((name, spec))
    return sorted(names)


def _read(directory, language):
    try:
        path = directory / (language + ".json")
        if path.stat().st_size > 1024 * 1024:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


class Message(str):
    def __new__(cls, source, **arguments):
        obj = str.__new__(cls, source)
        obj.arguments = arguments
        return obj

    def render(self, language="zh-CN"):
        return Translator(language).render(self)


def tr(source, **arguments):
    return Message(source, **arguments)


def join(separator, messages):
    parts = list(messages)
    return tr(separator.join("{p" + str(i) + "}" for i in range(len(parts))),
              **{f"p{i}": part for i, part in enumerate(parts)})


class Translator:
    def __init__(self, language="zh-CN", *, directory=DIRECTORY):
        self.language = normalize_language(language)
        self.directory = Path(directory)
        self._catalogs = {}
        self._bindings = weakref.WeakValueDictionary()

    def render(self, message):
        if not isinstance(message, Message):
            return message
        source = str(message)
        if self.language not in self._catalogs:
            self._catalogs[self.language] = _read(self.directory, self.language)
        translated = self._catalogs[self.language].get(source, source)
        try:
            if not isinstance(translated, str) or not translated.strip() or fields(translated) != fields(source):
                translated = source
        except ValueError:
            translated = source
        # 参数里恰好是已登记文案的普通字符串也翻译（如 pill_button 的 label）。
        args = {k: self.render(as_message(v)) for k, v in message.arguments.items()}
        if not args:
            return translated
        try:
            return translated.format(**args)
        except (ValueError, KeyError, TypeError):
            try:
                return source.format(**args)
            except (ValueError, KeyError, TypeError):
                return source

    def register(self, target):
        self._bindings[id(target)] = target

    def switch(self, language):
        self.language = normalize_language(language)
        for target in list(self._bindings.values()):
            try:
                if hasattr(target, "_l10n_refresh"):
                    target._l10n_refresh()
                for name, message in list(getattr(target, "_l10n_custom", {}).items()):
                    getattr(target, name)(message)
            except tk.TclError:
                self._bindings.pop(id(target), None)


def owner(widget):
    return getattr(widget._root(), "_forge_locale", None)


_KNOWN_SOURCES = None


def known_source(value) -> bool:
    """普通字符串是否恰好是一条已登记的界面文案（以英文目录的键为准）。"""
    global _KNOWN_SOURCES
    if _KNOWN_SOURCES is None:
        _KNOWN_SOURCES = frozenset(k for k in _read(DIRECTORY, "en") if "{" not in k)
    return isinstance(value, str) and value in _KNOWN_SOURCES


def as_message(value):
    """把恰好等于已登记文案的普通字符串当作 tr() 消息：旧代码里直接写中文
    字面量的控件也能翻译，并随语言切换刷新。带占位符的文案不自动转换。"""
    if not isinstance(value, str) or isinstance(value, Message):
        return value
    if known_source(value):
        return Message(value)
    # 「图标字形 + 空格 + 文案」（如 "▤ 工作区"）：只翻译文案部分。
    head, sep, rest = value.partition(" ")
    if sep and head and len(head) <= 2 and not re.search(r"[一-鿿A-Za-z0-9]", head)             and known_source(rest):
        return Message("{icon} {label}", icon=head, label=Message(rest))
    return value


def resolve(value, context):
    if not isinstance(value, Message):
        return value
    locale = Translator(context) if isinstance(context, str) else owner(context)
    return (locale or _SOURCE_LOCALE).render(value)


def remember(widget, value, *, method="configure"):
    """Bind custom controls with an explicit text setter (no Tk monkey patch)."""
    if not hasattr(widget, "_l10n_custom"):
        widget._l10n_custom = {}
    if isinstance(value, Message):
        widget._l10n_custom[method] = value
        locale = owner(widget)
        if locale:
            locale.register(widget)
    else:
        widget._l10n_custom.pop(method, None)


class _TextMixin:
    def __init__(self, master=None, **kwargs):
        value = as_message(kwargs.get("text"))
        if value is not None:
            kwargs["text"] = value
        self._l10n_message = value if isinstance(value, Message) else None
        if value is not None:
            kwargs["text"] = resolve(value, master) if master is not None else value
        super().__init__(master, **kwargs)
        locale = owner(self)
        if locale:
            locale.register(self)

    def configure(self, cnf=None, **kwargs):
        if isinstance(cnf, dict):
            kwargs = {**cnf, **kwargs}
            cnf = None
        if "text" in kwargs:
            value = kwargs["text"] = as_message(kwargs["text"])
            self._l10n_message = value if isinstance(value, Message) else None
            kwargs["text"] = resolve(value, self)
        return super().configure(cnf, **kwargs) if cnf is not None else super().configure(**kwargs)

    config = configure

    def _l10n_refresh(self):
        if self._l10n_message is not None:
            self.configure(text=self._l10n_message)


class Label(_TextMixin, tk.Label):
    pass


class Button(_TextMixin, tk.Button):
    pass


class Checkbutton(_TextMixin, tk.Checkbutton):
    pass


class Radiobutton(_TextMixin, tk.Radiobutton):
    pass


class Toplevel(tk.Toplevel):
    def title(self, string=None):
        if string is None:
            return super().title()
        remember(self, string, method="title")
        return super().title(resolve(string, self))


class Menu(tk.Menu):
    def __init__(self, master=None, **kwargs):
        self._l10n_entries = {}
        super().__init__(master, **kwargs)
        locale = owner(self)
        if locale:
            locale.register(self)

    def add(self, itemType, cnf=None, **kwargs):
        options = {**(cnf or {}), **kwargs}
        label = options.get("label")
        if label is not None:
            options["label"] = resolve(label, self)
        super().add(itemType, **options)
        if isinstance(label, Message):
            self._l10n_entries[self.index("end")] = label

    def delete(self, index1, index2=None):
        first = self.index(index1)
        last = self.index(index2) if index2 is not None else first
        super().delete(index1, index2)
        if first is not None and last is not None:
            self._l10n_entries = {i if i < first else i - (last-first+1): value
                for i, value in self._l10n_entries.items() if not first <= i <= last}

    def _l10n_refresh(self):
        for index, label in self._l10n_entries.items():
            self.entryconfigure(index, label=resolve(label, self))


class _Dialogs:
    def __getattr__(self, name):
        native = getattr(_messagebox, name)
        def show(title=None, message=None, **options):
            parent = options.get("parent") or tk._default_root
            if parent is not None:
                title, message = resolve(title, parent), resolve(message, parent)
                if "detail" in options:
                    options["detail"] = resolve(options["detail"], parent)
            return native(title=title, message=message, **options)
        return show


dialogs = _Dialogs()

_SOURCE_LOCALE = Translator()


class StringVar(tk.StringVar):
    """For display state only; editable inputs and filter IDs use tk.StringVar."""
    def __init__(self, master=None, value=None, name=None):
        self._l10n_message = None
        super().__init__(master, value, name)
        self._locale = getattr(self._root, "_forge_locale", None)
        if self._locale:
            self._locale.register(self)
        if value is not None:
            self.set(value)

    def set(self, value):
        value = as_message(value)
        self._l10n_message = value if isinstance(value, Message) else None
        locale = getattr(self, "_locale", None)
        super().set(locale.render(value) if locale else str(value))

    def _l10n_refresh(self):
        if self._l10n_message is not None:
            self.set(self._l10n_message)


def validate_catalog(language):
    source = _read(DIRECTORY, "zh-CN")
    target = _read(DIRECTORY, language)
    errors = [f"Missing: {key}" for key in source.keys() - target.keys()]
    for key, value in target.items():
        try:
            if not isinstance(value, str) or not value.strip() or fields(key) != fields(value):
                errors.append(f"Invalid: {key}")
        except ValueError:
            errors.append(f"Invalid: {key}")
    return errors
