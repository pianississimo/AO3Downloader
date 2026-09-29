import os
import re
import sys
import time
import json
import subprocess
import traceback
from http.cookiejar import Cookie, CookieJar, MozillaCookieJar
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import requests
from bs4 import BeautifulSoup

# Interaction language chosen by the user; default is English until they pick.
CURRENT_LANG = "en"

def select_interaction_language():
    """Ask the user to choose English or Chinese for all subsequent prompts and logs."""
    global CURRENT_LANG
    print("Please choose the interaction language / 请选择交互语言:", flush=True)
    print("  1. English", flush=True)
    print("  2. 中文", flush=True)
    while True:
        choice = input("> ").strip()
        if choice == "1":
            CURRENT_LANG = "en"
            return
        if choice == "2":
            CURRENT_LANG = "zh"
            return
        print("Please enter 1 or 2. / 请输入 1 或 2。", flush=True)

def p(en_text, zh_text):
    """Print only the line that matches the language the user chose."""
    if CURRENT_LANG == "zh":
        print(zh_text, flush=True)
    else:
        print(en_text, flush=True)

def get_script_dir():
    """Return the real directory of this script or the packaged executable (.exe)."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    else:
        return os.path.dirname(os.path.abspath(__file__))

AO3_SESSION_COOKIE_NAMES = {
    "_otwarchive_session",
    "user_credentials",
    "remember_user_token",
}


def _cookiejar_has_ao3_session(cj):
    """True if the jar looks like a logged-in AO3 account, not just any AO3 cookie."""
    if not cj:
        return False
    for cookie in cj:
        host = (cookie.domain or "").lstrip(".").lower()
        if "archiveofourown.org" not in host:
            continue
        if cookie.name in AO3_SESSION_COOKIE_NAMES and cookie.value:
            return True
    return False


def _chromium_user_data_dirs():
    """Return (browser name, user-data directory) pairs that exist on this machine."""
    home = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA", "")
    if sys.platform == "win32":
        candidates = [
            ("Chrome", os.path.join(local, "Google", "Chrome", "User Data")),
            ("Edge", os.path.join(local, "Microsoft", "Edge", "User Data")),
            ("Brave", os.path.join(local, "BraveSoftware", "Brave-Browser", "User Data")),
        ]
    elif sys.platform == "darwin":
        candidates = [
            ("Chrome", os.path.join(home, "Library", "Application Support", "Google", "Chrome")),
            ("Edge", os.path.join(home, "Library", "Application Support", "Microsoft Edge")),
            ("Brave", os.path.join(home, "Library", "Application Support", "BraveSoftware", "Brave-Browser")),
        ]
    else:
        candidates = [
            ("Chrome", os.path.join(home, ".config", "google-chrome")),
            ("Chromium", os.path.join(home, ".config", "chromium")),
            ("Edge", os.path.join(home, ".config", "microsoft-edge")),
            ("Brave", os.path.join(home, ".config", "BraveSoftware", "Brave-Browser")),
        ]
    return [(name, path) for name, path in candidates if os.path.isdir(path)]


def _iter_chromium_cookie_files():
    """Yield (browser, profile, cookie_db_path) for Default and Profile N folders."""
    for browser, user_data in _chromium_user_data_dirs():
        try:
            entries = os.listdir(user_data)
        except OSError:
            continue
        for profile in entries:
            if profile != "Default" and not profile.startswith("Profile "):
                continue
            profile_dir = os.path.join(user_data, profile)
            for rel in (os.path.join("Network", "Cookies"), "Cookies"):
                cookie_path = os.path.join(profile_dir, rel)
                if os.path.isfile(cookie_path):
                    yield browser, profile, cookie_path
                    break


def _copy_file_allow_sharing(src, dest):
    """Copy a file even if Chrome/Edge has it open (Windows share-mode read)."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        GENERIC_READ = 0x80000000
        FILE_SHARE_ALL = 0x1 | 0x2 | 0x4
        OPEN_EXISTING = 3
        FILE_ATTRIBUTE_NORMAL = 0x80
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
        handle = ctypes.windll.kernel32.CreateFileW(
            str(src),
            GENERIC_READ,
            FILE_SHARE_ALL,
            None,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if handle in (None, 0, -1, INVALID_HANDLE_VALUE):
            raise PermissionError(src)
        try:
            buf = ctypes.create_string_buffer(1024 * 1024)
            read = wintypes.DWORD()
            chunks = []
            while True:
                ok = ctypes.windll.kernel32.ReadFile(handle, buf, len(buf), ctypes.byref(read), None)
                if not ok:
                    raise PermissionError(src)
                if read.value == 0:
                    break
                chunks.append(buf.raw[:read.value])
            with open(dest, "wb") as out:
                out.write(b"".join(chunks))
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
        return
    import shutil
    shutil.copy2(src, dest)


def _copy_cookie_db(src_path):
    """Copy Chrome's cookie SQLite file so we can read it while the browser is open."""
    import tempfile
    fd, dest = tempfile.mkstemp(suffix=".cookies")
    os.close(fd)
    _copy_file_allow_sharing(src_path, dest)
    for suffix in ("-wal", "-shm"):
        extra = src_path + suffix
        if os.path.isfile(extra):
            try:
                _copy_file_allow_sharing(extra, dest + suffix)
            except OSError:
                pass
    return dest


def _inspect_ao3_cookie_db(cookie_path):
    """Read AO3 cookie names from Chrome's SQLite DB without decrypting values."""
    import sqlite3
    tmp = _copy_cookie_db(cookie_path)
    found = []
    saw_v20 = False
    try:
        con = sqlite3.connect(tmp)
        try:
            rows = con.execute(
                "SELECT host_key, name, encrypted_value FROM cookies "
                "WHERE host_key LIKE ?",
                ("%archiveofourown.org%",),
            ).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            con.close()
        for host, name, enc in rows:
            prefix = ""
            if isinstance(enc, bytes) and len(enc) >= 3:
                prefix = enc[:3].decode("ascii", errors="ignore")
            if prefix == "v20":
                saw_v20 = True
            found.append((host or "", name or "", prefix))
    finally:
        for path in (tmp, tmp + "-wal", tmp + "-shm"):
            try:
                os.remove(path)
            except OSError:
                pass
    return found, saw_v20


def _load_chromium_cookie_file(browser_cookie3, browser, cookie_path):
    """Load one Chromium-family cookie database via a temp copy."""
    loaders = {
        "Chrome": browser_cookie3.chrome,
        "Chromium": browser_cookie3.chromium,
        "Edge": browser_cookie3.edge,
        "Brave": browser_cookie3.brave,
    }
    loader = loaders.get(browser, browser_cookie3.chrome)
    tmp = _copy_cookie_db(cookie_path)
    try:
        return loader(cookie_file=tmp, domain_name="archiveofourown.org")
    finally:
        for path in (tmp, tmp + "-wal", tmp + "-shm"):
            try:
                os.remove(path)
            except OSError:
                pass


def get_ao3_cookies():
    """Try each supported local browser until an AO3 login session is found."""
    p("Attempting to scan browser cookies... Please make sure your AO3 account is logged in on Chrome, Safari, or Firefox (Mozilla).",
      "正在扫描浏览器 Cookie... 请确保您的 AO3 账号已在 Chrome、Safari 或 Firefox (Mozilla) 浏览器中登录。")
    p("Loading cookies, please wait a moment...",
      "正在尝试加载 Cookie，请稍候...\n")
    
    # Import browser_cookie3 lazily so startup is not blocked by a slow import.
    try:
        import browser_cookie3
    except ImportError:
        p("  -> [Warning] 'browser_cookie3' module not installed. A login session is required.",
          "  -> [警告] 未安装 'browser_cookie3' 模块。必须先加载登录会话。")
        return None

    chromium_files = list(_iter_chromium_cookie_files())

    for browser, profile, cookie_path in chromium_files:
        label = f"{browser} / {profile}"
        p(f"  -> Trying [{label}]...",
          f"  -> 正在尝试 [{label}]...")
        try:
            found, saw_v20 = _inspect_ao3_cookie_db(cookie_path)
        except OSError as e:
            p(f"  -> [{label}] cookie file is locked (fully quit the browser): {e}",
              f"  -> [{label}] Cookie 文件被占用（请完全退出浏览器）: {e}")
            continue
        if found:
            names = ", ".join(sorted({name for _, name, _ in found}))
            p(f"  -> [{label}] cookie file contains AO3 cookies: {names}",
              f"  -> [{label}] Cookie 文件中有 AO3 Cookie: {names}")
        else:
            p(f"  -> [{label}] cookie file has no AO3 cookies.",
              f"  -> [{label}] Cookie 文件中没有 AO3 Cookie。")

        try:
            cj = _load_chromium_cookie_file(browser_cookie3, browser, cookie_path)
        except Exception as e:
            p(f"  -> [{label}] could not decrypt cookies: {e}",
              f"  -> [{label}] 无法解密 Cookie: {e}")
            continue

        if _cookiejar_has_ao3_session(cj):
            p(f"  -> Successfully loaded a logged-in AO3 session via [{label}]!",
              f"  -> 成功通过 [{label}] 加载已登录的 AO3 会话！")
            return cj

        cookie_count = len(cj) if cj else 0
        p(f"  -> [{label}] decrypted {cookie_count} usable AO3 cookie(s), but no login session.",
          f"  -> [{label}] 成功解密 {cookie_count} 个可用 AO3 Cookie，但没有登录会话。")

    other_browsers = [
        ("Firefox", lambda: browser_cookie3.firefox(domain_name="archiveofourown.org")),
        ("Safari", lambda: browser_cookie3.safari(domain_name="archiveofourown.org")),
    ]
    if not chromium_files:
        other_browsers = [
            ("Chrome", lambda: browser_cookie3.chrome(domain_name="archiveofourown.org")),
            ("Chromium", lambda: browser_cookie3.chromium(domain_name="archiveofourown.org")),
            ("Edge", lambda: browser_cookie3.edge(domain_name="archiveofourown.org")),
            ("Brave", lambda: browser_cookie3.brave(domain_name="archiveofourown.org")),
        ] + other_browsers

    for name, b_func in other_browsers:
        p(f"  -> Trying [{name}]...",
          f"  -> 正在尝试 [{name}]...")
        try:
            cj = b_func()
        except Exception as e:
            p(f"  -> [{name}] could not be read: {e}",
              f"  -> [{name}] 无法读取: {e}")
            continue

        if _cookiejar_has_ao3_session(cj):
            p(f"  -> Successfully loaded a logged-in AO3 session via [{name}]!",
              f"  -> 成功通过 [{name}] 加载已登录的 AO3 会话！")
            return cj

        cookie_count = len(cj) if cj else 0
        p(f"  -> [{name}] had {cookie_count} AO3 cookie(s), but no login session.",
          f"  -> [{name}] 找到 {cookie_count} 个 AO3 Cookie，但没有登录会话。")

    p("  -> [Warning] No logged-in AO3 session found in local browsers.",
      "  -> [警告] 未在本地浏览器中找到已登录的 AO3 会话。")
    return None


def require_ao3_cookies():
    """Keep trying until an AO3 login session is loaded. Guest mode is not allowed."""
    while True:
        cookies = get_ao3_cookies()
        if cookies is not None:
            return cookies
        if sys.platform == "win32":
            cookies = _read_ao3_cookies_via_windows_browser()
            if cookies is not None:
                return cookies
        p("A logged-in AO3 session is required. Guest mode is not available.",
          "必须已登录 AO3，没有游客模式。")
        p("Fully quit Edge/Chrome, make sure you are logged in at archiveofourown.org, then press Enter to retry.",
          "请完全退出 Edge/Chrome，确认已在 archiveofourown.org 登录，然后按回车重试。")
        input("> ")


WINDOWS_CDP_PORT = 19222


def _windows_browser_targets():
    local = os.environ.get("LOCALAPPDATA", "")
    pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    return [
        (
            "Chrome",
            [
                os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(pf86, "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"),
            ],
            os.path.join(local, "Google", "Chrome", "User Data"),
        ),
        (
            "Edge",
            [
                os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe"),
                os.path.join(pf86, "Microsoft", "Edge", "Application", "msedge.exe"),
            ],
            os.path.join(local, "Microsoft", "Edge", "User Data"),
        ),
    ]


def _first_existing_file(paths):
    for path in paths:
        if path and os.path.isfile(path):
            return path
    return None


def _cdp_fetch_all_cookies(port):
    import websocket
    version = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=3).json()
    ws_url = version.get("webSocketDebuggerUrl")
    if not ws_url:
        tabs = requests.get(f"http://127.0.0.1:{port}/json/list", timeout=3).json()
        for tab in tabs:
            if tab.get("webSocketDebuggerUrl"):
                ws_url = tab["webSocketDebuggerUrl"]
                break
    if not ws_url:
        raise RuntimeError("Chrome DevTools websocket was not available")
    ws = websocket.create_connection(ws_url, timeout=10)
    try:
        ws.send(json.dumps({"id": 1, "method": "Network.getAllCookies"}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                if msg.get("error"):
                    raise RuntimeError(msg["error"])
                return msg.get("result", {}).get("cookies") or []
    finally:
        ws.close()


def _cookiejar_from_cdp_cookies(cdp_cookies):
    cj = CookieJar()
    for item in cdp_cookies:
        domain = item.get("domain") or ""
        if "archiveofourown.org" not in domain.lower():
            continue
        name = item.get("name") or ""
        value = item.get("value") or ""
        if name:
            cj.set_cookie(_ao3_cookie(name, value, domain=domain))
    return cj


def _read_ao3_cookies_via_windows_browser():
    """Ask Chrome/Edge (running as itself) for AO3 cookies through DevTools after the user quits the browser."""
    if sys.platform != "win32":
        return None
    try:
        import websocket  # noqa: F401
    except ImportError:
        p("  -> Optional module 'websocket-client' is missing, so Chrome/Edge helper is unavailable.",
          "  -> 未安装 'websocket-client'，无法通过 Chrome/Edge 自动读取。")
        return None

    p("Windows cannot decrypt Chrome cookies from this program. Fully quit Chrome and Edge, then press Enter. The program will start your browser once to read the AO3 login.",
      "Windows 无法从本程序直接解密 Chrome Cookie。请完全退出 Chrome 和 Edge，然后按回车。程序会启动一次浏览器来读取 AO3 登录。")
    input("> ")

    for name, exes, user_data in _windows_browser_targets():
        exe = _first_existing_file(exes)
        if not exe or not os.path.isdir(user_data):
            continue
        p(f"  -> Starting [{name}] to read cookies...",
          f"  -> 正在启动 [{name}] 以读取 Cookie...")
        proc = subprocess.Popen(
            [
                exe,
                f"--remote-debugging-port={WINDOWS_CDP_PORT}",
                "--remote-allow-origins=*",
                f"--user-data-dir={user_data}",
                "--headless=new",
                "--disable-gpu",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            cookies = None
            last_err = None
            for _ in range(30):
                time.sleep(0.4)
                try:
                    cookies = _cdp_fetch_all_cookies(WINDOWS_CDP_PORT)
                    break
                except Exception as e:
                    last_err = e
            if cookies is None:
                p(f"  -> [{name}] did not open DevTools: {last_err}. If it was still running, quit it completely (system tray too) and retry.",
                  f"  -> [{name}] 未能打开调试接口: {last_err}。若浏览器仍在运行，请连托盘图标一并退出后再试。")
                continue
            cj = _cookiejar_from_cdp_cookies(cookies)
            if _cookiejar_has_ao3_session(cj):
                p(f"  -> Successfully loaded a logged-in AO3 session via [{name}]!",
                  f"  -> 成功通过 [{name}] 加载已登录的 AO3 会话！")
                return cj
            p(f"  -> [{name}] started, but no AO3 login session was found.",
              f"  -> [{name}] 已启动，但没有 AO3 登录会话。")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
    return None


def _ao3_cookie(name, value, domain=".archiveofourown.org"):
    if not domain.startswith("."):
        domain = "." + domain.lstrip(".")
    return Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=True,
        domain_initial_dot=domain.startswith("."),
        path="/",
        path_specified=True,
        secure=True,
        expires=None,
        discard=True,
        comment=None,
        comment_url=None,
        rest={"HttpOnly": None},
        rfc2109=False,
    )


def _cookies_from_header(text):
    """Parse 'a=b; c=d' or a raw _otwarchive_session value into a CookieJar."""
    cj = CookieJar()
    raw = text.strip()
    if not raw:
        return None
    if "=" not in raw:
        cj.set_cookie(_ao3_cookie("_otwarchive_session", raw))
        return cj
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip()
        if name:
            cj.set_cookie(_ao3_cookie(name, value))
    return cj if list(cj) else None


def prompt_manual_ao3_cookies():
    """Let the user paste Chrome cookies or load a cookies.txt file (no extra browser)."""
    p("Automatic Chrome/Edge cookie reading does not work on current Windows. You can still use this program without installing another browser.",
      "当前 Windows 无法自动读取 Chrome/Edge Cookie。您仍可不用安装其他浏览器来继续。")
    p("In Chrome: F12 -> Application -> Cookies -> https://archiveofourown.org -> copy _otwarchive_session (and user_credentials if present).",
      "在 Chrome 中：F12 -> Application -> Cookies -> https://archiveofourown.org -> 复制 _otwarchive_session（如有 user_credentials 一并复制）。")
    p("Then paste either the session value, a full Cookie header (name=value; ...), or a cookies.txt file path. Press Enter to stay in Guest mode.",
      "然后粘贴会话值、完整 Cookie 头（name=value; ...），或 cookies.txt 文件路径。直接回车则继续游客模式。")
    raw = input("> ").strip().strip('"')
    if not raw:
        p("Continuing in Guest mode.", "继续以游客模式运行。")
        return None

    if os.path.isfile(raw):
        try:
            jar = MozillaCookieJar(raw)
            jar.load(ignore_discard=True, ignore_expires=True)
        except Exception as e:
            p(f"  -> Failed to load cookie file: {e}",
              f"  -> 无法读取 Cookie 文件: {e}")
            return None
        if _cookiejar_has_ao3_session(jar):
            p("  -> Loaded a logged-in AO3 session from cookie file.",
              "  -> 已从 Cookie 文件加载登录会话。")
            return jar
        p("  -> Cookie file had no AO3 login session.",
          "  -> Cookie 文件中没有 AO3 登录会话。")
        return None

    cj = _cookies_from_header(raw)
    if cj and _cookiejar_has_ao3_session(cj):
        p("  -> Using the pasted AO3 login cookie.",
          "  -> 已使用您粘贴的 AO3 登录 Cookie。")
        return cj
    p("  -> The pasted text did not contain a usable AO3 login cookie. Continuing in Guest mode.",
      "  -> 粘贴内容不是可用的 AO3 登录 Cookie，继续游客模式。")
    return None

def truncate_by_bytes(text, max_bytes=200):
    """Safely truncate a string by UTF-8 byte length without splitting multi-byte characters or exceeding OS limits."""
    encoded = text.encode('utf-8', errors='ignore')
    if len(encoded) <= max_bytes:
        return text
    # Reserve 3 bytes for the ellipsis "..."
    truncated_bytes = encoded[:max_bytes - 3]
    # errors='ignore' drops a trailing incomplete UTF-8 byte sequence.
    return truncated_bytes.decode('utf-8', errors='ignore').rstrip() + "..."

def sanitize_filename(name, max_bytes=150):
    """Strip illegal filename characters and truncate safely by UTF-8 byte length."""
    clean_name = re.sub(r'[\\/*?:"<>|]', "", name).strip()
    return truncate_by_bytes(clean_name, max_bytes=max_bytes)

def set_url_page(url, page_num):
    """Set or replace the page query parameter on a URL."""
    parsed_url = urlparse(url)
    query_params = parse_qs(parsed_url.query, keep_blank_values=True)
    query_params['page'] = [str(page_num)]
    new_query = urlencode(query_params, doseq=True)
    return urlunparse((
        parsed_url.scheme,
        parsed_url.netloc,
        parsed_url.path,
        parsed_url.params,
        new_query,
        parsed_url.fragment
    ))

def requests_get_with_retry(url, cookies=None, max_retries=3, base_delay=2):
    """HTTP GET wrapper with retries and exponential backoff."""
    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}, cookies=cookies, timeout=25)
            if response.status_code in [500, 502, 503, 504, 525]:
                raise requests.exceptions.HTTPError(f"Server returned status code: {response.status_code}")
            return response
        except (requests.exceptions.RequestException, requests.exceptions.HTTPError) as e:
            sleep_time = base_delay * (2 ** attempt)
            if attempt < max_retries - 1:
                p(f"  [Network fluctuation: {e}], retrying in {sleep_time}s (Attempt {attempt + 1})...",
                  f"  [网络波动: {e}]，{sleep_time} 秒后重试 (第 {attempt + 1} 次尝试)...")
                time.sleep(sleep_time)
            else:
                p(f"  [Error] Failed after {max_retries} continuous retries. URL: {url}",
                  f"  [错误] 连续 {max_retries} 次重试失败。URL: {url}")
                raise e
    return None

def get_work_ids_from_search(base_search_url, cookies=None, start_page=1, max_empty_pages=3):
    """Collect all work IDs from AO3 search result pages."""
    work_ids = []
    failed_pages = []
    current_page = start_page
    consecutive_empty_pages = 0

    while True:
        current_url = set_url_page(base_search_url, current_page)
        p(f"Scanning search page (Page {current_page}): {current_url}",
          f"正在扫描搜索页面 (第 {current_page} 页): {current_url}")
        
        try:
            response = requests_get_with_retry(current_url, cookies=cookies)
            if not response or response.status_code != 200:
                p(f"  -> [Warning] Failed to fetch Page {current_page}. Skipping this page.",
                  f"  -> [警告] 获取第 {current_page} 页失败，跳过该页。")
                failed_pages.append(current_page)
                current_page += 1
                continue
        except Exception:
            p(f"  -> [Warning] Error on Page {current_page} after retries. Skipping this page.",
              f"  -> [警告] 第 {current_page} 页多次重试后仍然出错，跳过该页。")
            failed_pages.append(current_page)
            current_page += 1
            continue

        soup = BeautifulSoup(response.text, "html.parser")
        work_items = soup.find_all("li", class_="work")

        if not work_items:
            consecutive_empty_pages += 1
            p(f"  -> Page {current_page} is empty.",
              f"  -> 第 {current_page} 页为空。")
            if consecutive_empty_pages >= max_empty_pages:
                p("Reached the end of search results.",
                  "已到达搜索结果末尾。")
                break
        else:
            consecutive_empty_pages = 0
            for item in work_items:
                h4 = item.find("h4", class_="heading")
                if h4 and h4.a:
                    href = h4.a.get("href")
                    match = re.search(r"/works/(\d+)", href)
                    if match:
                        work_ids.append(match.group(1))

        next_link = soup.select_one("li.next > a")
        if not next_link or not next_link.get("href"):
            if work_items:
                p("Reached the last page (no 'next' link found).",
                  "已到达最后一页（未找到下一页链接）。")
                break

        current_page += 1
        time.sleep(1)

    return list(dict.fromkeys(work_ids)), failed_pages

def fetch_work_metadata(wid, cookies=None):
    """Fetch a work's title and author."""
    url = f"https://archiveofourown.org/works/{wid}"
    try:
        res = requests_get_with_retry(url, cookies=cookies)
        if res and res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            
            title_tag = soup.find("h2", class_="title heading")
            title = title_tag.get_text(strip=True) if title_tag else f"Work_{wid}"
            
            author_tags = soup.select("a[rel='author']")
            if author_tags:
                authors = [a.get_text(strip=True) for a in author_tags]
                unique_authors = list(dict.fromkeys(authors))
                if len(unique_authors) > 3:
                    author_str = ", ".join(unique_authors[:3]) + " et al."
                else:
                    author_str = ", ".join(unique_authors)
            else:
                author_str = "Anonymous"
            
            return title, author_str
    except Exception as e:
        p(f"  -> Failed to fetch metadata for work {wid}: {e}",
          f"  -> 获取作品 {wid} 元数据失败: {e}")
    
    return None, None

def download_single_work(wid, file_format, cookies=None, output_dir="ao3_downloads"):
    """Download a single work (filename capped at 200 bytes)."""
    title, author = fetch_work_metadata(wid, cookies=cookies)
    download_url = f"https://archiveofourown.org/downloads/{wid}/fic.{file_format}"
    
    if not title or not author:
        p(f"  -> [Failed] Could not retrieve metadata for work {wid}.",
          f"  -> [失败] 无法获取作品 {wid} 的元数据。")
        return False, (wid, f"Work_{wid}", "Unknown", download_url)
    
    # Cap title at 110 bytes and author at 50 bytes.
    safe_title = sanitize_filename(title, max_bytes=110)
    safe_author = sanitize_filename(author, max_bytes=50)
    
    raw_filename = f"{safe_title} - {safe_author} [{wid}]"
    # Ensure the full base filename (including ID) stays within 200 bytes.
    safe_filename_base = truncate_by_bytes(raw_filename, max_bytes=200)
    
    filename = f"{safe_filename_base}.{file_format}"
    file_path = os.path.join(output_dir, filename)

    if os.path.exists(file_path):
        p(f"  -> File already exists, skipping: {filename}",
          f"  -> 文件已存在，跳过: {filename}")
        return True, None

    p(f"  -> Downloading: {title} / {author} [{file_format.upper()}]",
      f"  -> 正在下载: {title} / {author} [{file_format.upper()}]")

    try:
        res = requests_get_with_retry(download_url, cookies=cookies)
        if res and res.status_code == 200:
            with open(file_path, "wb") as f:
                f.write(res.content)
            p(f"  -> Saved successfully: {filename}",
              f"  -> 保存成功: {filename}")
            return True, None
        else:
            p(f"  -> Download response abnormal, status code: {res.status_code if res else 'None'}",
              f"  -> 下载响应异常，状态码: {res.status_code if res else 'None'}")
    except Exception as e:
        p(f"  -> Download error: {e}",
          f"  -> 下载出错: {e}")

    return False, (wid, title, author, download_url)

def download_works(work_ids, file_format, cookies=None, output_dir="ao3_downloads"):
    """Download a batch of works."""
    os.makedirs(output_dir, exist_ok=True)
    total = len(work_ids)
    failed_works = []

    for i, wid in enumerate(work_ids, 1):
        p(f"\n[{i}/{total}] Processing Work ID: {wid} ...",
          f"\n[{i}/{total}] 正在处理作品 ID: {wid} ...")
        success, failed_info = download_single_work(wid, file_format, cookies=cookies, output_dir=output_dir)
        if not success and failed_info:
            failed_works.append(failed_info)

        time.sleep(1.5)

    return failed_works

def select_file_format():
    """Prompt for the download file format."""
    formats = {
        "1": "azw3",
        "2": "epub",
        "3": "mobi",
        "4": "pdf",
        "5": "html"
    }
    p("Please select the download format:",
      "请选择要下载的格式：")
    
    print("  1. AZW3\n  2. EPUB\n  3. MOBI\n  4. PDF\n  5. HTML")
    
    p("Enter option number (default is 2 - EPUB):",
      "请输入选项数字（默认为 2 - EPUB）：")
    choice = input("> ").strip()
    
    selected_format = formats.get(choice, "epub")
    p(f"Selected format: {selected_format.upper()}",
      f"已选择格式: {selected_format.upper()}\n")
    return selected_format

def handle_failed_downloads(failed_works, file_format, cookies=None, output_dir="ao3_downloads"):
    """Retry or report works that failed to download."""
    current_failed = failed_works
    while current_failed:
        p("\nThe following work cannot be downloaded due to network issue, would you like to retry? (yes/no)",
          "\n由于网络问题，以下作品无法下载，您是否想要重试？(yes/no)")
        
        p("Failed works list:", "失败作品列表：")
        for wid, title, author, dl_url in current_failed:
            p(f"  - Work ID: {wid} | Title: {title} - {author}",
              f"  - 作品 ID: {wid} | 标题: {title} - {author}")

        p("Accept yes or no:", "请输入 yes 或 no：")
        user_choice = input("> ").strip().lower()

        if user_choice in ["yes", "y"]:
            p("Retrying failed downloads...", "正在重试下载失败的项目...")
            new_failed = []
            for idx, (wid, title, author, dl_url) in enumerate(current_failed, 1):
                p(f"\n[Retry {idx}/{len(current_failed)}] Processing Work ID: {wid} ...",
                  f"\n[重试 {idx}/{len(current_failed)}] 正在处理作品 ID: {wid} ...")
                success, failed_info = download_single_work(wid, file_format, cookies=cookies, output_dir=output_dir)
                if not success and failed_info:
                    new_failed.append(failed_info)
                time.sleep(1.5)
            
            current_failed = new_failed
            if not current_failed:
                p("\nAll failed works have been successfully downloaded!",
                  "\n所有先前失败的作品均已成功下载！")
        elif user_choice in ["no", "n"]:
            p("\nYou can use the provided link to download manually:",
              "\n您可以使用以下提供的链接进行手动下载：")
            for wid, title, author, dl_url in current_failed:
                print(f"  - [{wid}] {title} - {author}: {dl_url}")
            break
        else:
            p("Invalid input. Please enter 'yes' or 'no'.",
              "无效的输入，请输入 'yes' 或 'no'。")

def _pause_before_exit():
    """Keep the Windows console open after success or crash so the error stays visible."""
    try:
        p("\nPress Enter to exit...", "\n按回车键退出...")
        input()
    except Exception:
        time.sleep(15)


def main():
    select_interaction_language()

    print("=" * 40, flush=True)
    p("  Initializing AO3 Batch Downloader...", "  正在启动程序...")
    print("=" * 40 + "\n", flush=True)

    cookies = require_ao3_cookies()

    file_format = select_file_format()

    # Resolve output path.
    base_dir = get_script_dir()
    output_dir = os.path.join(base_dir, "ao3_downloads")
    abs_output_path = os.path.abspath(output_dir)

    p("Please enter the full AO3 search page URL:",
      "请输入完整的 AO3 搜索页 URL：")
    search_page_url = input("> ").strip()

    p("Please enter starting page number (Press Enter to start from page 1):",
      "请输入起始页码（按回车默认从第 1 页开始）：")
    start_page_input = input("> ").strip()
    start_page = int(start_page_input) if start_page_input.isdigit() else 1

    if search_page_url:
        p(f"\nStarting to scan work IDs from search pages (starting from page {start_page})...",
          f"\n开始从搜索页面扫描作品 ID（从第 {start_page} 页开始）...")
        work_ids, failed_pages = get_work_ids_from_search(search_page_url, cookies=cookies, start_page=start_page)
        p(f"\nScanning finished. Found {len(work_ids)} work IDs in total.",
          f"\n扫描完成。共找到 {len(work_ids)} 个作品 ID。")

        failed_works = []
        if work_ids:
            failed_works = download_works(work_ids, file_format, cookies=cookies, output_dir=output_dir)

        print("\n" + "="*40)
        p("Task Execution Summary Report", "任务执行总结报告")
        print("="*40)
        
        p(f"Download Directory: {abs_output_path}",
          f"文件保存路径: {abs_output_path}\n")

        if failed_pages:
            p(f"[Failed Search Pages ({len(failed_pages)}):]", f"[失败的搜索页 ({len(failed_pages)}):]")
            for page in failed_pages:
                p(f"  - Page {page}", f"  - 第 {page} 页")
        else:
            p("[Search Page Scanning]: All successful, no failed pages.",
              "[搜索页扫描]: 全部成功，无失败页面。")

        if failed_works:
            handle_failed_downloads(failed_works, file_format, cookies=cookies, output_dir=output_dir)
        else:
            p("[Work Downloads]: All successful, no failed items.",
              "[作品下载]: 全部成功，无失败项目。")

        print("="*40)

    p("\nWorkflow completed.", "\n工作流执行完毕。")
    p(f"All downloaded files can be found at:\n  {abs_output_path}",
      f"所有已下载的文件均可在以下路径找到：\n  {abs_output_path}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.", flush=True)
    except Exception:
        print("\n" + "=" * 40, flush=True)
        print("Unhandled error / 未处理的错误:", flush=True)
        traceback.print_exc()
        print("=" * 40, flush=True)
    finally:
        _pause_before_exit()