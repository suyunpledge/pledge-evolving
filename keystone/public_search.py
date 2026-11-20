"""Search engines with public entry points and credential-free result adapters."""
from __future__ import annotations

from html.parser import HTMLParser
import ipaddress
import json
import re
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit
from urllib.request import Request

SOURCES = {
    "google": ("Google", "https://www.google.com/search", "q"),
    "duckduckgo": ("DuckDuckGo", "https://duckduckgo.com/", "q"),
    "yahoo": ("Yahoo", "https://search.yahoo.com/search", "p"),
    "bing": ("Bing", "https://www.bing.com/search", "q"),
    "baidu": ("百度", "https://m.baidu.com/s", "word"),
    "quark": ("夸克", "https://quark.sm.cn/s", "q"),
    "sogou": ("搜狗", "https://www.sogou.com/web", "query"),
    "github": ("GitHub", "https://github.com/search", "q"),
}
GITHUB_KINDS = ("repositories", "issues", "pullrequests")
MAX_QUERY = 500
MAX_PAGE = 3_000_000


class SearchUnavailable(RuntimeError):
    """A search provider couldn't return readable results."""


def clean_query(value):
    query = value.strip()
    if not query or len(query) > MAX_QUERY or any(ord(c) < 32 for c in query):
        raise ValueError(f"请输入 1–{MAX_QUERY} 个字符的单行搜索词。")
    return query


def search_url(engine, query, kind="repositories"):
    _, base, param = SOURCES[engine]
    params = {param: clean_query(query)}
    if engine == "github":
        if kind not in GITHUB_KINDS:
            raise ValueError("GitHub 搜索类型应为仓库、议题或拉取请求。")
        params["type"] = kind
    return base + "?" + urlencode(params)


def safe_target(value):
    if not isinstance(value, str) or len(value) > 2500 or re.search(r"[\x00-\x20\\\x7f]", value):
        return ""
    try:
        url = urlsplit(value)
        host = (url.hostname or "").rstrip(".")
        if url.scheme not in ("https", "http") or not host or url.username is not None:
            return ""
        url.port  # Reject malformed port values.
        try:
            return value if ipaddress.ip_address(host).is_global else ""
        except ValueError:
            return value if "." in host and not host.endswith((".localhost", ".local", ".internal")) else ""
    except ValueError:
        pass
    return ""


class _PageResults(HTMLParser):
    """Extract linked result headlines from the provider's visible HTML."""
    VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
    PRIVATE = frozenset({"style", "script", "template", "noscript"})

    def __init__(self, engine, base):
        super().__init__(convert_charrefs=True)
        self.engine = engine
        self.base = base
        self.stack = []
        self.results = []

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
            return
        attrs = dict(attrs)
        classes = set((attrs.get("class") or "").split())
        href = attrs.get("href") or ""
        if tag == "article" and self.engine == "baidu":
            href = attrs.get("rl-link-data-url") or attrs.get("rl-link-href") or ""
        quark_title = tag == "a" and self.engine == "quark" and any(
            "qk-title-content" in frame[1] for frame in self.stack)
        candidate = tag == "h3" or quark_title
        self.stack.append((tag, classes, href, [] if candidate else None))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if not any(frame[0] in self.PRIVATE for frame in self.stack):
            for _, _, _, title in self.stack:
                if title is not None:
                    title.append(data)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                break
        else:
            return
        _, classes, href, title = self.stack[index]
        if tag == "a" and href:
            # Some providers nest the link inside the h3. Retain it until h3 closes.
            for position in range(index - 1, -1, -1):
                outer_tag, outer_classes, outer_href, outer_title = self.stack[position]
                if outer_tag == "h3" and not outer_href:
                    self.stack[position] = (outer_tag, outer_classes, href, outer_title)
                    break
        if title is not None and (self.engine != "sogou" or "vr-title" in classes):
            ancestor = next((frame[2] for frame in reversed(self.stack[:index]) if frame[2]), "")
            self._result(href or ancestor, " ".join("".join(title).split()))
        del self.stack[index:]

    def _result(self, href, title):
        if not href or not title or len(self.results) >= 50:
            return
        try:
            target = self._target(urljoin(self.base, href))
        except ValueError:
            return
        if target:
            self.results.append({"title": title[:300], "url": target, "snippet": ""})

    def _target(self, url):
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if self.engine == "google" and host in ("google.com", "www.google.com"):
            if parsed.path != "/url":
                return ""
            fields = parse_qs(parsed.query, max_num_fields=30)
            url = (fields.get("q") or fields.get("url") or [""])[0]
        elif self.engine == "yahoo" and host.endswith(".yahoo.com"):
            match = re.search(r"/RU=([^/]+)", parsed.path)
            if match:
                url = unquote(match[1])
            elif parsed.path.startswith(("/search", "/yhs/")):
                return ""
        elif host == urlsplit(self.base).hostname:
            if self.engine == "baidu" and parsed.path.startswith("/from=") and "/tc" in parsed.path:
                pass
            elif self.engine == "sogou" and parsed.path == "/link":
                pass
            else:
                return ""
        return safe_target(url)


def html_provider(engine, query, open_, headers, timeout):
    url = search_url(engine, query)
    if engine == "yahoo":
        url += "&ei=UTF-8"
    req = Request(url, headers=headers)
    with open_(req, timeout) as response:
        if 300 <= getattr(response, "status", 200) < 400:
            raise SearchUnavailable("此搜索源要求通过浏览器继续。")
        raw = response.read(MAX_PAGE + 1)
    if len(raw) > MAX_PAGE:
        raise SearchUnavailable("搜索页面超过读取限制。")
    parser = _PageResults(engine, url)
    parser.feed(raw.decode("utf-8", "replace"))
    parser.close()
    if not parser.results:
        raise SearchUnavailable("站点没有返回可读取的结果。可打开原站查看。")
    return parser.results


def github_provider(query, count, kind, open_, timeout):
    if kind not in GITHUB_KINDS:
        raise ValueError("GitHub 搜索仅支持公开仓库、议题和拉取请求。")
    resource = "repositories" if kind == "repositories" else "issues"
    if kind != "repositories":
        # Append the chosen tab's qualifier after the user query.
        query += " is:pr" if kind == "pullrequests" else " is:issue"
    url = f"https://api.github.com/search/{resource}?" + urlencode({"q": query, "per_page": count})
    req = Request(url, headers={"User-Agent": "Keystone-Public-Search", "Accept": "application/vnd.github+json"})
    with open_(req, timeout) as response:
        raw = response.read(MAX_PAGE + 1)
    if len(raw) > MAX_PAGE:
        raise SearchUnavailable("GitHub 搜索响应超过读取限制。")
    body = json.loads(raw)
    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
        raise SearchUnavailable("GitHub 返回了无效的搜索响应。")
    rows = []
    for item in body["items"]:
        if not isinstance(item, dict):
            continue
        title = item.get("full_name") or item.get("title")
        target = safe_target(item.get("html_url"))
        detail = item.get("description") or item.get("body") or ""
        if not isinstance(title, str) or not target or urlsplit(target).netloc.lower() != "github.com":
            continue
        snippet = " ".join(detail[:1600].split()) if isinstance(detail, str) else ""
        rows.append({"title": title[:300], "url": target, "snippet": snippet[:300]})
    return rows
