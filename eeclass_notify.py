"""每天登入中央大學 eeclass，整理作業期限、新公告、新教材，傳到 Telegram。

需要的環境變數：
  EECLASS_ACCOUNT   eeclass 帳號（學號）
  EECLASS_PASSWORD  eeclass 密碼
  TG_BOT_TOKEN      Telegram bot token（找 @BotFather 拿）
  TG_CHAT_ID        你的 Telegram chat id（找 @userinfobot 拿）

選用：
  EECLASS_BASE      預設 https://ncueeclass.ncu.edu.tw
  STATE_FILE        記錄看過哪些項目，預設 state/seen.json
  FULL              設成 1 的話傳完整早報（每天 7 點後第一次執行會自動這樣做）；
                    其他時候只在有新東西、或作業 3 小時內截止時才傳
  RESEND_ALL        設成 1 的話，把目前所有公告、教材當成新的重傳一次
  DUE_DAYS          列出幾天內截止的作業，預設 7
  GEMINI_API_KEY    有的話就用 Google Gemini（免費額度）幫公告寫一句話重點、早上那則加「今日重點」
  AI_MODEL          指定 Gemini 模型，沒指定就從 gemini-flash-lite-latest 開始依序試
  GITHUB_TOKEN      沒有 Gemini 金鑰時改試 GitHub Models
  DEBUG             設成 1 才會把頁面文字印到 log、存截圖（含個人課程資料，只在 private repo 開）
"""

import html
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import requests
from playwright.sync_api import sync_playwright

# workflow 沒設定 Variable 時會傳空字串進來，所以用 or 給預設值
BASE = (os.environ.get("EECLASS_BASE", "").strip() or "https://ncueeclass.ncu.edu.tw").rstrip("/")
STATE_FILE = Path(os.environ.get("STATE_FILE", "").strip() or "state/seen.json")
DEBUG_DIR = Path("debug")
TW = timezone(timedelta(hours=8))
DUE_DAYS = int(os.environ.get("DUE_DAYS", "").strip() or "7")
MAX_PER_SECTION = 10
REMIND_HOURS = 3      # 作業截止前幾小時提醒一次
DIGEST_HOUR = 7       # 每天幾點之後的第一次執行傳早報
QUIET_START, QUIET_END = 1, 7   # 台灣時間 1 點到 7 點不檢查（外部排程每小時叫，夜間直接結束）
MAX_FILES = 8         # 每次最多傳幾個檔案到 Telegram，免得洗版
MAX_FILE_MB = 50      # Telegram bot 單一檔案上限
# Google Gemini 的 OpenAI 相容端點（免費額度），金鑰在 https://aistudio.google.com/apikey 申請
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
# 先試回應快的 lite 模型，失敗或逾時再換下一個
GEMINI_MODELS = [m for m in [os.environ.get("AI_MODEL", "").strip(), "gemini-flash-lite-latest",
                             "gemini-2.5-flash-lite", "gemini-flash-latest", "gemini-2.5-flash",
                             "gemini-2.0-flash"] if m]
GITHUB_MODELS_URL = "https://models.github.ai/inference/chat/completions"
WEEKDAYS = "一二三四五六日"
DEBUG = os.environ.get("DEBUG", "").strip() == "1"


def env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"缺少環境變數 {name}")
    return value


def redact(text):
    """把可能出現在錯誤訊息裡的 bot token 遮掉（例如連線失敗時 requests 會印出完整網址）"""
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    return str(text).replace(token, "***") if token else str(text)


def save_debug(page, tag):
    """失敗時留下現場。頁面內容有姓名、學號、課程，只有 DEBUG=1 才記錄"""
    if not DEBUG:
        print(f"--- [{tag}] 失敗（設定 DEBUG=1 可看頁面文字和截圖）")
        return
    DEBUG_DIR.mkdir(exist_ok=True)
    # artifact 不方便看，把頁面上看得到的文字也印到 log
    try:
        text = page.locator("body").inner_text(timeout=5000)
        print(f"--- [{tag}] {page.url}\n{' '.join(text.split())[:1500]}\n---")
    except Exception as e:
        print(f"--- [{tag}] {page.url}（讀不到頁面文字: {e}）")
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{tag}.png"), full_page=True)
        (DEBUG_DIR / f"{tag}.html").write_text(page.content(), encoding="utf-8")
    except Exception as e:  # 除錯資料存不了也不要讓主流程掛掉
        print(f"存除錯資料失敗: {e}")


def first_visible(page, selectors):
    for sel in selectors:
        loc = page.locator(sel)
        for i in range(loc.count()):
            if loc.nth(i).is_visible():
                return loc.nth(i)
    return None


def login(page, account, password):
    page.goto(f"{BASE}/index/login?next=/dashboard", wait_until="domcontentloaded")
    page.wait_for_timeout(1500)

    user_box = first_visible(page, [
        'input[name="account"]', 'input[name="username"]', 'input[name="email"]',
        'input[type="text"]', 'input:not([type])',
    ])
    pass_box = first_visible(page, ['input[name="password"]', 'input[type="password"]'])
    if not user_box or not pass_box:
        save_debug(page, "login-form-not-found")
        raise RuntimeError("找不到登入欄位，請看 debug/ 裡的截圖")

    if first_visible(page, ['input[name*="captcha" i]', 'img[src*="captcha" i]']):
        save_debug(page, "captcha")
        raise RuntimeError("登入頁出現驗證碼，沒辦法自動登入")

    user_box.fill(account)
    pass_box.fill(password)

    button = first_visible(page, [
        'form button[type="submit"]', 'form input[type="submit"]',
        'button:has-text("登入")', 'button:has-text("Login")', 'form button',
    ])
    if button:
        button.click()
    else:
        pass_box.press("Enter")

    # 帳號在別處登入時會跳「是否要將其他裝置登出?」，按「保持登入」，不踢掉其他裝置
    keep_login = page.locator('text="保持登入"')
    for _ in range(90):  # 學校伺服器忙的時候會卡在 Loading 很久，最多等 45 秒
        if "/index/login" not in page.url:
            return
        for i in range(keep_login.count()):
            if keep_login.nth(i).is_visible():
                print("帳號已在其他位置登入，按「保持登入」")
                keep_login.nth(i).click()
                break
        page.wait_for_timeout(500)
    # 頁面沒跳轉不代表沒登入成功，直接開 dashboard 看會不會被導回登入頁
    try:
        page.goto(f"{BASE}/dashboard", wait_until="domcontentloaded", timeout=30000)
        if "/index/login" not in page.url:
            print("登入頁沒跳轉，但直接開 dashboard 是登入狀態")
            return
    except Exception as err:
        print(f"直接開 dashboard 失敗: {err}")
    save_debug(page, "login-failed")
    raise RuntimeError("登入失敗（帳密錯誤或頁面改版）")


# 讀「最近事件 / 最新公告 / 最新教材 / 最新討論」頁面的表格，每列回傳標題、連結、課程、日期
ROWS_JS = """() => [...document.querySelectorAll('table tr')]
    .filter(tr => !tr.classList.contains('fs-th') && tr.querySelector('td'))
    .map(tr => {
        const cells = [...tr.querySelectorAll('td')];
        const links = [...tr.querySelectorAll('a[href]')];
        const isCourse = a => /^\\/course\\/\\d+$/.test(a.getAttribute('href'));
        const course = links.find(isCourse);
        const link = links.find(a => a.classList.contains('fs-bulletin-item')) || links.find(a => !isCourse(a));
        const last = cells[cells.length - 1];
        const dateEl = last && (last.querySelector('[title]') || last);
        const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
        return {
            title: link ? clean(link.getAttribute('data-modal-title') || link.innerText) : clean(cells[1] && cells[1].innerText),
            href: link ? (link.getAttribute('data-url') || link.getAttribute('href')) : '',
            course: course ? clean(course.innerText) : '',
            date: clean(dateEl && (dateEl.getAttribute('title') || dateEl.innerText)),
        };
    })
    .filter(r => r.title)"""


def short_course(name):
    """「作業研究 Ⅰ Operations Research Ⅰ」→「作業研究 Ⅰ」，全英文課名就原樣保留"""
    m = re.match(r"^[^A-Za-z]*[一-鿿][^A-Za-z]*", name)
    return m.group(0).strip(" -－") if m else name


def item_id(href):
    if "id=" in href:
        return parse_qs(urlparse(href).query).get("id", [href])[0]
    m = re.search(r"(\d+)/?$", href.split("?")[0])
    return m.group(1) if m else href


def read_table(page, path):
    page.goto(f"{BASE}/dashboard/{path}", wait_until="networkidle")
    rows = page.evaluate(ROWS_JS)
    for r in rows:
        r["course"] = short_course(r["course"])
    return rows


BULLETIN_JS = """html => {
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const body = doc.querySelector('.bulletin-content');
    return {
        text: body ? body.textContent.replace(/\\s+/g, ' ').trim() : '',
        files: [...doc.querySelectorAll('.fs-filelist li a[href]')].map(a => ({
            name: (a.querySelector('.text') || a).childNodes[0].textContent.trim(),
            href: a.getAttribute('href')})),
    };
}"""


def bulletin_detail(page, data_url):
    """公告內文摘要和附件"""
    try:
        r = page.request.get(BASE + data_url, timeout=15000)
        detail = page.evaluate(BULLETIN_JS, r.text())
        text = detail["body"] = detail["text"]
        detail["text"] = text[:250] + ("…" if len(text) > 250 else "")
        for f in detail["files"]:
            f["url"] = BASE + f["href"]
        return detail
    except Exception as err:
        print(f"讀公告內容失敗: {err}")
        return {"text": "", "files": []}


def extract_text(name, data, limit=3000):
    """從 PDF / PPTX 抓文字給 AI 摘要用，其他格式回傳空字串"""
    ext = name.lower().rsplit(".", 1)[-1]
    try:
        if ext == "pdf":
            from pypdf import PdfReader
            pages = PdfReader(io.BytesIO(data)).pages[:15]
            text = " ".join(p.extract_text() or "" for p in pages)
        elif ext == "pptx":
            from pptx import Presentation
            text = " ".join(shape.text_frame.text for slide in Presentation(io.BytesIO(data)).slides
                            for shape in slide.shapes if shape.has_text_frame)
        else:
            return ""
    except Exception as err:
        print(f"讀不到 {name} 的文字: {err}")
        return ""
    return " ".join(text.split())[:limit]


def filename_from(headers, fallback):
    cd = headers.get("content-disposition", "")
    m = re.search(r"filename\*=(?:UTF-8|utf-8)''([^;]+)", cd) or re.search(r'filename="?([^";]+)"?', cd)
    name = unquote(m.group(1)).strip() if m else ""
    if name and not name.isascii() and "%" not in m.group(1):
        try:  # 有些伺服器把 UTF-8 檔名直接塞進 header，requests 會用 latin-1 讀
            name = name.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return name or fallback


def download(page, href, fallback_name):
    """用登入中的瀏覽器下載檔案，回傳 {name, data, text}；不是檔案（例如網頁）或太大就回傳 None"""
    try:
        r = page.request.get(BASE + href, timeout=90000)
        if not r.ok or "text/html" in r.headers.get("content-type", ""):
            print(f"下載 {fallback_name} 失敗或不是檔案: {r.status} {r.headers.get('content-type')}")
            return None
        data = r.body()
        name = filename_from(r.headers, fallback_name)
        if len(data) > MAX_FILE_MB * 1024 * 1024:
            print(f"{name} 超過 {MAX_FILE_MB} MB，不傳")
            return None
        return {"name": name, "data": data, "text": extract_text(name, data)}
    except Exception as err:
        print(f"下載 {fallback_name} 失敗: {err}")
        return None


def material_files(page, href):
    """教材頁面上的下載連結；影片等沒有檔案的教材回傳空 list"""
    try:
        page.goto(BASE + href, wait_until="networkidle")
        links = page.eval_on_selector_all(
            'a[href*="filedownload" i], a[href*="docDownload" i], a[href*="/download" i], a[download]',
            "els => els.map(a => ({name: a.innerText.replace(/\\s+/g, ' ').trim(), href: a.getAttribute('href')}))")
        if not links:
            hrefs = page.eval_on_selector_all("a[href]", "els => els.map(a => a.getAttribute('href'))")
            if DEBUG:
                print(f"教材 {href} 沒有下載連結，頁面連結：{[h for h in hrefs if h and 'javascript' not in h][:15]}")
        unique = {}
        for l in links:
            unique.setdefault(l["href"], l)
        return list(unique.values())
    except Exception as err:
        print(f"讀教材頁面失敗 {href}: {err}")
        return []


def collect_files(page, new):
    """下載新公告的附件和新教材的檔案，最多 MAX_FILES 個"""
    budget = MAX_FILES
    for kind, items in (("公告", new["bulletins"]), ("教材", new["materials"])):
        for item in items[:MAX_PER_SECTION]:
            if kind == "教材":
                # 教材頁的下載連結文字常常只是「下載」，改用教材標題
                item["files"] = [{**f, "url": BASE + f["href"],
                                  "name": f["name"] if len(f["name"]) > 4 else item["title"]}
                                 for f in material_files(page, item["href"])]
            item["downloads"] = []
            for f in item.get("files", []):
                if budget <= 0:
                    break
                got = download(page, f["href"], f["name"] or item["title"])
                if got:
                    got["caption"] = f"📎 <b>{e(item['course'] or kind)}</b>｜{e(item['title'])}"
                    item["downloads"].append(got)
                    budget -= 1
            item["file_text"] = " ".join(d["text"] for d in item["downloads"] if d["text"])[:4000]
    print(f"下載了 {MAX_FILES - budget} 個檔案")


def parse_due(value):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            due = datetime.strptime(value, fmt).replace(tzinfo=TW)
            return due.replace(hour=23, minute=59) if fmt == "%Y-%m-%d" else due
        except ValueError:
            pass
    return None


def scrape(page):
    data = {"events": [], "bulletins": [], "materials": [], "discussions": []}

    for r in read_table(page, "latestEvent"):
        kind = "考試" if "/exam/" in r["href"] else "問卷" if "/questionnaire/" in r["href"] else "作業"
        data["events"].append({**r, "kind": kind, "key": f"event:{item_id(r['href'])}",
                               "url": BASE + r["href"], "due": parse_due(r["date"])})

    for r in read_table(page, "latestBulletin"):
        data["bulletins"].append({**r, "key": f"bulletin:{item_id(r['href'])}",
                                  "url": f"{BASE}/dashboard/latestBulletin"})

    for r in read_table(page, "latestMaterial"):
        data["materials"].append({**r, "key": f"material:{item_id(r['href'])}", "url": BASE + r["href"]})

    # 「最新討論」頁面的列表是之後才載入的，改從首頁的「最新討論」區塊抓
    page.goto(f"{BASE}/dashboard", wait_until="networkidle")
    for d in page.eval_on_selector_all('a[href*="Discuss/"]', """els => els.map(a => ({
            title: a.innerText.replace(/\\s+/g, ' ').trim(), href: a.getAttribute('href'),
            context: (a.closest('tr, li') || a.parentElement).innerText.replace(/\\s+/g, ' ').trim()}))"""):
        if d["title"] and not d["title"].isdigit():
            data["discussions"].append({**d, "course": "", "key": f"discuss:{item_id(d['href'])}",
                                        "url": BASE + d["href"]})

    for name, items in data.items():
        unique = {}
        for item in items:
            unique.setdefault(item["key"], item)
        data[name] = list(unique.values())
    print("抓到：" + "、".join(f"{k} {len(v)}" for k, v in data.items()))
    if not any(data.values()):
        save_debug(page, "empty")
        raise RuntimeError("四個頁面都沒抓到東西，頁面可能改版了")
    return data


def load_state():
    """看過的項目、已提醒過的作業、今天的早報傳了沒。沒有紀錄或是舊格式就回傳 None（當成第一次執行）"""
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(state, dict) and state.get("version") == 2:
            return {"keys": set(state["keys"]), "reminded": set(state.get("reminded", [])),
                    "digest_date": state.get("digest_date", "")}
    return None


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"version": 2, "digest_date": state["digest_date"],
                                      "reminded": sorted(state["reminded"]), "keys": sorted(state["keys"])},
                                     indent=0), encoding="utf-8")


def e(text):
    return html.escape(str(text), quote=False)


def link(text, url):
    return f'<a href="{html.escape(url)}">{e(text)}</a>'


def fmt_due(due, now):
    days = (due.date() - now.date()).days
    clock = due.strftime("%H:%M")
    if days == 0:
        when, icon = f"今天 {clock}", "🔴"
    elif days == 1:
        when, icon = f"明天 {clock}", "🔴"
    else:
        when = f"{due.month}/{due.day}（{WEEKDAYS[due.weekday()]}）{clock}"
        icon = "🟠" if days <= 3 else "🟢"
    left = "" if days <= 1 else f"・剩 {days} 天"
    return icon, f"<b>{when}</b>{left}"


def by_course(items):
    groups = {}
    for item in items:
        groups.setdefault(item["course"] or "其他", []).append(item)
    return groups


def capped(items, url):
    extra = len(items) - MAX_PER_SECTION
    more = [f"　…還有 {extra} 則，{link('到 eeclass 看', url)}"] if extra > 0 else []
    return items[:MAX_PER_SECTION], more


def ai_summarize(items, upcoming, now, want_overview):
    """用 Gemini 幫新公告、新教材各寫一句話重點，並寫「今日重點」。失敗就回傳空的，不影響通知"""
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if gemini_key:
        attempts = [(GEMINI_URL, gemini_key, model, {"response_format": {"type": "json_object"}})
                    for model in GEMINI_MODELS]
    elif github_token:
        attempts = [(GITHUB_MODELS_URL, github_token, "openai/gpt-4o-mini", {"response_format": {"type": "json_object"}})]
    else:
        attempts = []
    items = [i for i in items if i.get("body") or i.get("files") or i.get("file_text")]
    if not attempts or not (items or (want_overview and upcoming)):
        return {}, ""

    lines = [f"現在時間：{now:%Y-%m-%d %H:%M}（{WEEKDAYS[now.weekday()]}）"]
    if items:
        lines.append("\n新公告和新教材：")
        for i, item in enumerate(items):
            files = "、".join(f["name"] for f in item.get("files", []))
            lines.append(f"[{i}] {item['kind']}｜課程：{item['course']}｜標題：{item['title']}"
                         + (f"｜內文：{item['body'][:1500]}" if item.get("body") else "")
                         + (f"｜附件：{files}" if files else "")
                         + (f"｜檔案內容節錄：{item['file_text']}" if item.get("file_text") else ""))
    if want_overview and upcoming:
        lines.append("\n即將截止的作業：")
        for ev in upcoming:
            lines.append(f"- {ev['due']:%m/%d %H:%M} {ev['course']}：{ev['title']}")

    prompt = (
        "你是幫大學生整理課程通知的助理。根據下面的資料，用繁體中文回傳 JSON：\n"
        '{"items": {"編號": "這則公告或教材的重點，一句話、40 字以內；公告寫出要做什麼或日期，'
        '教材寫出在講什麼主題，不要重複標題"}, '
        '"overview": "' + ("今天最需要注意的 1～3 件事，每件一行，以「• 」開頭，口語、具體，提到日期和課程"
                           if want_overview else "") + '"}\n'
        "有檔案內容節錄的，根據內容寫；只有附件檔名的，就說明附件是什麼。不要編造資料裡沒有的資訊。\n\n" + "\n".join(lines)
    )
    body = {"messages": [{"role": "user", "content": prompt}], "temperature": 0.2}
    try:
        content = None
        # 依序試每個模型，第一個成功的就用
        deadline = time.monotonic() + 240  # 全部模型加起來最多等 4 分鐘
        for url, key, model, extra in attempts:
            left = deadline - time.monotonic()
            if left < 10:
                break
            started = time.monotonic()
            try:
                r = requests.post(url, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                                  json={**body, "model": model, **extra}, timeout=min(90, left))
                content = r.json()["choices"][0]["message"]["content"]
                print(f"AI 模型：{model}（{time.monotonic() - started:.0f} 秒）")
                break
            except requests.RequestException as err:
                print(f"AI 模型 {model} 連線失敗（{time.monotonic() - started:.0f} 秒）: {err}")
            except (ValueError, KeyError, IndexError, TypeError):
                print(f"AI 回應無法解析（{model}）: {r.status_code}" + (f" {r.text[:300]!r}" if DEBUG else ""))
        if content is None:
            return {}, ""
        result = json.loads(content[content.index("{"):content.rindex("}") + 1])
        per_item = {items[int(k)]["key"]: str(v).strip()
                    for k, v in (result.get("items") or result.get("bulletins") or {}).items()
                    if str(k).isdigit() and int(k) < len(items) and str(v).strip()}
        overview = str(result.get("overview") or "").strip() if want_overview else ""
        print(f"AI 摘要：{len(per_item)} 則公告／教材" + ("、今日重點" if overview else ""))
        return per_item, overview
    except Exception as err:
        print(f"AI 摘要失敗: {err}")
        return {}, ""


def upcoming_events(data, now, window):
    upcoming = [ev for ev in data["events"] if ev["due"] and now <= ev["due"] <= now + window]
    return sorted(upcoming, key=lambda ev: ev["due"])


def build_message(data, new, first_run, now, full, overview="", reminders=()):
    """回傳要傳的區塊（每個區塊是一段 HTML），沒東西要傳就回傳空 list。
    full=True 是每天的早報：列出 DUE_DAYS 天內所有期限，沒有新東西也會傳。
    其他時候只在有新東西或有作業快截止（reminders）時才傳"""
    blocks = []

    if overview:
        blocks.append(f"🤖 <b>今日重點</b>\n{e(overview)}")

    if full:
        upcoming, label = upcoming_events(data, now, timedelta(days=DUE_DAYS)), f"{DUE_DAYS} 天內截止"
    else:
        upcoming, label = list(reminders), f"{REMIND_HOURS} 小時內截止，記得交"
    if upcoming:
        lines = [f"⏰ <b>{label}</b>（{len(upcoming)}）"]
        for ev in upcoming:
            icon, when = fmt_due(ev["due"], now)
            tag = "🆕 " if ev["key"] in new["events_keys"] else ""
            kind = "" if ev["kind"] == "作業" else f"[{ev['kind']}] "
            lines.append(f"{icon} {when}\n　{tag}{kind}{link(ev['title'], ev['url'])}\n　<i>{e(ev['course'])}</i>")
        blocks.append("\n".join(lines))

    has_new = any(new[name] for name in ("bulletins", "materials", "discussions")) or new["events_keys"]
    later = [ev for ev in data["events"] if ev["key"] in new["events_keys"] and ev not in upcoming]
    if later and not first_run:
        lines = [f"🆕 <b>新的作業／考試</b>（{len(later)}）"]
        for ev in later:
            due = f"{ev['due'].month}/{ev['due'].day} 截止" if ev["due"] else ev["date"]
            kind = "" if ev["kind"] == "作業" else f"[{ev['kind']}] "
            lines.append(f"• {kind}{link(ev['title'], ev['url'])}\n　<i>{e(ev['course'])}・{e(due)}</i>")
        blocks.append("\n".join(lines))

    sections = [
        ("bulletins", "📢", "新公告", f"{BASE}/dashboard/latestBulletin"),
        ("materials", "📂", "新教材", f"{BASE}/dashboard/latestMaterial"),
        ("discussions", "💬", "新討論", f"{BASE}/dashboard/latestDiscuss"),
    ]
    for name, icon, label, page_url in sections:
        items, more = capped(new[name], page_url)
        if not items:
            continue
        lines = [f"{icon} <b>{label}</b>（{len(new[name])}）"]
        groups = by_course(items)
        for course, group in groups.items():
            if list(groups) != ["其他"]:
                lines.append(f"\n▸ <b>{e(course)}</b>")
            for item in group:
                date = item.get("date", "")[:10][-5:].replace("-", "/")
                lines.append(f"• {link(item['title'], item['url'])}" + (f" <i>{date}</i>" if date else ""))
                if item.get("ai"):
                    lines.append(f"　🤖 {e(item['ai'])}")
                files = "\n".join(f"📎 {link(f['name'], f['url'])}" for f in item.get("files", []))
                quote = "\n".join(x for x in (e(item.get("text", "")), files) if x)
                if quote:
                    lines.append(f"<blockquote expandable>{quote}</blockquote>")
        blocks.append("\n".join(lines + more))

    if first_run:
        blocks.append(f"✅ 開始追蹤：{len(data['bulletins'])} 則公告、{len(data['materials'])} 份教材，"
                      "之後有新的才會通知你")

    if not full and not first_run and not has_new and not upcoming:
        return []
    if full and not has_new and not first_run:
        blocks.append("✅ 沒有新的公告、教材或作業")
    title = f"📚 <b>eeclass</b>｜{now.month}/{now.day}（{WEEKDAYS[now.weekday()]}）{now:%H:%M}"
    footer = link("打開 eeclass", f"{BASE}/dashboard")
    return [title] + blocks + [footer]


_migrated_chat_id = None


def tg_post(method, payload, **kwargs):
    """呼叫 Telegram API。群組升級成超級群組時 ID 會變，自動改用新 ID 再送一次"""
    global _migrated_chat_id
    token = env("TG_BOT_TOKEN")
    payload = {**payload, "chat_id": _migrated_chat_id or env("TG_CHAT_ID")}
    send = (lambda p: requests.post(f"https://api.telegram.org/bot{token}/{method}", data=p, **kwargs)) \
        if "files" in kwargs else \
        (lambda p: requests.post(f"https://api.telegram.org/bot{token}/{method}", json=p, **kwargs))
    r = send(payload)
    try:
        new_id = r.json().get("parameters", {}).get("migrate_to_chat_id")
    except ValueError:
        new_id = None
    if not r.ok and new_id:
        _migrated_chat_id = str(new_id)
        print(f"⚠️ Telegram 群組已升級成超級群組，新的 ID 是 {new_id}，請把 GitHub Secret TG_CHAT_ID 改成這個")
        r = send({**payload, "chat_id": _migrated_chat_id})
    return r


def send_telegram(blocks):
    # Telegram 單則上限 4096 字，照區塊切，避免把 HTML 標籤切斷
    chunks, current = [], ""
    for block in blocks:
        if current and len(current) + len(block) + 2 > 3800:
            chunks.append(current)
            current = ""
        current += block + "\n\n"
    chunks.append(current)

    for chunk in chunks:
        r = tg_post("sendMessage", {"text": chunk.strip(), "parse_mode": "HTML",
                                    "disable_web_page_preview": True}, timeout=30)
        if not r.ok:
            raise RuntimeError(redact(f"Telegram 傳送失敗: {r.status_code} {r.text}"))


def send_files(files):
    """把下載好的附件一個一個用 sendDocument 傳過去；某個失敗不影響其他的"""
    for f in files:
        try:
            r = tg_post("sendDocument", {"caption": f["caption"][:1000], "parse_mode": "HTML"},
                        files={"document": (f["name"], f["data"])}, timeout=120)
            if not r.ok:
                print(f"傳檔案 {f['name']} 失敗: {r.status_code} {r.text[:200]}")
        except requests.RequestException as err:
            print(f"傳檔案 {f['name']} 失敗: {redact(err)}")


def main():
    hour = datetime.now(TW).hour
    if QUIET_START <= hour < QUIET_END and os.environ.get("FULL") != "1":
        print(f"台灣時間 {hour} 點，夜間不檢查")
        return
    account, password = env("EECLASS_ACCOUNT"), env("EECLASS_PASSWORD")
    state = load_state()
    first_run = state is None
    state = state or {"keys": set(), "reminded": set(), "digest_date": ""}
    seen = set() if os.environ.get("RESEND_ALL") == "1" else state["keys"]
    if os.environ.get("RESEND_ALL") == "1":
        first_run = False

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for attempt in range(1, 4):
                context = browser.new_context(locale="zh-TW")
                page = context.new_page()
                page.on("dialog", lambda d: (print(f"dialog: {d.message}"), d.accept()))
                try:
                    login(page, account, password)
                    data = scrape(page)
                    new = {name: [i for i in data[name] if i["key"] not in seen]
                           for name in ("bulletins", "materials", "discussions")}
                    new["events_keys"] = {ev["key"] for ev in data["events"] if ev["key"] not in seen}
                    if not first_run:
                        for b in new["bulletins"][:MAX_PER_SECTION]:
                            b.update(bulletin_detail(page, b["href"]))
                        collect_files(page, new)
                        if DEBUG and new["bulletins"]:
                            b = new["bulletins"][0]
                            print(f"公告範例: {b.get('text', '')[:80]}／附件 {len(b.get('files', []))} 個")
                    break
                except Exception as err:
                    print(f"第 {attempt} 次失敗: {err}")
                    if attempt == 3:
                        raise
                    page.wait_for_timeout(15000)
                finally:
                    context.close()
            browser.close()
    except Exception as err:
        send_telegram([f"⚠️ eeclass 通知執行失敗：\n{e(redact(err)[:500])}"])
        raise

    if first_run:
        for name in ("bulletins", "materials", "discussions"):
            new[name] = []
        new["events_keys"] = set()

    now = datetime.now(TW)
    today = now.strftime("%Y-%m-%d")
    auto_digest = now.hour >= DIGEST_HOUR and state["digest_date"] != today
    full = os.environ.get("FULL") == "1" or auto_digest or first_run
    reminders = [ev for ev in upcoming_events(data, now, timedelta(hours=REMIND_HOURS))
                 if ev["key"] not in state["reminded"]]
    to_summarize = ([{**b, "kind": "公告"} for b in new["bulletins"][:MAX_PER_SECTION]]
                    + [{**m, "kind": "教材"} for m in new["materials"][:MAX_PER_SECTION]])
    per_item, overview = ai_summarize(to_summarize, upcoming_events(data, now, timedelta(days=DUE_DAYS)),
                                      now, want_overview=full and not first_run)
    for item in new["bulletins"] + new["materials"]:
        item["ai"] = per_item.get(item["key"], "")
    blocks = build_message(data, new, first_run, now, full, overview, reminders)
    print(f"新公告 {len(new['bulletins'])}、新教材 {len(new['materials'])}、新討論 {len(new['discussions'])}、"
          f"新作業 {len(new['events_keys'])}、快截止 {len(reminders)}、早報 {'是' if full else '否'}")
    if blocks:
        send_telegram(blocks)
        send_files([d for item in new["bulletins"] + new["materials"] for d in item.get("downloads", [])])
    else:
        print("沒有新東西，不傳")

    all_keys = {i["key"] for name in ("events", "bulletins", "materials", "discussions") for i in data[name]}
    state["keys"] = state["keys"] | all_keys
    # 早報裡已經列過的快截止作業不用再提醒；只留還沒截止的，紀錄才不會一直變大
    state["reminded"] = {k for k in state["reminded"] | {ev["key"] for ev in reminders}
                         if any(ev["key"] == k and ev["due"] and ev["due"] >= now for ev in data["events"])}
    if auto_digest:
        state["digest_date"] = today
    save_state(state)


if __name__ == "__main__":
    main()
