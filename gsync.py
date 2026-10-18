"""Google ToDo（Google Tasks）連動。

「今日」の画面に入るタスク（期限が今日まで＋今日やる）だけを、専用リスト「gototask 今日」へ送る。
スマホで完了にしたら gototask も済にし、スマホでそのリストに足したタスクは受信箱（今日やる）へ取り込む。
gototask 側で済・やらない・今日から外したものは Google 側から消す。

対応は各 TASKS.md の字下げ行 `google: <ID>` で持つ。認可情報は data/（gitignore済）に置く。
標準ライブラリだけで動く（OAuth はループバック＋PKCE）。
"""
import base64
import datetime
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import tasksmd as T

DATA = T.DATA
CLIENT = DATA / "google_client.json"   # Google Cloud で作った「デスクトップアプリ」の OAuth クライアント
TOKEN = DATA / "google_token.json"
STATE = DATA / "google_state.json"
LOG = DATA / "google_sync.log"
STATUS = DATA / "google_status.json"  # 画面に出す最終同期の結果
SCOPE = "https://www.googleapis.com/auth/tasks"
LIST_TITLE = "gototask 今日"
API = "https://tasks.googleapis.com/tasks/v1"
INTERVAL = 300  # 自動同期の間隔（秒）


# ---------- 認可 ----------

def _client():
    d = json.loads(CLIENT.read_text(encoding="utf-8"))
    return d.get("installed") or d.get("web") or d


def _post_form(url, data):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def _save(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(path, 0o600)


def authorize():
    """ブラウザで許可してもらい、リフレッシュトークンを保存する。"""
    c = _client()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    got = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            got.update({k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<p>gototask：Google ToDo の許可を受け取りました。このタブは閉じてかまいません。</p>".encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    redirect = f"http://127.0.0.1:{srv.server_port}"
    url = c.get("auth_uri", "https://accounts.google.com/o/oauth2/auth") + "?" + urllib.parse.urlencode(dict(
        client_id=c["client_id"], redirect_uri=redirect, response_type="code", scope=SCOPE,
        access_type="offline", prompt="consent", code_challenge=challenge, code_challenge_method="S256", state=state))
    print("ブラウザで許可してください（開かなければ次のURLを開く）:\n" + url)
    webbrowser.open(url)
    while "code" not in got and "error" not in got:
        srv.handle_request()
    if got.get("state") != state or "code" not in got:
        raise SystemExit(f"許可できませんでした: {got.get('error', 'state不一致')}")
    tok = _post_form(c.get("token_uri", "https://oauth2.googleapis.com/token"), dict(
        code=got["code"], client_id=c["client_id"], client_secret=c.get("client_secret", ""),
        redirect_uri=redirect, grant_type="authorization_code", code_verifier=verifier))
    tok["obtained"] = time.time()
    _save(TOKEN, tok)
    print("許可を保存しました。")


def _access_token():
    tok = json.loads(TOKEN.read_text(encoding="utf-8"))
    if tok.get("obtained", 0) + tok.get("expires_in", 0) - 60 > time.time():
        return tok["access_token"]
    c = _client()
    new = _post_form(c.get("token_uri", "https://oauth2.googleapis.com/token"), dict(
        client_id=c["client_id"], client_secret=c.get("client_secret", ""),
        refresh_token=tok["refresh_token"], grant_type="refresh_token"))
    tok.update(new, obtained=time.time())
    _save(TOKEN, tok)
    return tok["access_token"]


def _api(method, path, body=None, params=None):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Authorization", "Bearer " + _access_token())
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise RuntimeError(f"Google ToDo {method} {path}: {e.code} {e.read()[:300]!r}") from None


# ---------- 同期 ----------

def _state():
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}


def _list_id():
    state = _state()
    lid = state.get("list_id")
    if lid and _api("GET", f"/users/@me/lists/{lid}"):
        return lid
    lists = (_api("GET", "/users/@me/lists", params={"maxResults": 100}) or {}).get("items", [])
    lid = next((l["id"] for l in lists if l["title"] == LIST_TITLE), None)
    if not lid:
        lid = _api("POST", "/users/@me/lists", {"title": LIST_TITLE})["id"]
    _save(STATE, {"list_id": lid})  # 別のリストになったら、覚えていた ID も捨てる
    return lid


def _remote(lid):
    items, token = [], None
    while True:
        params = {"showCompleted": "true", "showHidden": "true", "maxResults": 100}
        if token:
            params["pageToken"] = token
        r = _api("GET", f"/lists/{lid}/tasks", params=params) or {}
        items += r.get("items", [])
        token = r.get("nextPageToken")
        if not token:
            return {t["id"]: t for t in items}


def _payload(t, today):
    due = t["due"] if T.is_due(t, today) else today
    notes = t["project"] + (f" / {t['category']}" if t["category"] else "")
    note = "\n".join(t["memo"]).strip()
    if note:
        notes += "\n\n" + note[:800]
    title = ("★ " if t["important"] else "") + t["title"] + "".join(f" @{w}" for w in t["who"])
    return {"title": title, "notes": notes, "due": f"{due}T00:00:00.000Z"}


def _apply(text, t, fn, *a, **kw):
    """1タスクを書き換え、書き換え後のそのタスクも返す（同じタスクに続けて手を入れるため）。"""
    text = fn(text, t, *a, **kw)
    t2 = next(x for x in T.parse(text, T.default_project(t["file"]))[0] if x["line"] == t["line"])
    t2["file"] = t["file"]
    return text, t2


def sync():
    """1回同期する。戻り値は変更の要約。"""
    lid = _list_id()
    remote = _remote(lid)
    today = T.today_str()
    summary = {"送信": 0, "完了取込": 0, "追加取込": 0, "Google側から削除": 0}
    # gototask が送った・取り込んだ ID。手元から消えた ID は「こちらで消した」なので、受信箱に戻さず Google 側も消す
    known = set(_state().get("known", []))
    with T.LOCK:
        tasks, _, _ = T.load()
        open_ = [t for t in tasks if not t["done"]]
        due, picked, _carry = T.today_groups(open_)
        want = {(t["file"], t["line"]) for t in due + picked}
        mapped = set()
        for f in T.files():
            text = before = T.read(f)
            # 後ろのタスクから書き換えると、前のタスクの行番号がずれない
            for t in sorted((t for t in tasks if t["file"] == f), key=lambda t: -t["line"]):
                gid = t["google"]
                wanted = (f, t["line"]) in want and not t["done"]
                if gid.startswith("skip:"):
                    # スマホで消した日は送り直さない。日付が変われば解除
                    if gid[5:] == today:
                        continue
                    if wanted:
                        r = _api("POST", f"/lists/{lid}/tasks", _payload(t, today))
                        text, t = _apply(text, t, T.set_sub, "google", r["id"])
                        summary["送信"] += 1
                    else:
                        text, t = _apply(text, t, T.set_sub, "google", "")
                    continue
                if gid:
                    mapped.add(gid)
                    known.add(gid)
                    r = remote.get(gid)
                    if r is None or r.get("deleted"):
                        # スマホで消した＝今日から外した（期限で入っているものも今日は送り直さない）
                        text, t = _apply(text, t, T.set_sub, "google", f"skip:{today}")
                        text, t = _apply(text, t, T.set_today, False)
                    elif r.get("status") == "completed":
                        text, t = _apply(text, t, T.set_sub, "google", "")
                        if not t["done"]:
                            text, t = _apply(text, t, T.set_state, "x")
                            summary["完了取込"] += 1
                    elif not wanted:
                        _api("DELETE", f"/lists/{lid}/tasks/{gid}")
                        text, t = _apply(text, t, T.set_sub, "google", "")
                        summary["Google側から削除"] += 1
                    else:
                        p = _payload(t, today)
                        if any(r.get(k) != p[k] for k in ("title", "notes")) or (r.get("due") or "")[:10] != p["due"][:10]:
                            _api("PATCH", f"/lists/{lid}/tasks/{gid}", p)
                elif wanted:
                    r = _api("POST", f"/lists/{lid}/tasks", _payload(t, today))
                    text, t = _apply(text, t, T.set_sub, "google", r["id"])
                    known.add(r["id"])
                    summary["送信"] += 1
            if text != before:
                T.write(f, text)
        # スマホでこのリストに直接足したもの → 受信箱へ（今日やる）
        text = before = T.read(T.INBOX_FILE)
        for gid, r in remote.items():
            if gid in mapped or r.get("deleted") or r.get("status") == "completed" or not r.get("title", "").strip():
                continue
            if gid in known:
                _api("DELETE", f"/lists/{lid}/tasks/{gid}")
                known.discard(gid)
                summary["Google側から削除"] += 1
                continue
            title = r["title"].strip()
            text = T.add_task(text, T.INBOX, "", title, r.get("notes", ""), "", today, default=T.INBOX)
            t = next(x for x in T.parse(text, T.INBOX)[0] if x["project"] == T.INBOX and x["desc"] == title
                     and not x["google"])
            text = T.set_sub(text, t, "google", gid)
            known.add(gid)
            summary["追加取込"] += 1
        if text != before:
            T.write(T.INBOX_FILE, text)
        # Google 側から消えた ID は忘れる（今回送ったものは remote にまだ無いので known 側に残る）
        gone = {gid for gid, r in remote.items() if r.get("deleted")} | (set(_state().get("known", [])) - set(remote))
        _save(STATE, {"list_id": lid, "known": sorted(known - gone)})
    return summary


def configured():
    return CLIENT.exists() and TOKEN.exists()


def _log(msg):
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def start_background():
    """サーバー内で常時同期（INTERVAL 秒ごと＋画面で操作した直後）。未設定なら何もしない。"""
    def loop():
        while True:
            T.SYNC_NOW.wait(INTERVAL)
            T.SYNC_NOW.clear()
            if not configured():
                continue
            try:
                s = sync()
                if any(s.values()):
                    _log(json.dumps(s, ensure_ascii=False))
                _save(STATUS, {"ok": True, "at": f"{datetime.datetime.now():%H:%M}"})
            except Exception as e:  # 通信断・許可切れでもサーバーは止めない
                _log(f"失敗: {e}")
                auth = "invalid_grant" in str(e) or "401" in str(e)
                _save(STATUS, {"ok": False, "at": f"{datetime.datetime.now():%H:%M}",
                               "msg": "許可が切れました（gtask gsync --auth）" if auth else "同期に失敗しました（通信など）"})
    T.SYNC_NOW.set()  # 起動直後に1回
    threading.Thread(target=loop, daemon=True).start()
