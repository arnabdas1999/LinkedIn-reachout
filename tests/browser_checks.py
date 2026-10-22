"""Browser adapter checks on intercepted HTML fixtures, with zero LinkedIn traffic.

Run explicitly: python -m unittest discover -s tests -p browser_checks.py -v
"""

from pathlib import Path
import os
import re
import tempfile
import unittest

from playwright.sync_api import sync_playwright

from linkedin_browser import LinkedInBrowser, brave_executable
from outreach import StopRun
from outreach_errors import ProfileNeedsReview

URL = "https://www.linkedin.com/in/alex/"
LEAD = {"id": 1, "url": URL, "first_name": "Alex", "full_name": "Alex Example", "company": "Oracle"}
NOTE = "Hi Alex, would love to connect!"
MESSAGE = "Hi Alex, thanks for accepting my invite!"

HTML = """<!doctype html><html lang="en"><body>
<nav>LinkedIn fixture</nav>
<main><section><h1>Alex Example</h1><span class="dist-value">DEGREE</span>
<button id="connect" aria-label="Invite Alex Example to connect" onclick="document.querySelector('dialog').showModal()">Connect</button>
<button aria-label="Message Alex Example" onclick="document.querySelector('.msg-overlay-conversation-bubble').style.display='block'">Message</button>
</section></main>
<dialog><button onclick="document.querySelector('textarea').hidden=false">Add a note</button>
<textarea hidden></textarea>
<button onclick="window.invitationText=document.querySelector('textarea').value; document.querySelector('dialog').close(); document.querySelector('#connect').outerHTML='<button>Pending</button>'; window.sends=(window.sends||0)+1;">Send</button>
</dialog>
<section class="msg-overlay-conversation-bubble" style="display:none">
<header class="msg-overlay-bubble-header"><h2><span class="msg-overlay-bubble-header__title">Alex Example</span></h2>
<button aria-label="Close your conversation with Alex Example" onclick="document.querySelector('.msg-overlay-conversation-bubble').style.display='none'">Close</button></header>
<div class="msg-s-message-list"><ul class="msg-s-message-list-content" id="messages">EVENTS</ul></div>
<div class="msg-form__contenteditable" role="textbox" contenteditable="true"></div>
<button onclick="const ed=document.querySelector('[contenteditable]'); const li=document.createElement('li'); li.className='msg-s-event-listitem'; const p=document.createElement('p'); p.className='msg-s-event-listitem__body'; p.textContent=ed.innerText; li.append(p); document.querySelector('#messages').append(li); ed.innerText=''; window.sends=(window.sends||0)+1;">Send</button>
</section>
</body></html>"""

INVITE_FRAME = """<!doctype html><html><body><dialog open>
<h2>Personalize your invitation to Alex Example</h2>
<button onclick="document.querySelector('textarea').hidden=false">Add a note</button><textarea hidden></textarea>
<button onclick="parent.invitationText=document.querySelector('textarea').value;parent.document.querySelector('#connect').outerHTML='<button>Pending</button>';parent.sends=(parent.sends||0)+1;parent.document.querySelector('iframe').remove()">Send</button>
</dialog></body></html>"""


def event(text, sender="me"):
    return f'<li class="msg-s-event-listitem"><a href="/in/{sender}/">Sender</a><p class="msg-s-event-listitem__body">{text}</p></li>'


class PersistentBrowserChecks(unittest.TestCase):
    def test_real_persistent_headless_launch_can_send_only_to_fixture(self):
        browser = "chromium"
        if os.environ.get("OUTREACH_TEST_BROWSER") != "chromium":
            try:
                brave_executable()
                browser = "brave"
            except StopRun:
                pass
        with tempfile.TemporaryDirectory() as directory:
            with LinkedInBrowser(Path(directory) / "profile", browser, headless=True) as adapter:
                adapter.page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=HTML.replace("DEGREE", "2nd").replace("EVENTS", "")))
                self.assertEqual(adapter.open_profile(LEAD), "new")
                adapter.send_invitation(LEAD, NOTE)
                self.assertEqual(adapter.page.evaluate("window.sends"), 1)
                self.assertEqual(adapter.profile_state(), "pending")


class BrowserChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        options = {"headless": True, "chromium_sandbox": True}
        if os.environ.get("OUTREACH_TEST_BROWSER", "") != "chromium":
            try:
                options["executable_path"] = brave_executable()
            except StopRun:
                pass
        cls.browser = cls.pw.chromium.launch(**options)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.page.set_default_timeout(1500)
        self.page.set_default_navigation_timeout(15000)
        self.adapter = LinkedInBrowser(Path("unused"))
        self.adapter.page = self.page
        self.adapter.context = self.context
        self.requests = []

    def tearDown(self):
        self.context.close()

    def fixture(self, degree="2nd", events="", transform=lambda html: html, frame_html=INVITE_FRAME):
        html = transform(HTML.replace("DEGREE", degree).replace("EVENTS", events))
        def route(request):
            self.requests.append(request.request.url)
            body = frame_html if "/preload/" in request.request.url else html
            request.fulfill(status=200, content_type="text/html", body=body)
        self.page.route("https://www.linkedin.com/**", route)
        self.page.goto(URL, wait_until="domcontentloaded")

    def test_invitation_is_personalized_and_confirmed_pending(self):
        self.fixture()
        self.assertEqual(self.adapter.open_profile(LEAD), "new")
        self.adapter.send_invitation(LEAD, NOTE)
        self.assertEqual(self.page.evaluate("window.invitationText"), NOTE)
        self.assertEqual(self.page.evaluate("window.sends"), 1)
        self.assertEqual(self.adapter.profile_state(), "pending")

    def test_more_menu_retries_only_read_only_open_when_first_click_is_not_ready(self):
        def hydration(html):
            html = re.sub(r'<button id="connect"[^>]*>Connect</button>', '', html)
            more = '''<button aria-label="More" onclick="window.moreClicks=(window.moreClicks||0)+1;if(window.moreClicks>1)document.querySelector('#profile-menu').hidden=false">More</button>'''
            menu = '<div id="profile-menu" role="menu" hidden><button>Connect</button></div>'
            return html.replace('</section></main>', more + '</section></main>' + menu)
        self.fixture(transform=hydration)
        self.assertEqual(self.adapter.open_profile(LEAD), "new")
        self.assertEqual(self.page.evaluate('window.moreClicks'), 2)
        self.assertIsNone(self.page.evaluate('window.sends'))

    def test_delayed_note_editor_is_waited_for_before_sending(self):
        self.fixture(transform=lambda html: html.replace("onclick=\"document.querySelector('textarea').hidden=false\"", "onclick=\"setTimeout(() => document.querySelector('textarea').hidden=false, 300)\""))
        self.assertEqual(self.adapter.open_profile(LEAD), "new")
        self.adapter.send_invitation(LEAD, NOTE)
        self.assertEqual(self.page.evaluate("window.invitationText"), NOTE)
        self.assertEqual(self.page.evaluate("window.sends"), 1)

    def test_pending_link_with_long_accessible_label_confirms_success(self):
        def pending_link(html):
            return html.replace("document.querySelector('#connect').outerHTML='<button>Pending</button>'",
                                "const p=document.createElement('a');p.href='/in/alex/';p.setAttribute('aria-label','Pending, click to withdraw invitation sent to Alex Example');p.textContent='Pending';document.querySelector('#connect').replaceWith(p)")
        self.fixture(transform=pending_link)
        self.assertEqual(self.adapter.open_profile(LEAD), "new")
        self.adapter.send_invitation(LEAD, NOTE)
        self.assertEqual(self.adapter.profile_state(), "pending")
        self.assertEqual(self.page.evaluate("window.sends"), 1)

    def test_remove_connection_menu_identifies_existing_connection(self):
        def existing_connection(html):
            html = re.sub(r'<button id="connect"[^>]*>Connect</button>',
                          '<button aria-label="More" onclick="setTimeout(() => document.querySelector(\'[role=menu]\').style.display=\'block\',200)">More</button>', html)
            return html.replace('</body>', '<div role="menu" style="display:none"><div role="menuitem">Remove connection</div></div></body>')
        self.fixture(transform=existing_connection)
        self.assertEqual(self.adapter.open_profile(LEAD), "connected")
        self.assertIsNone(self.page.evaluate("window.sends"))

    def test_changed_profile_name_prevents_connection(self):
        self.fixture(transform=lambda h: h.replace("<h1>Alex Example</h1>", "<h1>Someone Else</h1>"))
        with self.assertRaises(ProfileNeedsReview):
            self.adapter.open_profile(LEAD)
        self.assertIsNone(self.page.evaluate("window.sends"))

    def test_current_h2_profile_layout_is_identified(self):
        self.fixture(transform=lambda h: h.replace("<h1>Alex Example</h1>", "<h2>Alex Example</h2>"))
        self.assertEqual(self.adapter.open_profile(LEAD), "new")
        self.assertIsNone(self.page.evaluate("window.sends"))

    def test_current_layout_connect_link_sends_personalized_invitation(self):
        def modern(html):
            html = html.replace("<h1>Alex Example</h1>", "<h2>Alex Example</h2>")
            html = html.replace("<main><section>", "<main><div>").replace("</section></main>", '<button aria-label="More">More</button></div></main>')
            return re.sub(r'<button id="connect"[^>]*>Connect</button>',
                          '<a id="connect" href="#invite" aria-label="Connect with Alex Example" onclick="event.preventDefault();document.querySelector(\'dialog\').showModal()">Connect</a>', html)
        self.fixture(transform=modern)
        self.assertEqual(self.adapter.open_profile(LEAD), "new")
        self.adapter.send_invitation(LEAD, NOTE)
        self.assertEqual(self.page.evaluate("window.invitationText"), NOTE)
        self.assertEqual(self.page.evaluate("window.sends"), 1)

    def test_invitation_in_profile_bound_frame_sends_note_once(self):
        def framed(html):
            return re.sub(r'<button id="connect"[^>]*>Connect</button>',
                          '<a id="connect" href="#invite" aria-label="Connect with Alex Example" onclick="event.preventDefault();const f=document.createElement(\'iframe\');f.src=\'/preload/custom-invite/?vanityName=alex\';document.body.append(f)">Connect</a>', html)
        self.fixture(transform=framed)
        self.assertEqual(self.adapter.open_profile(LEAD), "new")
        self.adapter.send_invitation(LEAD, NOTE)
        self.assertEqual(self.page.evaluate("window.invitationText"), NOTE)
        self.assertEqual(self.page.evaluate("window.sends"), 1)

    def test_generic_preload_frame_must_name_selected_recipient(self):
        def framed(html):
            return re.sub(r'<button id="connect"[^>]*>Connect</button>',
                          '<a id="connect" href="#invite" aria-label="Connect with Alex Example" onclick="event.preventDefault();const f=document.createElement(\'iframe\');f.src=\'/preload/\';document.body.append(f)">Connect</a>', html)
        self.fixture(transform=framed)
        self.assertEqual(self.adapter.open_profile(LEAD), "new")
        self.adapter.send_invitation(LEAD, NOTE)
        self.assertEqual(self.page.evaluate("window.invitationText"), NOTE)
        self.assertEqual(self.page.evaluate("window.sends"), 1)

    def test_generic_preload_frame_with_wrong_name_never_sends(self):
        def framed(html):
            return re.sub(r'<button id="connect"[^>]*>Connect</button>',
                          '<a id="connect" href="#invite" aria-label="Connect with Alex Example" onclick="event.preventDefault();const f=document.createElement(\'iframe\');f.src=\'/preload/\';document.body.append(f)">Connect</a>', html)
        self.fixture(transform=framed, frame_html=INVITE_FRAME.replace("Alex Example", "Different Person"))
        self.assertEqual(self.adapter.open_profile(LEAD), "new")
        with self.assertRaises(StopRun):
            self.adapter.send_invitation(LEAD, NOTE)
        self.assertIsNone(self.page.evaluate("window.sends"))

    def test_wrong_conversation_recipient_prevents_message(self):
        self.fixture("1st", event(NOTE), lambda h: h.replace('class="msg-overlay-bubble-header__title">Alex Example', 'class="msg-overlay-bubble-header__title">Different Person'))
        with self.assertRaises(StopRun):
            self.adapter.send_message(LEAD, MESSAGE, {"invite": NOTE})
        self.assertIsNone(self.page.evaluate("window.sends"))

    def test_current_conversation_header_verifies_recipient_and_confirms_message(self):
        self.fixture("1st", event(NOTE), lambda h: h.replace('<header class="msg-overlay-bubble-header">',
                                                            '<header class="msg-overlay-conversation-bubble-header">'))
        self.adapter.send_message(LEAD, MESSAGE, {"invite": NOTE})
        self.assertEqual(self.page.evaluate("window.sends"), 1)
        self.assertEqual(self.page.locator(".msg-s-event-listitem__body").last.inner_text(), MESSAGE)

    def test_current_conversation_header_with_wrong_recipient_never_sends(self):
        def changed_header(html):
            return html.replace('<header class="msg-overlay-bubble-header">',
                                '<header class="msg-overlay-conversation-bubble-header">').replace(
                                    'class="msg-overlay-bubble-header__title">Alex Example',
                                    'class="msg-overlay-bubble-header__title">Different Person')
        self.fixture("1st", event(NOTE), changed_header)
        with self.assertRaises(StopRun):
            self.adapter.send_message(LEAD, MESSAGE, {"invite": NOTE})
        self.assertIsNone(self.page.evaluate("window.sends"))

    def test_current_conversation_header_still_stops_after_reply(self):
        self.fixture("1st", event(NOTE) + event("Thanks for connecting", "alex"),
                     lambda h: h.replace('<header class="msg-overlay-bubble-header">',
                                         '<header class="msg-overlay-conversation-bubble-header">'))
        with self.assertRaises(StopRun):
            self.adapter.send_message(LEAD, MESSAGE, {"invite": NOTE})
        self.assertIsNone(self.page.evaluate("window.sends"))

    def test_restored_thread_is_closed_before_opening_selected_recipient(self):
        def restored_thread(html):
            html = html.replace('class="msg-overlay-conversation-bubble" style="display:none"',
                                'class="msg-overlay-conversation-bubble" style="display:block"')
            html = html.replace('class="msg-overlay-bubble-header__title">Alex Example',
                                'class="msg-overlay-bubble-header__title">Previous Contact')
            return html.replace("document.querySelector('.msg-overlay-conversation-bubble').style.display='block'",
                                "document.querySelector('.msg-overlay-bubble-header__title').textContent='Alex Example';document.querySelector('.msg-overlay-conversation-bubble').style.display='block';window.messageOpens=(window.messageOpens||0)+1")
        self.fixture("1st", event(NOTE), restored_thread)
        self.adapter.send_message(LEAD, MESSAGE, {"invite": NOTE})
        self.assertEqual(self.page.evaluate("window.messageOpens"), 1)
        self.assertEqual(self.page.evaluate("window.sends"), 1)
        self.assertEqual(self.page.locator('.msg-overlay-bubble-header__title').inner_text(), LEAD['full_name'])

    def test_message_opener_retries_when_first_click_is_not_ready(self):
        self.fixture("1st", event(NOTE), lambda html: html.replace(
            "document.querySelector('.msg-overlay-conversation-bubble').style.display='block'",
            "window.messageOpens=(window.messageOpens||0)+1;if(window.messageOpens>1)document.querySelector('.msg-overlay-conversation-bubble').style.display='block'"))
        self.adapter.send_message(LEAD, MESSAGE, {"invite": NOTE})
        self.assertEqual(self.page.evaluate("window.messageOpens"), 2)
        self.assertEqual(self.page.evaluate("window.sends"), 1)

    def test_reply_in_thread_prevents_followup(self):
        self.fixture("1st", event(NOTE) + event("Thanks for connecting", "alex"))
        self.assertTrue(self.adapter.conversation(LEAD, {"invite": NOTE}).inbound)
        with self.assertRaises(StopRun):
            self.adapter.send_message(LEAD, MESSAGE, {"invite": NOTE})
        self.assertIsNone(self.page.evaluate("window.sends"))

    def test_recorded_note_allows_message_and_confirms_it_in_history(self):
        self.fixture("1st", event(NOTE))
        self.adapter.send_message(LEAD, MESSAGE, {"invite": NOTE})
        self.assertEqual(self.page.evaluate("window.sends"), 1)
        self.assertEqual(self.page.locator(".msg-s-event-listitem__body").last.inner_text(), MESSAGE)

    def test_message_in_embedded_conversation_verifies_recipient_and_sends_once(self):
        frame_html = HTML.replace("DEGREE", "1st").replace("EVENTS", event(NOTE)).replace('style="display:none"', 'style="display:block"')
        def framed(html):
            return html.replace("document.querySelector('.msg-overlay-conversation-bubble').style.display='block'",
                                "const f=document.createElement('iframe');f.src='/preload/?conversation=alex';document.body.append(f)")
        self.fixture("1st", transform=framed, frame_html=frame_html)
        self.adapter.send_message(LEAD, MESSAGE, {"invite": NOTE})
        self.assertEqual(self.adapter.message_frame.evaluate("window.sends"), 1)
        self.assertEqual(self.adapter.message_frame.locator(".msg-s-event-listitem__body").last.inner_text(), MESSAGE)

    def test_changed_history_markup_is_not_treated_as_empty(self):
        self.fixture("1st", '<li class="new-message-layout">An existing reply</li>')
        history = self.adapter.conversation(LEAD, {"invite": NOTE})
        self.assertTrue(history.unknown)

    def test_attachment_without_readable_text_blocks_sending(self):
        self.fixture("1st", '<li class="msg-s-event-listitem"><img alt="Attachment"></li>')
        history = self.adapter.conversation(LEAD, {"invite": NOTE})
        self.assertTrue(history.unknown)


if __name__ == "__main__":
    unittest.main()
