# Edits / lists / hides short links on the shortener websites (Arolinks, Vplink).
# Both sites run the same script and have no API for this, so the bot does what the
# website does: signs in with the account username and password, opens the page and
# submits the same form. Only the Long URL is changed on an edit.
#
# Bot variables:
#   VPLINK_USERNAME, VPLINK_PASSWORD         (vplink.in)
#   AROLINKS_USERNAME, AROLINKS_PASSWORD     (arolinks.com, only while old links exist)

import html
import os
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import aiohttp

UA = ("Mozilla/5.0 (Linux; Android 13; Mobile) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36")

SITES = {
    "vplink.in": {"base": "https://vplink.in", "user": "VPLINK_USERNAME", "pass": "VPLINK_PASSWORD"},
    "arolinks.com": {"base": "https://arolinks.com", "user": "AROLINKS_USERNAME", "pass": "AROLINKS_PASSWORD"},
}


def host_of(url):
    host = (urlparse(url or "").netloc or "").lower()
    return host[4:] if host.startswith("www.") else host


def site_for(url):
    """Site settings for a short link url, or None if it is not a known shortener."""
    return SITES.get(host_of(url))


def configured(host):
    s = SITES.get(host)
    return bool(s and os.getenv(s["user"]) and os.getenv(s["pass"]))


class _Forms(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = []
        self._form = None
        self._area = None
        self._select = None     # [name, first option value, selected option value or None]

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._form = {"action": a.get("action") or "", "id": a.get("id") or "", "fields": []}
            self.forms.append(self._form)
        elif tag == "input" and self._form is not None and a.get("name"):
            kind = (a.get("type") or "text").lower()
            if kind in ("submit", "button", "image", "file", "reset"):
                return
            if kind in ("checkbox", "radio") and "checked" not in a:
                return
            self._form["fields"].append([a["name"], a.get("value") or ""])
        elif tag == "select" and self._form is not None and a.get("name"):
            self._select = [a["name"], None, None]
        elif tag == "option" and self._select is not None:
            value = a.get("value") or ""
            if self._select[1] is None:
                self._select[1] = value
            if "selected" in a:
                self._select[2] = value
        elif tag == "textarea" and self._form is not None and a.get("name"):
            self._area = [a["name"], ""]
            self._form["fields"].append(self._area)

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None
        elif tag == "select" and self._select is not None:
            # like a browser: the selected option, else the first one (empty for the date fields)
            value = self._select[2] if self._select[2] is not None else (self._select[1] or "")
            if self._form is not None:
                self._form["fields"].append([self._select[0], value])
            self._select = None
        elif tag == "textarea":
            self._area = None

    def handle_data(self, data):
        if self._area is not None:
            self._area[1] += data


def _find_form(page, test):
    parser = _Forms()
    parser.feed(page)
    for form in parser.forms:
        if test(form):
            return form
    return None


def _set(fields, name, value):
    for f in fields:
        if f[0] == name:
            f[1] = value
            return True
    return False


def _value(fields, name):
    for f in fields:
        if f[0] == name:
            return html.unescape(f[1])
    return None


# One row of "All Links": alias, short url, destination, date
_ROW = re.compile(r'<h4><a href="(?P<short>[^"]+)"[^>]*>.*?</h4>(?P<rest>.*?)(?=<h4><a href="|$)', re.S)
_DEST = re.compile(r'fa-calendar"></i>\s*(?P<date>[^<]*?)\s*-\s*<a [^>]*href="(?P<dest>[^"]+)"', re.S)


def parse_links(page):
    """[{alias, short, dest, date}] from one All Links page."""
    out = []
    for m in _ROW.finditer(page):
        short = html.unescape(m.group("short"))
        alias = short.rstrip("/").rsplit("/", 1)[-1]
        d = _DEST.search(m.group("rest"))
        out.append({
            "alias": alias, "short": short,
            "dest": html.unescape(d.group("dest")) if d else "",
            "date": (d.group("date").strip() if d else ""),
        })
    return out


def has_next(page):
    return 'rel="next"' in page


def _page_error(page):
    """The error text a form page shows (alert / help-block), short, or ''."""
    for pat in (r'class="[^"]*alert-(?:danger|warning)[^"]*"[^>]*>(.*?)</div>',
                r'class="help-block[^"]*"[^>]*>\s*([^<]{3,})<',
                r'class="[^"]*error[^"]*"[^>]*>(.*?)</'):
        m = re.search(pat, page, re.S)
        if m:
            text = re.sub(r"<[^>]+>", " ", m.group(1))
            text = re.sub(r"\s+", " ", html.unescape(text)).strip()
            if text:
                return text[:80]
    return ""


class Session:
    """async with Session("vplink.in") as s: ...  (signs in once, reuses the session)"""

    def __init__(self, host):
        self.host = host
        self.site = SITES[host]
        self.base = self.site["base"]
        self._http = None

    async def __aenter__(self):
        if not configured(self.host):
            raise RuntimeError(f"{self.site['user']} / {self.site['pass']} are not set")
        self._http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=40), headers={"User-Agent": UA},
            cookie_jar=aiohttp.CookieJar(),
        )
        try:
            await self._login()
        except BaseException:
            await self._http.close()
            raise
        return self

    async def __aexit__(self, *exc):
        if self._http:
            await self._http.close()

    async def _get(self, path):
        async with self._http.get(f"{self.base}{path}") as r:
            if r.status != 200:
                raise RuntimeError(f"Page opened with HTTP {r.status}")
            return await r.text()

    async def _login(self):
        page = await self._get("/auth/signin")
        form = _find_form(page, lambda f: f["id"] == "signin-form") or \
            _find_form(page, lambda f: any(x[0] == "password" for x in f["fields"]))
        if not form:
            raise RuntimeError("Sign-in form not found")
        fields = form["fields"]
        _set(fields, "username", os.getenv(self.site["user"], ""))
        _set(fields, "password", os.getenv(self.site["pass"], ""))
        async with self._http.post(urljoin(self.base, form["action"] or "/auth/signin"),
                                   data=[tuple(f) for f in fields]) as r:
            after = await r.text()
        if any(x[0] == "password" for x in (_find_form(after, lambda f: any(x[0] == "password" for x in f["fields"])) or {"fields": []})["fields"]):
            raise RuntimeError("Sign-in failed (check the username and password)")

    async def list_page(self, page_no):
        """(links, has_next) of one All Links page."""
        page = await self._get(f"/member/links?page={page_no}")
        return parse_links(page), has_next(page)

    async def all_links(self, limit_pages=200):
        out, n = [], 1
        while n <= limit_pages:
            links, more = await self.list_page(n)
            out += links
            if not more or not links:
                break
            n += 1
        return out

    async def _edit_form(self, alias):
        page = await self._get(f"/member/links/edit/{alias}")
        form = _find_form(page, lambda f: f["action"].rstrip("/").endswith(f"/member/links/edit/{alias}"))
        if not form:
            raise RuntimeError("Edit form not found for this short link")
        return form

    async def get_destination(self, alias):
        return _value((await self._edit_form(alias))["fields"], "url")

    async def set_destination(self, alias, new_url):
        """Saves the new Long URL and checks on the website that it really is saved."""
        form = await self._edit_form(alias)
        if not _set(form["fields"], "url", new_url):
            raise RuntimeError("Long URL field not found")
        action = urljoin(self.base, form["action"])
        headers = {"Referer": f"{self.base}/member/links/edit/{alias}", "Origin": self.base}
        async with self._http.post(action, data=[tuple(f) for f in form["fields"]], headers=headers) as r:
            answer = await r.text()
            status = r.status
        saved = await self.get_destination(alias)
        if saved != new_url:
            # say what the website answered, so the real reason can be seen
            note = _page_error(answer) or re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(answer))).strip()[:80]
            raise RuntimeError(
                f"The website did not save the new URL (HTTP {status}"
                + (f", {note}" if note else "") + f", it keeps {(saved or '')[:60]})")

    async def hide(self, alias):
        """Hides a link from All Links (the website has no delete)."""
        n = 1
        while True:
            page = await self._get(f"/member/links?page={n}")
            form = _find_form(page, lambda f: f["action"].rstrip("/").endswith(f"/member/links/hide/{alias}"))
            if form:
                async with self._http.post(urljoin(self.base, form["action"]), data=[tuple(f) for f in form["fields"]]) as r:
                    await r.text()
                return
            if not has_next(page):
                raise RuntimeError("Link not found in the list")
            n += 1


async def set_destination_once(short_url, new_url):
    """One edit with its own sign-in. Returns (True, "") or (False, short reason)."""
    site = site_for(short_url)
    if not site:
        return False, "Unknown shortener"
    host = host_of(short_url)
    if not configured(host):
        return False, f"{site['user']} / {site['pass']} are not set"
    alias = short_url.rstrip("/").rsplit("/", 1)[-1]
    try:
        async with Session(host) as s:
            await s.set_destination(alias, new_url)
        return True, ""
    except Exception as e:
        return False, str(e) if isinstance(e, RuntimeError) else type(e).__name__
