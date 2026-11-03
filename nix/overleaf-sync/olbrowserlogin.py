"""Ol Browser Login Utility - CLI fallback (no QtWebEngine)"""

import getpass
import webbrowser
import requests
from bs4 import BeautifulSoup

LOGIN_URL = "https://www.overleaf.com/login"
PROJECT_URL = "https://www.overleaf.com/project"
COOKIE_NAMES = ["overleaf_session2", "GCLB"]

_credentials = {}  # set by prompt_credentials(), consumed by login()


def prompt_credentials():
    """Collect login info before the yaspin spinner starts."""
    print("Overleaf login")
    print("  [1] Email + password")
    print("  [2] Google / SSO (paste cookies from browser)")
    choice = input("Choice [1/2]: ").strip()
    _credentials["method"] = "oauth" if choice == "2" else "password"

    if _credentials["method"] == "password":
        _credentials["email"] = input("Email: ")
        _credentials["password"] = getpass.getpass("Password: ")
    else:
        print()
        webbrowser.open(PROJECT_URL)
        print("Opening https://www.overleaf.com/project in your browser.")
        print("Log in there (via Google or SSO), then open DevTools (F12).")
        print()
        print("Go to: Application → Cookies → https://www.overleaf.com")
        print("Copy the value of overleaf_session2 (starts with s:...).")
        print("Also copy GCLB if present (press Enter to skip).")
        print()
        _credentials["session2"] = input("overleaf_session2: ").strip()
        _credentials["gclb"] = input("GCLB (Enter to skip): ").strip()


BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _session():
    s = requests.Session()
    s.headers.update({"User-Agent": BROWSER_UA})
    return s


def login():
    if _credentials.get("method") == "oauth":
        session2 = _credentials["session2"]
        gclb = _credentials.get("gclb", "")

        # Send cookies as a raw header string to avoid Python cookie encoding
        # issues with the "s:" prefix in Overleaf session values.
        cookie_str = f"overleaf_session2={session2}"
        if gclb:
            cookie_str += f"; GCLB={gclb}"

        s = _session()
        test = s.get(PROJECT_URL, headers={"Cookie": cookie_str})
        csrf_tag = BeautifulSoup(test.content, "html.parser").find(
            "meta", {"name": "ol-csrfToken"}
        )
        if csrf_tag is None:
            print(f"\nCookie verification failed (ended up at: {test.url})")
            print("The session may be expired or IP-restricted.")
            return None

        cookies = {"overleaf_session2": session2}
        if gclb:
            cookies["GCLB"] = gclb
        return {"cookie": cookies, "csrf": csrf_tag.get("content")}

    # Email + password flow
    session = _session()
    resp = session.get(LOGIN_URL)
    resp.raise_for_status()
    csrf_tag = BeautifulSoup(resp.content, "html.parser").find("input", {"name": "_csrf"})
    if csrf_tag is None:
        print("Could not find CSRF token on login page.")
        return None

    login_resp = session.post(
        LOGIN_URL,
        json={
            "_csrf": csrf_tag.get("value"),
            "email": _credentials.get("email", ""),
            "password": _credentials.get("password", ""),
        },
    )
    if login_resp.status_code != 200:
        print(f"Login request failed with status {login_resp.status_code}.")
        return None

    projects_resp = session.get(PROJECT_URL)
    new_csrf_tag = BeautifulSoup(projects_resp.content, "html.parser").find(
        "meta", {"name": "ol-csrfToken"}
    )
    if new_csrf_tag is None:
        print("Login failed: wrong credentials or captcha triggered.")
        return None

    cookies = {name: session.cookies[name] for name in COOKIE_NAMES if name in session.cookies}
    if "overleaf_session2" not in cookies:
        print("Login failed: session cookie not received.")
        return None

    return {"cookie": cookies, "csrf": new_csrf_tag.get("content")}
