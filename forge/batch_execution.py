"""Opt-in Batch job lifecycle for OpenAI-compatible vendor Batch APIs.

No polling loop in chat, no automatic retry of POST, and no local execution of
returned tool calls. Jobs and per-request plans persist without credentials.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import urllib.request
import uuid

from .model import _plan_and_shape


class BatchExecutor:
    MAX_BYTES = 8 * 1024 * 1024

    def __init__(self, provider, home, *, opener=urllib.request.urlopen, timeout=60):
        self.provider = provider
        self.home = Path(home) / "batch-jobs"
        self.opener = opener
        self.timeout = timeout

    def _call(self, path, body=None, *, content_type="application/json", method=None):
        # The supplied endpoint is the user's provider; never follow a remote
        # result URL or send the API key to a URL embedded in a result.
        request = urllib.request.Request(self.provider.url(path), data=body,
                    headers={**self.provider.auth_headers(), "content-type": content_type},
                    method=method or ("POST" if body is not None else "GET"))
        with self.opener(request, timeout=self.timeout) as response:
            data = response.read(self.MAX_BYTES + 1)
        if len(data) > self.MAX_BYTES:
            raise ValueError("Batch response exceeds 8 MiB; download through the vendor console")
        return data

    @staticmethod
    def _identifier(value):
        if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,160}", value):
            raise ValueError("Invalid remote job/file identifier")
        return value

    def _save(self, job):
        job_id = self._identifier(job.get("id"))
        self.home.mkdir(parents=True, exist_ok=True)
        path = self.home / (job_id + ".json")
        temp = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        temp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)

    def submit(self, requests):
        if not isinstance(requests, list) or not 1 <= len(requests) <= 100:
            raise ValueError("A batch must contain 1–100 requests")
        rows, plans, ids = [], [], set()
        for row in requests:
            if not isinstance(row, dict):
                raise ValueError("Batch rows must be objects")
            custom_id = self._identifier(row.get("custom_id"))
            if custom_id in ids:
                raise ValueError("Duplicate batch custom_id")
            ids.add(custom_id)
            payload = row.get("body")
            if not isinstance(payload, dict) or not payload.get("model"):
                raise ValueError("Batch body requires model")
            if self.provider.wire != "openai":
                raise ValueError("Batch requires an OpenAI-compatible endpoint")
            shaped, plan = _plan_and_shape(payload, payload.get("messages") or [], self.provider,
                str(payload["model"]), batch=True, batch_submission=True, interactive=False)
            rows.append({"custom_id": custom_id, "method": "POST", "url": "/v1/chat/completions", "body": shaped})
            plans.append({"custom_id": custom_id, "plan": plan, "invoice_verified": False})
        content = ("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n").encode()
        if len(content) > 1024 * 1024:
            raise ValueError("Batch submission exceeds 1 MiB")
        boundary = "forge-" + uuid.uuid4().hex
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="purpose"\r\n\r\nbatch\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="requests.jsonl"\r\n'
                'Content-Type: application/jsonl\r\n\r\n').encode() + content + f'\r\n--{boundary}--\r\n'.encode()
        uploaded = json.loads(self._call("/files", body, content_type="multipart/form-data; boundary=" + boundary))
        file_id = self._identifier(uploaded.get("id"))
        submission = {"input_file_id": file_id, "endpoint": "/v1/chat/completions", "completion_window": "24h"}
        # Persist an input-file receipt BEFORE POST: if the network drops after
        # acceptance, this is recoverable evidence, and we never resubmit it.
        self._save({"id": "submission-" + file_id, "input_file_id": file_id, "status": "submitting",
                    "requests": plans, "provider": self.provider.name, "endpoint": self.provider.base_url})
        remote = json.loads(self._call("/batches", json.dumps(submission).encode()))
        job = {**remote, "requests": plans, "provider": self.provider.name,
               "endpoint": self.provider.base_url, "invoice_verified": False}
        self._save(job)
        return job

    def status(self, job_id):
        job_id = self._identifier(job_id)
        path = self.home / (job_id + ".json")
        saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if saved and (saved.get("endpoint") != self.provider.base_url or saved.get("provider") != self.provider.name):
            raise ValueError("Job belongs to a different provider endpoint")
        remote = json.loads(self._call("/batches/" + job_id))
        job = {**saved, **remote}
        self._save(job)
        return job

    def results(self, job_id):
        from .vendors import normalize_usage
        job = self.status(job_id)
        files = [job.get("output_file_id"), job.get("error_file_id")]
        rows = []
        for file_id in files:
            if file_id:
                data = self._call("/files/" + self._identifier(file_id) + "/content")
                rows.extend(json.loads(line) for line in data.splitlines() if line.strip())
        receipts = []
        plans = {r["custom_id"]: r["plan"] for r in job.get("requests", [])}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Malformed Batch result row")
            response = row.get("response") or {}
            body = response.get("body") or {}
            plan = plans.get(row.get("custom_id"), {})
            model = str(plan.get("model") or body.get("model") or "")
            receipts.append({"custom_id": row.get("custom_id"), "model": model,
                "provider": self.provider.name, "plan": plan, "http_status": response.get("status_code"),
                "usage": normalize_usage(self.provider.profile_for_model(model), body.get("usage")),
                "error": row.get("error"), "invoice_verified": False})
        return {"job": job, "results": rows, "requests": receipts, "invoice_verified": False}

    def cancel(self, job_id):
        job = self.status(job_id)
        remote = json.loads(self._call("/batches/" + self._identifier(job_id) + "/cancel", b"{}"))
        self._save({**job, **remote})
        return {**job, **remote}
