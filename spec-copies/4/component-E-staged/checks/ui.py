#!/usr/bin/env python3
"""Browser checks for component E (Playwright, headless Chromium at 1280x900). Not run directly by the agent.

Usage: ui.py colours | e5 | e6
Prints one line per rule: "<rule> PASS <what passed>" or "<rule> FAIL <what went wrong>".
Stops starting new rules after 25 s so the whole command stays under the 30 s limit.
"""
import json, re, sys, time, traceback, urllib.error, urllib.request

BASE = "http://127.0.0.1:8000"
PW = "correct-horse-1"
START = time.time()
ROLES = {
    "primary": ("rgb(0, 122, 90)", "rgb(255, 255, 255)",
                "auth-submit send-btn thread-send create-channel-submit join-workspace-submit workspace-general-submit "
                "create-invitation-submit channel-topic-submit channel-add-member-submit"),
    "danger": ("rgb(224, 30, 90)", "rgb(255, 255, 255)", "archive-channel-btn logout-btn"),
    "secondary": ("rgb(18, 100, 163)", "rgb(255, 255, 255)",
                  "new-channel-btn create-invitation-btn unarchive-channel-btn create-channel-cancel close-thread "
                  "channel-settings-close workspace-settings-close channel-settings-btn workspace-settings-btn"),
}
# the pinned data-testids that are in the initial HTML (E.4's first list); each must exist once in the page
STATIC_TESTIDS = (
    "auth-form auth-submit auth-toggle workspace-create-form empty-state-create-workspace join-workspace-form "
    "join-workspace-submit channel-list dms-list new-channel-btn current-user logout-btn workspace-header "
    "workspace-settings-btn workspace-settings-close workspace-name-input workspace-join-mode workspace-general-submit "
    "channel-title channel-topic channel-settings-btn channel-settings-title channel-settings-close channel-members-list "
    "channel-add-member-form channel-add-member-input channel-add-member-submit channel-topic-input channel-topic-submit "
    "archive-channel-btn unarchive-channel-btn create-channel-form create-channel-private create-channel-submit "
    "create-channel-cancel message-list message-input send-btn thread-panel thread-input thread-send close-thread "
    "settings-tab-members settings-tab-invitations settings-tab-general settings-pane-members settings-pane-invitations "
    "settings-pane-general create-invitation-btn create-invitation-form create-invitation-submit invitations-list"
).split()
# can a person tell what each control is for? (inside the element matching `root`, or the whole page)
READABLE_JS = """root => {
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length) && getComputedStyle(e).visibility !== 'hidden';
  const R = root ? [...document.querySelectorAll(root)].find(vis) : document;
  if (!R) return {unlabeled: [], blank: [], avatar: null, missing: true};
  const t = s => (s || '').trim();
  const who = e => e.getAttribute('data-testid') || (e.getAttribute('name') ? 'name=' + e.getAttribute('name') : e.tagName.toLowerCase());
  const named = e => t(e.getAttribute('placeholder')) || [...(e.labels || [])].some(l => t(l.innerText));
  const skip = ['hidden', 'checkbox', 'radio', 'submit', 'button', 'file', 'color', 'range', 'image', 'reset'];
  const inputs = [...R.querySelectorAll('input, textarea')].filter(e => vis(e) && !skip.includes((e.type || '').toLowerCase()));
  const buttons = [...R.querySelectorAll('button, [role=button], input[type=submit], input[type=button]')].filter(vis);
  const av = R.querySelector('[data-testid="message-list"] [data-testid="message-avatar"]');
  const box = av && vis(av) ? av.getBoundingClientRect() : null;
  // text contrast (WCAG formula) against the first solid background behind the button; white if none
  const rgba = s => { const m = (s || '').match(/[\\d.]+/g); return m ? m.map(Number) : null; };
  const lum = c => { const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
                     return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]); };
  const bgOf = e => { for (let n = e; n && n.nodeType === 1; n = n.parentElement) { const s = getComputedStyle(n);
                        if (s.backgroundImage && s.backgroundImage !== 'none') return null;
                        const c = rgba(s.backgroundColor); if (c && (c.length < 4 || c[3] > 0.5)) return c; }
                      return [255, 255, 255]; };
  const ratio = e => { const fg = rgba(getComputedStyle(e).color), bg = bgOf(e); if (!fg || !bg) return 99;
                       const a = lum(fg), b = lum(bg); return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05); };
  return {
    unlabeled: inputs.filter(e => !named(e)).map(who),
    blank: buttons.filter(e => !t(e.innerText || e.value) && !t(e.getAttribute('aria-label')) && !t(e.getAttribute('title'))).map(who),
    lowcontrast: buttons.filter(e => !e.disabled && /[A-Za-z0-9]/.test(e.innerText || e.value || '') && ratio(e) < 3)
                        .map(e => who(e) + ' (' + ratio(e).toFixed(1) + ':1)'),
    avatar: box ? [Math.round(box.width), Math.round(box.height)] : null,
  };
}"""
# what the page shows right now, added to every browser FAIL line
STATE_JS = """names => {
  const q = t => document.querySelector('[data-testid="' + t + '"]');
  const vis = e => !!e && !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const txt = t => { const e = q(t); return e ? (e.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 40) : null; };
  const count = {};
  document.querySelectorAll('[data-testid]').forEach(e => { const t = e.getAttribute('data-testid'); count[t] = (count[t] || 0) + 1; });
  return {
    title: txt('channel-title'), header: txt('workspace-header'),
    channels: [...document.querySelectorAll('[data-testid="channel-list"] [data-testid="channel-entry"]')]
      .filter(vis).map(e => e.getAttribute('data-channel-name')).slice(0, 6),
    messages: document.querySelectorAll('[data-testid="message-list"] [data-testid="message"]').length,
    open: ['auth-form', 'workspace-create-form', 'create-channel-form', 'thread-panel', 'emoji-picker', 'message-edit-input']
      .filter(t => vis(q(t))),
    dupes: names.filter(n => (count[n] || 0) > 1).map(n => n + ' x' + count[n]),
  };
}"""


# ---------- API helpers (setup data without the UI) ----------
def api(method, path, body=None, token=None):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {})})
    try:
        r = urllib.request.urlopen(req, timeout=5); return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}


def uniq(prefix):
    return f"{prefix}{int(time.time() * 1000) % 10**9}"


def new_user():
    u = uniq("ui")
    s, d = api("POST", "/api/auth/register", {"username": u, "password": PW, "display_name": "UI " + u})
    if s != 201:
        raise RuntimeError(f"setup: register gave {s}")
    return u, d["token"], d["user"]["id"]


def new_ws(token):
    slug = uniq("ui-")
    s, d = api("POST", "/api/workspaces", {"slug": slug, "name": "Space " + slug}, token)
    if s != 201:
        raise RuntimeError(f"setup: create workspace gave {s}")
    return slug, "Space " + slug, d["general_channel"]["id"], d["general_channel"]["name"].lstrip("#")


def post(token, cid, body, parent=None):
    b = {"body": body}
    if parent is not None:
        b["parent_id"] = parent
    s, d = api("POST", f"/api/channels/{cid}/messages", b, token)
    if s != 201:
        raise RuntimeError(f"setup: post gave {s}")
    return d["message"]["id"]


# ---------- browser helpers ----------
class Run:
    def __init__(self, browser):
        self.browser, self.step, self.dialogs, self.errors, self.pg = browser, "start", [], [], None

    def page(self, token=None):
        ctx = self.browser.new_context(viewport={"width": 1280, "height": 900})
        if token:
            ctx.add_init_script("try{localStorage.setItem('huddle.token', %s)}catch(e){}" % json.dumps(token))
        page = ctx.new_page()
        page.set_default_timeout(3000)
        page.on("dialog", lambda d: (self.dialogs.append(d.type), d.dismiss()))
        page.on("pageerror", lambda e: self.errors.append(short(e)[:150]))
        self.pg = page
        page.goto(BASE + "/", wait_until="load")
        try:  # let the SPA finish its first render (scripts often fetch /api/auth/me first)
            page.wait_for_load_state("networkidle", timeout=2000)
        except Exception:
            pass
        return page

    def snapshot(self):
        """The page's state, e.g. ' [page now: channel-title 'general'; ...]', or '' if it cannot be read."""
        if self.pg is None:
            return ""
        try:
            s = self.pg.evaluate(STATE_JS, STATIC_TESTIDS)
        except Exception:
            return ""
        show = lambda v: f"'{v}'" if v is not None else "missing"
        parts = [f"channel-title {show(s['title'])}", f"workspace-header {show(s['header'])}",
                 f"visible channel-entry {s['channels']}", f"{s['messages']} message(s) in message-list",
                 f"visible {s['open']}"]
        if s["dupes"]:
            parts.append("pinned elements appearing more than once: " + ", ".join(s["dupes"][:6]))
        if self.errors:
            parts.append("JavaScript error: " + self.errors[0])
        return " [page now: " + "; ".join(parts) + "]"

    def dupes(self):
        try:
            return self.pg.evaluate(STATE_JS, STATIC_TESTIDS)["dupes"]
        except Exception:
            return []


def tid(scope, name):
    return scope.locator(f'[data-testid="{name}"]')


def sentence(text):
    """True if text reads as words a person understands, not a bare code like '400' or 'invalid'."""
    return len(re.findall(r"[A-Za-z]{2,}", text or "")) >= 2


def visible_text(loc):
    loc.first.wait_for(state="visible")
    return (loc.first.inner_text() or "").strip()


def wait_text(page, name, text):
    """Waits until the first [data-testid=name] includes text; on timeout says what it shows instead."""
    try:
        page.wait_for_function("([n, t]) => (document.querySelector('[data-testid=\"' + n + '\"]') || {}).innerText?.includes(t)",
                               arg=[name, text])
    except Exception:
        el = tid(page, name)
        now = re.sub(r"\s+", " ", el.first.inner_text(timeout=500)).strip()[:60] if el.count() else None
        raise AssertionError(f"{name} shows {repr(now) if now is not None else 'nothing (no such element)'} "
                             f"(want it to include '{text}')")


def open_channel(page, name):
    entry = tid(page, "channel-list").locator(f'[data-channel-name="{name}"]').first
    try:
        entry.wait_for(state="visible")
    except Exception:
        raise AssertionError(f"no visible element with data-channel-name=\"{name}\" inside channel-list")
    entry.click()
    wait_text(page, "channel-title", name)


def check_dupes(r, when):
    d = r.dupes()
    if d:
        raise AssertionError(f"{when}, pinned elements appear more than once: {', '.join(d[:8])} "
                             "(each pinned element must exist once: no hidden copies)")


def message_by_id(page, mid, scope="message-list"):
    return tid(page, scope).locator(f'[data-message-id="{mid}"]').first


# ---------- rules ----------
def rule_colours(r):
    page = r.page()
    bad = []
    for role, (bg, fg, names) in ROLES.items():
        for n in names.split():
            el = tid(page, n)
            if el.count() == 0:
                bad.append(f"{n} absent"); continue
            got = el.first.evaluate("e => { const s = getComputedStyle(e); return [s.backgroundColor, s.color]; }")
            if "" in got:  # the element was replaced while we read it: read the current one again
                page.wait_for_timeout(300)
                got = tid(page, n).first.evaluate("e => { const s = getComputedStyle(e); return [s.backgroundColor, s.color]; }")
            if got != [bg, fg]:
                bad.append(f"{n}={got[0]}/{got[1]} (want {bg}/{fg})")
    if bad:
        return False, "buttons with the wrong computed colours: " + "; ".join(bad)[:700]
    return True, "every pinned button has its role's exact background and text colour"


def rule_signup(r):
    page = r.page()
    u = uniq("su")
    form = tid(page, "auth-form")
    r.step = "auth-form in signup mode"
    if form.get_attribute("data-mode") != "signup":
        tid(page, "auth-toggle").click()
    if form.get_attribute("data-mode") != "signup":
        return False, f"auth-form data-mode is '{form.get_attribute('data-mode')}' after auth-toggle (want signup)"
    r.step = "the logged-out page"
    rd = page.evaluate(READABLE_JS, '[data-testid="auth-form"]')
    if rd.get("missing"):
        return False, "no visible auth-form on the logged-out page"
    whole = page.evaluate(READABLE_JS, None)
    app_shown = [n for n in ("logout-btn", "message-input", "new-channel-btn") if page.locator(f'[data-testid="{n}"]:visible').count()]
    sub = re.sub(r"\s+", " ", page.locator('[data-testid="auth-submit"]:visible').first.inner_text()).strip()[:40]
    tog = re.sub(r"\s+", " ", page.locator('[data-testid="auth-toggle"]:visible').first.inner_text()).strip()[:40]
    problems = []
    if app_shown:
        problems.append(f"the app is visible before login: {app_shown} (show only the login screen)")
    if rd["unlabeled"]:
        problems.append(f"auth-form inputs without a visible <label> or placeholder: {rd['unlabeled']}")
    if not sub or not tog:
        problems.append(f"auth-submit text '{sub}', auth-toggle text '{tog}' (want text on both)")
    if whole["lowcontrast"]:
        problems.append(f"button text hard to read against its background: {whole['lowcontrast'][:5]} (want contrast at least 3:1)")
    if problems:
        return False, "logged-out page: " + "; ".join(problems)
    r.step = "invalid sign-up (7-character password)"
    form.locator('[name="username"]').fill(u); form.locator('[name="password"]').fill("short77")
    dn = form.locator('[name="display_name"]')
    if dn.count() and dn.is_visible():
        dn.fill("Sign Up " + u)
    tid(page, "auth-submit").click()
    err = visible_text(tid(page, "auth-error"))
    logged_in = tid(page, "current-user").first.is_visible()
    if not sentence(err) or logged_in:
        return False, (f"invalid sign-up: auth-error text '{err[:60]}' (want a sentence of at least two words saying what is "
                       f"wrong, not a bare code) and current-user visible: {logged_in} (want no login)")
    r.step = "valid sign-up"
    form.locator('[name="username"]').fill(u); form.locator('[name="password"]').fill(PW); tid(page, "auth-submit").click()
    tid(page, "current-user").first.wait_for(state="visible")
    check_dupes(r, "after sign-up")
    tok = page.evaluate("localStorage.getItem('huddle.token')")
    s, d = api("GET", "/api/auth/me", token=tok) if tok else (0, {})
    if s != 200 or d.get("user", {}).get("username") != u:
        return False, f"after sign-up: localStorage huddle.token '{str(tok)[:12]}...' gives /api/auth/me {s} (want 200 for {u})"
    r.step = "create a workspace with workspace-create-form"
    slug, wname = uniq("su-"), "Signup Space"
    wf = tid(page, "workspace-create-form")
    wf.locator('[name="slug"]').fill(slug); wf.locator('[name="name"]').fill(wname); tid(page, "workspace-create-submit").click()
    wait_text(page, "workspace-header", wname)
    check_dupes(r, "after creating a workspace")
    r.step = "logout"
    tid(page, "logout-btn").click(); form.wait_for(state="visible")
    if page.evaluate("localStorage.getItem('huddle.token')"):
        return False, "logout-btn left huddle.token in localStorage (want it cleared)"
    r.step = "login again in login mode"
    if form.get_attribute("data-mode") != "login":
        tid(page, "auth-toggle").click()
    form.locator('[name="username"]').fill(u); form.locator('[name="password"]').fill(PW); tid(page, "auth-submit").click()
    wait_text(page, "workspace-header", wname)
    if r.dialogs:
        return False, f"browser dialogs appeared: {r.dialogs} (never use alert/confirm/prompt)"
    return True, "login screen only before login, labeled and readable; invalid sign-up shows auth-error; sign-up logs in with huddle.token; logout and login restore the workspace"


def rule_channels(r):
    u, tok, _ = new_user(); slug, wname, gid, gname = new_ws(tok)
    page = r.page(tok)
    r.step = "channel-list with channel-entry items"
    tid(page, "channel-entry").first.wait_for(state="visible")
    entries = tid(page, "channel-list").locator('[data-testid="channel-entry"]')
    info = entries.evaluate_all("es => es.map(e => [e.innerText.trim(), e.getAttribute('data-channel-id'), e.getAttribute('data-channel-name')])")
    badent = [i for i in info if not (i[0].startswith("#") and i[1] and i[2])]
    if not info or badent:
        return False, f"channel-entry items need text starting with # and data-channel-id/name: {badent or 'none found'}"
    r.step = "create a channel with new-channel-btn"
    num = uniq("")
    typed, cname = f"Team {num}", f"team-{num}"   # the UI must lowercase the name and turn spaces into hyphens
    tid(page, "new-channel-btn").click()
    cf = tid(page, "create-channel-form"); cf.locator('[name="name"]').fill(typed); tid(page, "create-channel-submit").click()
    try:
        tid(page, "channel-list").locator(f'[data-channel-name="{cname}"]').first.wait_for(state="visible")
    except Exception:
        shown = tid(page, "create-channel-error").first.inner_text(timeout=500).strip()[:60] if tid(page, "create-channel-error").count() else ""
        raise AssertionError(f"typed '{typed}' in create-channel-form: no channel-entry named '{cname}' appeared "
                             f"(lowercase it and turn spaces into hyphens before sending); create-channel-error shows '{shown}'")
    r.step = "switch to the new channel after creating it"
    wait_text(page, "channel-title", cname)
    r.step = "duplicate channel name"
    tid(page, "new-channel-btn").click()
    cf.locator('[name="name"]').fill(typed); tid(page, "create-channel-submit").click()
    err = visible_text(tid(page, "create-channel-error"))
    n = tid(page, "channel-list").locator(f'[data-channel-name="{cname}"]').count()
    if not sentence(err) or n != 1:
        return False, (f"duplicate name '{typed}': create-channel-error text '{err[:60]}' (want a sentence of at least two "
                       f"words, not a bare code), entries named {cname}: {n} (want 1)")
    if tid(page, "create-channel-cancel").is_visible():
        tid(page, "create-channel-cancel").click()
    r.step = f"switch back to #{gname}"
    open_channel(page, gname)
    return True, "channel-list entries with #, ids and names; 'Team N' creates #team-N and switches to it; duplicate shows a readable create-channel-error; switching works"


def rule_messages(r):
    u, tok, _ = new_user(); slug, wname, gid, gname = new_ws(tok)
    page = r.page(tok)
    r.step = f"open #{gname} by clicking its channel-entry"
    open_channel(page, gname)
    t1, t2, t3 = uniq("first "), uniq("edited "), uniq("doomed ")
    r.step = "post with message-input and send-btn"
    tid(page, "message-input").fill(t1); tid(page, "send-btn").click()
    m1 = tid(page, "message-list").locator('[data-testid="message"]', has_text=t1).first
    try:
        m1.wait_for(state="visible")
    except Exception:
        raise AssertionError(f"no visible message with text '{t1}' in message-list after send-btn")
    mid = m1.get_attribute("data-message-id")
    if not mid:
        return False, "the posted message has no data-message-id"
    r.step = "empty message"
    before = tid(page, "message-list").locator('[data-testid="message"]').count()
    tid(page, "message-input").fill(""); tid(page, "send-btn").click(); page.wait_for_timeout(600)
    after = tid(page, "message-list").locator('[data-testid="message"]').count()
    if after != before:
        return False, f"an empty message was posted ({before} messages before, {after} after)"
    r.step = "edit through the hover toolbar"
    m1.hover(); tid(m1, "message-edit-btn").click()
    box = tid(page, "message-edit-input").first; box.fill(t2); box.press("Enter")
    try:
        page.wait_for_function("([id, t]) => { const m = document.querySelector('[data-testid=\"message-list\"] [data-message-id=\"' + id + '\"]'); return m && m.querySelector('[data-testid=\"message-body\"]')?.innerText.includes(t); }", arg=[mid, t2])
    except Exception:
        b = tid(message_by_id(page, mid), "message-body")
        now = b.first.inner_text(timeout=500).strip()[:60] if b.count() else None
        raise AssertionError(f"after Enter in message-edit-input, message {mid} message-body is "
                             f"{repr(now) if now is not None else 'missing'} (want '{t2}')")
    tid(message_by_id(page, mid), "message-edited").first.wait_for(state="visible")
    r.step = "delete through the hover toolbar"
    tid(page, "message-input").fill(t3); tid(page, "send-btn").click()
    m3 = tid(page, "message-list").locator('[data-testid="message"]', has_text=t3).first; m3.wait_for(state="visible")
    did = m3.get_attribute("data-message-id")
    m3.hover(); tid(m3, "message-delete-btn").click()
    message_by_id(page, did).wait_for(state="detached")
    r.step = "after a hard reload"
    page.reload(wait_until="load"); open_channel(page, gname)
    m1 = message_by_id(page, mid); m1.wait_for(state="visible")
    body = tid(m1, "message-body").first.inner_text()
    edited = tid(m1, "message-edited").first.is_visible() if tid(m1, "message-edited").count() else False
    gone = tid(page, "message-list").locator(f'[data-message-id="{did}"]').count() == 0
    if t2 not in body or not edited or not gone:
        return False, f"after reload: edited body '{body[:60]}' (want '{t2}'), message-edited visible: {edited}, deleted message gone: {gone}"
    if r.dialogs:
        return False, f"browser dialogs appeared: {r.dialogs} (never use alert/confirm/prompt)"
    return True, "post, empty not posted, edit with 'edited', delete without a dialog; all still right after a reload"


def rule_e6(r):
    """E6.1 to E6.3 share one setup and one page."""
    out = {}
    u, tok, _ = new_user(); slug, wname, gid, gname = new_ws(tok)
    api("PATCH", f"/api/channels/{gid}", {"topic": "Layout topic"}, tok)
    pid = post(tok, gid, "thread parent " + uniq(""))
    rid = post(tok, gid, "react to me " + uniq(""))
    s, d = api("POST", f"/api/workspaces/{slug}/channels", {"name": uniq("empty-")}, tok)
    empty = d.get("channel", {}).get("name") if s == 201 else None
    page = r.page(tok)
    r.step = f"open #{gname} by clicking its channel-entry"
    open_channel(page, gname)
    panel_hidden_at_start = not tid(page, "thread-panel").first.is_visible()
    create_hidden_at_start = not tid(page, "create-channel-form").first.is_visible()

    # E6.1 threads
    try:
        r.step = "open-thread-btn"
        parent = message_by_id(page, pid); parent.wait_for(state="visible"); parent.hover(); tid(parent, "open-thread-btn").click()
        panel = tid(page, "thread-panel").first; panel.wait_for(state="visible")
        pb, lb = panel.bounding_box(), tid(page, "message-list").first.bounding_box()
        if not pb or not lb or pb["x"] < lb["x"] + lb["width"] / 2:
            raise AssertionError(f"thread-panel is not to the right of message-list (panel {pb}, list {lb})")
        r.step = "reply with thread-input and thread-send"
        reply = uniq("reply ")
        tid(page, "thread-input").first.fill(reply); tid(page, "thread-send").first.click()
        panel.locator('[data-testid="message"]', has_text=reply).first.wait_for(state="visible")
        r.step = "thread-reply-count on the parent"
        try:
            page.wait_for_function("id => /\\b1 repl/i.test(document.querySelector('[data-testid=\"message-list\"] [data-message-id=\"' + id + '\"] [data-testid=\"thread-reply-count\"]')?.innerText || '')", arg=str(pid))
        except Exception:
            rc = tid(message_by_id(page, pid), "thread-reply-count")
            now = re.sub(r"\s+", " ", rc.first.inner_text(timeout=500)).strip()[:40] if rc.count() else None
            raise AssertionError(f"after the reply, the parent message's thread-reply-count shows "
                                 f"{repr(now) if now is not None else 'nothing (no such element in the parent message)'} "
                                 f"(want text like '1 reply')")
        tid(page, "close-thread").first.click(); panel.wait_for(state="hidden")
        out["E6.1"] = (True, "thread-panel opens on the right, the reply appears there, the parent shows 1 reply, close-thread hides it")
    except Exception as e:
        out["E6.1"] = (False, f"at '{r.step}': {short(e)}{r.snapshot()}")

    # E6.2 reactions
    try:
        r.step = "reaction-button opens emoji-picker"
        m = message_by_id(page, rid); m.hover(); tid(m, "reaction-button").first.click()
        picker = tid(page, "emoji-picker").first; picker.wait_for(state="visible")
        n = picker.locator('[data-testid="emoji-option"]').count()
        if n < 8:
            raise AssertionError(f"emoji-picker has {n} emoji-option buttons (want at least 8)")
        r.step = "choose an emoji"
        picker.locator('[data-testid="emoji-option"]').first.click()
        chip = tid(m, "reaction-chip").first; chip.wait_for(state="visible")
        if "1" not in chip.inner_text():
            raise AssertionError(f"reaction-chip text '{chip.inner_text()}' does not show the count 1")
        s, d = api("GET", f"/api/channels/{gid}/messages", token=tok)
        stored = any(str(x["id"]) == str(rid) and any(rr.get("count") == 1 for rr in x.get("reactions", [])) for x in d.get("messages", []))
        if not stored or r.dialogs:
            raise AssertionError(f"reaction stored through the API: {stored}; browser dialogs: {r.dialogs or 'none'}")
        out["E6.2"] = (True, f"emoji-picker grid with {n} options, reaction-chip with count 1, reaction stored, no dialog")
    except Exception as e:
        out["E6.2"] = (False, f"at '{r.step}': {short(e)}{r.snapshot()}")

    # E6.3 layout and empty state
    try:
        r.step = "sidebar, header and message parts"
        bg = tid(page, "sidebar").first.evaluate("e => getComputedStyle(e).backgroundColor")
        problems = []
        if bg in ("rgba(0, 0, 0, 0)", "transparent", "rgb(255, 255, 255)"):
            problems.append(f"sidebar background {bg} (want a solid colour, not white)")
        if wname not in tid(page, "workspace-header").first.inner_text():
            problems.append("workspace-header does not show the workspace name")
        if gname not in tid(page, "channel-title").first.inner_text() or "Layout topic" not in tid(page, "channel-topic").first.inner_text():
            problems.append("channel-title / channel-topic do not show the channel name and topic")
        m = message_by_id(page, pid)
        tm = tid(m, "message-time").first.inner_text().strip() if tid(m, "message-time").count() else ""
        if not (tid(m, "message-avatar").count() and tid(m, "message-author").first.inner_text().strip() and tm
                and not re.search(r"\d{4}-\d\d-\d\dT\d\d", tm) and tid(m, "message-body").count()):
            problems.append(f"message parts: avatar {tid(m, 'message-avatar').count()}, author, time '{tm}' (human-readable, not ISO), body")
        if not panel_hidden_at_start:
            problems.append("thread-panel was visible before any thread was opened")
        if not create_hidden_at_start:
            problems.append("create-channel-form was visible before new-channel-btn was pressed")
        r.step = "labels, buttons and avatar"
        rd = page.evaluate(READABLE_JS, None)
        if rd["unlabeled"]:
            problems.append(f"text inputs without a visible <label> or placeholder: {rd['unlabeled'][:6]}")
        if rd["blank"]:
            problems.append(f"buttons with no text or aria-label: {rd['blank'][:6]}")
        if rd["lowcontrast"]:
            problems.append(f"button text hard to read against its background: {rd['lowcontrast'][:5]} (want contrast at least 3:1)")
        if not rd["avatar"] or min(rd["avatar"]) < 24:
            problems.append(f"message-avatar size {rd['avatar'] or 'not visible'} px (want a box at least 24x24)")
        r.step = "empty channel"
        if empty:
            open_channel(page, empty)
            if not visible_text(tid(page, "channel-empty-state")):
                problems.append("channel-empty-state has no visible text")
        else:
            problems.append("setup: could not create an empty channel")
        if problems:
            raise AssertionError("; ".join(problems))
        out["E6.3"] = (True, "coloured sidebar with workspace and channels; header with name and topic; message rows with avatar, author, readable time, body; empty state; thread panel and channel form on demand; labeled inputs and buttons")
    except Exception as e:
        out["E6.3"] = (False, f"at '{r.step}': {short(e)}{r.snapshot()}")
    return out


def short(e):
    s = str(e).strip()
    if not s:
        return type(e).__name__
    msg = re.sub(r"\s+", " ", s.splitlines()[0])
    m = re.search(r"waiting for (.+)", s)   # Playwright's call log names the element it waited for
    if m and "waiting for" not in msg:
        msg += " (waiting for " + re.sub(r"\s+", " ", m.group(1)).strip()[:140] + ")"
    return msg[:300]


SUITES = {"colours": [("E4.4", rule_colours)],
          "e5": [("E5.1", rule_signup), ("E5.2", rule_channels), ("E5.3", rule_messages)],
          "e6": [("E6.*", rule_e6)]}


def main(suite):
    rules = SUITES[suite]
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        for name, _ in rules:
            print(f"{name if '*' not in name else 'E6.1'} FAIL setup: Playwright is not importable by this python3")
        return
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        for name, fn in rules:
            if time.time() - START > 25:
                print(f"{name} FAIL skipped: the browser checks ran out of time (25 s)"); continue
            r = Run(browser)
            try:
                res = fn(r)
                items = res.items() if isinstance(res, dict) else [(name, res)]
            except Exception as e:
                ids = ["E6.1", "E6.2", "E6.3"] if name == "E6.*" else [name]
                items = [(i, (False, f"at '{r.step}': {short(e)}{r.snapshot()}")) for i in ids]
            for rule, (good, text) in items:
                print(f"{rule} {'PASS' if good else 'FAIL'} {text}")
        browser.close()


if __name__ == "__main__":
    try:
        main(sys.argv[1])
    except Exception as e:
        print(f"ERROR {type(e).__name__}: {short(e)}")
