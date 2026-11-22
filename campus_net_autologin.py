#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
校园网自动认证监控脚本（深澜 SRUN v4 协议）—— 常驻轮询版
==========================================================
协议依据：门户真实 jquery.srun.portal.js（SRunCGIAuthIntfSvr V1.18）
  1. get_challenge : GET /cgi-bin/get_challenge?username=&ip=&callback=
                     -> 返回 challenge(token)、client_ip
  2. info 加密      : info = "{SRBX1}" + 深澜表base64(xEncode(JSON{username,
                     password,ip,acid,enc_ver:"srun_bx1"}, token))，base64 使用
                     门户 jquery-base64 v1.0 默认字母表（非标准表）
  3. 密码           : password = "{MD5}" + hmd5，hmd5 = HMAC-MD5(key=token,
                     msg=password)（门户 md5 库为 blueimp-md5，md5(str,key)
                     第二参数触发 HMAC；旧实现 md5(token+password) 已被证实
                     错误，门户返回 auth_info_error）
  4. chksum         : sha1(token+username + token+hmd5 + token+ac_id + token+ip
                     + token+n + token+type + token+info)
  5. 登录           : GET /cgi-bin/srun_portal?action=login&username=&password=
                     {MD5}hmd5&ac_id=&ip=&chksum=&info=&n=200&type=1&os=&name=
                     &double_stack=0&callback=
  6. 登出           : GET /cgi-bin/srun_portal?action=logout&ac_id=&ip=&username=
                     （仅 --logout 参数手动触发，正常轮询绝不登出）
  7. 权威状态       : GET /cgi-bin/rad_user_info?callback= -> JSON，
                     user_name 非空 且 online_ip 与本机内网 IP 一致才判定已认证

认证判断流程（以门户权威接口为主依据，避免门户劫持 HTTP 200 造成误判）：
  1) 外网多目标探活作为辅助参考并记录结果（baidu/qq/163/gov.cn）；
  2) 以 rad_user_info 为主依据判定认证状态：
     - user_name 为空/not_online_error -> 未认证；
     - user_name 非空但 online_ip 与本机内网 IP 不一致 -> 视为未认证（他人在线）；
     - user_name 非空且 online_ip 一致 -> 已认证，等待下轮；
     - rad_user_info 不可达 -> 网络栈未就绪，本轮不登录，等待下轮；
  3) 判定未认证 -> 发起 SRUN v4 登录；
  4) 登录后必须再用 rad_user_info 验证认证是否真正生效，失败记录详细日志等待下轮。

功能：
  - 常驻运行，每 60 秒检测，未认证自动登录，断开自动重连；
  - 仅使用 Python 标准库（urllib/hashlib/base64/socket），无第三方依赖；
  - 日志写文件（脚本同目录 campus_net_autologin.log），兼容 pythonw 无窗口；
  - 账号密码从 config.json 或环境变量读取，不写死在代码里；
  - --once 单次检测退出；--logout 手动登出后退出（默认不执行）。

配置：
  复制 config.example.json 为 config.json（同目录），填 portal_host / ac_id /
  username / password / target_wifi_ssid。也可用环境变量覆盖：
  CAMPUS_PORTAL_HOST / CAMPUS_AC_ID / CAMPUS_USERNAME / CAMPUS_PASSWORD /
  CAMPUS_WIFI_SSID / CAMPUS_INTERVAL

运行：
  python campus_net_autologin.py           # 常驻轮询
  pythonw campus_net_autologin.py          # 无窗口后台（开机自启）
  python campus_net_autologin.py --once    # 单次检测
  python campus_net_autologin.py --logout  # 手动登出（慎用）
"""
import sys
import os
import time
import base64
import hmac
import json
import re
import hashlib
import socket
import subprocess
import urllib.request
import urllib.parse
import urllib.error

if getattr(sys, "frozen", False):
    # 打包成 exe 后，配置和日志要跟着 exe 走，不能丢进解包出来的临时目录
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ============ 默认配置（真正的值请放 config.json / 环境变量） ============
DEFAULT_CONFIG = {
    # 门户地址：形如 http://10.x.x.x（学校认证页所在的内网地址，不带结尾斜杠）
    "portal_host": "http://10.0.0.1",
    # ac_id：门户登录页 URL 里的 ac_id 参数
    "ac_id": "2",
    "username": "",          # 学号 / 账号
    "password": "",          # 校园网认证密码
    # 仅在连接该名称的 WiFi 时才执行认证逻辑（留空表示不限制）
    "target_wifi_ssid": "",
    "check_interval": 60,    # 轮询间隔（秒）
    "timeout": 8,            # 单次 HTTP 请求超时（秒）
    "n": 200,
    "type": 1,
}

ENV_MAP = {
    "portal_host": "CAMPUS_PORTAL_HOST",
    "ac_id": "CAMPUS_AC_ID",
    "username": "CAMPUS_USERNAME",
    "password": "CAMPUS_PASSWORD",
    "target_wifi_ssid": "CAMPUS_WIFI_SSID",
    "check_interval": "CAMPUS_INTERVAL",
}

# 门户 jquery-base64 v1.0 默认字母表（深澜自定义，非标准表），info 加密必须用它
SRBX1_B64_ALPHA = "LVoJPiCN2R8G90yg+hmFHuacZ1OWMnrsSTXkYpUq/3dlbfKwv6xztjI7DeBE45QA"
STD_B64_ALPHA = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
ENC_VER = "srun_bx1"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
# 外网探活多目标（辅助参考，任一返回 200 即外网可用）
PROBE_URLS = [
    "http://www.baidu.com",
    "http://www.qq.com",
    "http://www.163.com",
    "http://www.gov.cn",
]
LOG_FILE = os.path.join(BASE_DIR, "campus_net_autologin.log")


def _config_path_from_argv():
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--config" and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("--config="):
            return a.split("=", 1)[1]
    return os.path.join(BASE_DIR, "config.json")


def load_config(path=None):
    """默认值 <- config.json <- 环境变量。返回 (config, 实际读取的路径)"""
    cfg = dict(DEFAULT_CONFIG)
    path = path or _config_path_from_argv()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                user_cfg = json.load(f)
            if isinstance(user_cfg, dict):
                for k, v in user_cfg.items():
                    if k in cfg and v not in (None, ""):
                        cfg[k] = v
        except Exception as e:
            print("[配置] 读取 {} 失败: {}".format(path, e), file=sys.stderr)
    for key, env_name in ENV_MAP.items():
        v = os.environ.get(env_name)
        if v:
            cfg[key] = v
    try:
        cfg["check_interval"] = max(10, int(cfg["check_interval"]))
    except Exception:
        cfg["check_interval"] = DEFAULT_CONFIG["check_interval"]
    try:
        cfg["timeout"] = max(1, int(cfg["timeout"]))
    except Exception:
        cfg["timeout"] = DEFAULT_CONFIG["timeout"]
    cfg["portal_host"] = str(cfg["portal_host"]).rstrip("/")
    return cfg, path


# ============ 由配置派生出的全局量 ============
CONFIG = {}
HOST = ""
PORTAL_URL = ""
USERNAME = ""
PASSWORD = ""
AC_ID = "2"
TARGET_WIFI_SSID = ""
CHECK_INTERVAL = 60
TIMEOUT = 8
N = 200
TYPE = 1
LOG_SINK = None      # GUI 可以挂一个回调，实时收日志


def apply_config(cfg):
    """把一份配置写进模块级全局量（CLI 与 GUI 共用）"""
    global CONFIG, HOST, PORTAL_URL, USERNAME, PASSWORD, AC_ID
    global TARGET_WIFI_SSID, CHECK_INTERVAL, TIMEOUT, N, TYPE
    CONFIG = dict(cfg)
    HOST = str(CONFIG["portal_host"]).rstrip("/")
    PORTAL_URL = "{}/srun_portal_pc?ac_id={}&theme=basic".format(
        HOST, CONFIG["ac_id"])
    USERNAME = str(CONFIG["username"]).strip()
    PASSWORD = str(CONFIG["password"])
    AC_ID = str(CONFIG["ac_id"])
    TARGET_WIFI_SSID = str(CONFIG["target_wifi_ssid"]).strip()
    CHECK_INTERVAL = CONFIG["check_interval"]
    TIMEOUT = CONFIG["timeout"]
    N = CONFIG["n"]
    TYPE = CONFIG["type"]


CONFIG, CONFIG_PATH = load_config()
apply_config(CONFIG)


def log(msg):
    """带时间戳日志：写文件，控制台 try 包裹（兼容 pythonw 无窗口）"""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = "[{}] {}".format(ts, msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    if LOG_SINK is not None:
        try:
            LOG_SINK(line)
        except Exception:
            pass


# ---------------- 深澜 xEncode 算法（与 jquery.srun.portal.js 一致） ----------------

def _u32(x):
    return x & 0xFFFFFFFF


def _str_to_words(a, b):
    """JS s(str, appendLen)：每 4 字符打包成小端 32 位 word"""
    c = len(a)
    v = []
    for i in range(0, c, 4):
        w = 0
        for j in range(4):
            ch = ord(a[i + j]) if i + j < c else 0
            w |= ch << (8 * j)
        v.append(w)
    if b:
        v.append(c)
    return v


def _words_to_str(a, b):
    """JS l(arr, hasLen)：word 拆回 4 字符（code point 0-255）"""
    d = len(a)
    if b:
        c = (d - 1) << 2
        m = a[d - 1]
        if m < c - 3 or m > c:
            return None
        c = m
    out = []
    for w in a:
        out.append(chr(w & 0xFF))
        out.append(chr((w >> 8) & 0xFF))
        out.append(chr((w >> 16) & 0xFF))
        out.append(chr((w >> 24) & 0xFF))
    s = ''.join(out)
    return s[:c] if b else s


def x_encode(s, key):
    """深澜 xEncode（TEA 变种加密）"""
    if not s:
        return ""
    v = _str_to_words(s, True)
    k = _str_to_words(key, False)
    while len(k) < 4:
        k.append(0)
    n = len(v) - 1
    z = v[n]
    y = v[0]
    c = 0x86014019 | 0x183639A0
    q = 6 + 52 // (n + 1)
    d = 0
    while q > 0:
        q -= 1
        d = _u32(d + c)
        e = (d >> 2) & 3
        p = 0
        while p < n:
            y = v[p + 1]
            m = _u32((z >> 5) ^ ((y << 2) & 0xFFFFFFFF))
            m = _u32(m + (((y >> 3) ^ ((z << 4) & 0xFFFFFFFF)) ^ (d ^ y)))
            m = _u32(m + (k[(p & 3) ^ e] ^ z))
            v[p] = _u32(v[p] + m)
            z = v[p]
            p += 1
        y = v[0]
        m = _u32((z >> 5) ^ ((y << 2) & 0xFFFFFFFF))
        m = _u32(m + (((y >> 3) ^ ((z << 4) & 0xFFFFFFFF)) ^ (d ^ y)))
        m = _u32(m + (k[(p & 3) ^ e] ^ z))
        v[n] = _u32(v[n] + m)
        z = v[n]
    return _words_to_str(v, False)


# ---------------- HTTP 与 JSONP 请求 ----------------

def _http_get(full_url):
    req = urllib.request.Request(full_url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read()
    try:
        text = raw.decode("utf-8", errors="ignore")
    except Exception:
        text = raw.decode("gbk", errors="ignore")
    return resp.status, text


def _get_jsonp(path, params):
    """GET 并解析 JSONP（深澜 cgi-bin 必须带 callback 参数）"""
    p = dict(params)
    p["callback"] = "jQuery" + str(int(time.time() * 1000))[-8:]
    qs = urllib.parse.urlencode(p)
    status, text = _http_get(HOST + path + "?" + qs)
    m = re.search(r'^\s*[\w$]+\((.*)\)\s*$', text, re.S)
    if m:
        return json.loads(m.group(1))
    return json.loads(text)


# ---------------- SRUN v4 协议核心 ----------------

def get_local_ip():
    """获取本机内网 IP（UDP 连接不发送数据，探测出口地址）"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(3)
        s.connect((urllib.parse.urlsplit(HOST).hostname or "10.0.0.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception as e:
        log("[状态] 获取本机内网 IP 失败: {}".format(e))
        return ""


def get_challenge():
    """GET /cgi-bin/get_challenge -> (token, client_ip)"""
    data = _get_jsonp("/cgi-bin/get_challenge",
                      {"username": USERNAME, "ip": ""})
    log("[challenge] error={} srun_ver={}".format(
        data.get("error"), data.get("srun_ver", "?")))
    if data.get("error") != "ok":
        raise RuntimeError("get_challenge 失败: {}".format(data))
    token = data["challenge"]
    client_ip = data.get("client_ip") or ""
    log("[challenge] token={}... client_ip={}".format(token[:16], client_ip))
    return token, client_ip


def check_auth_state():
    """权威认证状态判断：rad_user_info -> (portal_reachable, authed)
    authed 条件：user_name 非空 且 online_ip 与本机内网 IP 一致"""
    try:
        data = _get_jsonp("/cgi-bin/rad_user_info", {})
        user = data.get("user_name", "") or ""
        online_ip = data.get("online_ip", "") or ""
        local_ip = get_local_ip()
        log("[状态] rad_user_info 原始字段: error={} user_name={} online_ip={} "
            "local_ip={} client_ip={}".format(
                data.get("error"), user or "(空)", online_ip or "(空)",
                local_ip or "(获取失败)", data.get("client_ip", "")))

        if not user or user == "not_online_error":
            log("[状态] user_name 为空/not_online_error -> 未认证")
            return True, False
        if local_ip and online_ip and online_ip != local_ip:
            log("[状态] online_ip({}) 与本机 IP({}) 不一致 -> 视为未认证".format(
                online_ip, local_ip))
            return True, False
        log("[状态] user_name={} 且 online_ip 与本机一致 -> 已认证".format(user))
        return True, True
    except Exception as e:
        log("[状态] rad_user_info 不可达: {}".format(e))
        return False, False


def _srun_b64(data):
    """门户 jquery-base64 v1.0 encode：标准 base64 算法 + 深澜自定义字母表"""
    std = base64.b64encode(data).decode("ascii")
    return std.translate(str.maketrans(STD_B64_ALPHA, SRBX1_B64_ALPHA))


def build_info(token, ip):
    """info = {SRBX1} + 深澜表base64(xEncode(json, token))"""
    info_obj = {
        "username": USERNAME,
        "password": PASSWORD,
        "ip": ip,
        "acid": AC_ID,
        "enc_ver": ENC_VER,
    }
    json_str = json.dumps(info_obj, separators=(",", ":"))
    enc = x_encode(json_str, token)
    info_bytes = bytes(ord(ch) & 0xFF for ch in enc)
    return "{SRBX1}" + _srun_b64(info_bytes)


def build_hmd5(token):
    """hmd5 = HMAC-MD5(key=token, msg=password)（门户 md5(password, token)
    第二参数触发 blueimp-md5 的 HMAC 分支；utf8 字节参与计算）"""
    return hmac.new(token.encode("utf-8"), PASSWORD.encode("utf-8"),
                    hashlib.md5).hexdigest()


def build_chksum(token, hmd5, ip, info):
    """chksum = sha1(分段拼接)"""
    chkstr = token + USERNAME
    chkstr += token + hmd5
    chkstr += token + AC_ID
    chkstr += token + ip
    chkstr += token + str(N)
    chkstr += token + str(TYPE)
    chkstr += token + info
    return hashlib.sha1(chkstr.encode("utf-8")).hexdigest()


def do_login(token=None, client_ip=None):
    """完整登录：get_challenge -> 计算参数 -> GET /cgi-bin/srun_portal?action=login
    可复用外部已获取的 token/client_ip（避免重复请求）"""
    if token is None:
        token, client_ip = get_challenge()
    ip = client_ip or ""

    hmd5 = build_hmd5(token)
    info = build_info(token, ip)
    chksum = build_chksum(token, hmd5, ip, info)

    params = {
        "action": "login",
        "username": USERNAME,
        "password": "{MD5}" + hmd5,
        "ac_id": AC_ID,
        "ip": ip,
        "chksum": chksum,
        "info": info,
        "n": str(N),
        "type": str(TYPE),
        "os": "Windows 10",
        "name": "Windows",
        "double_stack": "0",
    }
    log("[登录] username={} ac_id={} ip={}".format(USERNAME, AC_ID, ip))
    log("[登录] hmd5={} chksum={}".format(hmd5, chksum))

    data = _get_jsonp("/cgi-bin/srun_portal", params)
    err = data.get("error", "")
    log("[登录] 响应: error={} error_msg={}".format(err, data.get("error_msg", "")))
    if err == "ok":
        return True, "登录成功"
    return False, "登录失败 error={}: {}".format(err, data.get("error_msg", ""))


def do_logout():
    """登出：GET /cgi-bin/srun_portal?action=logout（仅手动触发）"""
    try:
        _, client_ip = get_challenge()
    except Exception:
        client_ip = ""
    params = {"action": "logout", "ac_id": AC_ID, "ip": client_ip,
              "username": USERNAME}
    data = _get_jsonp("/cgi-bin/srun_portal", params)
    if data.get("error") == "ok":
        return True, "登出成功"
    return False, "登出失败 error={}".format(data.get("error"))


# ---------------- WiFi 过滤（仅目标 WiFi 才执行认证） ----------------

def get_current_wifi_ssid():
    """netsh wlan show interfaces 解析当前已连接 WiFi 的 SSID。
    可能含多个无线接口，取 State=connected 的接口；解析失败返回 None（保守）"""
    try:
        proc = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:
        log("[WiFi] netsh 调用失败: {}".format(e))
        return None
    raw = proc.stdout
    out = ""
    for enc in ("utf-8", "gbk"):
        try:
            out = raw.decode(enc)
            break
        except Exception:
            continue
    if not out:
        out = raw.decode("utf-8", errors="ignore")
    # 按空行分段，每段对应一个无线接口
    for block in re.split(r"\n\s*\n", out):
        state_val = None
        ssid_val = None
        for line in block.splitlines():
            m = re.match(r"^\s*(State|状态)\s*[:：]\s*(.+)$", line, re.I)
            if m:
                state_val = m.group(2).strip().lower()
            m2 = re.match(r"^\s*SSID\s*[:：]\s*(.+)$", line, re.I)
            if m2:
                ssid_val = m2.group(1).strip()
        if state_val in ("connected", "已连接") and ssid_val:
            return ssid_val
    return None


def is_target_wifi():
    """当前 WiFi 是否为目标 SSID；未配置目标则不做限制。
    解析失败按非目标处理（保守不误登录）"""
    if not TARGET_WIFI_SSID:
        return True
    ssid = get_current_wifi_ssid()
    if ssid is None:
        log("[WiFi] 无法确定当前 WiFi（解析失败），按非目标 WiFi 跳过本轮")
        return False
    if ssid == TARGET_WIFI_SSID:
        log("[WiFi] 当前 WiFi: {}（目标网络）".format(ssid))
        return True
    log("[WiFi] 非目标 WiFi: {}，跳过本轮".format(ssid))
    return False


# ---------------- 探活与轮询 ----------------

def is_online():
    """多目标外网探活（辅助参考）：任一目标返回 200 即外网可用"""
    reachable = []
    for url in PROBE_URLS:
        try:
            status, _ = _http_get(url)
            if status == 200:
                log("[探活] {} 可达（HTTP 200）".format(url))
                return True
            reachable.append("{}:HTTP{}".format(url, status))
        except Exception as e:
            reachable.append("{}:{}".format(url, type(e).__name__))
    log("[探活] 全部失败: {}".format(" | ".join(reachable)))
    return False


def ensure_online():
    """单轮检测：WiFi 过滤 -> 探活(辅) + rad_user_info(主) -> 未认证才登录 -> 登录后验证"""
    # 每轮先检查 WiFi，非目标网络直接跳过，不执行任何认证动作
    if not is_target_wifi():
        log("[判断] 非目标WiFi，跳过")
        return False, "非目标WiFi，跳过"

    probe_ok = is_online()
    log("[判断] 外网探活结果: {}".format("成功（快速参考）" if probe_ok else "失败"))

    # 主依据：rad_user_info 权威状态
    try:
        portal_ok, authed = check_auth_state()
    except Exception as e:
        log("[状态] rad_user_info 异常: {}".format(e))
        portal_ok, authed = False, False

    if not portal_ok:
        log("[判断] 门户不可达（rad_user_info 获取失败），可能网络栈未就绪，本轮不登录")
        return False, "门户不可达，等待下轮"

    if authed:
        log("[判断] rad_user_info 权威确认已认证（探活={}），无需登录".format(
            "成功" if probe_ok else "失败"))
        return True, "rad_user_info 确认已认证"

    log("[判断] 权威确认未认证，发起登录...")
    try:
        ok, msg = do_login()
    except Exception as e:
        log("[登录] 异常: {}".format(e))
        return False, "登录异常，等待下轮重试"

    if not ok:
        log("[判断] 登录接口返回失败，等待下轮重试: {}".format(msg))
        return False, msg

    # 登录后必须用 rad_user_info 验证认证是否真正生效
    log("[验证] 登录接口成功，等待 2 秒后 rad_user_info 复核...")
    time.sleep(2)
    try:
        portal_ok2, authed2 = check_auth_state()
    except Exception as e:
        log("[验证] rad_user_info 复核异常: {}".format(e))
        portal_ok2, authed2 = False, False

    if authed2:
        log("[验证] rad_user_info 复核通过：认证已生效，外网应已恢复")
        probe2 = is_online()
        log("[验证] 外网探活复核: {}".format("可达" if probe2 else "暂不可达（可能 DNS 缓存）"))
        return True, "登录成功且 rad_user_info 验证通过"
    log("[验证] rad_user_info 复核未通过：认证可能未真正生效，记录日志并等待下轮重试")
    return False, "登录响应成功但验证未通过"


def main():
    args = sys.argv[1:]
    once_mode = "--once" in args
    logout_mode = "--logout" in args

    if "--help" in args or "-h" in args:
        print(__doc__)
        return 0

    if not logout_mode and (not USERNAME or not PASSWORD):
        log("配置缺失：请在 config.json 里填写 username 和 password"
            "（当前读取: {}）".format(CONFIG_PATH))
        return 2

    log("========== 校园网自动认证监控启动（SRUN v4） ==========")
    log("账号: {}, 门户: {}, 轮询间隔: {} 秒".format(
        USERNAME, HOST, CHECK_INTERVAL))

    if logout_mode:
        ok, msg = do_logout()
        log("[登出] {}".format(msg))
        log("========== 登出执行完毕 ==========")
        return 0 if ok else 1

    if once_mode:
        if not is_target_wifi():
            log("[本轮] 非目标WiFi，跳过")
            log("========== 单次检测结束 ==========")
            return 0
        ok, action = ensure_online()
        log("[本轮] {}".format(action))
        log("========== 单次检测结束 ==========")
        return 0 if ok else 1

    while True:
        try:
            ok, action = ensure_online()
            log("[本轮] {}".format(action))
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log("[异常] 本轮检测出错: {}".format(e))

        log("休眠 {} 秒后继续检测...".format(CHECK_INTERVAL))
        try:
            time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt:
            raise


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("用户中断，程序退出。")
        sys.exit(130)
