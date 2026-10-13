"""Browser tools: a real Chromium driven through Playwright.

The model never sees raw HTML. After every action it receives a compact
"reader view" snapshot of the page in document order, where each interactive
element carries a short ref (e.g. [e12 button "Save invoice"]) that later
actions target. This keeps observations small, grounded and robust to changes in
markup/ids. Every action also produces a screenshot for the UI and the evidence
trail; the model gets the image only when it asks for one.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from ..safety import READ, WRITE
from .base import Action, Tool, ToolContext, ToolError, ToolResult, schema

SNAPSHOT_LIMIT = 12_000

SNAPSHOT_JS = r"""
() => {
  const out = []; const meta = {}; let n = 0;
  document.querySelectorAll('[data-agent-ref]').forEach(e => e.removeAttribute('data-agent-ref'));
  const visible = el => {
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0 || el.tagName === 'OPTION';
  };
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const labelOf = el => {
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return clean(l.innerText); }
    const wrap = el.closest('label'); if (wrap) return clean(wrap.innerText);
    return clean(el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('name') || el.getAttribute('title') || '');
  };
  const isInteractive = el => {
    const t = el.tagName.toLowerCase();
    if (t === 'a') return el.hasAttribute('href');
    if (['button', 'select', 'textarea', 'summary'].includes(t)) return true;
    if (t === 'input') return el.type !== 'hidden';
    return el.getAttribute('role') === 'button' || el.hasAttribute('onclick');
  };
  const BLOCK = new Set(['div','p','section','article','aside','header','footer','nav','main','form','ul','ol','li',
    'table','tr','dl','dt','dd','h1','h2','h3','h4','h5','h6','pre','br','hr','fieldset','label']);
  const push = s => out.push(s);
  const walk = node => {
    if (node.nodeType === 3) { const t = node.textContent.replace(/\s+/g, ' '); if (t.trim()) push(t); return; }
    if (node.nodeType !== 1) return;
    const el = node, tag = el.tagName.toLowerCase();
    if (['script','style','noscript','template','head','svg'].includes(tag)) return;
    if (!visible(el)) return;
    if (isInteractive(el)) {
      const ref = 'e' + (++n); el.setAttribute('data-agent-ref', ref);
      const form = el.form || el.closest('form');
      const m = {tag, type: (el.type || '').toLowerCase(), form_method: form ? (form.getAttribute('method') || 'get').toLowerCase() : null,
                 form_action: form ? form.action : null,
                 form_has_password: form ? !!form.querySelector('input[type=password]') : false};
      let tok;
      if (tag === 'a') {
        const label = clean(el.innerText) || labelOf(el);
        let href = el.getAttribute('href');
        try { const u = new URL(el.href); href = (u.origin === location.origin) ? u.pathname + u.search : u.href; } catch (e) {}
        tok = `[${ref} link "${label}" -> ${href}]`; m.label = label; m.href = el.href;
      } else if (tag === 'select') {
        const label = labelOf(el);
        const opts = Array.from(el.options).slice(0, 40).map(o => `"${clean(o.text)}"`).join(' | ');
        const sel = el.selectedOptions[0] ? clean(el.selectedOptions[0].text) : '';
        tok = `[${ref} select "${label}" selected="${sel}" options: ${opts}]`; m.label = label;
      } else if (tag === 'input' || tag === 'textarea') {
        const label = labelOf(el); const type = tag === 'textarea' ? 'textarea' : el.type;
        if (['submit','button','reset'].includes(type)) {
          tok = `[${ref} button "${clean(el.value) || label}"]`; m.label = clean(el.value) || label; m.submit = type === 'submit';
        } else if (['checkbox','radio'].includes(type)) {
          tok = `[${ref} ${type} "${label}" ${el.checked ? 'checked' : 'unchecked'}]`; m.label = label;
        } else {
          let v = el.value || '';
          if (type === 'password') v = v ? '••••••' : '';
          if (v.length > 300) v = v.slice(0, 300) + '…';
          tok = `[${ref} ${type === 'text' ? 'input' : type} "${label}" value="${v}"]`; m.label = label;
        }
      } else {
        const label = clean(el.innerText) || labelOf(el);
        const submit = tag === 'button' && (el.getAttribute('type') || 'submit').toLowerCase() === 'submit' && !!form;
        tok = `[${ref} button "${label}"]`; m.label = label; m.submit = submit;
      }
      meta[ref] = m; push(' ' + tok + ' ');
      return;
    }
    if (tag === 'pre') { push('\n' + el.innerText + '\n'); return; }
    if (tag === 'img') { const alt = clean(el.getAttribute('alt')); if (alt) push(`[image: ${alt}]`); return; }
    const block = BLOCK.has(tag);
    if (block) push('\n');
    if (/^h[1-6]$/.test(tag)) push('#'.repeat(+tag[1]) + ' ');
    if (tag === 'li') push('- ');
    for (const c of el.childNodes) walk(c);
    if (tag === 'td' || tag === 'th') push(' | ');
    if (block) push('\n');
  };
  walk(document.body);
  const text = out.join('').split('\n').map(l => l.replace(/[ \t]+/g, ' ').trim()).filter(l => l && l !== '|')
    .map(l => l.replace(/\s*\|\s*$/, '')).join('\n');
  return {text, meta, title: document.title};
}
"""


class BrowserSession:
    """Owns the Playwright browser for one run. Must be used from a single thread."""

    def __init__(self, ctx: ToolContext):
        self.ctx = ctx
        self._pw = sync_playwright().start()
        # Container-friendly flags: small /dev/shm and no GPU are common on free hosting.
        self._browser = self._pw.chromium.launch(headless=ctx.settings.headless,
                                                 args=["--disable-dev-shm-usage", "--disable-gpu"])
        self.context = self._browser.new_context(viewport={"width": 1280, "height": 860}, accept_downloads=True)
        self.context.route("**/*", self._route)
        self.page = self.context.new_page()
        self.page.set_default_timeout(10_000)
        self.page.on("download", lambda d: self._downloads.append(d))
        self.page.on("dialog", self._on_dialog)
        self.page.on("response", self._on_response)
        self.refs: dict[str, dict] = {}
        self.last_status: int | None = None
        self._downloads: list = []
        self._dialogs: list[str] = []
        self.blocked: list[str] = []

    # -- event hooks --------------------------------------------------------
    def _route(self, route) -> None:
        url = route.request.url
        if self.ctx.policy.check_url(url):
            self.blocked.append(url)
            route.abort("blockedbyclient")
        else:
            route.continue_()

    def _on_dialog(self, dialog) -> None:
        self._dialogs.append(f"{dialog.type}: {dialog.message}")
        dialog.dismiss()

    def _on_response(self, response) -> None:
        try:
            if response.request.is_navigation_request() and response.frame == self.page.main_frame:
                self.last_status = response.status
        except PlaywrightError:
            pass

    def close(self) -> None:
        for closer in (self.context.close, self._browser.close, self._pw.stop):
            try:
                closer()
            except Exception:
                pass

    # -- observation --------------------------------------------------------
    def snapshot(self, limit: int = SNAPSHOT_LIMIT) -> str:
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except PlaywrightTimeout:
            pass
        snap = self.page.evaluate(SNAPSHOT_JS)
        self.refs = snap["meta"]
        url = self.page.url
        header = f"URL: {url}\nTitle: {snap['title']}"
        if self.last_status:
            header += f"\nHTTP status: {self.last_status}"
            if self.last_status >= 400:
                header += "  <-- the server returned an error page"
        text = snap["text"]
        if len(text) > limit:
            text = text[:limit] + f"\n[... snapshot truncated at {limit} chars; call browser_snapshot with full=true]"
        return f"{header}\n---\n{text}"

    def screenshot(self, full_page: bool = False) -> str:
        path = self.ctx.run.next_screenshot_path()
        self.page.screenshot(path=str(path), full_page=full_page)
        self.ctx.run.screenshots.append(str(path))
        rel = path.relative_to(self.ctx.run.dir).as_posix()
        self.ctx.run.emit("screenshot", path=rel, url=self.page.url)
        return str(path)

    def element(self, ref: str):
        if ref not in self.refs:
            raise ToolError(f"Unknown element ref '{ref}'. Refs change after every page update; "
                            f"use refs from the most recent snapshot.")
        loc = self.page.locator(f'[data-agent-ref="{ref}"]')
        if loc.count() == 0:
            raise ToolError(f"Element {ref} is no longer on the page (the page changed). Take a new snapshot.")
        return loc.first

    def settle(self) -> None:
        """Waits for navigation/network activity triggered by an action to finish."""
        try:
            self.page.wait_for_load_state("load", timeout=10_000)
            self.page.wait_for_load_state("networkidle", timeout=3_000)
        except PlaywrightTimeout:
            pass
        # A short Playwright-side wait lets queued events (downloads, dialogs) reach our handlers.
        self.page.wait_for_timeout(250)

    def collect_downloads(self) -> list[str]:
        saved = []
        dest = self.ctx.run.workspace / "downloads"
        dest.mkdir(parents=True, exist_ok=True)
        while self._downloads:
            d = self._downloads.pop(0)
            target = _unique(dest / d.suggested_filename)
            d.save_as(str(target))
            saved.append(target.relative_to(self.ctx.run.workspace).as_posix())
            self.ctx.run.emit("download", path=saved[-1], url=d.url)
        return saved

    def observe(self, action_note: str, url_before: str) -> ToolResult:
        """Builds the observation returned after every browser action."""
        self.settle()
        downloads = self.collect_downloads()
        lines = [action_note]
        if self.page.url != url_before:
            lines.append(f"Page changed: {url_before} -> {self.page.url}")
        for path in downloads:
            size = (self.ctx.run.workspace / path).stat().st_size
            lines.append(f"Downloaded file: {path} ({size:,} bytes). Use read_file to read it.")
        if self._dialogs:
            lines.append("Browser dialog(s) dismissed: " + "; ".join(self._dialogs))
            self._dialogs.clear()
        if self.blocked:
            lines.append("Requests blocked by sandbox policy: " + ", ".join(self.blocked[:3]))
            self.blocked.clear()
        shot = self.screenshot()
        status = self.last_status or 0
        return ToolResult("\n".join(lines) + "\n\n" + self.snapshot(),
                          data={"screenshot": shot, "url": self.page.url, "http_status": status})


def _unique(path: Path) -> Path:
    i, candidate = 1, path
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}-{i}{path.suffix}")
        i += 1
    return candidate


def session(ctx: ToolContext) -> BrowserSession:
    if ctx.browser is None:
        ctx.browser = BrowserSession(ctx)
    return ctx.browser


def _resolve(ctx: ToolContext, url: str) -> str:
    base = ctx.browser.page.url if ctx.browser and ctx.browser.page.url.startswith("http") else ctx.env.start_url
    return urljoin(base, url)


# -- tool handlers ------------------------------------------------------------

def navigate(ctx: ToolContext, args: dict) -> ToolResult:
    b = session(ctx)
    url = _resolve(ctx, args["url"])
    if err := ctx.policy.check_url(url):
        raise ToolError(err)
    before = b.page.url
    for attempt in (1, 2):  # navigation is idempotent, so one automatic retry is safe
        try:
            b.page.goto(url, wait_until="domcontentloaded", timeout=15_000)
            break
        except PlaywrightError as e:
            if "Download is starting" in str(e):
                break  # the URL is a file download; collected in observe()
            if attempt == 2 or not isinstance(e, PlaywrightTimeout):
                raise ToolError(f"Navigation to {url} failed: {str(e).splitlines()[0]}")
    return b.observe(f"Navigated to {url}", before)


def click(ctx: ToolContext, args: dict) -> ToolResult:
    b = session(ctx)
    el = b.element(args["ref"])
    label = b.refs[args["ref"]].get("label", "")
    before = b.page.url
    try:
        el.click(timeout=5_000)
    except PlaywrightTimeout:
        raise ToolError(f"Could not click {args['ref']} ('{label}'): it is not clickable (covered, disabled or "
                        f"off-screen).")
    return b.observe(f"Clicked {args['ref']} ('{label}')", before)


def type_text(ctx: ToolContext, args: dict) -> ToolResult:
    b = session(ctx)
    ref = args["ref"]
    el = b.element(ref)
    meta = b.refs[ref]
    if meta["tag"] == "select":
        raise ToolError(f"{ref} is a dropdown; use browser_select instead.")
    before = b.page.url
    el.fill(args["text"])
    note = f"Typed into {ref} ('{meta.get('label', '')}')"
    if args.get("press_enter"):
        el.press("Enter")
        note += " and pressed Enter"
    return b.observe(note, before)


def select(ctx: ToolContext, args: dict) -> ToolResult:
    b = session(ctx)
    el = b.element(args["ref"])
    option = args["option"]
    before = b.page.url
    try:
        chosen = el.select_option(label=option, timeout=3_000)
    except PlaywrightError:
        try:
            chosen = el.select_option(value=option, timeout=3_000)
        except PlaywrightError:
            raise ToolError(f"Option '{option}' not found in {args['ref']}. Use the exact option text from the snapshot.")
    return b.observe(f"Selected '{option}' in {args['ref']} (value={chosen})", before)


def back(ctx: ToolContext, args: dict) -> ToolResult:
    b = session(ctx)
    before = b.page.url
    b.page.go_back()
    return b.observe("Went back", before)


def snapshot_tool(ctx: ToolContext, args: dict) -> ToolResult:
    b = session(ctx)
    return ToolResult(b.snapshot(limit=60_000 if args.get("full") else SNAPSHOT_LIMIT))


def screenshot_tool(ctx: ToolContext, args: dict) -> ToolResult:
    b = session(ctx)
    path = b.screenshot(full_page=bool(args.get("full_page")))
    rel = Path(path).relative_to(ctx.run.dir).as_posix()
    return ToolResult(f"Screenshot of {b.page.url} saved as evidence: {rel}", images=[path],
                      data={"screenshot": path})


def download(ctx: ToolContext, args: dict) -> ToolResult:
    b = session(ctx)
    url = _resolve(ctx, args["url"])
    if err := ctx.policy.check_url(url):
        raise ToolError(err)
    resp = b.context.request.get(url)  # shares the browser's cookies / login
    if not resp.ok:
        raise ToolError(f"Download failed: HTTP {resp.status} for {url}")
    name = urlparse(url).path.rsplit("/", 1)[-1] or "download.bin"
    dest = ctx.run.workspace / "downloads"
    dest.mkdir(parents=True, exist_ok=True)
    target = _unique(dest / name)
    target.write_bytes(resp.body())
    rel = target.relative_to(ctx.run.workspace).as_posix()
    ctx.run.emit("download", path=rel, url=url)
    return ToolResult(f"Downloaded {url} -> {rel} ({target.stat().st_size:,} bytes). Use read_file to read it.")


# -- side-effect classification --------------------------------------------

def _describe_click(ctx: ToolContext, args: dict) -> Action:
    meta = ctx.browser.refs.get(args.get("ref", ""), {}) if ctx.browser else {}
    label = meta.get("label", "")
    desc = f"click '{label}'"
    if meta.get("submit") and meta.get("form_has_password"):
        return Action(READ, f"{desc} (sign-in form)", meta.get("form_action"))  # authentication, no business effect
    if meta.get("submit") and meta.get("form_method") == "post":
        return Action(WRITE, f"{desc} (submits a form to {meta.get('form_action')})", meta.get("form_action"))
    return Action(READ, desc, meta.get("href"))


def _describe_type(ctx: ToolContext, args: dict) -> Action:
    meta = ctx.browser.refs.get(args.get("ref", ""), {}) if ctx.browser else {}
    if args.get("press_enter") and meta.get("form_method") == "post" and not meta.get("form_has_password"):
        return Action(WRITE, f"press Enter in '{meta.get('label', '')}' (submits a form to {meta.get('form_action')})",
                      meta.get("form_action"))
    return Action(READ, f"type into '{meta.get('label', '')}'")


REF = {"type": "string", "description": "Element ref from the latest snapshot, e.g. 'e12'."}

TOOLS = [
    Tool("browser_navigate",
         "Open a URL in the browser (absolute, or relative to the current page) and return the page snapshot. "
         "Navigating to a file URL downloads it into the workspace.",
         schema({"url": {"type": "string"}}, ["url"]), navigate,
         lambda ctx, a: Action(READ, f"open {a.get('url')}", a.get("url"))),
    Tool("browser_click",
         "Click an element (link, button, checkbox...) by its ref. Returns what changed plus the new page snapshot. "
         "Clicking a submit button sends the form.",
         schema({"ref": REF}, ["ref"]), click, _describe_click),
    Tool("browser_type",
         "Replace the contents of a text input/textarea with `text`. Use {{secret:NAME}} for credentials. "
         "Set press_enter to submit.",
         schema({"ref": REF, "text": {"type": "string"}, "press_enter": {"type": "boolean"}}, ["ref", "text"]),
         type_text, _describe_type),
    Tool("browser_select", "Choose an option in a dropdown by its visible text (or value).",
         schema({"ref": REF, "option": {"type": "string"}}, ["ref", "option"]), select),
    Tool("browser_back", "Go back to the previous page.", schema({}), back),
    Tool("browser_snapshot", "Re-read the current page (text with element refs). Use full=true for long pages.",
         schema({"full": {"type": "boolean"}}), snapshot_tool),
    Tool("browser_screenshot",
         "Capture a screenshot of the current page. You will see the image, and it is saved as evidence for the "
         "final report.",
         schema({"full_page": {"type": "boolean"}}), screenshot_tool),
    Tool("browser_download", "Download a file URL using the browser's session (cookies) into the workspace.",
         schema({"url": {"type": "string"}}, ["url"]), download,
         lambda ctx, a: Action(READ, f"download {a.get('url')}", a.get("url"))),
]
