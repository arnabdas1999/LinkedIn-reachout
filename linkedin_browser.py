"""Dedicated browser session with visible login and headless routine runs.

LinkedIn changes its UI. Unrecognized states stop the run instead of guessing.
"""

from dataclasses import dataclass
import json
from pathlib import Path
import os
import re
import shutil
import sys
import time
import unicodedata
from urllib.parse import parse_qs, urljoin, urlsplit

from playwright.sync_api import Error as BrowserError, TimeoutError as BrowserTimeoutError, sync_playwright

from outreach_errors import ProfileNeedsReview, StopRun
from lead_data import canonical_profile


def normalized(text):
    return " ".join(unicodedata.normalize("NFKC", text).split()).casefold()


def brave_executable():
    candidates = [Path(os.environ[key]) / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe"
                  for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
                  if os.environ.get(key)]
    if sys.platform == "darwin":
        candidates.extend([Path('/Applications/Brave Browser.app/Contents/MacOS/Brave Browser'),
                           Path.home() / 'Applications/Brave Browser.app/Contents/MacOS/Brave Browser'])
    if sys.platform.startswith("linux"):
        candidates.extend(Path(value) for name in ('brave-browser', 'brave-browser-stable', 'brave')
                          if (value := shutil.which(name)))
    executable = next((path for path in candidates if path.is_file()), None)
    if executable is None:
        raise StopRun("Brave was not found. Install it or set browser_executable in settings.json")
    return str(executable)


@dataclass
class History:
    inbound: bool = False
    unknown: bool = False


class LinkedInBrowser:
    def __init__(self, profile_dir, channel="chromium", *, headless=True, executable_path=None):
        self.profile_dir, self.channel = Path(profile_dir), channel
        self.headless, self.executable_path = headless, executable_path

    def __enter__(self):
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.pw = sync_playwright().start()
        try:
            options = dict(user_data_dir=str(self.profile_dir), headless=self.headless, chromium_sandbox=True,
                           viewport={"width": 1360, "height": 900}, locale="en-US")
            if self.executable_path:
                executable = Path(self.executable_path).expanduser()
                if not executable.is_file():
                    raise StopRun(f"Browser executable not found: {executable}")
                options["executable_path"] = str(executable)
            elif self.channel == "brave":
                options["executable_path"] = brave_executable()
            else:
                # Use full Chromium's new headless mode so login and routine
                # runs share the same browser implementation and profile.
                options["channel"] = self.channel
            self.context = self.pw.chromium.launch_persistent_context(**options)
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.close_other_pages()
            self.page.set_default_timeout(10000)
            self.page.set_default_navigation_timeout(45000)
            return self
        except BaseException:
            self.pw.stop()
            raise

    def __exit__(self, *args):
        try:
            self.context.close()
        finally:
            self.pw.stop()

    def screenshot(self, path):
        self.page.screenshot(path=str(path), full_page=False)

    def profile_diagnostic(self, lead, path):
        """Save public profile headings/controls only; omit inbox and account chrome."""
        try:
            current_url = canonical_profile(self.page.url)
        except ValueError:
            return
        if current_url != lead["url"]:
            return
        main = self.one_visible(self.page.get_by_role("main"), "public profile content", optional=True)
        if main is None:
            return
        data = main.evaluate("""root => ({
            headings: [...root.querySelectorAll('h1,h2,h3,[role="heading"]')].slice(0,18).map(el => ({
                text: el.innerText, tag: el.tagName, role: el.getAttribute('role'),
                parents: [...(function*(node) { for(let i=0; node && i<5; i++,node=node.parentElement) yield node; })(el.parentElement)]
                    .map(p => ({tag:p.tagName,cls:p.className,view:p.getAttribute('data-view-name'),role:p.getAttribute('role')}))
            })),
            controls: [...root.querySelectorAll('button,[role="button"],a')].filter(el => el.getClientRects().length)
                .slice(0,35).map(el => ({text:el.innerText, label:el.getAttribute('aria-label'), tag:el.tagName,href:el.getAttribute('href'),target:el.getAttribute('target')}))
        })""")
        data["profile_actions"] = self.topcard().evaluate("""root => [...root.querySelectorAll('button,a,[role="button"]')]
            .filter(el => el.getClientRects().length).map(el => ({text:el.innerText,label:el.getAttribute('aria-label'),tag:el.tagName,href:el.getAttribute('href'),target:el.getAttribute('target')}))""")
        Path(path).write_text(json.dumps({"url": current_url, **data, "profile_menu": getattr(self, "last_profile_menu", None)}, ensure_ascii=False, indent=2), encoding="utf-8")

    def one_visible(self, locator, description, optional=False):
        visible = [item for item in locator.all() if item.is_visible()]
        if len(visible) == 1:
            return visible[0]
        if not visible and optional:
            return None
        raise StopRun(f"Expected one {description}; found {len(visible)}. UI needs inspection.")

    def guard(self, frame=None):
        surface = frame or self.page
        url = surface.url.casefold()
        if any(part in url for part in ("/checkpoint", "/challenge", "/authwall", "/login", "/uas/")):
            raise StopRun("LinkedIn needs login or verification. Complete it manually with the login command.")
        body = surface.locator("body").inner_text(timeout=30000).casefold()
        blockers = (
            "you've reached the weekly invitation limit", "you have reached the weekly invitation limit",
            "you've reached your invitation limit", "you have reached your invitation limit",
            "too many invitations", "no personalized invitations left", "no free personalized invitations left",
            "your account has been restricted", "your account is temporarily restricted",
            "verify your identity", "security verification", "unusual activity",
            "unusual traffic", "let's do a quick security check", "you've reached your monthly limit",
        )
        if any(text in body for text in blockers):
            raise StopRun("LinkedIn displayed a limit or verification notice. No further activity will run.")

    def login(self):
        if self.headless:
            raise StopRun("Login requires a visible browser. Use the login command.")
        self.page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
        self.page.bring_to_front()
        print(f"Sign in yourself in the opened {self.channel} window, including any verification.", flush=True)
        print("This window stays open until you sign in or close it. This command never sends anything.", flush=True)
        while True:
            if self.page.is_closed():
                raise StopRun("Browser closed before login finished")
            path = self.page.url.casefold()
            if "/feed" in path or "/mynetwork" in path:
                self.guard()
                self.page.locator("nav").first.wait_for(state="visible", timeout=30000)
                print("Signed in. Session saved for the next run.", flush=True)
                return
            self.page.wait_for_timeout(1000)

    def require_login(self):
        self.page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
        self.guard()
        self.page.locator("nav").first.wait_for(state="visible", timeout=30000)
        self.guard()
        self.close_other_pages()

    def close_other_pages(self):
        # This dedicated profile is owned by this script. Avoid restoring an
        # accumulating set of old profile tabs on every campaign check.
        for page in self.context.pages:
            if page != self.page and not page.is_closed():
                page.close()

    def conversation_panels(self):
        panels = []
        for frame in self.page.frames:
            parts = urlsplit(frame.url)
            if frame != self.page.main_frame and not (
                parts.hostname in ("www.linkedin.com", "linkedin.com")
                and (parts.path.startswith("/preload/") or parts.path.startswith("/messaging/"))
            ):
                continue
            try:
                panels.extend((panel, frame) for panel in frame.locator(".msg-overlay-conversation-bubble").all() if panel.is_visible())
            except BrowserError:
                if not frame.is_detached():
                    raise
        return panels

    def close_conversations(self):
        for panel, _ in self.conversation_panels():
            editors = panel.locator('[contenteditable="true"]').all()
            if any(editor.inner_text().strip() for editor in editors if editor.is_visible()):
                raise StopRun("An open conversation contains an unsent draft. Clear it manually before running.")
            close = self.one_visible(panel.get_by_role("button", name=re.compile(r"^Close (?:your )?conversation", re.I)),
                                     "close-conversation button")
            close.click()
            panel.wait_for(state="hidden")

    def profile_heading(self):
        # LinkedIn's current profile layout uses h2 for the name; older layouts use h1.
        return self.page.get_by_role("main").locator("h1,h2").first

    def topcard(self):
        heading = self.profile_heading()
        modern = heading.locator("xpath=ancestor::*[self::div or self::section][.//button[@aria-label='More' or @aria-label='More actions']][1]")
        if modern.count() == 1:
            return modern
        legacy = heading.locator("xpath=ancestor::section[1]")
        if legacy.count() == 1:
            return legacy
        raise StopRun("Cannot isolate this profile's introduction and action controls")

    def button(self, root, pattern, description, optional=True):
        name = re.compile(pattern, re.I)
        controls = root.get_by_role("button", name=name).or_(root.get_by_role("link", name=name))
        return self.one_visible(controls, description, optional)

    def pending_button(self, root):
        control = self.button(root, r"^(?:Pending(?: invitation(?: to .+)?)?|Invitation pending|Invite sent|Withdraw invitation(?: to .+)?)$", "pending invitation")
        if control:
            return control
        return self.one_visible(root.get_by_text("Pending", exact=True), "Pending invitation label", optional=True)

    def connect_button(self, root):
        return self.button(root, r"^(?:Connect(?: with .+)?|Invite .+ to connect)$", "connect action")

    def more_menu(self):
        menus = self.page.locator('[role="menu"]:visible, .artdeco-dropdown__content-inner:visible')
        for attempt in range(2):
            more = self.button(self.topcard(), r"^(?:More|More actions)$", "profile More button", optional=False)
            more.click()
            try:
                menus.first.wait_for(state="visible", timeout=10000)
                break
            except BrowserTimeoutError:
                self.guard()
                # Some profiles paint controls before attaching their handlers.
                # Retry only the read-only menu opener, never Connect or Send.
                if attempt or more.get_attribute("aria-expanded") == "true":
                    raise StopRun("Profile More menu did not become readable; no invitation sent") from None
        menu = self.one_visible(menus, "profile More menu")
        self.last_profile_menu = menu.evaluate("""root => ({text:root.innerText,
            controls:[...root.querySelectorAll('button,a,[role="menuitem"],[role="button"]')]
                .map(el => ({tag:el.tagName,text:el.innerText,label:el.getAttribute('aria-label'),role:el.getAttribute('role')}))})""")
        return menu

    def menu_connect(self, menu):
        # Some LinkedIn menus expose text rather than menuitem semantics.
        semantic = self.one_visible(menu.get_by_role("menuitem", name=re.compile(r"^Connect(?: with .+)?$", re.I)), "Connect menu item", optional=True)
        if semantic:
            return semantic
        semantic = self.connect_button(menu)
        if semantic:
            return semantic
        return self.one_visible(menu.get_by_text("Connect", exact=True), "Connect menu item", optional=True)

    def profile_state(self):
        root = self.topcard()
        degree = root.locator(".dist-value")
        if any(normalized(item.inner_text()) == "1st" for item in degree.all() if item.is_visible()):
            return "connected"
        if self.one_visible(root.get_by_text(re.compile(r"^1st(?: degree connection)?$")), "first-degree label", optional=True):
            return "connected"
        if self.pending_button(root):
            return "pending"
        if self.connect_button(root):
            return "new"
        menu = self.more_menu()
        try:
            if self.menu_connect(menu):
                return "new"
            if self.one_visible(menu.get_by_text(re.compile(r"^(?:Pending|Withdraw invitation)$", re.I)), "pending menu item", optional=True):
                return "pending"
            if self.one_visible(menu.get_by_text(re.compile(r"^Remove connection(?: with .+)?$", re.I)), "Remove connection menu item", optional=True):
                return "connected"
            raise StopRun("Could not determine whether this profile is connected or pending")
        finally:
            self.page.keyboard.press("Escape")

    def open_profile(self, lead):
        self.close_conversations()
        self.page.goto(lead["url"], wait_until="domcontentloaded")
        self.guard()
        unavailable = self.page.get_by_text(re.compile(r"^(?:This profile is not available|This page doesn.t exist).*$", re.I))
        if self.one_visible(unavailable, "unavailable-profile notice", optional=True):
            return "unavailable"
        self.profile_heading().wait_for(state="visible", timeout=30000)
        self.guard()
        try:
            actual_url = canonical_profile(self.page.url)
        except ValueError:
            raise StopRun("Profile navigation redirected to an unexpected URL") from None
        if actual_url != lead["url"]:
            raise ProfileNeedsReview("Profile URL changed. Review this lead before sending anything")
        name = self.one_visible(self.profile_heading(), "profile name").inner_text()
        if normalized(name) != normalized(lead["full_name"]):
            raise ProfileNeedsReview(f"Profile name mismatch: CSV has {lead['full_name']!r}; page shows {name!r}")
        return self.profile_state()

    def invitation_dialog(self, lead):
        expected = urlsplit(lead["url"]).path.strip("/").split("/")[-1]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            for frame in self.page.frames:
                parts = urlsplit(frame.url)
                is_invite = (
                    parts.hostname in ("www.linkedin.com", "linkedin.com")
                    and parts.path.rstrip("/") == "/preload/custom-invite"
                    and parse_qs(parts.query).get("vanityName") == [expected]
                )
                is_preload = parts.hostname in ("www.linkedin.com", "linkedin.com") and parts.path.rstrip("/") == "/preload"
                if frame != self.page.main_frame and not is_invite and not is_preload:
                    continue
                dialog = self.one_visible(frame.get_by_role("dialog"), "invitation dialog", optional=True)
                if dialog:
                    if is_preload and normalized(lead["full_name"]) not in normalized(dialog.inner_text()):
                        raise StopRun("Embedded invitation names a different recipient; nothing was sent")
                    return dialog, frame
                if is_invite:
                    note = self.button(frame, r"^Add a note$", "Add a note button")
                    if note:
                        # This route is bound to the exact profile selected above.
                        return frame.locator("body"), frame
            self.page.wait_for_timeout(250)
        patterns = re.compile(r"^(?:Add a note|Send(?: invitation)?|Send without a note|Cancel|Close)$", re.I)
        controls = self.page.get_by_role("button", name=patterns).or_(self.page.get_by_role("link", name=patterns))
        data = {"page_path": self.page.url.split("?")[0],
                "open_page_paths": [page.url.split("?")[0] for page in self.context.pages if not page.is_closed()],
                "frame_paths": [{"path": urlsplit(frame.url).path,
                                  "target_matches": parse_qs(urlsplit(frame.url).query).get("vanityName") == [expected]} for frame in self.page.frames],
                "controls": [{"text": item.inner_text(), "label": item.get_attribute("aria-label")} for item in controls.all() if item.is_visible()],
                "textareas": [{"name": item.get_attribute("name"), "id": item.get_attribute("id"), "label": item.get_attribute("aria-label")} for item in self.page.locator("textarea").all() if item.is_visible()]}
        from outreach import ROOT
        (ROOT / ".local" / "invitation-diagnostic.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
        self.page.screenshot(path=str(ROOT / ".local" / "invitation-inspection.png"))
        raise StopRun("Invitation form was not recognized. No Send action was clicked")

    def send_invitation(self, lead, message):
        self.guard()
        if self.profile_state() != "new":
            raise StopRun("This profile is no longer eligible for a new invitation")
        connect = self.connect_button(self.topcard())
        if connect is None:
            connect = self.menu_connect(self.more_menu())
        if connect is None:
            raise StopRun("Connect action is missing")
        # This click itself can send on some LinkedIn UI variants. Journal is already committed.
        connect.click()
        dialog, frame = self.invitation_dialog(lead)
        self.guard()
        self.guard(frame)
        note_fields = dialog.locator("textarea")
        has_note_field = any(item.is_visible() for item in note_fields.all())
        if not has_note_field:
            add_note_controls = dialog.get_by_role("button", name=re.compile(r"^Add a note$", re.I)).or_(dialog.get_by_role("link", name=re.compile(r"^Add a note$", re.I)))
            add_note_controls.first.wait_for(state="visible", timeout=10000)
            add_note = self.one_visible(add_note_controls, "Add a note button")
            add_note.click()
        note_fields.first.wait_for(state="visible", timeout=10000)
        editor = self.one_visible(note_fields, "invitation note field")
        editor.fill(message)
        send = self.button(dialog, r"^Send(?: invitation)?$", "Send invitation button", optional=False)
        self.guard()
        self.guard(frame)
        send.click()
        try:
            dialog.wait_for(state="hidden", timeout=15000)
        except BrowserError:
            if not frame.is_detached():
                raise
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            self.guard()
            if self.pending_button(self.topcard()):
                return
            self.page.wait_for_timeout(500)
        # The legacy invitation frame may not refresh the modern parent card.
        # Read the profile again once; never click Send again to confirm success.
        if self.open_profile(lead) == "pending":
            return
        raise StopRun("Clicked Send but could not verify Pending. Check LinkedIn; do not retry blindly")

    def open_conversation(self, lead):
        for attempt in range(2):
            self.guard()
            panels = self.conversation_panels()
            if not panels:
                button = self.button(self.topcard(), r"^Message(?: .+)?$", "profile Message button", optional=False)
                button.click()
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    panels = self.conversation_panels()
                    if panels:
                        break
                    self.page.wait_for_timeout(250)
            if len(panels) != 1:
                # Like More, Message can render before its handler is ready.
                # Only retry opening the conversation, never composing or sending.
                if not panels and not attempt:
                    continue
                raise StopRun(f"Expected one conversation panel; found {len(panels)}")
            panel, self.message_frame = panels[0]
            headers = panel.locator(".msg-overlay-bubble-header:visible, .msg-overlay-conversation-bubble-header:visible")
            headers.first.wait_for(state="visible")
            header = self.one_visible(headers, "conversation header")
            names = header.locator("h2, .msg-overlay-bubble-header__title")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if any(normalized(node.inner_text()) == normalized(lead["full_name"]) for node in names.all() if node.is_visible()):
                    break
                self.page.wait_for_timeout(100)
            else:
                self.guard(self.message_frame)
                if attempt:
                    raise StopRun("Conversation recipient does not match the selected profile")
                # A previous thread can be restored after profile navigation.
                # Close it without changing drafts, then select this profile again.
                self.close_conversations()
                continue
            break
        editors = panel.locator('[contenteditable="true"][role="textbox"], .msg-form__contenteditable[contenteditable="true"]')
        editors.first.wait_for(state="visible")
        editor = self.one_visible(editors, "message editor")
        if editor.inner_text().strip():
            raise StopRun("Conversation has an unsent draft. Clear it manually")
        return panel, editor

    def history(self, panel, lead, known):
        self.guard()
        if getattr(self, "message_frame", None) is not None:
            self.guard(self.message_frame)
        message_list = self.one_visible(panel.locator(".msg-s-message-list-content"), "message history", optional=True)
        if message_list is None:
            message_list = self.one_visible(panel.locator(".msg-s-message-list"), "message history", optional=True)
        if message_list is None:
            raise StopRun("Cannot verify loaded message history")
        # Wait for the rendered conversation to settle, not for arbitrary send timing.
        previous = None
        stable_since = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            self.guard()
            text = message_list.inner_text()
            loading = any(node.is_visible() for node in panel.locator('[role="progressbar"], .artdeco-loader').all())
            if text == previous and not loading:
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= 2:
                    break
            else:
                previous, stable_since = text, None
            self.page.wait_for_timeout(250)
        else:
            raise StopRun("Message history did not finish loading")
        bodies = panel.locator(".msg-s-event-listitem__body")
        texts = [normalized(item.inner_text()) for item in bodies.all() if item.is_visible()]
        allowed = {normalized(text) for text in known.values()}
        result = History()
        for text in texts:
            # Any message not sent by this campaign stops the sequence. This includes
            # recipient replies and your own manual messages, which also deserve a stop.
            if text not in allowed:
                result.inbound = True
        for link in panel.locator(".msg-s-event-listitem a[href*='/in/']").all():
            try:
                if canonical_profile(urljoin("https://www.linkedin.com", link.get_attribute("href") or "")) == lead["url"]:
                    result.inbound = True
            except ValueError:
                pass
        # Textless messages/attachments are not silently treated as an empty thread.
        events = panel.locator(".msg-s-event-listitem")
        if events.count() > len(texts):
            result.unknown = True
        if not texts and message_list.inner_text().strip():
            # A changed message layout must not make a populated thread look empty.
            result.unknown = True
        for kind, message in known.items():
            if kind != "invite" and normalized(message) not in texts:
                result.unknown = True
        return result

    def conversation(self, lead, known):
        panel, _ = self.open_conversation(lead)
        return self.history(panel, lead, known)

    def send_message(self, lead, message, known):
        if self.profile_state() != "connected":
            raise StopRun("Recipient is no longer a first-degree connection")
        panel, editor = self.open_conversation(lead)
        history = self.history(panel, lead, known)
        if history.inbound or history.unknown:
            raise StopRun("Conversation changed or has a reply. Message was not sent")
        target = normalized(message)
        before = sum(normalized(item.inner_text()) == target for item in panel.locator(".msg-s-event-listitem__body").all())
        if before:
            raise StopRun("This exact message already exists in the conversation")
        editor.fill(message)
        self.guard()
        # Recheck after composing, immediately before the mutating click.
        current = self.history(panel, lead, known)
        if current.inbound or current.unknown:
            editor.fill("")
            raise StopRun("Conversation changed while composing. Message was not sent")
        send = self.button(panel, r"^Send(?: message)?$", "Send message button", optional=False)
        send.click()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            self.guard()
            count = sum(normalized(item.inner_text()) == target for item in panel.locator(".msg-s-event-listitem__body").all())
            if count > before and not editor.inner_text().strip():
                return
            self.page.wait_for_timeout(500)
        raise StopRun("Clicked Send but could not verify the message. Check LinkedIn before reconciling")
