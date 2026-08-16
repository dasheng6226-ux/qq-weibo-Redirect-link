import os
import random
import re
import time
import datetime
import locale
import json
import logging
import requests
import pyperclip
import pyautogui
import win32gui
import win32con
import win32clipboard
import threading
import queue
import uuid
import struct
import traceback
import sys
import socket
import shutil
from logging.handlers import TimedRotatingFileHandler

from urllib.parse import unquote, urlparse, parse_qs, quote
from io import BytesIO
from PIL import Image
from flask import Flask, request, jsonify
from bs4 import BeautifulSoup, NavigableString, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ================= 获取脚本绝对路径 (核心修复) =================
# 打包为 EXE 后，将配置、日志和浏览器缓存放在 EXE 旁，而不是临时解压目录。
SCRIPT_DIR = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
              else os.path.dirname(os.path.abspath(__file__)))
WEIBO_IMGS_DIR = os.path.join(SCRIPT_DIR, "weibo_imgs")

# ================= 统一配置区 =================

WECHAT_CHAT_NAME = ""
LISTEN_QQ_GROUPS = []
WEBHOOK_PORT = 5000  

APPKEY = ""
SID = ""
PID = ""
UNIONID = ""

CONVERT_API_TAOBAO = "https://api.zhetaoke.com:10001/api/open_gaoyongzhuanlian_tkl_piliang.ashx"
CONVERT_API_JD = "http://api.zhetaoke.com:20000/api/open_gaoyongzhuanlian_tkl_piliang.ashx"
ENABLE_CONVERT_QQ = True  

# 【重要】请在此处填入你的 PC 端微博 Cookie
WEIBO_COOKIE = ''
# 从 Chrome 缓存读取完整的微博 Cookie 组；SUB 仍会显示在 UI 供人工核对。
WEIBO_COOKIE_JAR = {}

WEIBO_USERS = [
    {"uid": "", "history_file": "history_weibo_ids.txt"},
]

SKIP_KEYWORDS = ["加群", "进群", ] 
QQ_BLACKLIST_KEYWORDS = [] 
REPLACE_KEYWORDS = {
    "评论区": " ",
}
MAX_IMAGES_QQ = 3  

# ================= 可视化配置 =================
# 配置保存在脚本同目录，微博会话只从默认 Chrome 缓存读取。
CONFIG_FILE = os.path.join(SCRIPT_DIR, "ecommerce_bot_config.json")
LOG_FILE = os.path.join(SCRIPT_DIR, "ecommerce_bot.log")
WEIBO_BROWSER_PROFILE_DIR = os.path.join(SCRIPT_DIR, "weibo_chrome_profile")
WEIBO_AUTO_REFRESH_COOLDOWN = 10 * 60

def _split_lines(value):
    """将 UI 中的逗号/换行分隔配置转换成去重列表。"""
    if isinstance(value, list):
        values = value
    else:
        values = re.split(r"[,，\n\r]+", str(value or ""))
    return list(dict.fromkeys(str(v).strip() for v in values if str(v).strip()))

def parse_replace_rules(value):
    """解析 UI 的“原文 => 新文”替换规则；右侧为空表示删除。"""
    if isinstance(value, dict):
        return {str(key): str(item) for key, item in value.items() if str(key)}
    rules = {}
    for line in str(value or "").splitlines():
        if "=>" not in line:
            continue
        old, new = line.split("=>", 1)
        old = old.strip()
        if old:
            rules[old] = new.strip()
    return rules

def load_user_config():
    """读取 UI 配置并覆盖默认值；坏配置不会阻止程序启动。"""
    global WECHAT_CHAT_NAME, LISTEN_QQ_GROUPS, WEBHOOK_PORT
    global APPKEY, SID, PID, UNIONID, WEIBO_COOKIE, WEIBO_COOKIE_JAR, WEIBO_USERS
    global SKIP_KEYWORDS, QQ_BLACKLIST_KEYWORDS, REPLACE_KEYWORDS, ENABLE_CONVERT_QQ
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        WECHAT_CHAT_NAME = str(cfg.get("wechat_chat_name", WECHAT_CHAT_NAME)).strip() or WECHAT_CHAT_NAME
        LISTEN_QQ_GROUPS = [int(x) for x in _split_lines(cfg.get("listen_qq_groups", LISTEN_QQ_GROUPS)) if str(x).isdigit()]
        WEBHOOK_PORT = int(cfg.get("webhook_port", WEBHOOK_PORT))
        APPKEY = str(cfg.get("appkey", APPKEY)).strip()
        SID = str(cfg.get("sid", SID)).strip()
        PID = str(cfg.get("pid", PID)).strip()
        UNIONID = str(cfg.get("unionid", UNIONID)).strip()
        WEIBO_COOKIE = str(cfg.get("weibo_cookie", WEIBO_COOKIE)).strip()
        saved_jar = cfg.get("weibo_cookie_jar", {})
        if isinstance(saved_jar, dict):
            WEIBO_COOKIE_JAR = {str(k): str(v) for k, v in saved_jar.items() if k and v}
        SKIP_KEYWORDS = _split_lines(cfg.get("weibo_blacklist", SKIP_KEYWORDS))
        QQ_BLACKLIST_KEYWORDS = _split_lines(cfg.get("qq_blacklist", QQ_BLACKLIST_KEYWORDS))
        REPLACE_KEYWORDS = parse_replace_rules(cfg.get("replace_keywords", REPLACE_KEYWORDS))
        ENABLE_CONVERT_QQ = bool(cfg.get("enable_convert_qq", ENABLE_CONVERT_QQ))
        users = cfg.get("weibo_users", WEIBO_USERS)
        if isinstance(users, list):
            WEIBO_USERS = [{"uid": str(u.get("uid", "")).strip(),
                            "history_file": str(u.get("history_file", "")).strip() or f"history_weibo_ids_{u.get('uid', '')}.txt"}
                           for u in users if isinstance(u, dict) and str(u.get("uid", "")).strip()]
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass

load_user_config()

# ================= 全局实例 =================
app = Flask(__name__)
msg_queue = queue.Queue()  

req_session = requests.Session()
weibo_cookie_refresh_lock = threading.Lock()
last_weibo_cookie_refresh = 0.0
retries = Retry(total=3, backoff_factor=1, status_forcelist=[432, 500, 502, 503, 504])
req_session.mount('http://', HTTPAdapter(max_retries=retries))
req_session.mount('https://', HTTPAdapter(max_retries=retries))

# ======== 新增：将写死的字符串 Cookie 转换为 Session 可自动管理的字典 ========
cookie_dict = {}
for item in WEIBO_COOKIE.split(';'):
    if '=' in item:
        key, value = item.strip().split('=', 1)
        cookie_dict[key] = value
cookie_dict.update(WEIBO_COOKIE_JAR)
req_session.cookies.update(cookie_dict)

# ================= 微信安全发送与图像模块 =================

def find_wechat_window(chat_name):
    def callback(hwnd, windows):
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd):
            windows.append((hwnd, win32gui.GetWindowText(hwnd)))
    windows = []
    win32gui.EnumWindows(callback, windows)
    for hwnd, title in windows:
        if title == chat_name or chat_name in title: return hwnd
    return None

def send_image_to_clipboard_url(image_url):
    try:
        response = requests.get(image_url, timeout=10)
        if response.status_code != 200: return False
        image = Image.open(BytesIO(response.content))
        width, height = image.size
        if width > 800 or height > 700:
            ratio = min(800 / width, 700 / height)
            image = image.resize((int(width * ratio), int(height * ratio)), Image.LANCZOS)
        output = BytesIO()
        image.convert("RGB").save(output, "BMP")
        data = output.getvalue()[14:]
        output.close()
        image.close()

        for _ in range(5):
            try:
                win32clipboard.OpenClipboard()
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32clipboard.CF_DIB, data)
                win32clipboard.CloseClipboard()
                return True
            except Exception: time.sleep(0.2)
        return False
    except Exception as e:
        print(f"❌ URL图片处理失败: {e}")
        return False

def send_image_to_clipboard_local(file_path):
    try:
        abs_path = os.path.abspath(file_path)
        dropfiles_header = struct.pack("IIIII", 20, 0, 0, 0, 1)
        files_str = abs_path + "\0\0"
        files_data = files_str.encode("utf-16le")
        data = dropfiles_header + files_data
        
        for _ in range(5):
            try:
                win32clipboard.OpenClipboard()
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_HDROP, data)
                win32clipboard.CloseClipboard()
                return True
            except Exception: time.sleep(0.2)
        print(f"❌ 系统剪贴板被占用，文件复制失败")
        return False
    except Exception as e:
        print(f"❌ 本地长图文件处理失败: {e}")
        return False


def send_compound_msg_to_wechat(text, local_images=None, url_images=None):
    hwnd = find_wechat_window(WECHAT_CHAT_NAME)
    if not hwnd:
        print(f"❌ 未找到微信窗口: '{WECHAT_CHAT_NAME}'")
        return False

    try:
        if win32gui.IsIconic(hwnd): win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        
        # 【致命Bug终极修复】千万不能按 Esc！改为连按两次 Alt，既能夺回焦点，又能安全释放菜单栏
        pyautogui.press('alt')
        time.sleep(0.1)
        pyautogui.press('alt')
        time.sleep(0.1)

        try: win32gui.SetForegroundWindow(hwnd)
        except: pass
        win32gui.BringWindowToTop(hwnd)
        time.sleep(0.5)

        if local_images:
            for img_path in local_images:
                if img_path and send_image_to_clipboard_local(img_path):
                    pyautogui.hotkey('ctrl', 'v')
                    time.sleep(1.0) 

        if url_images:
            for img_url in url_images[:MAX_IMAGES_QQ]:
                if send_image_to_clipboard_url(img_url):
                    pyautogui.hotkey('ctrl', 'v')
                    time.sleep(0.5)

        if text:
            for _ in range(3):
                try:
                    pyperclip.copy(text)
                    break
                except: time.sleep(0.2)
            time.sleep(0.2)
            pyautogui.hotkey('ctrl', 'v')
            time.sleep(0.5)

        pyautogui.press('enter')
        print(f"✅ 图文合并发送成功！\n")
        return True
    except Exception as e:
        print(f"💥 微信发送失败: {e}")
        return False

def cleanup_old_images(save_dir=WEIBO_IMGS_DIR):
    try:
        if not os.path.exists(save_dir): return
        now = time.time()
        for filename in os.listdir(save_dir):
            filepath = os.path.join(save_dir, filename)
            if os.path.isfile(filepath) and (now - os.path.getmtime(filepath)) > 3600:
                os.remove(filepath)
    except Exception: pass

# ================= QQ 消息处理逻辑 =================

def clean_html_qq(text):
    if not text: return ""
    text = re.sub(r'\[url=\s*', text) if re.search(r'\[url=\s*', text) else text
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\[emoji=[^\]]+\]', '', text)
    return re.sub(r' {2,}', ' ', re.sub(r'\n{3,}', '\n\n', text)).strip()

def detect_platform_qq(content_text):
    if not content_text: return 'unknown'
    if any(kw in content_text for kw in ['jd.com', 'item.jd.com', '京东', 'JD']): return 'jd'
    return 'taobao'

def convert_content_with_api_qq(content_text):
    if not content_text: return None, "内容为空"
    convert_api = CONVERT_API_JD if detect_platform_qq(content_text) == 'jd' else CONVERT_API_TAOBAO
    try:
        resp = requests.get(f"{convert_api}?appkey={APPKEY}&sid={SID}&pid={PID}&unionid={UNIONID}&tkl={quote(content_text, safe='')}", timeout=15)
        if resp.status_code != 200: return None, f"HTTP错误: {resp.status_code}"
        data = resp.json()
        if data.get("status") != 200: return None, data.get("msg", "未知错误")
        converted_content = data.get("content")
        return str(converted_content) if converted_content else None, "未找到content字段"
    except Exception as e: return None, f"转链异常: {e}"

@app.route('/webhook', methods=['POST'])
def napcat_webhook():
    data = request.get_json(silent=True) or {}
    if data.get('post_type') == 'message' and data.get('message_type') == 'group':
        if data.get('group_id') in LISTEN_QQ_GROUPS:
            text_parts, image_urls = [], []
            for seg in data.get('message', []):
                if not isinstance(seg, dict):
                    continue
                if seg.get('type') == 'text': text_parts.append(seg.get('data', {}).get('text', ''))
                elif seg.get('type') == 'image' and seg.get('data', {}).get('url'):
                    image_urls.append(seg['data']['url'])
            msg_queue.put({'source': 'qq', 'text': ''.join(text_parts), 'images': image_urls})
    return jsonify({"status": "ok"})

# ================= 微博底层处理逻辑 =================

def get_pc_headers(uid=""):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        # 'Cookie': WEIBO_COOKIE,  <-- 【彻底删除这一行】，让 requests.Session 自己带上 Cookie
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'X-Requested-With': 'XMLHttpRequest'
    }
    if xsrf_token := req_session.cookies.get('XSRF-TOKEN'):
        headers['X-XSRF-TOKEN'] = xsrf_token
    headers['Referer'] = f'https://weibo.com/u/{uid}' if uid else 'https://weibo.com/'
    return headers

def weibo_time_to_timestamp(timestr):
    try:
        locale.setlocale(locale.LC_TIME, "C")
        return int(datetime.datetime.strptime(timestr, "%a %b %d %H:%M:%S %z %Y").timestamp())
    except: return int(time.time())

def load_history_ids(history_file):
    if not os.path.exists(history_file): return set()
    with open(history_file, "r", encoding="utf-8") as f: return set(line.strip() for line in f if line.strip())

def save_history_id(mid, history_file):
    with open(history_file, "a", encoding="utf-8") as f: f.write(str(mid) + "\n")

def get_watermark_free_url(url):
    if not url: return ''
    return re.sub(r'/(mw\d+|orj\d+|bmiddle|large|mw2000|largest|square)/', '/oslarge/', url)

def extract_pics_from_mblog(mblog):
    pics = []
    if 'pic_ids' in mblog and 'pic_infos' in mblog:
        for pid in mblog.get('pic_ids', []):
            url_img = mblog['pic_infos'].get(pid, {}).get('largest', {}).get('url') or mblog['pic_infos'].get(pid, {}).get('original', {}).get('url')
            if url_img: pics.append(get_watermark_free_url(url_img))
    elif 'pics' in mblog:
        for p in mblog.get('pics', []):
            url_img = p.get('large', {}).get('url') or p.get('url')
            if url_img: pics.append(get_watermark_free_url(url_img))
    return list(dict.fromkeys(pics))

def download_images_weibo(img_urls, save_dir=WEIBO_IMGS_DIR):
    os.makedirs(save_dir, exist_ok=True) 
    local_files = []
    for idx, url in enumerate(img_urls):
        try:
            unique_id = uuid.uuid4().hex[:8]
            fname = os.path.join(save_dir, f"img_{unique_id}_{idx}.jpg")
            r = req_session.get(url, headers={'Referer': 'https://weibo.com/'}, timeout=10)
            if r.status_code == 200:
                with open(fname, 'wb') as f: f.write(r.content)
                local_files.append(os.path.abspath(fname))
        except: pass
    return local_files

def merge_images_vertically(image_paths, save_dir=WEIBO_IMGS_DIR):
    if not image_paths: return None
    if len(image_paths) == 1: return image_paths[0]
    try:
        imgs = [Image.open(p).convert('RGB') for p in image_paths]
        max_width = max(img.width for img in imgs)
        total_height = sum(img.height for img in imgs)
        merged_img = Image.new('RGB', (max_width, total_height), (255, 255, 255))
        y_offset = 0
        for img in imgs:
            x_offset = (max_width - img.width) // 2
            merged_img.paste(img, (x_offset, y_offset))
            y_offset += img.height
            
        unique_id = uuid.uuid4().hex[:12]
        merged_path = os.path.join(save_dir, f"merged_{unique_id}.jpg")
        merged_img.save(merged_path, quality=85)
        
        for img in imgs: img.close()
        for p in image_paths:
            try: os.remove(p)
            except: pass
        return merged_path
    except: return image_paths[0] if image_paths else None

def fetch_weibo_by_mid(mid):
    try:
        resp = req_session.get(f'https://weibo.com/ajax/statuses/show?id={mid}', headers=get_pc_headers(), timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if 'text' in data: return data
            if 'data' in data and 'text' in data['data']: return data['data']
    except: pass
    return None

def fetch_author_comments(mid, uid):
    """【核心修复】兼容微博API突变的数据结构，强制按时间顺序拉取评论"""
    params = {
        "is_reload": 1, 
        "id": mid, 
        "is_show_bulletin": 2,
        "is_mix": 0, 
        "count": 50, 
        "uid": uid,
        "flow": 0  
    }
    try:
        resp = req_session.get("https://weibo.com/ajax/statuses/buildComments", headers=get_pc_headers(uid), params=params, timeout=10)
        
        if resp.status_code != 200:
            return []
            
        data = resp.json()
        if not isinstance(data, dict) or data.get('ok') != 1:
            return []
            
        # 【核心修复：柔性解析】判断微博返回的 data 到底是个字典还是列表
        raw_data = data.get('data')
        if isinstance(raw_data, dict):
            comments = raw_data.get('data', [])
        elif isinstance(raw_data, list):
            comments = raw_data
        else:
            comments = []
            
        author_comments = []
        
        for c in comments:
            # 确保 c 也是一个字典，防止内部结构继续抽风
            if isinstance(c, dict):
                c_uid = str(c.get('user', {}).get('id', ''))
                if c_uid == str(uid):
                    author_comments.append(c)
                
        # 按时间从旧到新排序
        author_comments.sort(key=lambda x: weibo_time_to_timestamp(x.get('created_at', '')))
        return author_comments
        
    except Exception as e: 
        print(f"    [异常] 拉取评论时发生报错: {e}")
        return []

def get_long_weibo_text(mblogid, uid=""):
    try:
        resp = req_session.get(f'https://weibo.com/ajax/statuses/longtext?id={mblogid}', headers=get_pc_headers(uid), timeout=10)
        if resp.json().get('ok') == 1:
            long_data = resp.json().get('data', {})
            text_html = long_data.get('longTextContent', '') or long_data.get('text', '')
            for u in long_data.get('url_struct', []):
                short_url = u.get('short_url')
                if short_url:
                    url_title = u.get('url_title', '')
                    if url_title and f"O{url_title}" in text_html: text_html = text_html.replace(f"O{url_title}", f" {short_url} ")
                    elif url_title and url_title in text_html: text_html = text_html.replace(url_title, f" {short_url} ")
                    elif short_url not in text_html: text_html += f" {short_url} "
            return text_html
    except: pass
    return ''

def get_full_html(mblog, uid=""):
    text_html = mblog.get('longTextContent', '') or mblog.get('text', '')
    is_long = mblog.get('isLongText', False)
    if not is_long and ('展开</' in text_html or '全文</' in text_html): is_long = True
    if is_long:
        if full_text := get_long_weibo_text(mblog.get('mblogid') or mblog.get('id'), uid): return full_text
    return text_html

def fetch_latest_weibos(uid, max_count=5):
    try:
        resp = req_session.get("https://weibo.com/ajax/statuses/mymblog", headers=get_pc_headers(uid), params={"uid": uid, "page": 1, "feature": 0}, timeout=15)
        if resp.status_code != 200:
            print(f"  ⚠️ 微博 API HTTP {resp.status_code}")
            return None
        try:
            payload = resp.json()
        except ValueError:
            print("  ⚠️ 微博 API 返回的不是 JSON，登录会话可能已失效。")
            return None
        if payload.get('ok') != 1:
            print(f"  ⚠️ 微博 API 拒绝请求：{payload.get('msg') or payload.get('message') or payload}")
            return None
        cards = payload.get('data', {}).get('list', [])
        weibos = []
        for mblog in cards:
            mblog['skip_retweet'] = False
            if retweeted_status := mblog.get('retweeted_status'):
                rt_uid, rt_ts = str(retweeted_status.get('user', {}).get('id', '')), weibo_time_to_timestamp(retweeted_status.get('created_at', ''))
                curr_ts = weibo_time_to_timestamp(mblog.get('created_at', ''))
                if rt_uid == str(uid) and (curr_ts - rt_ts) <= 120: mblog['skip_retweet'] = True
            weibos.append(mblog)
            if len(weibos) >= max_count: break
        return weibos
    except Exception as exc:
        print(f"  ⚠️ 获取微博列表异常：{exc}")
        return None

def is_ecommerce_link_weibo(url):
    """判断是否为标准电商链接（补充了 starlink 等官方链路）"""
    targets = ['jd.com', 'taobao.com', 'tmall.com', 'tb.cn', 'uland.taobao.com', 'dpurl.cn', 'meituan.com', 'waimai.meituan.com', 'ele.me', 'kfc.com.cn', 'yangkeduo.com', 'pinduoduo.com', 's.click.taobao.com', 'starlink']
    try: return any(t in urlparse(url).netloc for t in targets) if urlparse(url).netloc else False
    except: return any(t in url for t in targets) if url else False

def is_weibo_article_link(url): return bool(re.search(r'weibo\.(com|cn)/(status/|detail/|\d+/)', url))
def extract_mid(url):
    m = re.search(r'/(?:status|detail)/([a-zA-Z0-9]+)', url) or re.search(r'/\d+/([a-zA-Z0-9]+)', url)
    return m.group(1) if m else None

def get_final_url_weibo(initial_url):
    """解析真实重定向链接，强拆微博 sinaurl 外壳放行第三方福利链"""
    if not initial_url: return ""
    current_url = initial_url.strip()
    pc_headers = get_pc_headers()
    for _ in range(5):
        try:
            qs = parse_qs(urlparse(current_url).query)
            for key in ['url', 'u', 'target', 'jump', 'to']:
                if key in qs:
                    decoded = unquote(qs[key][0])
                    decoded = 'https:' + decoded if decoded.startswith('//') else ('https://' + decoded.lstrip('/') if not decoded.startswith('http') else decoded)
                    if is_ecommerce_link_weibo(decoded) or is_weibo_article_link(decoded): return decoded
                    
                    # 【核心修复1】只要是提取出的纯外部链接（如 carben.me），直接解绑放行！
                    if "weibo" not in decoded and "sina" not in decoded:
                        return decoded
        except: pass

        if is_ecommerce_link_weibo(current_url): return current_url

        try:
            resp = req_session.get(current_url, headers=pc_headers, timeout=8, allow_redirects=True)
            current_url = resp.url 
            
            if "passport.weibo.com" in current_url:
                qs = parse_qs(urlparse(current_url).query)
                if 'url' in qs:
                    extracted = unquote(qs['url'][0])
                    extracted = 'https://' + extracted.lstrip('/') if not extracted.startswith('http') else extracted
                    if is_ecommerce_link_weibo(extracted) or is_weibo_article_link(extracted): return extracted
                    # 【核心修复1补充】同理，登录墙里拦截的第三方链接也直接放行
                    if "weibo" not in extracted and "sina" not in extracted: return extracted
                    current_url = extracted
                break 
            
            if is_ecommerce_link_weibo(current_url) or is_weibo_article_link(current_url): return current_url
            
            html_content = resp.text
            patterns = [
                r'var\s+url\s*=\s*[\'"](https?://[^\'"]+\.(tmall|taobao|jd)\.com[^\'"]*)[\'"]',
                r'window\.location\.href\s*=\s*[\'"](https?://[^\'"]+\.(tmall|taobao|jd)\.com[^\'"]*)[\'"]',
                r'<meta[^>]*url=(https?://[^>"]+\.(tmall|taobao|jd)\.com[^>"]*)',
                r'<a[^>]*href="?(https?://[^>"]+\.(tmall|taobao|jd)\.com[^>"]*)"?',
                r'"jump_url"\s*:\s*"([^"]+)"'
            ]
            found_in_html = False
            for p in patterns:
                m = re.search(p, html_content, re.IGNORECASE)
                if m:
                    extracted = m.group(1).replace('\\/', '/')
                    if '\\u' in extracted: extracted = extracted.encode('utf-8').decode('unicode_escape')
                    if is_ecommerce_link_weibo(extracted): return extracted
                    current_url = extracted
                    found_in_html = True
                    break
            
            if not found_in_html:
                clean_text = unquote(html_content).replace('\\/', '/')
                for u in re.findall(r'(https?://[a-zA-Z0-9\-\.\/\?\&\=\%\_\#]+)', clean_text):
                    if is_ecommerce_link_weibo(u): return u
                break
        except: break
    return current_url

def get_better_url(mblog):
    page_info = mblog.get('page_info', {})
    if page_info:
        if url_ori := (page_info.get('url_ori') or page_info.get('page_url')):
            if is_ecommerce_link_weibo(res := get_final_url_weibo(url_ori)): return res
    return ""

def weibo_keep_alive_worker():
    """微博心跳保活线程：每隔15分钟摸一下首页，防止闲置掉线"""
    print("💓 微博心跳保活模块已启动...")
    while True:
        try:
            # 随机休眠 10 到 15 分钟
            time.sleep(random.uniform(600, 900))
            resp = req_session.get("https://weibo.com", headers=get_pc_headers(), timeout=10)
            if resp.status_code == 200:
                print(f"[{time.strftime('%H:%M:%S')}] 💓 微博心跳保活请求成功！")
            else:
                print(f"[{time.strftime('%H:%M:%S')}] ⚠️ 微博心跳响应异常: {resp.status_code}")
        except Exception as e:
            print(f"💥 微博心跳保活失败: {e}")


def purify_taobao_url(url):
    """淘宝/天猫纯商品链接净化器：精准剥离尾巴，避免报错"""
    if not url: return url
    if "detail.tmall.com/item.htm" in url or "item.taobao.com/item.htm" in url:
        try:
            qs = parse_qs(urlparse(url).query)
            if 'id' in qs:
                return f"https://item.taobao.com/item.htm?id={qs['id'][0]}"
        except Exception:
            pass
    return url


def _extract_taokouling(value):
    """只从 API 文本中提取明确的淘口令，避免把任意长字符串当成口令。"""
    text = str(value or "").strip()
    if not text or text.startswith("http"):
        return ""
    patterns = (
        r"￥[^￥\s]{6,40}￥",
        r"[＄$!/][A-Za-z0-9]{6,30}[＄$!/].*?",
        r"\(([A-Za-z0-9]{8,30})\)",
        r"([A-Za-z0-9]{4,30}\s+CZ\d+\s+[A-Za-z0-9_-]{3,40})",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            token = match.group(0).strip()
            return token if token.startswith(("￥", "(", "＄", "$", "!", "/")) else f"({token})"
    return ""

def _iter_response_values(value):
    """递归读取 API 可能嵌套在 result/data 下的文本字段。"""
    if isinstance(value, dict):
        for item in value.values():
            yield from _iter_response_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_response_values(item)
    elif isinstance(value, str):
        yield value

def _is_jd_short_url(url):
    try:
        host = urlparse(url).netloc.lower()
        return host in {"u.jd.com", "3.cn", "j.jd.com", "jdl.com"}
    except Exception:
        return False

def _extract_jd_short_url(payload):
    """从折淘客不同版本返回结构中提取京东短链。"""
    priority_keys = ("shortURL", "short_url", "shortUrl", "dwz", "url_short")
    candidates = []
    if isinstance(payload, dict):
        for key in priority_keys:
            if payload.get(key):
                candidates.append(str(payload[key]))
    candidates.extend(_iter_response_values(payload))
    for text in candidates:
        for url in re.findall(r"https?://[^\s\"'<>]+", text.replace("\\/", "/")):
            if _is_jd_short_url(url):
                return url
    return ""

def _get_first_http_url(payload):
    for text in _iter_response_values(payload):
        match = re.search(r"https?://[^\s\"'<>]+", text.replace("\\/", "/"))
        if match:
            return match.group(0)
    return ""

def ztk_convert_single_weibo(url):
    """将微博中的商品链接转换为口令；淘宝失败时禁止回退成长链接。"""
    if not url:
        return ""
    url = purify_taobao_url(str(url).strip())
    if not is_ecommerce_link_weibo(url):
        return url
    host = urlparse(url).netloc.lower()
    is_taobao = any(host == d or host.endswith("." + d) for d in ("taobao.com", "tmall.com", "tb.cn")) or \
        any(x in host for x in ("starlink", "uland.taobao.com", "s.click.taobao.com"))
    is_jd = host == "jd.com" or host.endswith(".jd.com")
    api_url = CONVERT_API_JD if is_jd else CONVERT_API_TAOBAO
    try:
        # requests 会自行进行表单编码；此前先 quote 导致部分链接被双重编码。
        resp = req_session.post(api_url, data={"appkey": APPKEY, "unionId": UNIONID,
                                                "pid": PID, "sid": SID, "tkl": url}, timeout=8)
        data = resp.json()
        if str(data.get("status", "200")) != "200" or "抱歉" in str(data) or "不能为空" in str(data):
            data = {}
        if is_taobao:
            for key in ("taokouling", "tkl", "result_tkl", "content", "model"):
                token = _extract_taokouling(data.get(key))
                if token:
                    return token
            target_url = data.get("result_url") or data.get("short_url") or url
            try:
                fallback = req_session.get("https://api.zhetaoke.com:10001/api/open_tkl_create.ashx",
                                           params={"appkey": APPKEY, "url": target_url, "text": "活动福利"}, timeout=5).json()
                for key in ("content", "tkl", "model", "taokouling"):
                    token = _extract_taokouling(fallback.get(key))
                    if token:
                        return token
            except Exception:
                pass
            # 关键保证：不再返回淘宝/天猫长链接或短链。
            return "[淘宝转链失败：未获取到淘口令]"
        # 京东必须输出短链：先找转链接口明确返回的短链，再将联盟长链交给短链接口。
        if short_url := _extract_jd_short_url(data):
            return short_url
        affiliate_url = (data.get("result_url") or data.get("click_url") or
                         data.get("coupon_url") or _get_first_http_url(data) or url)
        shortened = ztk_short_url_weibo(affiliate_url)
        if _is_jd_short_url(shortened) or (shortened.startswith("http") and len(shortened) <= 80):
            return shortened
        return "[京东转链失败：未获取到短链]"
    except Exception:
        return "[商品转链失败]" if is_taobao else "[京东转链失败]"

def ztk_short_url_weibo(long_url):
    try:
        payload = req_session.get("https://api.zhetaoke.com:10001/api/open_dwz.ashx",
                                  params={"appkey": APPKEY, "url": long_url}, timeout=5).json()
        for key in ("short_url", "shortURL", "shortUrl", "url"):
            if isinstance(payload, dict) and isinstance(payload.get(key), str) and payload[key].startswith("http"):
                return payload[key]
        for candidate in _iter_response_values(payload):
            match = re.search(r"https?://[^\s\"'<>]+", candidate)
            if match and len(match.group(0)) <= 80:
                return match.group(0)
    except Exception:
        pass
    return long_url

def extract_text_and_links_ordered(html):
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup.find_all('span', class_='surl-text'): tag.decompose()
    for tag in soup.find_all('span', class_='url-icon'): tag.decompose()
    for a in soup.find_all('a'):
        if a.text and any(w in a.text for w in ["展开", "全文"]): a.decompose()

    skip_words = ["全文", "视频", "微博视频", "分享图片", "网页链接", "收起", "c", "d", "O网页链接", "展开"]
    result, url_pattern = [], re.compile(r'(https?://[^\s\u4e00-\u9fa5"\'<>]+)')

    for elem in soup.descendants:
        if isinstance(elem, Tag) and elem.name == 'a':
            if href := elem.get('href', ''): result.append({'type': 'link', 'href': href})
        elif isinstance(elem, NavigableString):
            if not (text := str(elem).strip()): 
                result.append({'type': 'text', 'text': str(elem)})
                continue
            if text in skip_words or 'a' in [p.name for p in elem.parents]: continue
            matches = list(url_pattern.finditer(str(elem)))
            if not matches: result.append({'type': 'text', 'text': str(elem)})
            else:
                last_end = 0
                for m in matches:
                    if pre_text := str(elem)[last_end:m.start()]: result.append({'type': 'text', 'text': pre_text})
                    result.append({'type': 'link', 'href': m.group(1)})
                    last_end = m.end()
                if post_text := str(elem)[last_end:]: result.append({'type': 'text', 'text': post_text})
    return result


def process_comment_content(html, author_uid, better_url=""):
    items = extract_text_and_links_ordered(html.replace('<br />', '\n').replace('<br>', '\n').replace('<br/>', '\n'))
    lines = []
    for item in items:
        if item['type'] == 'text':
            lines.append(item['text'])
        elif item['type'] == 'link':
            href = item['href']

            m_topic = re.search(r'(%23.+?%23|#[^#]+#)', href)
            if m_topic and ("weibo" in href or "huati" in href or href.startswith('/')):
                lines.append(unquote(m_topic.group(1)))
                continue

            if "weibo.cn/search" in href or href.startswith("/n/"): continue

            final_link = get_final_url_weibo(href)
            if not is_ecommerce_link_weibo(final_link) and better_url and (
                    "apps.weibo.com" in href or "shop.sc.weibo.com" in href):
                final_link = better_url

            if is_weibo_article_link(final_link):
                if sub_mblog := fetch_weibo_by_mid(extract_mid(final_link)):
                    sub_text, _ = process_weibo_content(sub_mblog, author_uid, 1, set(), False)
                    # 智能判断换行，且不再附带多余尾随换行
                    prefix = "" if (not lines or lines[-1].endswith('\n')) else "\n"
                    lines.append(f"{prefix}══【引用1内容】══\n{sub_text.strip()}\n══【引用1结束】══")
                else:
                    prefix = "" if (not lines or lines[-1].endswith('\n')) else "\n"
                    lines.append(f"{prefix}{final_link}")
            else:
                converted = ztk_convert_single_weibo(final_link)
                if converted:
                    prefix = "" if (not lines or lines[-1].endswith('\n')) else "\n"
                    if converted.startswith('(') or converted.startswith('￥'):
                        lines.append(f"{prefix}{converted}")
                    else:
                        lines.append(f"{prefix}{converted} ")

    # 彻底去掉暴君正则，完美保留原始排版
    return ''.join(lines).strip()


def process_weibo_content(mblog, author_uid, level=0, visited_mids=None, skip_retweet=False):
    if visited_mids is None: visited_mids = set()
    lines, images = [], []
    if mid := str(mblog.get('id', '')): visited_mids.add(mid)

    better_url = mblog.get('better_url')
    if not better_url and mblog.get('page_info'):
        if ori := (mblog['page_info'].get('url_ori') or mblog['page_info'].get('page_url')):
            if is_ecommerce_link_weibo(res := get_final_url_weibo(ori)): better_url = res

    items = extract_text_and_links_ordered(
        get_full_html(mblog).replace('<br />', '\n').replace('<br>', '\n').replace('<br/>', '\n'))
    for item in items:
        if item['type'] == 'text':
            lines.append(item['text'])
        elif item['type'] == 'link':
            href = item['href']

            m_topic = re.search(r'(%23.+?%23|#[^#]+#)', href)
            if m_topic and ("weibo" in href or "huati" in href or href.startswith('/')):
                lines.append(unquote(m_topic.group(1)))
                continue

            if "weibo.cn/search" in href or href.startswith("/n/"): continue

            final_link = get_final_url_weibo(href)
            if not is_ecommerce_link_weibo(final_link) and better_url and (
                    "apps.weibo.com" in href or "shop.sc.weibo.com" in href):
                final_link = better_url

            if is_weibo_article_link(final_link):
                if (sub_mid := extract_mid(final_link)) and sub_mid not in visited_mids:
                    if sub_mblog := fetch_weibo_by_mid(sub_mid):
                        sub_text, sub_imgs = process_weibo_content(sub_mblog, author_uid, level + 1, visited_mids,
                                                                   False)
                        images.extend(sub_imgs)
                        prefix = "" if (not lines or lines[-1].endswith('\n')) else "\n"
                        lines.append(f"{prefix}══【引用{level + 1}内容】══\n{sub_text.strip()}\n══【引用{level + 1}结束】══")
                    else:
                        prefix = "" if (not lines or lines[-1].endswith('\n')) else "\n"
                        lines.append(f"{prefix}{final_link}")
            else:
                converted = ztk_convert_single_weibo(final_link)
                if converted:
                    prefix = "" if (not lines or lines[-1].endswith('\n')) else "\n"
                    if converted.startswith('(') or converted.startswith('￥'):
                        lines.append(f"{prefix}{converted}")
                    else:
                        lines.append(f"{prefix}{converted} ")

    if not skip_retweet and (retweeted_status := mblog.get('retweeted_status')):
        if (rt_mid := str(retweeted_status.get('id', ''))) and rt_mid not in visited_mids:
            rt_text, rt_imgs = process_weibo_content(retweeted_status, author_uid, level + 1, visited_mids, False)
            images.extend(rt_imgs)
            prefix = "" if (not lines or lines[-1].endswith('\n')) else "\n"
            lines.append(f"{prefix}══【详细内容】══\n{rt_text.strip()}")

    images.extend(extract_pics_from_mblog(mblog))

    # 彻底去掉暴君正则，完美保留原始排版
    return ''.join(lines).strip(), images

# ================= 后台工作线程 =================

def process_message_queue_worker():
    print("🚀 核心发送队列已启动，随时准备合并下发指令...")
    while True:
        try:
            msg_data = msg_queue.get()
            source = msg_data.get('source')

            if source == 'qq':
                raw_text, image_urls = msg_data.get('text', ''), msg_data.get('images', [])
                content_clean = clean_html_qq(raw_text)

                if any(kw in content_clean for kw in QQ_BLACKLIST_KEYWORDS): continue
                if not content_clean and not image_urls: continue

                final_text = content_clean
                if ENABLE_CONVERT_QQ and content_clean:
                    if converted_text := convert_content_with_api_qq(content_clean)[0]: final_text = converted_text

                print(f"  -> [队列执行] 准备发送 QQ 来源图文...")
                send_compound_msg_to_wechat(final_text, url_images=image_urls)
                time.sleep(3) 

            elif source == 'weibo':
                print("  -> [队列执行] 准备发送微博来源图文...")
                send_compound_msg_to_wechat(msg_data.get('text', ''), local_images=msg_data.get('local_images', []))
                time.sleep(3)
                
        except Exception as e:
            print(f"💥 队列处理异常: {e}")
            traceback.print_exc()

def weibo_monitor_worker():
    print("👀 微博雷达已开启，正在扫描监控列表...")
    while True:
        try:
            cleanup_old_images()

            for user in WEIBO_USERS:
                uid = user["uid"]
                if not uid:
                    continue
                # =================== 核心修复：绑定绝对路径 ===================
                history_file = os.path.join(SCRIPT_DIR, user["history_file"])
                history_ids = load_history_ids(history_file)
                
                # 打印日志暴露幽灵文件的位置
                print(f"\n[{time.strftime('%H:%M:%S')}] 📂 正在读取历史记录文件: {history_file}")
                print(f"  -> 当前文件内已有 {len(history_ids)} 条记录。")
                
                weibos = fetch_latest_weibos(uid, max_count=5)
                if not weibos:
                    print("  ⚠️ 微博 API 未获取到内容，准备检查浏览器缓存会话。")
                    if refresh_weibo_cookie_from_browser():
                        weibos = fetch_latest_weibos(uid, max_count=5)
                    if not weibos:
                        print("  ⚠️ Cookie 自动恢复失败，等待下一次检查。")
                        continue
                    
                weibos.sort(key=lambda x: weibo_time_to_timestamp(x.get('created_at', '')))
                print(f"  🔍 成功获取到 {len(weibos)} 条最新微博。")
                
                for idx, mblog in enumerate(weibos):
                    mid = str(mblog.get('id'))
                    print(f"  -> 正在诊断第 {idx+1} 条 (mid: {mid})...")
                    
                    better_url = mblog.get('better_url') or get_better_url(mblog)
                    
                    for comment in fetch_author_comments(mid, uid):
                        if (cid := str(comment.get('id'))) not in history_ids:
                            print(f"    ★ 发现新评论 (cid: {cid})，准备提取...")
                            c_content = process_comment_content(comment.get('text', ''), uid, better_url)
                            for old, new in REPLACE_KEYWORDS.items(): c_content = c_content.replace(old, new)
                            c_content = "⬆️内容补充：\n" + re.sub(r'https?://[^\s]+?\.(jpg|png|gif)', '', c_content, flags=re.IGNORECASE)
                            
                            c_local_images = []
                            c_img_url = (comment.get('pic', {}).get('largest', {}).get('url') or comment.get('pic', {}).get('original', {}).get('url') or comment.get('pic', {}).get('url'))
                            if c_img_url and (imgs := download_images_weibo([get_watermark_free_url(c_img_url)])): 
                                c_local_images = imgs
                                
                            msg_queue.put({'source': 'weibo', 'text': c_content, 'local_images': c_local_images})
                            save_history_id(cid, history_file)
                            history_ids.add(cid)
                    
                    if mid in history_ids: 
                        print(f"    - 该微博已存在于历史记录中，跳过。")
                        continue
                        
                    text_clean = re.sub('<.*?>', '', get_full_html(mblog))
                    if any(kw in text_clean for kw in SKIP_KEYWORDS):
                        print(f"    - 该微博包含过滤关键词，已被拦截抛弃。")
                        save_history_id(mid, history_file)
                        history_ids.add(mid)
                        continue
                    
                    print(f"\n    ★ 捕获新正文，准备组装发送: {mid}")
                    content, img_urls = process_weibo_content(mblog, author_uid=uid, level=0, visited_mids=set(), skip_retweet=mblog.get('skip_retweet', False))
                    for old, new in REPLACE_KEYWORDS.items(): content = content.replace(old, new)
                    content = re.sub(r'https?://[^\s]+?\.(jpg|png|gif)', '', content, flags=re.IGNORECASE)
                    
                    local_images = []
                    if img_urls and (img_files := download_images_weibo(img_urls)):
                        if merged := merge_images_vertically(img_files): local_images = [merged]
                    
                    msg_queue.put({'source': 'weibo', 'text': content, 'local_images': local_images})
                    save_history_id(mid, history_file)
                    history_ids.add(mid)

        except Exception as e: 
            print(f"💥 微博扫描异常崩溃: {e}")
            traceback.print_exc()
            
        time.sleep(random.uniform(5, 10))

# ================= UI 与服务入口 =================

def update_cookie(cookie, cookie_jar=None):
    """更新 SUB 和完整微博 Cookie 组，供网页 AJAX 接口复用。"""
    global WEIBO_COOKIE, WEIBO_COOKIE_JAR
    match = re.search(r"(?:^|;)\s*SUB=([^;]+)", cookie or "")
    if not match:
        return False
    WEIBO_COOKIE = f"SUB={match.group(1)};"
    # 手工粘贴的完整 Cookie 与浏览器取回的 Cookie 都会被保留。
    for item in str(cookie or "").split(';'):
        if '=' in item:
            key, value = item.strip().split('=', 1)
            if key and value:
                WEIBO_COOKIE_JAR[key] = value
    if isinstance(cookie_jar, dict):
        WEIBO_COOKIE_JAR.update({str(k): str(v) for k, v in cookie_jar.items() if k and v})
    WEIBO_COOKIE_JAR['SUB'] = match.group(1)
    req_session.cookies.clear()
    req_session.cookies.update(WEIBO_COOKIE_JAR)
    return True

def verify_weibo_session(cookie_jar):
    """在报告登录成功前，确认 Cookie 能访问实际使用的微博 AJAX 接口。"""
    uid = next((str(item.get('uid', '')).strip() for item in WEIBO_USERS if item.get('uid')), '')
    if not uid:
        return False, "未配置微博用户 UID"
    probe = requests.Session()
    probe.cookies.update(cookie_jar)
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'X-Requested-With': 'XMLHttpRequest',
        'Referer': f'https://weibo.com/u/{uid}',
    }
    if xsrf_token := cookie_jar.get('XSRF-TOKEN'):
        headers['X-XSRF-TOKEN'] = xsrf_token
    try:
        response = probe.get('https://weibo.com/ajax/statuses/mymblog', headers=headers,
                             params={'uid': uid, 'page': 1, 'feature': 0}, timeout=15)
        if 'passport.weibo.com' in response.url:
            return False, "微博仍跳转到登录页"
        try:
            payload = response.json()
        except ValueError:
            return False, f"微博返回非 JSON（HTTP {response.status_code}）"
        return payload.get('ok') == 1, payload.get('msg') or payload.get('message') or "微博接口拒绝该会话"
    except requests.RequestException as exc:
        return False, f"验证微博会话时网络异常：{exc}"

def copy_default_chrome_weibo_cache(default_user_data_dir):
    """复制默认 Chrome 的登录资料到自动化副本，避免 Selenium 锁定日常浏览器。"""
    files_to_copy = (
        "Local State",
        os.path.join("Default", "Network", "Cookies"),
        os.path.join("Default", "Network", "Cookies-journal"),
        os.path.join("Default", "Preferences"),
        os.path.join("Default", "Secure Preferences"),
    )
    copied = 0
    for relative_path in files_to_copy:
        source = os.path.join(default_user_data_dir, relative_path)
        destination = os.path.join(WEIBO_BROWSER_PROFILE_DIR, relative_path)
        if not os.path.isfile(source):
            continue
        try:
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            shutil.copy2(source, destination)
            copied += 1
        except OSError:
            # Chrome 未完全退出时个别锁定文件可能无法复制，其余 Cookie 仍可尝试使用。
            continue
    return copied

def create_weibo_driver():
    """创建使用持久化缓存目录的浏览器，供登录和 Cookie 自动刷新共用。"""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
    except ImportError as exc:
        raise RuntimeError("读取 Chrome 缓存需要安装 selenium：python -m pip install selenium") from exc
    default_user_data_dir = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data")
    # 读取用户日常 Chrome 的默认资料，但不直接让 Selenium 锁定这个资料目录。
    use_default_profile = os.path.isdir(default_user_data_dir)
    system_chrome_candidates = [
        shutil.which("chrome"),
        os.path.join(os.environ.get("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
    ]
    bundled_chrome = os.path.join(SCRIPT_DIR, "chrome", "chrome.exe")
    # 读取默认缓存时优先系统 Chrome，确保与默认缓存的加密上下文一致。
    chrome_candidates = system_chrome_candidates + [bundled_chrome] if use_default_profile \
        else [bundled_chrome] + system_chrome_candidates
    chrome_path = next((path for path in chrome_candidates if path and os.path.isfile(path)), "")
    if not chrome_path:
        raise RuntimeError("未检测到 Chrome 浏览器。请先安装 Google Chrome 后再获取 SUB Cookie。")
    if use_default_profile:
        copied = copy_default_chrome_weibo_cache(default_user_data_dir)
        print(f"🌐 已复制默认 Chrome 缓存文件 {copied} 项到自动化副本。")
    os.makedirs(WEIBO_BROWSER_PROFILE_DIR, exist_ok=True)
    options = Options()
    options.binary_location = chrome_path
    options.add_argument(f"--user-data-dir={WEIBO_BROWSER_PROFILE_DIR}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--disable-notifications")
    bundled_driver = os.path.join(SCRIPT_DIR, "chromedriver.exe")
    try:
        if os.path.isfile(bundled_driver):
            print("🌐 正在使用默认 Chrome 缓存副本与 ChromeDriver…")
            return webdriver.Chrome(service=Service(bundled_driver), options=options)
        print("🌐 正在启动默认 Chrome 缓存副本（首次运行可能需要联网准备匹配的浏览器驱动）…")
        return webdriver.Chrome(options=options)
    except Exception as exc:
        if "chrome instance exited" in str(exc).lower():
            raise RuntimeError("自动化 Chrome 无法启动。请关闭脚本此前打开的浏览器后重试；登录完成后请等待程序提示登录成功，由程序自动关闭浏览器。") from exc
        raise

def get_weibo_cookie_from_default_cache(timeout=30):
    """打开浏览器缓存副本，读取当前默认 Chrome 已登录微博会话的完整 Cookie。"""
    driver = None
    try:
        driver = create_weibo_driver()
        driver.get("https://weibo.com/")
        deadline = time.time() + timeout
        while time.time() < deadline:
            cookies = {c.get("name"): c.get("value") for c in driver.get_cookies()}
            if cookies.get("SUB"):
                update_cookie(f"SUB={cookies['SUB']};", cookies)
                write_user_config()
                print("✅ 已从默认 Chrome 缓存获取并保存 SUB，浏览器将自动关闭。")
                return f"SUB={cookies['SUB']};"
            time.sleep(1)
        raise RuntimeError("默认 Chrome 缓存中未读取到 SUB。请先用 Chrome 手动登录 weibo.com 后再重试。")
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

def refresh_weibo_cookie_from_browser(timeout=30):
    """后台检测到微博接口失败时，从默认 Chrome 缓存自动更新 Cookie。"""
    global last_weibo_cookie_refresh
    now = time.time()
    if now - last_weibo_cookie_refresh < WEIBO_AUTO_REFRESH_COOLDOWN:
        return False
    if not weibo_cookie_refresh_lock.acquire(blocking=False):
        return False
    last_weibo_cookie_refresh = now
    try:
        print("🔄 微博会话失效，正在使用浏览器缓存自动刷新 Cookie…")
        get_weibo_cookie_from_default_cache(timeout)
        print("✅ 已自动刷新微博 Cookie，后台将继续获取微博内容。")
        return True
    except Exception as exc:
        print(f"⚠️ 自动刷新微博 Cookie 失败：{exc}")
        return False
    finally:
        weibo_cookie_refresh_lock.release()

def write_user_config(extra=None):
    cfg = {
        "wechat_chat_name": WECHAT_CHAT_NAME,
        "listen_qq_groups": LISTEN_QQ_GROUPS,
        "webhook_port": WEBHOOK_PORT,
        "appkey": APPKEY, "sid": SID, "pid": PID, "unionid": UNIONID,
        "weibo_cookie": WEIBO_COOKIE,
        "weibo_cookie_jar": WEIBO_COOKIE_JAR,
        "weibo_users": WEIBO_USERS,
        "qq_blacklist": QQ_BLACKLIST_KEYWORDS,
        "weibo_blacklist": SKIP_KEYWORDS,
        "replace_keywords": REPLACE_KEYWORDS,
        "enable_convert_qq": ENABLE_CONVERT_QQ,
    }
    if extra: cfg.update(extra)
    with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)

def start_service():
    threading.Thread(target=process_message_queue_worker, daemon=True, name="message-worker").start()
    threading.Thread(target=weibo_monitor_worker, daemon=True, name="weibo-worker").start()
    threading.Thread(target=weibo_keep_alive_worker, daemon=True, name="weibo-heartbeat").start()
    print(f"📡 开启本地 Webhook 监听端口: {WEBHOOK_PORT}")
    app.run(host="0.0.0.0", port=WEBHOOK_PORT, debug=False, use_reloader=False)

def validate_webhook_port(port):
    """在启动前检测端口冲突，避免 Flask 在线程中静默退出。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))
    except OSError as exc:
        raise RuntimeError(f"Webhook 端口 {port} 无法使用：{exc}") from exc
    finally:
        probe.close()

class UiLogStream:
    """将原有 print 输出同时写入控制台、日志文件和 UI 消息框。"""
    def __init__(self, original_stream, event_queue, logger, level):
        self.original_stream = original_stream
        self.event_queue = event_queue
        self.logger = logger
        self.level = level

    def write(self, text):
        # PyInstaller 的 --windowed 模式没有控制台，stdout/stderr 会是 None。
        if self.original_stream is not None:
            self.original_stream.write(text)
            self.original_stream.flush()
        for line in str(text).splitlines():
            line = line.strip()
            if line:
                self.logger.log(self.level, line)
                self.event_queue.put(("log", line))

    def flush(self):
        if self.original_stream is not None:
            self.original_stream.flush()

    def isatty(self):
        return False

def launch_ui():
    import tkinter as tk
    from tkinter import ttk, messagebox

    root = tk.Tk()
    root.title("QQ / 微博电商转链机器人")
    root.geometry("760x700")
    root.minsize(700, 600)
    root.after(150, lambda: (root.deiconify(), root.lift(), root.focus_force()))
    vars_ = {}
    ui_events = queue.Queue()
    logger = logging.getLogger("ecommerce_bot")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False
    # 每两天滚动一次，仅保留一个历史文件，避免日志无限增长。
    file_handler = TimedRotatingFileHandler(LOG_FILE, when="midnight", interval=2,
                                            backupCount=1, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(file_handler)
    original_stdout, original_stderr = sys.stdout, sys.stderr
    sys.stdout = UiLogStream(original_stdout, ui_events, logger, logging.INFO)
    sys.stderr = UiLogStream(original_stderr, ui_events, logger, logging.ERROR)
    def field(parent, label, value="", show=None):
        row = ttk.Frame(parent); row.pack(fill="x", padx=10, pady=4)
        ttk.Label(row, text=label, width=18).pack(side="left")
        var = tk.StringVar(value=value); vars_[label] = var
        ttk.Entry(row, textvariable=var, show=show).pack(side="left", fill="x", expand=True)
        return var
    def text_field(parent, label, value="", height=3):
        box = ttk.Frame(parent); box.pack(fill="both", expand=False, padx=10, pady=4)
        ttk.Label(box, text=label, width=18).pack(side="left", anchor="n")
        txt = tk.Text(box, height=height, wrap="word"); txt.insert("1.0", value); txt.pack(side="left", fill="x", expand=True)
        vars_[label] = txt; return txt
    def val(label):
        obj = vars_[label]
        return obj.get("1.0", "end").strip() if isinstance(obj, tk.Text) else obj.get().strip()
    def populate():
        user_uids = "\n".join(u.get("uid", "") for u in WEIBO_USERS if u.get("uid"))
        field(general, "微信目标群名", WECHAT_CHAT_NAME)
        field(general, "QQ源QQ群号", ",".join(map(str, LISTEN_QQ_GROUPS)))
        field(general, "Webhook端口", str(WEBHOOK_PORT))
        text_field(general, "QQ转发屏蔽词", "\n".join(QQ_BLACKLIST_KEYWORDS))
        text_field(general, "微博转链屏蔽词", "\n".join(SKIP_KEYWORDS))
        text_field(general, "微博用户UID", user_uids, 2)
        text_field(replace_tab, "替换规则", "\n".join(f"{old} => {new}" for old, new in REPLACE_KEYWORDS.items()), 12)
        ttk.Label(replace_tab, text="每行一条：原文 => 新文。右侧留空即可删除原文；规则会在微博内容转发前按顺序应用。", foreground="#8a4b08").pack(padx=10, pady=8, anchor="w")
        field(credential, "折淘客 AppKey", APPKEY)
        field(credential, "折淘客 SID", SID)
        field(credential, "折淘客 PID", PID)
        field(credential, "折淘客 UnionID", UNIONID)
        field(credential, "微博 Cookie (SUB)", WEIBO_COOKIE)
    def save(show_message=True):
        global WECHAT_CHAT_NAME, LISTEN_QQ_GROUPS, WEBHOOK_PORT, APPKEY, SID, PID, UNIONID
        global WEIBO_COOKIE, QQ_BLACKLIST_KEYWORDS, SKIP_KEYWORDS, REPLACE_KEYWORDS, WEIBO_USERS
        WECHAT_CHAT_NAME = val("微信目标群名")
        LISTEN_QQ_GROUPS = [int(x) for x in _split_lines(val("QQ源QQ群号")) if x.isdigit()]
        WEBHOOK_PORT = int(val("Webhook端口") or 5000)
        QQ_BLACKLIST_KEYWORDS = _split_lines(val("QQ转发屏蔽词"))
        SKIP_KEYWORDS = _split_lines(val("微博转链屏蔽词"))
        REPLACE_KEYWORDS = parse_replace_rules(val("替换规则"))
        WEIBO_USERS = [{"uid": uid, "history_file": f"history_weibo_ids_{uid}.txt"}
                       for uid in _split_lines(val("微博用户UID"))]
        APPKEY, SID, PID, UNIONID = (val(k) for k in ("折淘客 AppKey", "折淘客 SID", "折淘客 PID", "折淘客 UnionID"))
        cookie = val("微博 Cookie (SUB)")
        if cookie and not update_cookie(cookie):
            raise ValueError("Cookie 中未找到 SUB=...")
        write_user_config()
        if show_message: messagebox.showinfo("已保存", f"配置已保存到：{CONFIG_FILE}")
    def get_sub_from_cache():
        get_sub_button.config(state="disabled")
        status.set("正在读取默认 Chrome 缓存中的微博 SUB…")
        def work():
            try:
                cookie = get_weibo_cookie_from_default_cache()
                ui_events.put(("sub_success", cookie))
            except Exception as exc:
                ui_events.put(("sub_error", str(exc) or exc.__class__.__name__))
        threading.Thread(target=work, daemon=True).start()
    def start():
        try:
            save(False)
            validate_webhook_port(WEBHOOK_PORT)
        except Exception as exc: messagebox.showerror("配置错误", str(exc)); return
        start_button.config(state="disabled"); status.set("正在启动服务…")
        def work():
            try:
                start_service()
            except BaseException as exc:
                ui_events.put(("service_error", str(exc) or exc.__class__.__name__))
        threading.Thread(target=work, daemon=True, name="flask-service").start()
    notebook = ttk.Notebook(root); notebook.pack(fill="both", expand=True, padx=10, pady=10)
    general = ttk.Frame(notebook); replace_tab = ttk.Frame(notebook); credential = ttk.Frame(notebook)
    notebook.add(general, text="转发与屏蔽"); notebook.add(credential, text="折淘客与微博")
    notebook.add(replace_tab, text="文本替换")
    populate()
    ttk.Label(credential, text="微博 SUB 从本机默认 Chrome 缓存读取。请先用 Chrome 手动登录 weibo.com；后台发现 Cookie 失效时会自动重新读取缓存。", foreground="#8a4b08").pack(padx=10, pady=8)
    actions = ttk.Frame(root); actions.pack(fill="x", padx=10, pady=4)
    ttk.Button(actions, text="保存配置", command=save).pack(side="left", padx=4)
    get_sub_button = ttk.Button(actions, text="从默认 Chrome 缓存获取 SUB", command=get_sub_from_cache)
    get_sub_button.pack(side="left", padx=4)
    start_button = ttk.Button(actions, text="启动服务", command=start); start_button.pack(side="left", padx=4)
    status = tk.StringVar(value="未启动")
    ttk.Label(root, textvariable=status).pack(anchor="w", padx=14)
    log_frame = ttk.Frame(root); log_frame.pack(fill="both", expand=True, padx=10, pady=8)
    log = tk.Text(log_frame, height=9, state="disabled", wrap="word")
    log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=log.yview)
    log.configure(yscrollcommand=log_scroll.set)
    log.pack(side="left", fill="both", expand=True); log_scroll.pack(side="right", fill="y")
    def poll_ui_events():
        try:
            while True:
                event, payload = ui_events.get_nowait()
                get_sub_button.config(state="normal")
                if event == "sub_success":
                    vars_["微博 Cookie (SUB)"].set(payload)
                    status.set("已从默认 Chrome 缓存获取 SUB Cookie；配置已自动保存。")
                    messagebox.showinfo("获取成功", "已从默认 Chrome 缓存获取并保存 SUB Cookie")
                elif event == "sub_error":
                    status.set("获取 SUB 失败")
                    messagebox.showerror("获取 SUB 失败", payload)
                elif event == "service_error":
                    start_button.config(state="normal")
                    status.set("服务启动失败")
                    messagebox.showerror("服务启动失败", payload)
                elif event == "log":
                    log.configure(state="normal")
                    log.insert("end", payload + "\n")
                    # UI 仅保留最近约 1,500 行，长期记录见 ecommerce_bot.log。
                    if int(log.index("end-1c").split(".")[0]) > 1500:
                        log.delete("1.0", "500.0")
                    log.see("end")
                    log.configure(state="disabled")
        except queue.Empty:
            pass
        root.after(100, poll_ui_events)
    root.after(100, poll_ui_events)
    def close_window():
        sys.stdout, sys.stderr = original_stdout, original_stderr
        for handler in logger.handlers:
            handler.close()
        root.destroy()
    root.protocol("WM_DELETE_WINDOW", close_window)
    root.mainloop()

# ================= 主程序入口 =================

if __name__ == "__main__":
    try:
        locale.setlocale(locale.LC_ALL, '')
        launch_ui()
    except Exception as exc:
        # 双击 .py 时控制台常常立即关闭；用弹窗和日志保留真正原因。
        error_text = f"启动失败：{exc}\n\n详细错误已写入：{os.path.join(SCRIPT_DIR, 'startup_error.log')}"
        with open(os.path.join(SCRIPT_DIR, "startup_error.log"), "w", encoding="utf-8") as fh:
            traceback.print_exc(file=fh)
        try:
            import tkinter as tk
            from tkinter import messagebox
            dialog = tk.Tk(); dialog.withdraw()
            messagebox.showerror("QQ / 微博转链机器人", error_text)
            dialog.destroy()
        finally:
            raise
