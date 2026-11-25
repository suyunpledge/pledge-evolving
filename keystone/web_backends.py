"""Default backends for the web_search / fetch_url tools. Standard library only.

An explicit engine uses a single credential-free source. Automatic search has two regions.
"cn" (Chinese web): Bocha with BOCHA_API_KEY, else Bing China.
"global" (US/international web): Brave with BRAVE_API_KEY, else Bing US, else DuckDuckGo.
"auto" picks cn for queries containing Chinese characters; "both" merges the two.

fetch_url only reads public http(s) pages: every hop, redirects included, must resolve
to global addresses, so a page cannot steer the agent into localhost or the LAN.
"""
from __future__ import annotations

import html
import http.client
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

from . import public_search

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

def _bounded_search_read(response):
    raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise public_search.SearchUnavailable('搜索响应超过读取限制。')
    return raw


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
        root = ET.fromstring(_bounded_search_read(resp))
    return [{"title": (item.findtext("title") or "").strip(), "url": (item.findtext("link") or "").strip(),
             "snippet": re.sub(r"<[^>]+>", "", item.findtext("description") or "").strip()}
            for item in root.iter("item")]


def _duckduckgo(query, count, open_):
    req = urllib.request.Request("https://html.duckduckgo.com/html/?"
        + urllib.parse.urlencode({"q": query, "kl": "us-en"}),
        headers={"User-Agent": UA, "Accept": "text/html"})
    with open_(req, TIMEOUT) as resp:
        page = _bounded_search_read(resp).decode("utf-8", "replace")
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


def _terms(query):
    query = re.sub(r'\bsite:\S+', '', query)
    stop = {'the', 'for', 'and', 'how', 'documentation', 'docs', 'official', 'with'}
    return list(dict.fromkeys(t for t in re.findall(r'[a-z][a-z0-9_.+#-]*', query.lower())
                              if len(t) > 1 and t not in stop))


def _rank(rows, query, domain=None):
    """Keep an explicit site restriction, then rank topical overlap stably."""
    blocked = {'pan.baidu.com', 'pan.quark.cn', 'alipan.com', 'lanzou.com', 'downcc.com', 'pc6.com'}
    blocked.update(h.strip().lower() for h in os.environ.get('KEYSTONE_SEARCH_BLOCKED_DOMAINS', '').split(',') if h.strip())
    terms = _terms(query)
    chinese = re.sub(r'官方文档|文档|教程|如何|什么是|怎么', '', query)
    pairs = {word[i:i+2] for word in re.findall(r'[\u3400-\u9fff]{2,}', chinese) for i in range(len(word)-1)}
    scored = []
    for row in rows:
        url = public_search.safe_target(row.get('url'))
        if not url or (domain and not _on_domain(url, domain)) or any(_on_domain(url, h) for h in blocked):
            continue
        title = (row.get('title', '') + ' ' + urllib.parse.urlsplit(url).path).casefold()
        snippet = row.get('snippet', '').casefold()
        matched = sum(t in title or t in snippet for t in terms)
        if len(terms) >= 3 and matched * 2 < len(terms):
            continue
        chinese_hits = sum(pair in title or pair in snippet for pair in pairs)
        if not terms and len(pairs) >= 2 and not chinese_hits:
            continue
        scored.append((matched + sum(t in title for t in terms) + min(3, chinese_hits), row))
    return [r for _, r in sorted(scored, key=lambda pair: -pair[0])]


def _technical_search(query, count, open_):
    terms = _terms(query)
    if len(terms) < 2:
        return []
    url = 'https://api.stackexchange.com/2.3/search/advanced?' + urllib.parse.urlencode(
        dict(site='stackoverflow', sort='relevance', order='desc', q=' '.join(terms), pagesize=min(30, count * 5)))
    with open_(urllib.request.Request(url, headers={'User-Agent': UA}), TIMEOUT) as resp:
        payload = json.loads(_bounded_search_read(resp))
    return [dict(title=html.unescape(item['title']), url=item['link'], snippet=' '.join(item.get('tags', [])))
            for item in payload.get('items', []) if isinstance(item, dict) and item.get('title') and item.get('link')]


def _one_region(query, count, region, open_, domain=None, skip=None):
    errors = []
    providers = list(PROVIDERS[region])
    if len(_terms(query)) >= 2 and not domain:
        if region == 'cn':
            providers.append(('duckduckgo', lambda q, n, o: _duckduckgo(' '.join(_terms(q)), n, o)))
        providers.append(('stackoverflow', _technical_search))
    for name, provider in providers:
        if name == skip or (skip == 'bing' and name.startswith('bing-')):
            continue
        try:
            rows = provider(query, count, open_)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            continue
        rows = [r for r in rows or [] if r.get("url", "").startswith(("http://", "https://"))
                and (domain is None or _on_domain(r["url"], domain))]  # Some engines ignore site:.
        rows = _rank(rows, query, domain)
        if rows:
            return [{**r, "snippet": r.get("snippet", "")[:300], "engine": name} for r in rows[:count]]
        if rows is not None:
            errors.append(f"{name}: no results")
    raise RuntimeError("; ".join(errors) or "no search provider available")


def _public_results(engine, query, count, region, kind, open_, domain):
    try:
        if engine == "bing":
            rows = _bing(query, count, open_, china=region == "cn")
        elif engine == "duckduckgo":
            rows = _duckduckgo(query, count, open_)
            if not rows:
                raise public_search.SearchUnavailable("站点没有返回可读取的结果。")
        elif engine == "github":
            rows = public_search.github_provider(query, count, kind, open_, TIMEOUT)
        else:
            headers = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml",
                       "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8" if engine in {"baidu", "quark", "sogou"}
                       else "en-US,en;q=0.9"}
            rows = public_search.html_provider(engine, query, open_, headers, TIMEOUT)
    except urllib.error.HTTPError as exc:
        raise public_search.SearchUnavailable(f"搜索源返回 HTTP {exc.code}。") from None
    except (OSError, http.client.HTTPException, ET.ParseError, json.JSONDecodeError):
        raise public_search.SearchUnavailable("搜索源暂不可用。") from None
    result, seen = [], set()
    for row in rows:
        url = public_search.safe_target(row.get("url"))
        if not url or url in seen or (domain and not _on_domain(url, domain)):
            continue
        seen.add(url)
        result.append({**row, "snippet": row.get("snippet", "")[:300], "engine": engine})
        if len(result) >= count:
            break
    return result


def search(*, query: str, num: int = 5, site: str = "all", region: str = "auto",
           engine: str = "auto", github_kind: str = "repositories",
           open_: Opener = _default_open) -> list[dict[str, str]]:
    query = public_search.clean_query(query)
    if engine != "auto" and engine not in public_search.SOURCES:
        raise ValueError("未知搜索源。")
    num = max(1, min(int(num or 5), 10))
    if engine == "github":
        return _public_results(engine, query, num, region, github_kind, open_, None)
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
    if engine != "auto":
        try:
            rows = _rank(_public_results(engine, query, num, regions[0], github_kind, open_, domain), query, domain)
        except (public_search.SearchUnavailable, RuntimeError) as exc:
            first_error = str(exc)
            rows = []
        else:
            first_error = f'{engine}: no relevant results'
        if rows:
            return rows
        try:
            return _one_region(query, num, regions[0], open_, domain, skip=engine)
        except RuntimeError as exc:
            raise public_search.SearchUnavailable(f'{first_error}. {exc}') from None
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


def _public_addresses(url: str):
    """Resolve the URL once and retain the checked socket addresses."""
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
        address = getattr(address, "ipv4_mapped", None) or address
        if address.is_multicast or (not address.is_global and address not in _FAKE_IP):
            raise ValueError(f"{parts.hostname} resolves to a private or local address ({address}); not fetched")
    if not infos:
        raise ValueError(f"cannot resolve {parts.hostname}")
    return tuple(infos)


def check_public(url: str) -> str:
    _public_addresses(url)
    return url


def _open_public(request, timeout):
    addresses = _public_addresses(request.full_url)

    def connect(_destination, timeout, source_address):
        last_error = None
        for family, kind, protocol, _, address in addresses:
            sock = socket.socket(family, kind, protocol)
            try:
                sock.settimeout(timeout)
                if source_address:
                    sock.bind(source_address)
                sock.connect(address)
                return sock
            except OSError as exc:
                last_error = exc
                sock.close()
        raise last_error

    def connection(kind):
        def create(host, **kwargs):
            conn = kind(host, **kwargs)
            # The original hostname stays in Host and TLS SNI. Connect only
            # to the checked socket addresses, without another DNS lookup.
            conn._create_connection = connect
            return conn
        return create

    class PublicHTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(connection(http.client.HTTPConnection), req)

    class PublicHTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(connection(http.client.HTTPSConnection), req, context=_tls_context())

    # A forward proxy could resolve the destination independently of this
    # process. Fetch uses the OS network route to the validated address.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect,
                                         PublicHTTPHandler(), PublicHTTPSHandler())
    try:
        return opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            return exc
        raise


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
        self.descriptions = {}
        self.embedded = []
        self.noscript = []
        self.alternate = None
        self.has_script = False
        self._json_script = False

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
        attributes = dict(attrs)
        if tag == 'meta':
            key = attributes.get('name') or attributes.get('property')
            if key in ('description', 'og:description') and attributes.get('content'):
                self.descriptions[key] = attributes['content'][:2000]
        if tag == 'link' and 'alternate' in (attributes.get('rel') or '').lower().split():
            if attributes.get('type') in {'text/markdown', 'text/plain'}:
                self.alternate = attributes.get('href')
        if tag == 'script':
            self.has_script = True
            self._json_script = attributes.get('type') == 'application/ld+json' or attributes.get('id') == '__NEXT_DATA__'
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
        if tag == 'script':
            self._json_script = False
        if tag == "title": self._in_title = False
        if tag in self.BLOCK: self._emit("\n")
        # Real pages leave tags unclosed: unwind to the matching open tag, if any.
        if not any(open_tag == tag for open_tag, _, _ in self._stack): return
        while self._stack:
            open_tag, skips, is_main = self._stack.pop()
            self._skip -= skips; self._main -= is_main
            if open_tag == tag: break

    def handle_data(self, data):
        if self._json_script and len(data) <= 512000:
            try:
                todo = [json.loads(data)]
                for _ in range(3000):
                    if not todo:
                        break
                    node = todo.pop()
                    if isinstance(node, dict):
                        for key, value in node.items():
                            if key in {'articleBody', 'description', 'headline', 'markdown'} and isinstance(value, str):
                                self.embedded.append(value[:8000])
                            elif isinstance(value, (dict, list)):
                                todo.append(value)
                    elif isinstance(node, list):
                        todo.extend(node[:100])
            except (ValueError, RecursionError):
                pass
        elif self._stack and self._stack[-1][0] == 'noscript':
            self.noscript.append(data)
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


def fetch(*, url: str, max_chars: int = 5000, open_: Opener | None = None,
          check: Callable[[str], str] = check_public, _allow_alternate=True) -> tuple[str, str]:
    for _ in range(6):
        req = urllib.request.Request(url, headers={"User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,text/plain,application/json;q=0.9,*/*;q=0.5",
            "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7"})
        if open_ is None:
            resp = _open_public(req, TIMEOUT)
        else:
            check(url)
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
        if len(body) < 160 and (parser.has_script or parser.alternate or not body):
            if parser.alternate and _allow_alternate:
                alternate = urllib.parse.urljoin(url, parser.alternate)
                if alternate != url and urllib.parse.urlsplit(alternate)[:2] == urllib.parse.urlsplit(url)[:2]:
                    try:
                        alt_title, alt_body = fetch(url=alternate, max_chars=max_chars, open_=open_, check=check,
                                                    _allow_alternate=False)
                        return alt_title or title, alt_body
                    except (OSError, ValueError, http.client.HTTPException):
                        pass
            parts = [*parser.embedded, *parser.noscript, parser.descriptions.get('description', ''),
                     parser.descriptions.get('og:description', '')]
            extra = '\n'.join(dict.fromkeys(x.strip() for x in parts if x.strip()))
            if extra:
                body = (body + '\n' + extra).strip()
            if parser.has_script and not any(len(v.strip()) >= 160 for v in parser.embedded):
                body += '\n此页可能需要脚本渲染才能显示完整正文。可尝试该站的文档或 Markdown 页面。'
    else:
        title, body = "", text.strip()
    note = f"\n\n[source: {url}]" + (" [page larger than 3 MB; only the start was read]" if truncated else "")
    if len(body) > max_chars:
        note = f"\n\n[showing the first {max_chars} of {len(body)} characters; raise max_chars to read more]" + note
        body = body[:max_chars]
    return title, body + note
