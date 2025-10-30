# -*- coding: utf-8 -*-
"""
進階班 LINE 推播後端（Flask + Google 試算表，完整檔案版＋診斷強化）

功能：
- Webhook：處理「連結 <姓名>」→ 寫入 users 工作表（或本地 users.json）
- GET /users：回傳 {name_to_uid, uid_to_name}
- POST /checkin：本機回報簽到（X-API-KEY），後端判斷當日去重與遲到，再推播
- POST /cron/morning_scan：平日 08:00 未簽到提醒（X-API-KEY）
- GET /push?name=...&text=...：測試推播
- GET /debug/sheets：檢查 Google 試算表連線/分頁狀態（含服務帳戶健檢）
- GET /debug/sheets/write：實際寫入一列驗證「可寫入」
- GET /webhook：診斷路由是否存在（LINE 仍走 POST）
- GET /health：健康檢查

需求套件：
Flask, line-bot-sdk (v3), gspread, google-auth, python-dotenv, requests
"""

import os, json, atexit, subprocess, time, requests, shutil, datetime, base64
from pathlib import Path
from urllib.parse import urljoin
from flask import Flask, request, jsonify

print("[BOOT] facecheck-backend FULL + diagnostics (GET /webhook, route listing)")

# ---------- 以此檔所在資料夾為工作目錄 ----------
BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

# ---------- 讀取 .env（若存在） ----------
def _safe_len(v): return 0 if not v else len(v)
def _mask(v, keep=4):
    if not v: return "(empty)"
    return v[:keep] + "*" * max(0, len(v) - keep)

try:
    from dotenv import load_dotenv, dotenv_values
    dotenv_path = BASE_DIR / ".env"
    print(f"[ENV] target: {dotenv_path} exists={dotenv_path.exists()}")
    if dotenv_path.exists():
        print("[ENV] keys in .env:", list(dotenv_values(dotenv_path, encoding="utf-8").keys()))
    load_dotenv(dotenv_path, override=True, encoding="utf-8")
    print("[ENV] loaded .env = True")
except Exception as e:
    print("[ENV][WARN] python-dotenv 未載入：", e)

# ---------- 線程/運算環境 ----------
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("ORT_NUM_THREADS", "1")
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
os.environ.setdefault("ORT_LOG_SEVERITY_LEVEL", "3")

# ---------- 時區與常數 ----------
from zoneinfo import ZoneInfo
TZ_NAME = os.environ.get("TZ", "Asia/Taipei")
try:
    TZ = ZoneInfo(TZ_NAME)
except Exception:
    TZ = ZoneInfo("UTC")
    TZ_NAME = "UTC"

LATE_CUTOFF = os.environ.get("LATE_CUTOFF", "08:00")   # "HH:MM"
ONLY_WEEKDAYS = str(os.environ.get("ONLY_WEEKDAYS", "1")).strip().lower() in ("1","true","yes","y","on")

def _now_local():
    return datetime.datetime.now(TZ)

def _today_str():
    return _now_local().date().isoformat()

def _parse_hhmm(s):
    try:
        h, m = s.strip().split(":")
        return int(h), int(m)
    except Exception:
        return 8, 0

def _parse_when_to_local(when_iso: str) -> datetime.datetime:
    dt = datetime.datetime.fromisoformat(when_iso)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)

# ---------- LINE Bot v3 ----------
from linebot.v3.webhook import WebhookHandler
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, PushMessageRequest, TextMessage
)
from linebot.v3.exceptions import InvalidSignatureError
try:
    from linebot.v3.messaging.exceptions import ApiException
except Exception:
    try:
        from linebot.v3.exceptions import ApiException
    except Exception:
        ApiException = Exception

# ---------- 自動啟動 ngrok（本機開發用） ----------
def _env_bool(name, default=False):
    v = os.environ.get(name, str(int(default))).strip().lower()
    return v in ("1", "true", "yes", "y", "on")

def _find_ngrok_exe():
    p = (os.environ.get("NGROK") or "").strip().strip('"')
    if p and os.path.isfile(p): return p
    p2 = shutil.which("ngrok")
    if p2: return p2
    for cand in (r"C:\tools\ngrok\ngrok.exe", r"C:\ngrok\ngrok.exe", "/usr/local/bin/ngrok", "/usr/bin/ngrok"):
        if os.path.isfile(cand): return cand
    return None

def _kill_ngrok_silent():
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/IM", "ngrok.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.run(["pkill", "-f", "ngrok"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

def _probe_public_url(timeout=25):
    api = "http://127.0.0.1:4040/api/tunnels"
    end = time.time() + timeout
    last_err = None
    while time.time() < end:
        try:
            r = requests.get(api, timeout=2)
            if r.ok:
                data = r.json()
                for t in data.get("tunnels", []):
                    pub = t.get("public_url", "")
                    if pub.startswith("https://"): return pub
                for t in data.get("tunnels", []):
                    pub = t.get("public_url", "")
                    if pub: return pub
        except Exception as e:
            last_err = e
        time.sleep(0.8)
    raise RuntimeError(f"無法從 4040 取得 public URL：{last_err}")

def start_ngrok_if_needed(local_host="127.0.0.1", port=5000, webhook_path="/webhook"):
    if not _env_bool("START_NGROK", True):
        print("[NGROK] 跳過啟動（START_NGROK=0）")
        return None
    exe = _find_ngrok_exe()
    if not exe:
        print("[NGROK][ERROR] 找不到 ngrok，可在 .env 設 NGROK=完整路徑")
        return None
    region = (os.environ.get("NGROK_REGION") or "").strip() or None
    extra  = (os.environ.get("NGROK_ARGS") or "").strip() or None

    _kill_ngrok_silent()
    cmd = [exe, "http", f"http://{local_host}:{port}"]
    if region: cmd += ["--region", region]
    if extra:  cmd += extra.split()

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=creationflags)
    atexit.register(lambda: (proc.poll() is None) and proc.terminate())

    try:
        public_url = _probe_public_url(timeout=25)
        full = urljoin(public_url + "/", webhook_path.lstrip("/"))
        print(f"[NGROK] public url: {public_url}")
        print(f"[NGROK] Webhook：{full}")
        try:
            if os.name == "nt":
                subprocess.run(f'echo {full}| clip', shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                print("[NGROK] 已將 Webhook URL 複製到剪貼簿。")
        except Exception:
            pass
        return public_url
    except Exception as e:
        print("[NGROK][WARN]", e)
        print("[NGROK][HINT] 打開 http://127.0.0.1:4040 檢查 ngrok 狀態。")
        return None

# ---------- 本地 users.json 退路 ----------
USERS_JSON = "users.json"
def _fs_load_users():
    try:
        with open(USERS_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"_by_user_id": {}, "_by_name": {}}
    except Exception as e:
        print("[USERS][ERROR] 讀取失敗", e); return {"_by_user_id": {}, "_by_name": {}}

def _fs_save_users(data):
    try:
        with open(USERS_JSON, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print("[USERS][ERROR] 寫入失敗", e); return False

# ---------- Google Sheets 介面（多來源載入 + 鍵值健檢 + 自動修復換行 + 舊名相容） ----------
USE_SHEETS = False
_gs_reason = None
_sa_info_cache = None
_sa_error_cache = None

try:
    import gspread
    from google.oauth2.service_account import Credentials  # 使用 google-auth
    from gspread.exceptions import WorksheetNotFound, APIError as GspreadAPIError
except Exception as e:
    _gs_reason = f"套件未備：{e}"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def _load_sa_info():
    """
    服務帳戶 JSON 來源（依序）：
      1) GOOGLE_SERVICE_ACCOUNT_JSON（整份 JSON）
      2) GOOGLE_SERVICE_ACCOUNT_FILE（容器內檔名）
      3) GOOGLE_SERVICE_ACCOUNT_B64（整份 JSON 的 base64）
    舊名相容：
      - SERVICE_ACCOUNT_JSON_B64（= GOOGLE_SERVICE_ACCOUNT_B64）
    並修復 private_key 換行與健檢。
    """
    global _sa_info_cache, _sa_error_cache
    if _sa_info_cache is not None or _sa_error_cache is not None:
        if _sa_info_cache is not None:
            return _sa_info_cache
        raise RuntimeError(_sa_error_cache)

    raw_json = (os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip()
    file_path = (os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE") or "").strip()
    b64_blob = (os.environ.get("GOOGLE_SERVICE_ACCOUNT_B64") or os.environ.get("SERVICE_ACCOUNT_JSON_B64") or "").strip()

    info = None
    source = None
    try:
        if raw_json:
            info = json.loads(raw_json); source = "JSON"
        elif file_path:
            with open(file_path, "r", encoding="utf-8") as f:
                info = json.load(f); source = "FILE"
        elif b64_blob:
            raw = base64.b64decode(b64_blob).decode("utf-8")
            info = json.loads(raw); source = "B64"
        else:
            raise RuntimeError("缺少服務帳戶來源（未設定 GOOGLE_SERVICE_ACCOUNT_JSON / FILE / B64）")

        # 修復與健檢 private_key
        pk = info.get("private_key", "")
        pk = pk.replace("\\r\\n", "\n").replace("\\n", "\n")
        pk = pk.strip().replace("\r\n", "\n").replace("\r", "\n")
        info["private_key"] = pk

        header_ok = pk.startswith("-----BEGIN PRIVATE KEY-----")
        footer_ok = pk.endswith("-----END PRIVATE KEY-----")
        line_count = pk.count("\n") + 1
        size_ok = len(pk) > 1000  # 常見長度 >1600，1000 做下限

        if not (header_ok and footer_ok and size_ok):
            raise RuntimeError(
                "服務帳戶私鑰格式錯誤："
                f"header={header_ok} footer={footer_ok} size_ok={size_ok} lines={line_count}"
            )

        _sa_info_cache = info
        print(f"[SA] Loaded from {source}: client_email={info.get('client_email')} key_len={len(pk)} lines={line_count}")
        return info
    except Exception as e:
        _sa_error_cache = f"服務帳戶讀取失敗（source={source}）：{e}"
        raise

def _gspread_client():
    info = _load_sa_info()
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)

# 啟用 Sheets 與否：同時需要 SA 與 SHEET_ID（支援舊名）
_sheet_id = (os.environ.get("GOOGLE_SHEET_ID") or os.environ.get("SHEET_ID") or "").strip()
if _sheet_id:
    try:
        _ = _load_sa_info()  # 試讀一次，能提早報錯
        USE_SHEETS = True
        _gs_reason = "OK"
    except Exception as e:
        USE_SHEETS = False
        _gs_reason = str(e)
else:
    USE_SHEETS = False
    if _gs_reason is None:
        _gs_reason = "缺少 GOOGLE_SHEET_ID（或 SHEET_ID）"

def _open_sheet(sheet_name):
    gc = _gspread_client()
    sh = gc.open_by_key(os.environ.get("GOOGLE_SHEET_ID") or os.environ.get("SHEET_ID"))
    try:
        ws = sh.worksheet(sheet_name)
    except WorksheetNotFound:
        # 初次自動建立分頁與表頭
        if sheet_name == "users":
            ws = sh.add_worksheet(title="users", rows=1000, cols=3)
            ws.update("A1:C1", [["name","user_id","updated_at"]])
        elif sheet_name == "checkin_log":
            ws = sh.add_worksheet(title="checkin_log", rows=20000, cols=4)
            ws.update("A1:D1", [["date","name","when","user_id"]])
        else:
            ws = sh.add_worksheet(title=sheet_name, rows=1000, cols=3)
    return ws

def sheets_upsert_user(name, user_id):
    ws = _open_sheet("users")
    recs = ws.get_all_records()
    name = name.strip()
    now = _now_local().strftime("%Y-%m-%d %H:%M:%S")
    target_row = None
    for i, r in enumerate(recs, start=2):
        if r.get("name","").strip() == name or r.get("user_id","").strip() == user_id:
            target_row = i; break
    if target_row:
        ws.update(f"A{target_row}:C{target_row}", [[name, user_id, now]])
    else:
        ws.append_row([name, user_id, now], value_input_option="RAW")

def sheets_load_users():
    ws = _open_sheet("users")
    recs = ws.get_all_records()
    n2u, u2n = {}, {}
    for r in recs:
        n = str(r.get("name","")).strip()
        u = str(r.get("user_id","")).strip()
        if n and u:
            n2u[n] = u; u2n[u] = n
    return {"name_to_uid": n2u, "uid_to_name": u2n}

def sheets_mark_checkin(name, when_iso, user_id):
    ws = _open_sheet("checkin_log")
    dt = _now_local().date().isoformat()
    ws.append_row([dt, name, when_iso, user_id], value_input_option="RAW")

def sheets_is_checked_today(name):
    ws = _open_sheet("checkin_log")
    today = _today_str()
    recs = ws.get_all_records()
    for r in recs:
        if str(r.get("date","")) == today and str(r.get("name","")).strip() == name.strip():
            return True
    return False

def sheets_list_unchecked_names():
    users = sheets_load_users()["name_to_uid"].keys()
    return [n for n in users if not sheets_is_checked_today(n)]

# ---------- 基本設定 ----------
PORT = int(os.environ.get("PORT", 5000))
HOST = os.environ.get("HOST", "0.0.0.0")
CHANNEL_ACCESS_TOKEN = (os.environ.get("CHANNEL_ACCESS_TOKEN") or "").strip()
CHANNEL_SECRET       = (os.environ.get("CHANNEL_SECRET") or "").strip()
API_KEY              = (os.environ.get("API_KEY") or "").strip()

print("[CONFIG] SECRET len =", _safe_len(CHANNEL_SECRET), "value:", _mask(CHANNEL_SECRET))
print("[CONFIG] TOKEN  len =", _safe_len(CHANNEL_ACCESS_TOKEN), "value:", _mask(CHANNEL_ACCESS_TOKEN))
print("[CONFIG] USE_SHEETS =", USE_SHEETS, "| TZ =", TZ_NAME, "| REASON:", _gs_reason or "OK")

if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    print("[HINT] 檢查：1) 環境變數是否已設；2) 值是否無多餘空白/引號/Bearer")
    raise SystemExit("[FATAL] 缺少 CHANNEL_SECRET 或 CHANNEL_ACCESS_TOKEN。")

app = Flask(__name__)
handler = WebhookHandler(CHANNEL_SECRET)
config  = Configuration(access_token=CHANNEL_ACCESS_TOKEN)

# ---------- 名單存取（自動判斷 Sheets / JSON） ----------
def upsert_user(name, user_id):
    if USE_SHEETS:
        return sheets_upsert_user(name, user_id)
    users = _fs_load_users()
    by_uid  = users.setdefault("_by_user_id", {})
    by_name = users.setdefault("_by_name", {})
    old_name = by_uid.get(user_id, {}).get("name")
    if old_name and old_name != name and by_name.get(old_name) == user_id:
        del by_name[old_name]
    by_uid[user_id] = {"name": name}
    by_name[name] = user_id
    _fs_save_users(users)

def load_users():
    if USE_SHEETS:
        return sheets_load_users()
    users = _fs_load_users()
    n2u = users.get("_by_name", {})
    u2n = {v: k for k, v in n2u.items()}
    return {"name_to_uid": n2u, "uid_to_name": u2n}

def is_checked_today(name):
    if USE_SHEETS:
        return sheets_is_checked_today(name)
    logp = BASE_DIR/"checkin_log.json"
    try:
        log = json.loads(logp.read_text("utf-8"))
    except Exception:
        log = {}
    today = _today_str()
    s = set(log.get(today, []))
    return name in s

def mark_checked(name, when_iso, user_id):
    if USE_SHEETS:
        return sheets_mark_checkin(name, when_iso, user_id)
    logp = BASE_DIR/"checkin_log.json"
    try:
        log = json.loads(logp.read_text("utf-8"))
    except Exception:
        log = {}
    today = _today_str()
    s = set(log.get(today, []))
    s.add(name)
    log[today] = sorted(s)
    logp.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")

def list_unchecked_names():
    if USE_SHEETS:
        return sheets_list_unchecked_names()
    users = load_users()["name_to_uid"].keys()
    return [n for n in users if not is_checked_today(n)]

# ---------- LINE API ----------
def line_reply(reply_token, text):
    with ApiClient(config) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=reply_token, messages=[TextMessage(text=text)])
        )

def line_push(user_id, text):
    with ApiClient(config) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(to=user_id, messages=[TextMessage(text=text)])
        )

# ---------- Routes ----------
@app.get("/")
def home():
    return jsonify({"status":"ok","service":"facecheck-backend","tz":TZ_NAME,"sheets":USE_SHEETS}), 200

@app.get("/health")
def health():
    return "OK", 200

# 新增：GET /webhook（診斷用）
@app.get("/webhook")
def webhook_debug_get():
    return "Webhook endpoint is alive (GET). Use POST for LINE.", 200

@app.post("/webhook")
def webhook():
    """
    重要：任何錯誤都回 200，避免 LINE 持續重試造成風暴。
    無效簽章也回 200，但記 log。
    """
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.exception("Invalid signature on /webhook")
    except Exception:
        app.logger.exception("Exception on /webhook")
    return "OK", 200

@app.get("/users")
def route_users():
    return jsonify(load_users()), 200

@app.get("/debug/sheets")
def debug_sheets():
    return jsonify(_probe_sheet()), 200

@app.get("/debug/sheets/write")
def debug_sheets_write():
    """實際寫入一列，驗證服務帳戶是否有『編輯』權限 & 指定分頁存在。"""
    try:
        if not USE_SHEETS:
            return jsonify(ok=False, error="USE_SHEETS_FALSE",
                           message="未啟用 Sheets。請設定 GOOGLE_SERVICE_ACCOUNT_* 與 GOOGLE_SHEET_ID（或 SHEET_ID）。"), 400
        sheets_upsert_user("測試用名字", "TEST_USER_ID")
        sheets_mark_checkin("測試用名字", _now_local().isoformat(), "TEST_USER_ID")
        return jsonify(ok=True), 200
    except GspreadAPIError as ge:
        code = getattr(getattr(ge, "response", None), "status_code", None)
        msg  = str(ge)
        hint = None
        if code == 403 or "PERMISSION" in msg.upper():
            hint = "權限不足：請把試算表分享給服務帳戶（可編輯）：{}".format(
                (_sa_info_cache or {}).get("client_email", "<service-account-email>")
            )
        return jsonify(ok=False, error="GSPREAD_API_ERROR", code=code, message=msg, hint=hint), 500
    except Exception as e:
        return jsonify(ok=False, error=type(e).__name__, message=str(e)), 500

@app.get("/push")
def push_to_name():
    name = (request.args.get("name") or "").strip()
    text = (request.args.get("text") or "測試訊息").strip()
    if not name: return "缺少 ?name=參數", 400
    users = load_users()
    user_id = users.get("name_to_uid", {}).get(name)
    if not user_id: return f"找不到此姓名的綁定：{name}", 404
    try:
        line_push(user_id, text)
        return f"Push 成功 → {name} ({user_id})：{text}", 200
    except ApiException as e:
        return f"Push 失敗 status={getattr(e,'status',None)}, body={getattr(e,'body',None)}", 500

@app.post("/checkin")
def checkin():
    if request.headers.get("X-API-KEY") != API_KEY:
        return jsonify({"error":"unauthorized"}), 401

    data = request.get_json(force=True, silent=True) or {}
    name = str(data.get("name","")).strip()
    when_iso = data.get("when")
    if not name: return jsonify({"error":"name required"}), 400
    if not when_iso:
        when_iso = _now_local().isoformat()

    users = load_users()["name_to_uid"]
    uid = users.get(name)
    if not uid:
        return jsonify({"error":f"name '{name}' not bound"}), 404

    if is_checked_today(name):
        return jsonify({"status":"duplicate", "date": _today_str()}), 200

    local_dt = _parse_when_to_local(when_iso)
    hh, mm = _parse_hhmm(LATE_CUTOFF)
    cutoff = local_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
    is_late = local_dt > cutoff

    msg = f"{name} 簽到成功（{local_dt.strftime('%Y-%m-%d %H:%M:%S')}）"
    if is_late:
        msg += f"（已超過 {LATE_CUTOFF}）"

    try:
        line_push(uid, msg)
        mark_checked(name, when_iso, uid)
        return jsonify({"status":"ok","pushed":True}), 200
    except ApiException as e:
        return jsonify({"status":"line_error","detail":getattr(e,'body',None)}), 502

@app.post("/cron/morning_scan")
def cron_morning_scan():
    if request.headers.get("X-API-KEY") != API_KEY:
        return jsonify({"error":"unauthorized"}), 401

    today = _now_local()
    if ONLY_WEEKDAYS and today.weekday() >= 5:  # 0=Mon ~ 6=Sun
        return jsonify({"status":"skip_weekend"}), 200

    hh, mm = _parse_hhmm(LATE_CUTOFF)
    cutoff = today.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if today <= cutoff:
        return jsonify({"status":"not_after_cutoff"}), 200

    data = load_users()
    n2u = data["name_to_uid"]
    unchecked = list_unchecked_names()
    count = 0
    for name in unchecked:
        uid = n2u.get(name)
        if not uid: continue
        text = f"{name}，提醒您今日尚未簽到（{today.strftime('%Y-%m-%d')}）。"
        try:
            line_push(uid, text)
            count += 1
        except ApiException:
            pass
    return jsonify({"status":"ok","reminded":count,"unchecked":unchecked}), 200

# ---------- Debug：檢查 Sheets 連線（含私鑰健檢結果） ----------
def _probe_sheet():
    info = {"USE_SHEETS": USE_SHEETS, "tz": TZ_NAME}
    try:
        if USE_SHEETS:
            sa = _load_sa_info()  # 會丟出詳細錯誤
            info["service_account_email"] = sa.get("client_email")
            info["sheet_id"] = os.environ.get("GOOGLE_SHEET_ID") or os.environ.get("SHEET_ID")
            info["private_key_head"] = sa.get("private_key","")[:30]
            info["private_key_tail"] = sa.get("private_key","")[-30:]
            info["private_key_len"]  = len(sa.get("private_key",""))
            info["private_key_has_begin"] = sa.get("private_key","").startswith("-----BEGIN PRIVATE KEY-----")
            info["private_key_has_end"]   = sa.get("private_key","").endswith("-----END PRIVATE KEY-----")
            gc = _gspread_client()
            sh = gc.open_by_key(info["sheet_id"])
            info["title"] = sh.title
            info["worksheets"] = [ws.title for ws in sh.worksheets()]
            try:
                u = sh.worksheet("users")
                info["users_rows"] = len(u.get_all_records())
            except Exception as e:
                info["users_error"] = str(e)
            try:
                c = sh.worksheet("checkin_log")
                info["checkin_log_rows"] = len(c.get_all_records())
            except Exception as e:
                info["checkin_log_error"] = str(e)
            info["ok"] = True
        else:
            info["hint"] = _gs_reason or "USE_SHEETS=False：檢查 GOOGLE_SERVICE_ACCOUNT_* 與 GOOGLE_SHEET_ID（或 SHEET_ID）"
    except Exception as e:
        info["error"] = str(e)
    return info

# ---------- 事件處理 ----------
@handler.add(MessageEvent, message=TextMessageContent)
def handle_text(event):
    user_id = getattr(event.source, "user_id", None)
    text = (event.message.text or "").strip()
    reply_text = None
    print(f"[EVENT] userId={user_id}, text={text}")

    if text.startswith("查詢"):
        data = load_users()
        bound_name = data.get("uid_to_name", {}).get(user_id)
        if bound_name:
            reply_text = f"目前已綁定 {bound_name} ✅"
        else:
            reply_text = "目前尚未綁定，請輸入：連結 你的名字"

    elif text.startswith("連結 "):
        new_name = text[3:].strip()
        if new_name and user_id:
            try:
                upsert_user(new_name, user_id)
                confirm = f"已綁定：{new_name} ✅\n你的 userId 是：{user_id}"
                reply_text = "綁定成功！已傳送確認訊息至你的 LINE。"
                try:
                    line_push(user_id, confirm)
                    print(f"[LINE] Push 綁定確認 → {user_id}: {confirm}")
                except ApiException as e_push:
                    print("[LINE][ERROR][push-confirm]", getattr(e_push,"status",None), getattr(e_push,"body",None))
                    reply_text = confirm
            except GspreadAPIError as ge:
                code = getattr(getattr(ge, "response", None), "status_code", None)
                msg  = str(ge)
                hint = None
                if code == 403 or "PERMISSION" in msg.upper():
                    hint = "權限不足：請把試算表分享給服務帳戶（可編輯）：{}".format(
                        (_sa_info_cache or {}).get("client_email", "<service-account-email>")
                    )
                print("[BIND][ERROR] GspreadAPIError", code, msg, "| hint:", hint)
                reply_text = "綁定失敗：後端寫入試算表權限不足，請稍後再試或通知管理員。"
            except Exception as e:
                diag = ""
                try:
                    sa = _load_sa_info()
                    pk = sa.get("private_key","")
                    diag = f" | pk_begin={pk.startswith('-----BEGIN PRIVATE KEY-----')} pk_end={pk.endswith('-----END PRIVATE KEY-----')} pk_len={len(pk)}"
                except Exception as ee:
                    diag = f" | SA_LOAD_ERROR={ee}"
                print("[BIND][ERROR]", e, diag)
                reply_text = "綁定失敗：後端服務暫時無法連線，稍後再試。"
        else:
            reply_text = '❌ 請輸入格式：連結 你的名字'

    else:
        data = load_users()
        bound_name = data.get("uid_to_name", {}).get(user_id)
        if bound_name:
            reply_text = "功能列表：\n1) 連結 你的名字（修改綁定）\n2) 查詢（查看綁定狀態）"
        else:
            reply_text = '請輸入「連結 你的名字」進行綁定'

    try:
        line_reply(event.reply_token, reply_text)
        print(f"[LINE] Reply 成功 → {user_id}: {reply_text}")
    except ApiException as e:
        print("[LINE][ERROR][reply]", getattr(e,"status",None), getattr(e,"body",None))
        try:
            if user_id:
                line_push(user_id, f"(fallback) {reply_text}")
        except ApiException as e2:
            print("[LINE][ERROR][push-fallback]", getattr(e2,"status",None), getattr(e2,"body",None))

# ---------- WSGI 入口（雲端用） ----------
def create_app():
    return app

# 讓 gunicorn 可 import 到 app
app = create_app()

# 啟動時列出所有路由（方便 Render Log 確認 /webhook 是否掛上）
print("[ROUTES] url_map =", app.url_map)
for r in app.url_map.iter_rules():
    print("[ROUTE]", r.rule, "methods=", sorted(r.methods))

# ---------- 本機進入點 ----------
if __name__ == "__main__":
    public_url = start_ngrok_if_needed(local_host="127.0.0.1", port=PORT, webhook_path="/webhook")
    if public_url:
        print("[提示] 到 LINE Developers 貼上：", f"{public_url}/webhook")
        print("      並確保 Use webhook = ON，再按 Verify。")
    print(f"[FLASK] http://127.0.0.1:{PORT}  /  http://{HOST}:{PORT}")
    app.run(host=HOST, port=PORT)
