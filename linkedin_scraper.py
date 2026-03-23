#!/usr/bin/env python3
"""
LinkedIn Profile Scraper (Chrome Extension + CDP)

Uses a Chrome extension and Chrome DevTools Protocol to visit a LinkedIn
profile, extract data from the DOM via a content script, take a screenshot,
and use Claude's vision to enrich the extracted profile information.

No Playwright dependency — only Chrome (or Chromium) with remote debugging.

Supports persistent cookies via Chrome's own user-data-dir so you only log in once.

Usage:
    python3 linkedin_scraper.py <linkedin_profile_url>
    python3 linkedin_scraper.py --login   # Log in to LinkedIn first (interactive)
"""

import argparse
import base64
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import anthropic
import requests
import websocket

SCRIPT_DIR = Path(__file__).parent.resolve()
EXTENSION_DIR = SCRIPT_DIR / "chrome_extension"
USER_DATA_DIR = SCRIPT_DIR / ".chrome_user_data"
OUTPUT_DIR = SCRIPT_DIR / "output"

CDP_PORT = 9222
CDP_BASE = f"http://127.0.0.1:{CDP_PORT}"


# ---------------------------------------------------------------------------
#  Chrome launcher
# ---------------------------------------------------------------------------

def find_chrome():
    """Find the Chrome / Chromium binary."""
    candidates = [
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for name in candidates:
        path = shutil.which(name)
        if path:
            return path
    # Check common Linux paths directly
    for name in candidates:
        if os.path.isfile(name) and os.access(name, os.X_OK):
            return name
    return None


def launch_chrome(headless=True):
    """Launch Chrome with remote debugging and the scraper extension loaded."""
    chrome_bin = find_chrome()
    if not chrome_bin:
        print("Error: Could not find Chrome or Chromium on your system.")
        print("Install Chrome and make sure it's in your PATH.")
        sys.exit(1)

    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    args = [
        chrome_bin,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={USER_DATA_DIR}",
        f"--load-extension={EXTENSION_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-default-apps",
        "--disable-popup-blocking",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--remote-allow-origins=*",
    ]

    if headless:
        args.append("--headless=new")

    # Start Chrome as a subprocess
    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for CDP to be available
    for attempt in range(30):
        try:
            resp = requests.get(f"{CDP_BASE}/json/version", timeout=2)
            if resp.status_code == 200:
                print(f"Chrome launched (PID {proc.pid}), CDP available on port {CDP_PORT}")
                return proc
        except requests.ConnectionError:
            pass
        time.sleep(1)

    proc.terminate()
    print("Error: Chrome did not start in time. Check if port 9222 is already in use.")
    sys.exit(1)


def kill_chrome(proc):
    """Gracefully shut down Chrome."""
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ---------------------------------------------------------------------------
#  CDP helpers
# ---------------------------------------------------------------------------

def cdp_get_targets():
    """Get the list of debuggable targets from CDP."""
    resp = requests.get(f"{CDP_BASE}/json", timeout=5)
    return resp.json()


def cdp_new_tab(url="about:blank"):
    """Open a new tab via CDP."""
    resp = requests.get(f"{CDP_BASE}/json/new?{url}", timeout=10)
    return resp.json()


def cdp_connect(ws_url):
    """Connect to a target via WebSocket and return a helper."""
    ws = websocket.create_connection(ws_url, timeout=30)
    return CDPSession(ws)


class CDPSession:
    """Minimal CDP session over WebSocket."""

    def __init__(self, ws):
        self.ws = ws
        self._id = 0

    def send(self, method, params=None):
        self._id += 1
        msg = {"id": self._id, "method": method, "params": params or {}}
        self.ws.send(json.dumps(msg))
        # Read responses until we get our reply
        while True:
            raw = self.ws.recv()
            data = json.loads(raw)
            if data.get("id") == self._id:
                if "error" in data:
                    raise RuntimeError(f"CDP error: {data['error']}")
                return data.get("result", {})
            # Otherwise it's an event — ignore it

    def close(self):
        self.ws.close()


# ---------------------------------------------------------------------------
#  Navigation & extraction
# ---------------------------------------------------------------------------

def navigate_and_wait(session, url, wait_seconds=5):
    """Navigate to a URL and wait for the page to load."""
    session.send("Page.enable")
    session.send("Page.navigate", {"url": url})
    print(f"Navigating to {url}")

    # Wait for load event
    deadline = time.time() + 30
    while time.time() < deadline:
        raw = session.ws.recv()
        event = json.loads(raw)
        if event.get("method") == "Page.loadEventFired":
            break

    print(f"Waiting {wait_seconds}s for dynamic content...")
    time.sleep(wait_seconds)


def capture_screenshot(session):
    """Capture a screenshot via CDP and return raw PNG bytes."""
    result = session.send("Page.captureScreenshot", {"format": "png"})
    return base64.b64decode(result["data"])


def check_login_wall(session):
    """Check if the current page is a LinkedIn login/sign-in wall.

    Returns a string describing the issue, or None if the page looks fine.
    """
    js_code = """
    (function() {
        var url = window.location.href;

        // URL-based detection
        if (url.includes('/login') || url.includes('/signin') ||
            url.includes('/authwall') || url.includes('/checkpoint')) {
            return 'redirect:' + url;
        }

        // DOM-based detection: look for the sign-in form
        if (document.querySelector('form.login__form') ||
            document.querySelector('#username[name="session_key"]') ||
            document.querySelector('input[name="session_key"]')) {
            return 'login-form';
        }

        // Auth-wall overlay
        if (document.querySelector('.authwall-join-form') ||
            document.querySelector('[data-tracking-control-name="auth_wall"]')) {
            return 'auth-wall';
        }

        return '';
    })()
    """
    result = session.send("Runtime.evaluate", {
        "expression": js_code,
        "returnByValue": True,
    })
    value = result.get("result", {}).get("value", "")
    return value if value else None


def scroll_to_experience(session):
    """Scroll the page so the Experience section is visible."""
    js_code = """
    (function() {
        var section = document.getElementById('experience');
        if (!section) {
            // Fallback: look for a section heading containing 'Experience'
            var headings = document.querySelectorAll('section h2, div#experience');
            for (var i = 0; i < headings.length; i++) {
                if (headings[i].textContent.trim().toLowerCase().includes('experience')) {
                    section = headings[i].closest('section') || headings[i];
                    break;
                }
            }
        }
        if (section) {
            section.scrollIntoView({block: 'start'});
            return true;
        }
        // Last resort: scroll down a fixed amount
        window.scrollBy(0, 800);
        return false;
    })()
    """
    result = session.send("Runtime.evaluate", {
        "expression": js_code,
        "returnByValue": True,
    })
    found = result.get("result", {}).get("value", False)
    if found:
        print("Scrolled to Experience section")
    else:
        print("Experience section not found, scrolled down as fallback")
    time.sleep(2)


def extract_experience_entries(session):
    """Grab every <a> tag on the entire page that points to /company/.

    No section scoping, no li walking, no fancy strategies.
    Just find every company link on the page, grab href + visible text.
    Also dump all hrefs from the experience section for debugging.
    """
    js_code = """
    (function() {
        var debug = {};

        // Grab EVERY link on the page that goes to a company profile
        var allAnchors = document.querySelectorAll('a');
        var companyLinks = [];
        var seen = {};
        var allExpHrefs = [];

        for (var i = 0; i < allAnchors.length; i++) {
            var a = allAnchors[i];
            var href = a.href || '';

            // Collect hrefs near experience section for debugging
            var nearExp = false;
            var p = a.parentElement;
            for (var k = 0; k < 10 && p; k++) {
                if (p.id === 'experience' || (p.tagName === 'SECTION' &&
                    p.textContent.substring(0, 200).toLowerCase().includes('experience'))) {
                    nearExp = true;
                    break;
                }
                p = p.parentElement;
            }
            if (nearExp) {
                allExpHrefs.push(href.substring(0, 150));
            }

            // Match company URLs
            if (href.indexOf('/company/') !== -1) {
                var clean = href.split('?')[0].replace(/\\/+$/, '');
                if (!seen[clean]) {
                    seen[clean] = true;
                    var text = a.textContent.trim().replace(/\\s+/g, ' ').substring(0, 100);
                    var aria = a.getAttribute('aria-label') || '';
                    companyLinks.push({
                        company_linkedin_url: clean,
                        company_name: text || aria,
                    });
                }
            }
        }

        debug.totalAnchorsOnPage = allAnchors.length;
        debug.companyLinksFound = companyLinks.length;
        debug.experienceSectionHrefs = allExpHrefs.slice(0, 20);

        companyLinks.push({_debug: debug});
        return JSON.stringify(companyLinks);
    })()
    """
    result = session.send("Runtime.evaluate", {
        "expression": js_code,
        "returnByValue": True,
    })
    value = result.get("result", {}).get("value", "[]")
    try:
        items = json.loads(value)
        debug = None
        clean = []
        for item in items:
            if isinstance(item, dict) and "_debug" in item:
                debug = item["_debug"]
            else:
                clean.append(item)
        if debug:
            print(f"  Company link extraction debug:")
            print(f"    Total <a> tags on page: {debug.get('totalAnchorsOnPage')}")
            print(f"    Company links found: {debug.get('companyLinksFound')}")
            exp_hrefs = debug.get('experienceSectionHrefs', [])
            if exp_hrefs:
                print(f"    Sample hrefs near experience section:")
                for h in exp_hrefs[:10]:
                    print(f"      {h}")
            else:
                print(f"    No hrefs found near experience section")
        return clean
    except (json.JSONDecodeError, TypeError):
        return []


def extract_profile_via_extension(session):
    """
    Execute the content script's extraction function directly via CDP's
    Runtime.evaluate, which works regardless of extension message passing.
    """
    # Inject and call the extraction function from the content script
    js_code = """
    (function() {
        // Close popups first
        var selectors = [
            'button[aria-label="Dismiss"]',
            'button[aria-label="Close"]',
            'button.msg-overlay-bubble-header__control--close',
            'button[action-type="DENY"]',
            '.artdeco-modal__dismiss',
            '.artdeco-toast-item__dismiss',
            '#artdeco-global-alert-container button'
        ];
        selectors.forEach(function(sel) {
            try {
                document.querySelectorAll(sel).forEach(function(btn) {
                    if (btn.offsetParent !== null) btn.click();
                });
            } catch(e) {}
        });

        var data = {};

        // Name
        var nameEl = document.querySelector('h1.text-heading-xlarge') ||
                     document.querySelector('h1.inline.t-24') ||
                     document.querySelector('.pv-top-card--list h1') ||
                     document.querySelector('h1');
        if (nameEl) data.name = nameEl.innerText.trim();

        // Headline
        var headlineEl = document.querySelector('div.text-body-medium.break-words') ||
                         document.querySelector('.pv-top-card--list .text-body-medium');
        if (headlineEl) data.headline = headlineEl.innerText.trim();

        // Location
        var locationEl = document.querySelector('span.text-body-small.inline.t-black--light.break-words') ||
                         document.querySelector('.pv-top-card--list-bullet .text-body-small');
        if (locationEl) data.location = locationEl.innerText.trim();

        // About
        var aboutEl = document.querySelector('#about ~ div .inline-show-more-text') ||
                      document.querySelector('section.pv-about-section .pv-about__summary-text');
        if (aboutEl) data.about = aboutEl.innerText.trim();

        // Profile image
        var imgEl = document.querySelector('img.pv-top-card-profile-picture__image--show') ||
                    document.querySelector('img.pv-top-card-profile-picture__image');
        if (imgEl && imgEl.src && imgEl.src.startsWith('http')) {
            data.profile_image_url = imgEl.src;
        }

        // Company page
        var compNameEl = document.querySelector('h1.org-top-card-summary__title span') ||
                         document.querySelector('h1.org-top-card-summary__title');
        if (compNameEl && !data.name) {
            data.name = compNameEl.innerText.trim();
            data.type = 'company';
        }

        var compLogo = document.querySelector('img.org-top-card-primary-content__logo');
        if (compLogo && compLogo.src && compLogo.src.startsWith('http')) {
            data.logo_url = compLogo.src;
        }

        data.profile_url = window.location.href;
        return JSON.stringify(data);
    })()
    """

    result = session.send("Runtime.evaluate", {
        "expression": js_code,
        "returnByValue": True,
    })
    value = result.get("result", {}).get("value", "{}")
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}


# ---------------------------------------------------------------------------
#  Claude analysis
# ---------------------------------------------------------------------------

def analyze_with_claude(screenshot_path):
    """Use Claude's vision to extract profile info from the screenshot."""
    client = anthropic.Anthropic()

    image_data = base64.standard_b64encode(screenshot_path.read_bytes()).decode("utf-8")

    print("Analyzing screenshot with Claude...")
    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2048,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": image_data,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "Analyze this LinkedIn profile/company page screenshot. "
                            "Extract ALL useful information you can see and return it as JSON. "
                            "Include these fields where available:\n"
                            "- name: person or company name\n"
                            "- headline: the headline/tagline\n"
                            "- company: current company name (for person profiles)\n"
                            "- title: current job title (for person profiles)\n"
                            "- location: geographic location\n"
                            "- industry: industry or sector\n"
                            "- about: summary/about text if visible\n"
                            "- connections: connection/follower count\n"
                            "- experience: list of visible experience entries\n"
                            "- education: list of visible education entries\n"
                            "- website: any website URLs shown\n"
                            "- profile_url: the LinkedIn URL if visible\n"
                            "- recent_activity: list of recent activity entries, each with "
                            "only 'type' (e.g. 'repost', 'post', 'like', 'comment') and "
                            "'when' set to the EXACT time label shown on screen (e.g. '1w', "
                            "'3d', '2mo', '5h', '1yr'). Copy the label exactly as displayed. "
                            "Do NOT paraphrase, summarize, or write 'recently' — use the literal text.\n"
                            "- any other useful fields you can identify\n\n"
                            "IGNORE: Do NOT include suggested/recommended profiles, "
                            "'People also viewed', 'People you may know' sections, "
                            "messaging availability status, posts_count, or counts of "
                            "comments/videos/images available.\n\n"
                            "Return ONLY valid JSON, no markdown fences or extra text."
                        ),
                    },
                ],
            }
        ],
    )

    response_text = message.content[0].text.strip()

    # Strip markdown fences if present
    if response_text.startswith("```"):
        response_text = re.sub(r'^```(?:json)?\s*', '', response_text)
        response_text = re.sub(r'\s*```$', '', response_text)

    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        print("Warning: Claude's response was not valid JSON. Saving raw text.")
        return {"raw_response": response_text}


def analyze_experience_with_claude(screenshot_path, company_links):
    """Use Claude's vision to extract experience details from the screenshot."""
    client = anthropic.Anthropic()

    image_data = base64.standard_b64encode(screenshot_path.read_bytes()).decode("utf-8")

    # Build a hint about company URLs so Claude can match them
    links_hint = ""
    if company_links:
        links_hint = (
            "\n\nThe following company LinkedIn URLs were extracted from the page. "
            "Match each to the correct experience entry by company name:\n"
            + json.dumps(company_links, indent=2)
        )

    print("Analyzing experience screenshot with Claude...")
    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2048,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": image_data,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "Analyze this LinkedIn Experience section screenshot. "
                            "Extract each work experience entry and return as JSON with a single key "
                            "'experience' containing a list. Each entry should have:\n"
                            "- title: job title\n"
                            "- company: company name\n"
                            "- company_linkedin_url: the company's LinkedIn URL (use the provided links below to match). "
                            "OMIT this field entirely if no URL is available — do NOT set it to null or \"null\".\n"
                            "- dates: the date range shown (e.g. 'Nov 2023 - Present')\n"
                            "- duration: the duration shown (e.g. '2 yrs 5 mos')\n"
                            "- description: the text description of the role. "
                            "Ignore any embedded images, videos, or links within the description text.\n\n"
                            "IGNORE: sidebar suggestions, 'People also viewed', ads, "
                            "and any non-experience content.\n"
                            + links_hint
                            + "\n\nReturn ONLY valid JSON, no markdown fences or extra text."
                        ),
                    },
                ],
            }
        ],
    )

    response_text = message.content[0].text.strip()

    if response_text.startswith("```"):
        response_text = re.sub(r'^```(?:json)?\s*', '', response_text)
        response_text = re.sub(r'\s*```$', '', response_text)

    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        print("Warning: Claude's experience response was not valid JSON. Saving raw text.")
        return {"raw_experience_response": response_text}


# ---------------------------------------------------------------------------
#  Download logo
# ---------------------------------------------------------------------------

def download_logo(logo_url, slug):
    """Download the logo image to the output directory."""
    if not logo_url:
        return None
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        resp = requests.get(logo_url, headers=headers, timeout=15)
        if resp.status_code == 200 and len(resp.content) > 100:
            ct = resp.headers.get("content-type", "")
            ext = ".png"
            if "jpeg" in ct or "jpg" in ct:
                ext = ".jpg"
            elif "svg" in ct:
                ext = ".svg"
            elif "gif" in ct:
                ext = ".gif"
            logo_path = OUTPUT_DIR / f"{slug}_logo{ext}"
            logo_path.write_bytes(resp.content)
            print(f"Logo saved to {logo_path}")
            return str(logo_path)
    except Exception as e:
        print(f"Warning: Could not download logo: {e}")
    return None


# ---------------------------------------------------------------------------
#  Login flow
# ---------------------------------------------------------------------------

def do_login():
    """Open a visible Chrome window for manual LinkedIn login."""
    print("Opening Chrome for LinkedIn login...")
    print("Log in manually, then press Enter in this terminal.")

    proc = launch_chrome(headless=False)

    try:
        # Open LinkedIn login in a new tab
        targets = cdp_get_targets()
        page_targets = [t for t in targets if t.get("type") == "page"]
        if not page_targets:
            # Create a new tab
            cdp_new_tab("https://www.linkedin.com/login")
        else:
            ws_url = page_targets[0]["webSocketDebuggerUrl"]
            session = cdp_connect(ws_url)
            session.send("Page.enable")
            session.send("Page.navigate", {"url": "https://www.linkedin.com/login"})
            session.close()

        try:
            input("\nPress Enter after logging in (or Ctrl+C to cancel)...")
        except (EOFError, KeyboardInterrupt):
            print("\nWaiting 60 seconds for login...")
            time.sleep(60)

    finally:
        kill_chrome(proc)

    print("Login complete! Cookies are saved in the Chrome user-data-dir.")
    print("You can now scrape profiles.")


# ---------------------------------------------------------------------------
#  Experience merge helper
# ---------------------------------------------------------------------------

def _is_valid_url(value):
    """Check if a company_linkedin_url value is a real URL, not null/N/A/empty."""
    if not value or not isinstance(value, str):
        return False
    v = value.strip().lower()
    if v in ("null", "none", "n/a", "unknown", ""):
        return False
    return v.startswith("http")


def _merge_urls_from_dom(experience_entries, company_links):
    """Inject company_linkedin_url from page-wide company links into experience entries.

    company_links is a flat list of {company_linkedin_url, company_name} dicts
    scraped from every <a> on the page pointing to /company/.
    """
    if not company_links:
        return

    # First: strip any garbage Claude wrote (like the string "null")
    for entry in experience_entries:
        if not _is_valid_url(entry.get("company_linkedin_url")):
            entry.pop("company_linkedin_url", None)

    # Build lookups from the company links
    name_to_url = {}
    slug_to_url = {}
    for cl in company_links:
        url = cl.get("company_linkedin_url", "")
        if not _is_valid_url(url):
            continue
        # Name lookup
        name = cl.get("company_name", "").strip().lower()
        if name:
            name_to_url[name] = url
        # Slug lookup: /company/google/ -> "google"
        slug = url.rstrip("/").split("/")[-1].lower()
        if slug:
            slug_to_url[slug] = url

    for entry in experience_entries:
        if _is_valid_url(entry.get("company_linkedin_url")):
            continue

        comp = entry.get("company", "").strip().lower()
        if not comp:
            continue

        # 1. Exact name match
        if comp in name_to_url:
            entry["company_linkedin_url"] = name_to_url[comp]
            continue

        # 2. Substring match (either direction) on link text
        matched = False
        for dname, durl in name_to_url.items():
            if dname in comp or comp in dname:
                entry["company_linkedin_url"] = durl
                matched = True
                break
        if matched:
            continue

        # 3. Slug match — compare company name against URL slugs
        #    e.g. "Meta Platforms" should match slug "meta-platforms"
        comp_clean = comp.replace(",", "").replace(".", "").replace("&", "and")
        comp_words = comp_clean.split()
        comp_as_slug = "-".join(comp_words)  # "meta platforms" -> "meta-platforms"

        for slug, surl in slug_to_url.items():
            slug_parts = slug.split("-")
            # Check: full slug match ("meta-platforms" == "meta-platforms")
            if comp_as_slug == slug:
                entry["company_linkedin_url"] = surl
                matched = True
                break
            # Check: any company word is the slug or starts the slug
            for word in comp_words:
                if word == slug or slug.startswith(word + "-") or slug.startswith(word):
                    entry["company_linkedin_url"] = surl
                    matched = True
                    break
            if matched:
                break
            # Check: any slug part matches a company word
            for sp in slug_parts:
                if sp in comp_words:
                    entry["company_linkedin_url"] = surl
                    matched = True
                    break
            if matched:
                break


# ---------------------------------------------------------------------------
#  Main scrape workflow
# ---------------------------------------------------------------------------

def scrape_profile(url):
    """Main scraping workflow using Chrome + CDP."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    proc = launch_chrome(headless=False)

    try:
        # Find or create a page target
        targets = cdp_get_targets()
        page_targets = [t for t in targets if t.get("type") == "page"]

        if page_targets:
            ws_url = page_targets[0]["webSocketDebuggerUrl"]
        else:
            tab_info = cdp_new_tab()
            ws_url = tab_info["webSocketDebuggerUrl"]

        session = cdp_connect(ws_url)

        # Set a larger viewport so the screenshot captures more content
        session.send("Emulation.setDeviceMetricsOverride", {
            "width": 1920,
            "height": 1080,
            "deviceScaleFactor": 1,
            "mobile": False,
        })

        # Navigate to the profile
        navigate_and_wait(session, url, wait_seconds=2)

        # Check if we hit a login wall
        login_issue = check_login_wall(session)
        if login_issue:
            print(f"\nError: LinkedIn is requiring sign-in ({login_issue}).")
            print("You are not logged in or your session has expired.")
            print("Please run the login command first:\n")
            print("    python3 linkedin_scraper.py --login\n")
            session.close()
            return None

        # Extract profile data from the DOM via CDP Runtime.evaluate
        print("Extracting profile data from page DOM...")
        dom_data = extract_profile_via_extension(session)
        print(f"DOM extraction found {len(dom_data)} fields")

        # Validate we got meaningful data (not a blank or error page)
        has_name = bool(dom_data.get("name"))
        has_profile_url = bool(dom_data.get("profile_url", ""))
        is_linkedin_profile = "linkedin.com" in dom_data.get("profile_url", "")
        if not has_name and not (has_profile_url and is_linkedin_profile):
            print("\nError: Could not extract profile data from the page.")
            print("The page may not have loaded correctly, or the URL may be invalid.")
            print(f"  URL requested: {url}")
            print(f"  Page URL seen: {dom_data.get('profile_url', 'unknown')}")
            print("\nNo data was saved.")
            session.close()
            return None


        # Capture screenshot
        slug = re.sub(r'[^a-zA-Z0-9]', '_', url.split("linkedin.com/")[-1].strip("/"))
        if not slug:
            slug = "profile"

        screenshot_bytes = capture_screenshot(session)
        screenshot_path = OUTPUT_DIR / f"{slug}_screenshot.png"
        screenshot_path.write_bytes(screenshot_bytes)
        print(f"Screenshot saved to {screenshot_path}")

        # Scroll to experience section, grab all company links, take screenshot
        scroll_to_experience(session)
        company_links = extract_experience_entries(session)
        print(f"Found {len(company_links)} company links on page")

        exp_screenshot_bytes = capture_screenshot(session)
        exp_screenshot_path = OUTPUT_DIR / f"{slug}_experience_screenshot.png"
        exp_screenshot_path.write_bytes(exp_screenshot_bytes)
        print(f"Experience screenshot saved to {exp_screenshot_path}")

        session.close()

    finally:
        kill_chrome(proc)

    # Analyze screenshot with Claude
    claude_data = analyze_with_claude(screenshot_path)

    # Analyze experience screenshot with Claude
    experience_data = analyze_experience_with_claude(exp_screenshot_path, company_links)

    # Merge: DOM data takes precedence, Claude fills in gaps
    profile_data = {**claude_data, **{k: v for k, v in dom_data.items() if v}}

    # Add experience from Claude analysis
    if "experience" in experience_data:
        profile_data["experience"] = experience_data["experience"]

    # Inject company URLs into experience entries from the DOM-extracted links
    if company_links and "experience" in profile_data and profile_data["experience"]:
        _merge_urls_from_dom(profile_data["experience"], company_links)

    # Add metadata
    profile_data["_source_url"] = url
    profile_data["_scraped_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    profile_data["_screenshot"] = str(screenshot_path)
    profile_data["_experience_screenshot"] = str(exp_screenshot_path)

    # Download logo if available
    logo_url = dom_data.get("logo_url") or dom_data.get("profile_image_url") or claude_data.get("logo_url")
    logo_path = download_logo(logo_url, slug)
    if logo_path:
        profile_data["_logo_file"] = logo_path

    # Save JSON
    json_path = OUTPUT_DIR / f"{slug}.json"
    json_path.write_text(json.dumps(profile_data, indent=2, ensure_ascii=False))
    print(f"\nProfile data saved to {json_path}")

    # Print summary
    print("\n--- Extracted Info ---")
    for key, value in profile_data.items():
        if not key.startswith("_") and key != "raw_response":
            print(f"  {key}: {value}")

    return profile_data


# ---------------------------------------------------------------------------
#  CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="LinkedIn Profile Scraper (Chrome Extension + CDP)")
    parser.add_argument("url", nargs="?", help="LinkedIn profile/company URL to scrape")
    parser.add_argument("--login", action="store_true", help="Open browser for manual LinkedIn login")
    args = parser.parse_args()

    if args.login:
        do_login()
        return

    if not args.url:
        parser.print_help()
        print("\nExamples:")
        print("  python3 linkedin_scraper.py --login")
        print("  python3 linkedin_scraper.py https://www.linkedin.com/in/someone/")
        print("  python3 linkedin_scraper.py https://www.linkedin.com/company/some-company/")
        sys.exit(1)

    if "linkedin.com" not in args.url:
        print("Error: URL must be a LinkedIn URL")
        sys.exit(1)

    result = scrape_profile(args.url)
    if result is None:
        sys.exit(1)


if __name__ == "__main__":
    main()
