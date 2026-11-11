#!/usr/bin/env python3
"""draft — a minimal, distraction-free Markdown writing app (GTK4).

The buffer is always plain Markdown. In rich view, formatting is rendered with
text tags and the syntax markers are hidden once a span is complete (they
reappear while the cursor is inside the span, so you can edit them). Raw view
shows the Markdown untouched. Documents live in ~/Writing and autosave.
Press F1 for keys.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

APP_ID = "io.github.draft"
DOC_DIR = Path(os.environ.get("DRAFT_DIR", Path.home() / "Writing"))
STATE_FILE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "draft/state.json"
AUTOSAVE_MS = 1500
COLUMN_CHARS = 66
MONO = "Go Mono"

THEMES = {
    "dark": dict(bg="#1a1a1a", fg="#d6d2c9", dim="#6b6862", caret="#e0a96d",
                 sel="#3a3530", panel="#232323", hl="#2e2b27", code="#242220", quote="#a19c92"),
    "light": dict(bg="#f5f0e6", fg="#2f2b26", dim="#a39b8d", caret="#b5651d",
                  sel="#e2d6bf", panel="#ece5d7", hl="#e2d9c7", code="#ebe3d3", quote="#6e665b"),
    "midnight": dict(bg="#000000", fg="#c8c8c8", dim="#5a5a5a", caret="#d9a35b",
                     sel="#2a2a2a", panel="#111111", hl="#1e1e1e", code="#141414", quote="#949494"),
    "slate": dict(bg="#232831", fg="#d3d8e0", dim="#68717f", caret="#88b4d6",
                  sel="#3a4352", panel="#2b313b", hl="#353c48", code="#2b313b", quote="#9aa3b1"),
    "forest": dict(bg="#1b211d", fg="#d3d8cc", dim="#66705f", caret="#a7c080",
                   sel="#344036", panel="#222a24", hl="#2d362f", code="#242c26", quote="#9aa391"),
    "sepia": dict(bg="#ecdfc4", fg="#3b2f22", dim="#a08c6c", caret="#9c4f1d",
                  sel="#d8c39b", panel="#e2d3b4", hl="#d8c7a5", code="#e2d3b4", quote="#6b5a45"),
    "paper": dict(bg="#ffffff", fg="#1c1c1c", dim="#a0a0a0", caret="#2a6bc4",
                  sel="#cfe0f7", panel="#f2f2f2", hl="#e6e6e6", code="#f2f2f2", quote="#5c5c5c"),
}
THEME_ORDER = list(THEMES)
FONTS = {"serif": "Charis SIL", "mono": MONO}

HELP = """\
WRITING                         FILES & VIEW
Ctrl+B   bold                   Ctrl+N    new document
Ctrl+I   italic                 Ctrl+O    open document
Ctrl+D   strikethrough          Ctrl+S    save (autosaves anyway)
Ctrl+E   inline code            Ctrl+R    raw markdown view
Ctrl+K   link                   Ctrl+\\    show / hide toolbar
Ctrl+1-3 heading                Ctrl+⇧I   show / hide status line
Ctrl+0   plain paragraph        Ctrl+T    cycle themes
Ctrl+L   bullet list            Ctrl+M    serif / mono
Ctrl+⇧L  numbered list          Ctrl+Y    typewriter scrolling
Ctrl+'   quote                  Ctrl+= -  font size
                                Ctrl+⇧F   fullscreen
                                Ctrl+⇧O   choose background image
                                Ctrl+⇧T   image on / off
                                Ctrl+[ ]  more / less image
Markdown typed by hand works     Ctrl+Q    quit
too: **bold**  *italic*  ~~strike~~  `code`  [text](url)
# heading  > quote  - list  1. list  ```fence```   -- becomes —

Files live in ~/Writing as plain .md"""

# --- markdown patterns -------------------------------------------------------
RE_HEADING = re.compile(r"^(#{1,6})[ \t]+")
RE_QUOTE = re.compile(r"^>[ \t]?")
RE_LIST = re.compile(r"^([ \t]*)([-*+]|\d+[.)])[ \t]+(\[[ xX]\][ \t]+)?")
RE_HR = re.compile(r"^[ \t]*([-*_])([ \t]*\1){2,}[ \t]*$")
RE_FENCE = re.compile(r"^[ \t]*(```|~~~)")
RE_BLOCK_PREFIX = re.compile(r"^(#{1,6}[ \t]+|>[ \t]?|[ \t]*(?:[-*+]|\d+[.)])[ \t]+)")

# (tag, regex, how to find markers). Order matters: code first so its content
# is masked from the others; bold before italic.
RE_CODE = re.compile(r"(`+)(?=[^`])(.+?)(?<=[^`])\1(?!`)")
RE_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
RE_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
RE_STRIKE = re.compile(r"(~~)(?=\S)(.+?)(?<=\S)\1")
RE_ITALIC = re.compile(r"(?<![*\w\\])(\*|_)(?=[^\s*_])(.+?)(?<=[^\s*_\\])\1(?![*\w])")

INLINE_MARKERS = {"bold": "**", "italic": "*", "strike": "~~", "code": "`"}
MASK = "\x00"


def parse_inline(s):
    """Return list of (tag, content_start, content_end, [(m_start, m_end)...], span_start, span_end)."""
    out = []
    work = s

    def mask(a, b, everything=False):
        nonlocal work
        work = work[:a] + MASK * (b - a) + work[b:]

    for m in RE_CODE.finditer(work):
        o = len(m.group(1))
        out.append(("code", m.start(2), m.end(2), [(m.start(), m.start() + o), (m.end(2), m.end())], m.start(), m.end()))
    for t in out:
        mask(t[4], t[5])
    for m in RE_LINK.finditer(work):
        out.append(("link", m.start(1), m.end(1), [(m.start(), m.start(1)), (m.end(1), m.end())], m.start(), m.end()))
        mask(m.start(), m.start(1))
        mask(m.end(1), m.end())
    for tag, rx in (("bold", RE_BOLD), ("strike", RE_STRIKE), ("italic", RE_ITALIC)):
        found = list(rx.finditer(work))
        for m in found:
            o = len(m.group(1))
            out.append((tag, m.start(2), m.end(2), [(m.start(), m.start() + o), (m.end(2), m.end())], m.start(), m.end()))
        for m in found:
            o = len(m.group(1))
            mask(m.start(), m.start() + o)
            mask(m.end(2), m.end())
    return out


def fence_map(lines):
    res, inside = [], False
    for line in lines:
        if RE_FENCE.match(line):
            res.append("fence")
            inside = not inside
        else:
            res.append("code" if inside else None)
    return res


# --- persistence -------------------------------------------------------------
def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state):
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except OSError:
        pass


def doc_title(path):
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip().lstrip("#").strip()
                if line:
                    return line[:80]
    except OSError:
        pass
    return "(empty)"


def new_doc_path():
    base = time.strftime("%Y-%m-%d-%H%M%S")
    p = DOC_DIR / f"{base}.md"
    n = 1
    while p.exists():
        p = DOC_DIR / f"{base}-{n}.md"
        n += 1
    return p


class DraftWindow(Gtk.ApplicationWindow):
    def __init__(self, app, initial_path=None):
        super().__init__(application=app, title="draft")
        self.set_default_size(1000, 760)
        DOC_DIR.mkdir(parents=True, exist_ok=True)

        self.state = load_state()
        self.theme = self.state.get("theme", "dark")
        if self.theme not in THEMES:
            self.theme = "dark"
        self.font = self.state.get("font", "serif")
        self.size = int(self.state.get("size", 15))
        self.typewriter = bool(self.state.get("typewriter", True))
        self.status_visible = bool(self.state.get("status", False))
        self.raw = bool(self.state.get("raw", False))
        self.bg_image = self.state.get("bg_image")
        self.bg_on = bool(self.state.get("bg_on", False))
        self.overlay = float(self.state.get("overlay", 0.82))
        self.path = None
        self.dirty = False
        self.save_source = None
        self.flash_source = None
        self.restyle_source = None
        self.dirty_lines = set()
        self.queue = []
        self.queue_lines = 0
        self.queue_source = None
        self.status_source = None
        self.full_restyle = True
        self.last_fences = None
        self.cursor_line = 0
        self.loading = False
        self.last_alloc = (0, 0)
        self.side = 24

        self.css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), self.css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        # --- editor ---
        self.buffer = Gtk.TextBuffer()
        self.make_tags()
        self.buffer.connect("changed", self.on_changed)
        self.buffer.connect_after("insert-text", self.on_insert_after)
        self.buffer.connect_after("delete-range", self.on_delete_after)
        self.buffer.connect("mark-set", self.on_mark_set)
        self.view = Gtk.TextView(buffer=self.buffer)
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.view.add_css_class("editor")
        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_child(self.view)
        self.view.add_tick_callback(self.on_tick)

        # --- toolbar ---
        self.toolbar = Gtk.Box(spacing=2)
        self.toolbar.add_css_class("toolbar")
        self.toolbar.set_halign(Gtk.Align.CENTER)
        self.toolbar.set_valign(Gtk.Align.START)
        self.tool_buttons = {}
        tools = [
            ("bold", "<b>B</b>", "Bold  Ctrl+B", lambda: self.toggle_inline("**")),
            ("italic", "<i>I</i>", "Italic  Ctrl+I", lambda: self.toggle_inline("*")),
            ("strike", "<s>S</s>", "Strikethrough  Ctrl+D", lambda: self.toggle_inline("~~")),
            ("code", "<tt>&lt;&gt;</tt>", "Code  Ctrl+E", lambda: self.toggle_inline("`")),
            ("link", "link", "Link  Ctrl+K", self.insert_link),
            None,
            ("h1", "H1", "Heading 1  Ctrl+1", lambda: self.set_block("# ")),
            ("h2", "H2", "Heading 2  Ctrl+2", lambda: self.set_block("## ")),
            ("h3", "H3", "Heading 3  Ctrl+3", lambda: self.set_block("### ")),
            ("quote", "“ ”", "Quote  Ctrl+'", lambda: self.set_block("> ")),
            ("ul", "•", "Bullet list  Ctrl+L", lambda: self.set_block("- ")),
            ("ol", "1.", "Numbered list  Ctrl+Shift+L", lambda: self.set_block("1. ")),
            None,
            ("raw", "MD", "Raw markdown  Ctrl+R", self.toggle_raw),
        ]
        for t in tools:
            if t is None:
                sep = Gtk.Label(label="·")
                sep.add_css_class("sep")
                self.toolbar.append(sep)
                continue
            key, markup, tip, fn = t
            lbl = Gtk.Label()
            lbl.set_markup(markup)
            b = Gtk.Button(child=lbl, tooltip_text=tip)
            b.set_focusable(False)
            b.set_focus_on_click(False)
            b.connect("clicked", lambda _b, fn=fn: (fn(), self.view.grab_focus()))
            self.toolbar.append(b)
            self.tool_buttons[key] = b

        # --- status line ---
        self.status = Gtk.Label(xalign=0.5)
        self.status.add_css_class("status")
        self.status.set_valign(Gtk.Align.END)
        self.status.set_halign(Gtk.Align.CENTER)
        self.status.set_can_target(False)

        # --- help panel ---
        self.help = Gtk.Label(label=HELP, xalign=0)
        self.help.add_css_class("panel")
        self.help.add_css_class("help")
        self.help.set_halign(Gtk.Align.CENTER)
        self.help.set_valign(Gtk.Align.CENTER)
        self.help.set_visible(False)

        # --- open picker ---
        self.search = Gtk.SearchEntry(placeholder_text="Find a document…")
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.BROWSE)
        self.listbox.set_filter_func(self.filter_row)
        self.listbox.connect("row-activated", lambda _lb, row: self.open_from_picker(row))
        self.search.connect("search-changed", self.on_search_changed)
        self.search.connect("activate", lambda *_: self.open_from_picker(self.listbox.get_selected_row()))
        self.search.connect("stop-search", lambda *_: self.close_picker())
        list_scroll = Gtk.ScrolledWindow()
        list_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        list_scroll.set_child(self.listbox)
        list_scroll.set_vexpand(True)
        self.picker = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.picker.add_css_class("panel")
        self.picker.append(self.search)
        self.picker.append(list_scroll)
        self.picker.set_size_request(620, 440)
        self.picker.set_halign(Gtk.Align.CENTER)
        self.picker.set_valign(Gtk.Align.CENTER)
        self.picker.set_visible(False)
        pk = Gtk.EventControllerKey()
        pk.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        pk.connect("key-pressed", self.on_picker_key)
        self.picker.add_controller(pk)

        overlay = Gtk.Overlay()
        overlay.add_css_class("canvas")  # carries the optional background image
        overlay.set_child(self.scroller)
        overlay.add_overlay(self.toolbar)
        overlay.add_overlay(self.status)
        overlay.add_overlay(self.help)
        overlay.add_overlay(self.picker)
        self.set_child(overlay)

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self.on_key)
        self.add_controller(keys)
        self.connect("close-request", self.on_close)

        self.apply_style()
        self.status.set_visible(self.status_visible)
        self.toolbar.set_visible(bool(self.state.get("toolbar", True)))

        last = self.state.get("last")
        if initial_path:
            self.open_path(Path(initial_path).expanduser())
        elif last and Path(last).exists():
            self.open_path(Path(last))
        else:
            self.new_doc()

        if self.state.get("fullscreen", True):
            self.fullscreen()

    # ---------- tags / styling ----------
    def make_tags(self):
        b = self.buffer
        self.tags = {}

        def tag(name, **props):
            self.tags[name] = b.create_tag(name, **props)

        # Earlier = lower priority.
        tag("codeblock", family=MONO, scale=0.9)
        tag("quote", style=Pango.Style.ITALIC)
        tag("list")
        tag("hr", justification=Gtk.Justification.CENTER)
        tag("h1", weight=Pango.Weight.BOLD, scale=1.75, pixels_above_lines=18, pixels_below_lines=6)
        tag("h2", weight=Pango.Weight.BOLD, scale=1.4, pixels_above_lines=14, pixels_below_lines=4)
        tag("h3", weight=Pango.Weight.BOLD, scale=1.18, pixels_above_lines=10, pixels_below_lines=2)
        tag("h4", weight=Pango.Weight.BOLD, scale=1.05)
        tag("bold", weight=Pango.Weight.BOLD)
        tag("italic", style=Pango.Style.ITALIC)
        tag("strike", strikethrough=True)
        tag("code", family=MONO, scale=0.9)
        tag("link", underline=Pango.Underline.SINGLE)
        tag("listmark", weight=Pango.Weight.BOLD)
        tag("marker", family=MONO, scale=0.85)
        tag("hidden", invisible=True)

    def color_tags(self):
        t = THEMES[self.theme]
        self.tags["marker"].set_property("foreground", t["dim"])
        self.tags["listmark"].set_property("foreground", t["caret"])
        self.tags["hr"].set_property("foreground", t["dim"])
        self.tags["link"].set_property("foreground", t["caret"])
        self.tags["code"].set_property("background", t["code"])
        self.tags["codeblock"].set_property("paragraph-background", t["code"])
        self.tags["quote"].set_property("foreground", t["quote"])

    def margin_tags(self):
        cw = self.char_width()
        # A tag's left-margin replaces the view's, so these are absolute.
        # Negative indent = Pango hanging indent: wrapped lines move right.
        self.tags["quote"].set_property("left-margin", self.side + int(cw * 2.5))
        self.tags["codeblock"].set_property("left-margin", self.side + int(cw * 1.5))
        hang = int(cw * 1.5)
        self.tags["list"].set_property("left-margin", self.side)
        self.tags["list"].set_property("indent", -hang)

    def apply_style(self):
        t = THEMES[self.theme]
        fam = FONTS["mono" if self.raw else self.font]
        mono = fam == MONO
        css = f"""
        window, scrolledwindow, textview, textview text {{
            background-color: {t['bg']}; color: {t['fg']};
        }}
        textview.editor {{
            font-family: "{fam}"; font-size: {self.size}pt;
            line-height: {self.line_factor(fam, 1.45 if mono else 1.6):.3f};
            caret-color: {t['caret']};
        }}
        textview text selection {{ background-color: {t['sel']}; color: {t['fg']}; }}
        scrollbar {{ opacity: 0; }}
        scrollbar:hover {{ opacity: 0.6; }}
        .toolbar {{
            background-color: {t['panel']}; border-radius: 0 0 10px 10px;
            padding: 2px 10px;
        }}
        .toolbar button {{
            background: none; border: none; box-shadow: none; color: {t['dim']}; opacity: 0.6;
            min-width: 30px; min-height: 26px; padding: 2px 6px; font-size: 11pt;
        }}
        .toolbar:hover button {{ opacity: 1; }}
        .toolbar button:hover {{ color: {t['fg']}; background-color: {t['hl']}; }}
        .toolbar button.on {{ color: {t['caret']}; opacity: 1; }}
        .toolbar .sep {{ color: {t['dim']}; margin: 0 4px; }}
        .status {{
            color: {t['dim']}; font-family: "{MONO}", monospace; font-size: 9.5pt;
            margin-bottom: 14px;
        }}
        .panel {{
            background-color: {t['panel']}; color: {t['fg']};
            border-radius: 10px; padding: 18px;
            box-shadow: 0 8px 40px rgba(0,0,0,0.35);
        }}
        .help {{ font-family: "{MONO}", monospace; font-size: 10.5pt; }}
        .panel entry {{ background: {t['bg']}; color: {t['fg']}; border: none; box-shadow: none; padding: 6px; }}
        .panel list, .panel row {{ background: transparent; color: {t['fg']}; }}
        .panel row {{ padding: 6px 8px; border-radius: 6px; }}
        .panel row:selected {{ background-color: {t['hl']}; }}
        .panel row .meta {{ color: {t['dim']}; font-size: 9pt; }}
        """
        css += self.image_css(t)
        self.css.load_from_string(css)
        self.color_tags()
        self.last_alloc = (0, 0)  # force margin recompute

    def line_factor(self, fam, target):
        """CSS line-height multiplies the font's natural line height, which varies a
        lot between fonts (Charis SIL is very tall). Normalise to target × size."""
        desc = Pango.FontDescription.from_string(f"{fam} {self.size}")
        natural = self.view.get_pango_context().get_metrics(desc, None).get_height() / Pango.SCALE
        px = self.size * 96 / 72
        return target * px / natural if natural > 0 else target

    def char_width(self):
        ctx = self.view.get_pango_context()
        fam = FONTS["mono" if self.raw else self.font]
        desc = Pango.FontDescription.from_string(f"{fam} {self.size}")
        return ctx.get_metrics(desc, None).get_approximate_char_width() / Pango.SCALE

    def on_tick(self, _widget, _clock):
        w, h = self.scroller.get_width(), self.scroller.get_height()
        if (w, h) != self.last_alloc and w > 0:
            self.last_alloc = (w, h)
            col = COLUMN_CHARS * self.char_width()
            self.side = max(24, int((w - col) / 2))
            self.view.set_left_margin(self.side)
            self.view.set_right_margin(self.side)
            self.view.set_top_margin(max(56, int(h * 0.12)))
            self.view.set_bottom_margin(int(h * 0.5) if self.typewriter else 48)
            self.margin_tags()
        return GLib.SOURCE_CONTINUE

    # ---------- live markdown rendering ----------
    def line_iter(self, line, offset=0):
        ok, it = self.buffer.get_iter_at_line_offset(line, offset)
        return it

    def schedule_restyle(self, *lines, full=False):
        self.dirty_lines.update(lines)
        if full:
            self.full_restyle = True
        if self.restyle_source is None:
            # Higher priority than redraw, so markers never flash on screen.
            self.restyle_source = GLib.idle_add(self.flush_restyle, priority=GLib.PRIORITY_HIGH_IDLE)

    def flush_restyle(self):
        """Restyle edited lines now; queue bulk work (load, fence changes) in chunks."""
        self.restyle_source = None
        lines = self.text().split("\n")
        fences = fence_map(lines)
        cur = self.buffer.get_iter_at_mark(self.buffer.get_insert())
        cline, coff = cur.get_line(), cur.get_line_offset()
        todo = set()
        if self.raw:
            self.buffer.remove_all_tags(self.buffer.get_start_iter(), self.buffer.get_end_iter())
            self.queue = []
        else:
            requeue = self.full_restyle or (self.queue and len(lines) != self.queue_lines)
            if requeue:
                self.queue = list(range(len(lines)))
            elif self.last_fences is not None and fences != self.last_fences:
                old = self.last_fences
                self.queue.extend(i for i in range(len(lines)) if i >= len(old) or fences[i] != old[i])
            for n in self.dirty_lines | {cline}:
                todo.update((n - 1, n, n + 1))
            todo = {n for n in todo if 0 <= n < len(lines)}
            if self.queue:
                # Nearest the cursor first, so what you see is rendered first.
                self.queue = sorted(set(self.queue) - todo, key=lambda n: -abs(n - cline))
                self.queue_lines = len(lines)
                if self.queue_source is None:
                    self.queue_source = GLib.idle_add(self.drain_queue)
        self.full_restyle = False
        self.dirty_lines.clear()
        self.last_fences = fences
        for n in sorted(todo):
            self.style_line(n, lines[n], fences[n], coff if n == cline else None)
        self.update_toolbar_state()
        return GLib.SOURCE_REMOVE

    def drain_queue(self):
        lines = self.text().split("\n")
        if self.raw or len(lines) != self.queue_lines:
            self.queue_source = None
            if not self.raw and self.queue:
                self.schedule_restyle(full=True)  # lines shifted under us: start over
            return GLib.SOURCE_REMOVE
        fences = fence_map(lines)
        cur = self.buffer.get_iter_at_mark(self.buffer.get_insert())
        cline, coff = cur.get_line(), cur.get_line_offset()
        deadline = time.perf_counter() + 0.012
        while self.queue and time.perf_counter() < deadline:
            n = self.queue.pop()
            self.style_line(n, lines[n], fences[n], coff if n == cline else None)
        if self.queue:
            return GLib.SOURCE_CONTINUE
        self.queue_source = None
        return GLib.SOURCE_REMOVE

    def style_line(self, n, s, kind, cur):
        buf = self.buffer
        start = self.line_iter(n)
        nxt = self.line_iter(n + 1) if n + 1 < buf.get_line_count() else buf.get_end_iter()
        buf.remove_all_tags(start, nxt)
        if not s:
            return

        def apply(name, a, b):
            if b > a:
                buf.apply_tag(self.tags[name], self.line_iter(n, a), self.line_iter(n, b))

        if kind in ("code", "fence"):
            apply("codeblock", 0, len(s))
            if kind == "fence":
                apply("marker", 0, len(s))
            return
        if RE_HR.match(s):
            apply("hr", 0, len(s))
            apply("marker", 0, len(s))
            return

        body = 0
        m = RE_HEADING.match(s)
        if m:
            level = min(len(m.group(1)), 4)
            apply(f"h{level}", 0, len(s))
            body = m.end()
        else:
            m = RE_QUOTE.match(s)
            if m:
                apply("quote", 0, len(s))
                body = m.end()
        if m:
            reveal = cur is not None and cur <= body
            apply("marker" if reveal else "hidden", 0, body)
        else:
            m = RE_LIST.match(s)
            if m:
                apply("list", 0, len(s))
                apply("listmark", m.start(2), m.end(2))
                if m.group(3):
                    apply("marker", m.start(3), m.end(3))
                body = m.end()

        for tag, a, b, markers, span_a, span_b in parse_inline(s[body:]):
            apply(tag, body + a, body + b)
            reveal = cur is not None and body + span_a < cur < body + span_b
            for ma, mb in markers:
                apply("marker" if reveal else "hidden", body + ma, body + mb)

    def on_insert_after(self, _buf, location, text, _len):
        if self.loading:
            return
        end = location.get_line()
        self.schedule_restyle(*range(end - text.count("\n"), end + 1))

    def on_delete_after(self, _buf, start, _end):
        if not self.loading:
            self.schedule_restyle(start.get_line())

    def on_mark_set(self, buf, it, mark):
        if mark is not buf.get_insert() or self.loading:
            return
        line = it.get_line()
        # Restyle old and new cursor lines so markers hide/reveal.
        self.schedule_restyle(self.cursor_line, line)
        self.cursor_line = line

    def update_toolbar_state(self):
        it = self.buffer.get_iter_at_mark(self.buffer.get_insert())
        on = set()
        if not self.raw:
            for k in ("bold", "italic", "strike", "code", "link"):
                if it.has_tag(self.tags[k]):
                    on.add(k)
        line = self.current_line_text()
        m = RE_HEADING.match(line)
        if m:
            on.add(f"h{len(m.group(1))}")
        elif RE_QUOTE.match(line):
            on.add("quote")
        else:
            m = RE_LIST.match(line)
            if m:
                on.add("ol" if m.group(2)[0].isdigit() else "ul")
        if self.raw:
            on.add("raw")
        for k, b in self.tool_buttons.items():
            (b.add_css_class if k in on else b.remove_css_class)("on")

    # ---------- formatting commands ----------
    def cursor_offset(self):
        return self.buffer.get_iter_at_mark(self.buffer.get_insert()).get_offset()

    def current_line_text(self):
        it = self.buffer.get_iter_at_mark(self.buffer.get_insert())
        a = self.line_iter(it.get_line())
        b = a.copy()
        if not b.ends_line():
            b.forward_to_line_end()
        return self.buffer.get_text(a, b, True)

    def iter_at(self, off):
        return self.buffer.get_iter_at_offset(off)

    def toggle_inline(self, mk):
        buf = self.buffer
        text = self.text()
        L = len(mk)

        def is_mark(a):
            if text[a:a + L] != mk:
                return False
            if mk == "*":  # don't mistake one half of ** for italic
                return text[a - 1:a] != "*" and text[a + 1:a + 2] != "*" or text[a - 2:a + 3].count("*") == 3
            return True

        buf.begin_user_action()
        try:
            sel = buf.get_selection_bounds()
            if sel:
                s, e = sel[0].get_offset(), sel[1].get_offset()
                while e > s and text[e - 1].isspace():
                    e -= 1
                if s >= L and is_mark(s - L) and is_mark(e):
                    buf.delete(self.iter_at(e), self.iter_at(e + L))
                    buf.delete(self.iter_at(s - L), self.iter_at(s))
                    buf.select_range(self.iter_at(s - L), self.iter_at(e - L))
                elif e - s >= 2 * L and is_mark(s) and is_mark(e - L):
                    buf.delete(self.iter_at(e - L), self.iter_at(e))
                    buf.delete(self.iter_at(s), self.iter_at(s + L))
                    buf.select_range(self.iter_at(s), self.iter_at(e - 2 * L))
                else:
                    buf.insert(self.iter_at(e), mk)
                    buf.insert(self.iter_at(s), mk)
                    buf.select_range(self.iter_at(s + L), self.iter_at(e + L))
                return
            c = self.cursor_offset()
            # Cursor sits just before a closing marker: step out of the span.
            if is_mark(c) and text.rfind(mk, 0, c) != -1:
                buf.place_cursor(self.iter_at(c + L))
                return
            # Inside an existing span of this kind: unwrap it.
            name = {"**": "bold", "*": "italic", "~~": "strike", "`": "code"}[mk]
            it = self.iter_at(c)
            tag = self.tags[name]
            if not self.raw and it.has_tag(tag) and not it.starts_tag(tag):
                a, b = it.copy(), it.copy()
                a.backward_to_tag_toggle(tag)
                b.forward_to_tag_toggle(tag)
                ao, bo = a.get_offset(), b.get_offset()
                if ao >= L and text[ao - L:ao] == mk and text[bo:bo + L] == mk:
                    buf.delete(self.iter_at(bo), self.iter_at(bo + L))
                    buf.delete(self.iter_at(ao - L), self.iter_at(ao))
                    buf.place_cursor(self.iter_at(c - L))
                    return
            buf.insert_at_cursor(mk + mk)
            buf.place_cursor(self.iter_at(c + L))
        finally:
            buf.end_user_action()

    def insert_link(self):
        buf = self.buffer
        buf.begin_user_action()
        sel = buf.get_selection_bounds()
        if sel:
            s, e = sel[0].get_offset(), sel[1].get_offset()
            buf.insert(self.iter_at(e), "]()")
            buf.insert(self.iter_at(s), "[")
            buf.place_cursor(self.iter_at(e + 3))  # inside ()
        else:
            c = self.cursor_offset()
            buf.insert_at_cursor("[]()")
            buf.place_cursor(self.iter_at(c + 1))
        buf.end_user_action()

    def set_block(self, prefix):
        """Apply a block prefix to every line in the selection, or toggle it off."""
        buf = self.buffer
        sel = buf.get_selection_bounds()
        if sel:
            first, last = sel[0].get_line(), sel[1].get_line()
            if sel[1].starts_line() and last > first:
                last -= 1
        else:
            first = last = buf.get_iter_at_mark(buf.get_insert()).get_line()
        lines = self.text().split("\n")
        kind = lambda p: "ol" if p[:1].isdigit() else p.strip()  # noqa: E731
        current = RE_BLOCK_PREFIX.match(lines[first])
        same = bool(prefix) and current and kind(current.group(1).strip() + " ") == kind(prefix)
        buf.begin_user_action()
        for i, n in enumerate(range(first, last + 1)):
            m = RE_BLOCK_PREFIX.match(lines[n])
            if m:
                buf.delete(self.line_iter(n), self.line_iter(n, m.end()))
            if prefix and not same:
                p = f"{i + 1}. " if prefix == "1. " else prefix
                buf.insert(self.line_iter(n), p)
        buf.end_user_action()
        self.schedule_restyle(*range(first, last + 1))

    def toggle_raw(self):
        self.raw = not self.raw
        self.toggle("raw", self.raw)
        self.apply_style()
        self.schedule_restyle(full=True)
        self.flash("raw markdown" if self.raw else "formatted")

    def smart_return(self):
        """Continue lists/quotes on Enter; an empty item ends the list."""
        buf = self.buffer
        if buf.get_has_selection():
            return False
        it = buf.get_iter_at_mark(buf.get_insert())
        n, off = it.get_line(), it.get_line_offset()
        line = self.current_line_text()
        if fence_map(self.text().split("\n"))[n] == "code":
            return False
        m = RE_LIST.match(line) or RE_QUOTE.match(line)
        if not m or off < m.end():
            return False
        buf.begin_user_action()
        if not line[m.end():].strip():
            buf.delete(self.line_iter(n), self.line_iter(n, len(line)))
        else:
            prefix = m.group(0)
            if m.re is RE_LIST:
                mark = m.group(2)
                if mark[0].isdigit():
                    mark = f"{int(mark[:-1]) + 1}{mark[-1]}"
                prefix = f"{m.group(1)}{mark} " + ("[ ] " if m.group(3) else "")
            buf.insert_at_cursor("\n" + prefix)
        buf.end_user_action()
        GLib.idle_add(self.scroll_to_cursor)
        return True

    def smart_dash(self, ch):
        """Turn `--` into an em dash once the next character is typed."""
        if ch == "-" or self.buffer.get_has_selection():
            return
        it = self.buffer.get_iter_at_mark(self.buffer.get_insert())
        off = it.get_line_offset()
        line = self.current_line_text()[:off]
        if off < 3 or not line.endswith("--") or line[-3] in "-`" or not line[:-2].strip():
            return
        if fence_map(self.text().split("\n"))[it.get_line()] or line.count("`") % 2:
            return
        c = it.get_offset()
        self.buffer.begin_user_action()
        self.buffer.delete(self.iter_at(c - 2), self.iter_at(c))
        self.buffer.insert(self.iter_at(c - 2), "—")
        self.buffer.end_user_action()

    # ---------- documents ----------
    def text(self):
        return self.buffer.get_text(self.buffer.get_start_iter(), self.buffer.get_end_iter(), True)

    def set_text(self, text):
        self.loading = True
        self.buffer.begin_irreversible_action()
        self.buffer.set_text(text)
        self.buffer.end_irreversible_action()
        self.buffer.place_cursor(self.buffer.get_end_iter())
        self.cursor_line = self.buffer.get_end_iter().get_line()
        self.loading = False
        self.dirty = False
        self.schedule_restyle(full=True)
        GLib.idle_add(self.scroll_to_cursor)

    def leave_current(self):
        """Save the current doc, discarding it if it is empty."""
        self.save()
        if self.path and self.path.exists() and not self.text().strip():
            try:
                self.path.unlink()
            except OSError:
                pass

    def new_doc(self):
        if self.path:
            self.leave_current()
        self.path = new_doc_path()
        self.set_text("")
        self.update_status()
        self.flash("new document")

    def open_path(self, path):
        if self.path:
            self.leave_current()
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        except (OSError, UnicodeDecodeError) as e:
            self.flash(f"can't open: {e}")
            return
        self.path = path
        self.set_text(text)
        self.state["last"] = str(path)
        save_state(self.state)
        self.update_status()

    def save(self):
        if self.save_source:
            GLib.source_remove(self.save_source)
            self.save_source = None
        if not self.dirty or not self.path:
            return True
        text = self.text()
        if not text.strip() and not self.path.exists():
            self.dirty = False
            return True
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError as e:
            self.flash(f"SAVE FAILED: {e}", 6000)
            return False
        self.dirty = False
        self.state["last"] = str(self.path)
        save_state(self.state)
        self.update_status()
        return True

    def on_changed(self, _buf):
        if self.loading:
            return
        self.dirty = True
        if self.save_source:
            GLib.source_remove(self.save_source)
        self.save_source = GLib.timeout_add(AUTOSAVE_MS, self._autosave)
        if self.typewriter:
            GLib.idle_add(self.scroll_to_cursor)
        if self.status_visible and self.status_source is None:
            self.status_source = GLib.timeout_add(400, self._status_tick)

    def _status_tick(self):
        self.status_source = None
        if self.flash_source is None:
            self.update_status()
        return GLib.SOURCE_REMOVE

    def _autosave(self):
        self.save_source = None
        self.save()
        return GLib.SOURCE_REMOVE

    def scroll_to_cursor(self):
        mark = self.buffer.get_insert()
        if self.typewriter:
            self.view.scroll_to_mark(mark, 0.0, True, 0.0, 0.5)
        else:
            self.view.scroll_mark_onscreen(mark)
        return GLib.SOURCE_REMOVE

    # ---------- status ----------
    def update_status(self, msg=None):
        if msg is None:
            words = len(re.findall(r"[\w'’-]+", self.text()))
            name = self.path.name if self.path else ""
            mark = "•" if self.dirty else "saved"
            msg = f"{name}   {words:,} words   {mark}"
        self.status.set_label(msg)

    def flash(self, msg, ms=1600):
        self.update_status(msg)
        self.status.set_visible(True)
        if self.flash_source:
            GLib.source_remove(self.flash_source)

        def done():
            self.flash_source = None
            self.update_status()
            self.status.set_visible(self.status_visible)
            return GLib.SOURCE_REMOVE

        self.flash_source = GLib.timeout_add(ms, done)

    # ---------- picker ----------
    def open_picker(self):
        self.save()
        while (row := self.listbox.get_row_at_index(0)) is not None:
            self.listbox.remove(row)
        files = sorted(DOC_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            title = doc_title(p)
            t = Gtk.Label(label=title, xalign=0, ellipsize=Pango.EllipsizeMode.END)
            when = time.strftime("%a %d %b %Y, %H:%M", time.localtime(p.stat().st_mtime))
            meta = Gtk.Label(label=f"{when}   {p.name}", xalign=0)
            meta.add_css_class("meta")
            box.append(t)
            box.append(meta)
            row.set_child(box)
            row.doc_path = p
            row.doc_key = f"{title} {p.name}".lower()
            self.listbox.append(row)
        self.search.set_text("")
        self.picker.set_visible(True)
        self.select_first_visible()
        self.search.grab_focus()

    def close_picker(self):
        self.picker.set_visible(False)
        self.view.grab_focus()

    def filter_row(self, row):
        q = self.search.get_text().strip().lower()
        return all(word in row.doc_key for word in q.split())

    def on_search_changed(self, _entry):
        self.listbox.invalidate_filter()
        self.select_first_visible()

    def visible_rows(self):
        rows, i = [], 0
        while (row := self.listbox.get_row_at_index(i)) is not None:
            if row.get_child_visible():
                rows.append(row)
            i += 1
        return rows

    def select_first_visible(self):
        rows = self.visible_rows()
        self.listbox.select_row(rows[0] if rows else None)

    def move_selection(self, delta):
        rows = self.visible_rows()
        if not rows:
            return
        cur = self.listbox.get_selected_row()
        i = rows.index(cur) if cur in rows else -1
        i = max(0, min(len(rows) - 1, i + delta))
        self.listbox.select_row(rows[i])
        self.search.grab_focus()
        self.search.set_position(-1)

    def open_from_picker(self, row):
        if row is None or not row.get_child_visible():
            return
        self.close_picker()
        if row.doc_path != self.path:
            self.open_path(row.doc_path)
        self.flash(doc_title(row.doc_path))

    def on_picker_key(self, _ctl, keyval, _code, _state):
        if keyval == Gdk.KEY_Escape:
            self.close_picker()
            return True
        if keyval in (Gdk.KEY_Down, Gdk.KEY_Tab):
            self.move_selection(1)
            return True
        if keyval in (Gdk.KEY_Up, Gdk.KEY_ISO_Left_Tab):
            self.move_selection(-1)
            return True
        return False

    # ---------- keys ----------
    def toggle(self, key, value):
        self.state[key] = value
        save_state(self.state)

    def on_key(self, _ctl, keyval, _code, state):
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        alt = bool(state & (Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK))
        k = Gdk.keyval_to_lower(keyval)

        if self.help.get_visible():
            self.help.set_visible(False)
            return True
        if keyval == Gdk.KEY_F1 or (ctrl and k in (Gdk.KEY_slash, Gdk.KEY_question)):
            self.help.set_visible(True)
            return True
        if keyval == Gdk.KEY_F11 or (ctrl and shift and k == Gdk.KEY_f):
            fs = not self.is_fullscreen()
            self.fullscreen() if fs else self.unfullscreen()
            self.toggle("fullscreen", fs)
            return True

        if not ctrl:
            if self.picker.get_visible() or alt:
                return False
            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and not shift:
                return self.smart_return()
            ch = Gdk.keyval_to_unicode(keyval)
            if ch:
                self.smart_dash(chr(ch))
            return False

        if shift and k == Gdk.KEY_o:
            self.choose_bg_image()
            return True
        if shift and k == Gdk.KEY_t:
            self.toggle_bg_image()
            return True

        # File-level commands work everywhere.
        if k == Gdk.KEY_q:
            self.close()
            return True
        if k == Gdk.KEY_s:
            self.dirty = True
            if self.save():
                self.flash("saved")
            return True
        if k == Gdk.KEY_n:
            if self.picker.get_visible():
                self.close_picker()
            self.new_doc()
            return True
        if k == Gdk.KEY_o:
            self.close_picker() if self.picker.get_visible() else self.open_picker()
            return True
        if self.picker.get_visible():
            return False

        commands = {
            Gdk.KEY_b: lambda: self.toggle_inline("**"),
            Gdk.KEY_d: lambda: self.toggle_inline("~~"),
            Gdk.KEY_e: lambda: self.toggle_inline("`"),
            Gdk.KEY_k: self.insert_link,
            Gdk.KEY_1: lambda: self.set_block("# "),
            Gdk.KEY_2: lambda: self.set_block("## "),
            Gdk.KEY_3: lambda: self.set_block("### "),
            Gdk.KEY_0: lambda: self.set_block(""),
            Gdk.KEY_apostrophe: lambda: self.set_block("> "),
            Gdk.KEY_r: self.toggle_raw,
            Gdk.KEY_backslash: self.toggle_toolbar,
            Gdk.KEY_t: self.toggle_theme,
            Gdk.KEY_m: self.toggle_font,
            Gdk.KEY_y: self.toggle_typewriter,
            Gdk.KEY_equal: lambda: self.font_size(1),
            Gdk.KEY_plus: lambda: self.font_size(1),
            Gdk.KEY_KP_Add: lambda: self.font_size(1),
            Gdk.KEY_minus: lambda: self.font_size(-1),
            Gdk.KEY_KP_Subtract: lambda: self.font_size(-1),
            Gdk.KEY_bracketleft: lambda: self.adjust_overlay(-0.05),
            Gdk.KEY_bracketright: lambda: self.adjust_overlay(0.05),
        }
        if k == Gdk.KEY_i:
            self.toggle_status() if shift else self.toggle_inline("*")
        elif k == Gdk.KEY_l:
            self.set_block("1. " if shift else "- ")
        elif k in commands:
            commands[k]()
        else:
            return False
        return True

    def toggle_status(self):
        self.status_visible = not self.status_visible
        self.update_status()
        self.status.set_visible(self.status_visible)
        self.toggle("status", self.status_visible)

    def toggle_toolbar(self):
        v = not self.toolbar.get_visible()
        self.toolbar.set_visible(v)
        self.toggle("toolbar", v)

    def toggle_theme(self):
        i = THEME_ORDER.index(self.theme) if self.theme in THEME_ORDER else -1
        self.theme = THEME_ORDER[(i + 1) % len(THEME_ORDER)]
        self.apply_style()
        self.toggle("theme", self.theme)
        self.flash(f"theme: {self.theme}")

    # ---------- background image ----------
    def image_css(self, t):
        """Image behind the text, under a wash of the theme's background colour."""
        if not (self.bg_on and self.bg_image):
            return ""
        if not Path(self.bg_image).is_file():
            self.bg_on = False
            GLib.idle_add(lambda: self.flash("background image not found") and False)
            return ""
        h = t["bg"].lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        wash = f"rgba({r},{g},{b},{self.overlay:.2f})"
        uri = Gio.File.new_for_path(self.bg_image).get_uri()
        return f"""
        .canvas {{
            background-image: linear-gradient({wash}, {wash}), url("{uri}");
            background-size: cover; background-position: center;
        }}
        scrolledwindow, textview, textview text {{ background-color: transparent; }}
        """

    def choose_bg_image(self):
        dialog = Gtk.FileDialog(title="Background image", modal=True)
        flt = Gtk.FileFilter(name="Images")
        flt.add_mime_type("image/*")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(flt)
        dialog.set_filters(filters)
        start = Path(self.bg_image).parent if self.bg_image else Path.home() / "Pictures"
        if start.is_dir():
            dialog.set_initial_folder(Gio.File.new_for_path(str(start)))

        def done(dlg, result):
            try:
                f = dlg.open_finish(result)
            except GLib.Error:
                f = None  # cancelled
            if f and f.get_path():
                self.bg_image, self.bg_on = f.get_path(), True
                self.toggle("bg_image", self.bg_image)
                self.toggle("bg_on", True)
                self.apply_style()
            self.view.grab_focus()

        dialog.open(self, None, done)

    def toggle_bg_image(self):
        if not self.bg_image:
            self.choose_bg_image()
            return
        self.bg_on = not self.bg_on
        self.toggle("bg_on", self.bg_on)
        self.apply_style()
        self.flash("image " + ("on" if self.bg_on else "off"))

    def adjust_overlay(self, d):
        if not (self.bg_on and self.bg_image):
            self.flash("no background image (Ctrl+Shift+O to choose one)")
            return
        self.overlay = round(max(0.3, min(0.97, self.overlay + d)), 2)
        self.toggle("overlay", self.overlay)
        self.apply_style()
        self.flash(f"image {round((1 - self.overlay) * 100)}% visible")

    def toggle_font(self):
        self.font = "mono" if self.font == "serif" else "serif"
        self.apply_style()
        self.toggle("font", self.font)

    def toggle_typewriter(self):
        self.typewriter = not self.typewriter
        self.last_alloc = (0, 0)
        self.toggle("typewriter", self.typewriter)
        self.flash("typewriter " + ("on" if self.typewriter else "off"))
        GLib.idle_add(self.scroll_to_cursor)

    def font_size(self, d):
        self.size = max(8, min(40, self.size + d))
        self.apply_style()
        self.toggle("size", self.size)

    def on_close(self, _win):
        if not self.save():
            return True  # saving failed: refuse to close so nothing is lost
        self.leave_current()
        return False


class DraftApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.initial = sys.argv[1] if len(sys.argv) > 1 else None

    def do_activate(self):
        win = self.get_active_window() or DraftWindow(self, self.initial)
        win.present()


if __name__ == "__main__":
    # Pass only argv[0] to GTK; we handle the optional file argument ourselves.
    sys.exit(DraftApp().run(sys.argv[:1]))
