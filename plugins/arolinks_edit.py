# Changes the destination ("Long URL") of an existing Arolinks short link.
#
# Arolinks has no API for this, so the bot does what the website does: it signs
# in with the account username and password, opens the Edit page of the short
# link, and submits that same form with the new URL. Only the Long URL is
# changed, every other field is sent back exactly as the page gave it.
#
# Railway variables (on the bot that runs Change Link):
#   AROLINKS_USERNAME, AROLINKS_PASSWORD
# If they are missing, or anything fails, nothing is changed on Arolinks and the
# caller shows the manual steps instead.

import html
import os
from html.parser import HTMLParser
from urllib.parse import urljoin

import aiohttp

BASE = "https://arolinks.com"
UA = ("Mozilla/5.0 (Linux; Android 13; Mobile) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36")


def configured():
    return bool(os.getenv("AROLINKS_USERNAME") and os.getenv("AROLINKS_PASSWORD"))


class _Forms(HTMLParser):
    """Collects every <form>: its action, id and its fields in order."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = []
        self._form = None
        self._area = None

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
        elif tag == "textarea" and self._form is not None and a.get("name"):
            self._area = [a["name"], ""]
            self._form["fields"].append(self._area)

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None
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


async def _login(session):
    async with session.get(f"{BASE}/auth/signin") as r:
        page = await r.text()
    form = _find_form(page, lambda f: f["id"] == "signin-form")
    if not form:
        raise RuntimeError("Sign-in form not found")
    fields = form["fields"]
    _set(fields, "username", os.getenv("AROLINKS_USERNAME", ""))
    _set(fields, "password", os.getenv("AROLINKS_PASSWORD", ""))
    async with session.post(urljoin(BASE, form["action"] or "/auth/signin"), data=[tuple(f) for f in fields]) as r:
        after = await r.text()
    if 'id="signin-form"' in after:
        raise RuntimeError("Sign-in failed (check the username and password)")


async def _edit_form(session, alias):
    async with session.get(f"{BASE}/member/links/edit/{alias}") as r:
        if r.status != 200:
            raise RuntimeError(f"Edit page opened with HTTP {r.status}")
        page = await r.text()
    form = _find_form(page, lambda f: f["action"].rstrip("/").endswith(f"/member/links/edit/{alias}"))
    if not form:
        raise RuntimeError("Edit form not found for this short link")
    return form


def _value(fields, name):
    for f in fields:
        if f[0] == name:
            return html.unescape(f[1])
    return None


async def set_destination(alias, new_url):
    """Returns (True, "") on success, otherwise (False, short reason)."""
    if not configured():
        return False, "AROLINKS_USERNAME / AROLINKS_PASSWORD are not set"
    try:
        timeout = aiohttp.ClientTimeout(total=40)
        async with aiohttp.ClientSession(
            timeout=timeout, headers={"User-Agent": UA}, cookie_jar=aiohttp.CookieJar(),
        ) as session:
            await _login(session)
            form = await _edit_form(session, alias)
            if not _set(form["fields"], "url", new_url):
                raise RuntimeError("Long URL field not found")
            async with session.post(urljoin(BASE, form["action"]), data=[tuple(f) for f in form["fields"]]) as r:
                await r.text()
            # check on the website itself that the new URL is really saved
            saved = _value((await _edit_form(session, alias))["fields"], "url")
            if saved != new_url:
                raise RuntimeError("Arolinks did not save the new URL")
        return True, ""
    except Exception as e:
        return False, str(e) if isinstance(e, RuntimeError) else type(e).__name__
