"""Default backends for the web_search / fetch_url tools. Standard library only.

Search has two regions. "cn" (Chinese web): Bocha with BOCHA_API_KEY, else Bing China.
"global" (US/international web): Brave with BRAVE_API_KEY, else Bing US, else DuckDuckGo.
"auto" picks cn for queries containing Chinese characters; "both" merges the two.

fetch_url only reads public http(s) pages: every hop, redirects included, must resolve
to global addresses, so a page cannot steer the agent into localhost or the LAN.
"""
from __future__ import annotations

import html
import ipaddress
import itertools
import json
import os
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from typing import Any, Callable

TIMEOUT = 15
MAX_BYTES = 3_000_000
REGIONS = ("auto", "cn", "global", "both")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
# Shortcuts for the site argument; any other value is treated as a domain.
SITES = {"zhihu": "zhihu.com", "xiaohongshu": "xiaohongshu.com", "weixin": "mp.weixin.qq.com",
         "bilibili": "bilibili.com", "github": "github.com", "reddit": "reddit.com",
         "stackoverflow": "stackoverflow.com", "wikipedia": "wikipedia.org"}
_CJK = re.compile(r"[㐀-鿿豈-﫿]")

Opener = Callable[..., Any]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # Redirects are followed by hand so each hop is checked.


def _tls_context():
    """OS certificate verification when truststore is installed. Windows fetches missing
    roots/intermediates on demand; OpenSSL's snapshot of the store fails on many real sites."""
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        return ssl.create_default_context()


def _default_open(request, timeout):
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=_tls_context()))
    try:
        return opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            return exc  # Carries status and Location for the manual redirect loop.
        raise


# ---------------------------------------------------------------- search

def _bocha(query, count, open_):
    key = os.environ.get("BOCHA_API_KEY", "")
    if not key:
        return None
    req = urllib.request.Request("https://api.bochaai.com/v1/web-search",
        data=json.dumps({"query": query, "count": count}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with open_(req, TIMEOUT) as resp:
        pages = json.loads(resp.read()).get("data", {}).get("webPages", {}).get("value", [])
    return [{"title": p.get("name", ""), "url": p.get("url", ""), "snippet": p.get("snippet", "")}
            for p in pages]


def _brave(query, count, open_):
    key = os.environ.get("BRAVE_API_KEY", "")
    if not key:
        return None
    req = urllib.request.Request("https://api.search.brave.com/res/v1/web/search?"
        + urllib.parse.urlencode({"q": query, "count": count}),
        headers={"X-Subscription-Token": key, "Accept": "application/json"})
    with open_(req, TIMEOUT) as resp:
        items = json.loads(resp.read()).get("web", {}).get("results", [])
    return [{"title": i.get("title", ""), "url": i.get("url", ""),
             "snippet": re.sub(r"<[^>]+>", "", i.get("description", ""))} for i in items]


def _bing(query, count, open_, *, china):
    params = {"q": query, "format": "rss", "count": count * 2}
    params.update({"mkt": "zh-CN", "ensearch": "0"} if china else
                  {"mkt": "en-US", "setlang": "en-US", "cc": "US"})
    host = "cn.bing.com" if china else "www.bing.com"
    # A bare UA gets empty RSS for Chinese queries; a full browser UA does not.
    req = urllib.request.Request(f"https://{host}/search?" + urllib.parse.urlencode(params),
        headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml"})
    with open_(req, TIMEOUT) as resp:
        root = ET.fromstring(resp.read())
    return [{"title": (item.findtext("title") or "").strip(), "url": (item.findtext("link") or "").strip(),
             "snippet": re.sub(r"<[^>]+>", "", item.findtext("description") or "").strip()}
            for item in root.iter("item")]


def _duckduckgo(query, count, open_):
    req = urllib.request.Request("https://html.duckduckgo.com/html/",
        data=urllib.parse.urlencode({"q": query, "kl": "us-en"}).encode(),
        headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"})
    with open_(req, TIMEOUT) as resp:
        page = resp.read().decode("utf-8", "replace")
    results = []
    links = re.findall(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', page, re.S)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', page, re.S)
    for i, (href, title) in enumerate(links):
        href = html.unescape(href)
        target = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg")
        clean = lambda s: html.unescape(re.sub(r"<[^>]+>", "", s)).strip()
        results.append({"title": clean(title), "url": target[0] if target else href,
                        "snippet": clean(snippets[i]) if i < len(snippets) else ""})
    return results


PROVIDERS = {
    "cn": (("bocha", _bocha), ("bing-cn", lambda q, n, o: _bing(q, n, o, china=True))),
    "global": (("brave", _brave), ("bing-us", lambda q, n, o: _bing(q, n, o, china=False)),
               ("duckduckgo", _duckduckgo)),
}


def resolve_region(query: str, region: str) -> list[str]:
    if region not in REGIONS:
        raise ValueError(f"region must be one of {', '.join(REGIONS)}")
    if region == "auto":
        return ["cn" if _CJK.search(query) else "global"]
    return ["cn", "global"] if region == "both" else [region]


def _on_domain(url, domain):
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host == domain or host.endswith("." + domain)


def _one_region(query, count, region, open_, domain=None):
    errors = []
    for name, provider in PROVIDERS[region]:
        try:
            rows = provider(query, count, open_)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            continue
        rows = [r for r in rows or [] if r.get("url", "").startswith(("http://", "https://"))
                and (domain is None or _on_domain(r["url"], domain))]  # Some engines ignore site:.
        if rows:
            return [{**r, "snippet": r.get("snippet", "")[:300], "engine": name} for r in rows[:count]]
        if rows is not None:
            errors.append(f"{name}: no results")
    raise RuntimeError("; ".join(errors) or "no search provider available")


def search(*, query: str, num: int = 5, site: str = "all", region: str = "auto",
           open_: Opener = _default_open) -> list[dict[str, str]]:
    num = max(1, min(int(num or 5), 10))
    site = (site or "all").strip().lower()
    domain = None
    if site not in ("all", "", "baidu", "bing"):
        domain = SITES.get(site, site)
        if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", domain):
            raise ValueError(f"site must be all, one of {', '.join(SITES)}, or a domain like github.com")
        query = f"{query} site:{domain}"
    if site == "baidu" and region == "auto":
        region = "cn"  # Kept for compatibility: means "Chinese web".
    regions = resolve_region(query, region)
    batches, errors = [], []
    for name in regions:
        try:
            batches.append(_one_region(query, num, name, open_, domain))
        except RuntimeError as exc:
            errors.append(f"[{name}] {exc}")
    if not batches:
        raise RuntimeError("all search providers failed: " + " | ".join(errors))
    # "both" interleaves the two regions so neither side is pushed off the list.
    merged, seen = [], set()
    for group in itertools.zip_longest(*batches):
        for row in group:
            if row is not None and row["url"].rstrip("/") not in seen:
                seen.add(row["url"].rstrip("/")); merged.append(row)
    return merged


# ---------------------------------------------------------------- fetch

# Proxy clients in fake-IP/TUN mode (Clash, sing-box, Surge) answer every DNS query with an
# address from the RFC 2544 benchmark range and route it through the proxy. No LAN service
# lives there, so it counts as public; loopback, private and link-local stay refused.
_FAKE_IP = ipaddress.ip_network("198.18.0.0/15")


def check_public(url: str) -> str:
    """Raises ValueError unless url is http(s) on a host that resolves only to public IPs."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("only http(s) URLs with a host can be fetched")
    if parts.username or parts.password:
        raise ValueError("URLs with embedded credentials are not fetched")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"cannot resolve {parts.hostname}: {exc}") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%")[0])
        if not address.is_global and address not in _FAKE_IP:
            raise ValueError(f"{parts.hostname} resolves to a private or local address ({address}); not fetched")
    return url


class _Text(HTMLParser):
    # Page chrome (menus, sidebars, footers, cookie bars) crowds the article out of a
    # 5000-character budget, so it is skipped by tag, ARIA role or class/id name.
    SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "canvas", "head",
            "nav", "aside", "footer", "form", "button", "select", "dialog"}
    ROLES = {"navigation", "banner", "contentinfo", "search", "menu", "menubar", "complementary", "dialog"}
    CHROME = re.compile(r"(?:^|[-_\s])(?:nav|navbar|navigation|menu|sidebar|breadcrumbs?|footer|toc|related|"
                        r"share|social|cookie|advert|ads|banner|skip|interlanguage|catlinks)(?:[-_\s]|$)", re.I)
    BLOCK = {"p", "div", "li", "tr", "section", "article", "header", "h1", "h2", "h3",
             "h4", "h5", "h6", "pre", "blockquote", "table", "ul", "ol", "dd", "dt", "main"}
    VOID = {"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "embed", "source", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.main, self.title = [], [], ""
        self._stack = []  # (tag, skips, is_main) for every open non-void element
        self._skip = self._main = 0
        self._in_title = False

    def _chrome(self, tag, attrs):
        attrs = dict(attrs)
        if tag in self.SKIP or attrs.get("role") in self.ROLES or "hidden" in attrs:
            return True
        if attrs.get("aria-hidden") == "true":
            return True
        names = f"{attrs.get('class') or ''} {attrs.get('id') or ''}"
        return tag not in ("body", "html", "main", "article") and bool(self.CHROME.search(names))

    def _emit(self, piece):
        if self._skip: return
        self.parts.append(piece)
        if self._main: self.main.append(piece)

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
            if tag in ("br", "hr"): self._emit("\n")
            return
        if tag == "title": self._in_title = True
        skips, is_main = self._chrome(tag, attrs), tag in ("main", "article")
        self._stack.append((tag, skips, is_main))
        self._skip += skips; self._main += is_main
        if tag in self.BLOCK: self._emit("\n")
        if tag in ("h1", "h2", "h3"): self._emit("#" * int(tag[1]) + " ")
        if tag == "li": self._emit("- ")

    def handle_startendtag(self, tag, attrs):
        if tag in ("br", "hr"): self._emit("\n")

    def handle_endtag(self, tag):
        if tag == "title": self._in_title = False
        if tag in self.BLOCK: self._emit("\n")
        # Real pages leave tags unclosed: unwind to the matching open tag, if any.
        if not any(open_tag == tag for open_tag, _, _ in self._stack): return
        while self._stack:
            open_tag, skips, is_main = self._stack.pop()
            self._skip -= skips; self._main -= is_main
            if open_tag == tag: break

    def handle_data(self, data):
        if self._in_title: self.title += data
        else: self._emit(data)

    @staticmethod
    def _tidy(parts):
        lines = (re.sub(r"[ \t\r\f\v ]+", " ", line).strip() for line in "".join(parts).split("\n"))
        kept = (line for line in lines if line not in ("-", "#", "##", "###"))
        return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()

    def text(self):
        main = self._tidy(self.main)
        # Prefer the marked-up article when it holds real content; otherwise the whole body.
        return main if len(main) >= 200 else self._tidy(self.parts)


def _charset(content_type, raw):
    match = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    if not match:
        match = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", raw[:4096], re.I)
    name = (match.group(1).decode() if isinstance(match.group(1), bytes) else match.group(1)) if match else "utf-8"
    name = name.lower()
    if name in ("gb2312", "gbk"):
        name = "gb18030"  # Superset; GB2312 labels routinely carry GBK characters.
    try:
        return raw.decode(name)
    except (LookupError, UnicodeDecodeError):
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("gb18030", "replace")


def fetch(*, url: str, max_chars: int = 5000, open_: Opener = _default_open,
          check: Callable[[str], str] = check_public) -> tuple[str, str]:
    for _ in range(6):
        check(url)
        req = urllib.request.Request(url, headers={"User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,text/plain,application/json;q=0.9,*/*;q=0.5",
            "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7"})
        resp = open_(req, TIMEOUT)
        try:
            status = getattr(resp, "status", None) or getattr(resp, "code", 200)
            if 300 <= status < 400:
                location = resp.headers.get("Location")
                if not location:
                    raise ValueError(f"redirect {status} without a Location header")
                url = urllib.parse.urljoin(url, location)
                continue
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read(MAX_BYTES + 1)
        finally:
            resp.close()
        break
    else:
        raise ValueError("too many redirects")
    kind = content_type.split(";")[0].strip().lower()
    if kind == "application/pdf" or raw[:5] == b"%PDF-":
        raise ValueError("this is a PDF; fetch_url only reads web pages and plain text")
    if kind and not (kind.startswith("text/") or kind in ("application/json", "application/xml",
                                                         "application/xhtml+xml", "application/rss+xml")):
        raise ValueError(f"unsupported content type {kind}")
    truncated = len(raw) > MAX_BYTES
    text = _charset(content_type, raw[:MAX_BYTES])
    if kind in ("text/html", "application/xhtml+xml") or (not kind and "<html" in text[:2000].lower()):
        parser = _Text(); parser.feed(text); parser.close()
        title, body = html.unescape(parser.title).strip(), parser.text()
    else:
        title, body = "", text.strip()
    note = f"\n\n[source: {url}]" + (" [page larger than 3 MB; only the start was read]" if truncated else "")
    if len(body) > max_chars:
        note = f"\n\n[showing the first {max_chars} of {len(body)} characters; raise max_chars to read more]" + note
        body = body[:max_chars]
    return title, body + note
