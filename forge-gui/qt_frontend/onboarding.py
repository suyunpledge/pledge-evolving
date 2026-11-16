"""Qt welcome preferences and the shared model setup service."""
from pathlib import Path
import threading

from PySide6.QtCore import QObject, Property, Signal, Slot

from config_model import load_user_layer
import model_onboarding as models
from provider_catalog import BY_KEY
import secret_store
from .store import SessionStore


class Onboarding(QObject):
    changed = Signal()
    saved = Signal(str)
    preferencesChanged = Signal()
    _finished = Signal(str, object)

    def __init__(self, home, parent=None, *, demo=False):
        super().__init__(parent)
        home = Path(home)
        self.home = home
        self.demo = demo
        self.path = home / "gui" / "qt" / "onboarding.json"
        self.credential_path = home / "secrets.json" if home != Path.home() / ".forge" else None
        prefs = SessionStore.read(self.path, {})
        self._prefs = prefs if isinstance(prefs, dict) else {}
        self._status = ""
        configured = models.configured_models(self.home)
        self._has_models = bool(configured)
        available = {v.id for v in models.VENDORS if v.wire == "openai"}
        preferred = self._prefs.get("provider")
        self._preferred_vendor = preferred if preferred in available else next(
            (vendor for vendor in configured if vendor in available), models.DEFAULT_VENDOR)
        self._ok = False
        self._busy = False
        self.can_save = lambda: True
        self._finished.connect(self._complete)

    language = Property(str, lambda self: self._prefs.get("language", "zh-CN"), notify=changed)
    developer = Property(bool, lambda self: self._prefs.get("experience") != "simple", notify=changed)
    needsGuide = Property(bool, lambda self: not self.demo and not self._prefs.get("onboarded"), notify=changed)
    initialDeveloper = Property(bool, lambda self: self.developer if not self.needsGuide else self._has_models, notify=changed)
    busy = Property(bool, lambda self: self._busy, notify=changed)
    status = Property(str, lambda self: self._status, notify=changed)
    success = Property(bool, lambda self: self._ok, notify=changed)
    preferredVendor = Property(str, lambda self: self._preferred_vendor, notify=changed)
    vendors = Property("QVariantList", lambda self: [
        {"id": v.id, "name": v.name, "keyless": v.keyless}
        for v in models.VENDORS if v.wire == "openai"
    ], constant=True)

    @Slot(str, result="QVariantMap")
    def defaults(self, vendor_id):
        v = models.vendor(vendor_id)
        if v is None:
            return {}
        try:
            config = next((row["config"] for row in load_user_layer(self.home)
                           if row["id"] == vendor_id), {})
        except (OSError, ValueError, KeyError):
            config = {}
        preset = BY_KEY.get(vendor_id)
        plans = [{"label": plan.label, "url": plan.base_url, "note": plan.note}
                 for plan in preset.plans if plan.usable and plan.wire == v.wire] if preset else []
        return {"url": config.get("baseURL") or v.base_url,
                "model": config.get("model") or v.models[0],
                "exampleModel": v.models[0], "plans": plans,
                "temperature": str(config["temperature"] if config.get("temperature") is not None
                                   else models.DEFAULT_TEMPERATURE),
                "keyless": v.keyless, "console": v.console}

    def preferences(self):
        return {"language": self.language, "provider": self._prefs.get("provider", "")}

    @Slot(str, str, result=bool)
    def finish(self, language, experience):
        if language not in {"en", "zh-CN"} or experience not in {"simple", "developer"}:
            return False
        prefs = {**self._prefs, "onboarded": True, "language": language, "experience": experience}
        try:
            if not self.demo:
                SessionStore.atomic(self.path, prefs)
        except OSError:
            self._complete("", {"ok": False, "error": "引导设置保存失败，请重试。"})
            return False
        self._prefs = prefs
        self.preferencesChanged.emit()
        self.changed.emit()
        return True

    @Slot()
    def clearStatus(self):
        if not self._busy and self._status:
            self._status = ""
            self.changed.emit()

    @Slot(str, str, str)
    def testConnection(self, vendor_id, key, url):
        self._run("test", vendor_id, lambda secret: models.test_connection(vendor_id, secret, base_url=url), key, url)

    @Slot(str, str, str, str, str)
    def saveModel(self, vendor_id, key, url, model, temperature):
        if self._busy:
            return
        if not self.can_save():
            self._complete("", {"ok": False, "error": "请先完成当前任务或保存工作台的修改。"})
            return
        self._run("save", vendor_id, lambda secret: models.apply_key(
            self.home, vendor_id, secret, base_url=url, model=model,
            temperature=temperature, credential_path=self.credential_path), key, url)

    def _run(self, operation, vendor_id, action, key, url):
        if self._busy:
            return
        vendor = models.vendor(vendor_id)
        if self.demo or vendor is None or vendor.wire != "openai":
            self._complete("", {"ok": False, "error": "此处需要正式工作台和 OpenAI 兼容厂商。"})
            return
        self._busy, self._ok, self._status = True, False, ""
        self.changed.emit()

        def worker():
            try:
                secret = key
                if not key.strip() and not vendor.keyless:
                    config = next((row.get("config", {}) for row in load_user_layer(self.home)
                                   if row.get("id") == vendor_id), {})
                    if str(config.get("baseURL", "")).rstrip("/") == url.strip().rstrip("/"):
                        secret = secret_store.load(strict=True, path=self.credential_path).get(vendor_id, "")
                result = action(secret)
            except Exception:
                result = {"ok": False, "error": "操作失败，请检查本机配置并重试。"}
            self._finished.emit(operation, result)

        threading.Thread(target=worker, daemon=True, name="Forge-Qt-model-setup").start()

    @Slot(str, object)
    def _complete(self, operation, result):
        self._busy = False
        self._ok = bool(result["ok"])
        self._status = result.get("message", "模型已保存，正在更新工作台。") if self._ok else result["error"]
        if self._ok and operation == "save":
            self._preferred_vendor = result["row_id"]
            prefs = {**self._prefs, "provider": result["row_id"]}
            try:
                SessionStore.atomic(self.path, prefs)
            except OSError:
                self._status = "模型已保存，本次会启用。默认模型偏好未能保存。"
            else:
                self._prefs = prefs
            self.saved.emit(result["row_id"])
        self.changed.emit()
