"""치지직 클립 다운로더 (비공식 API 사용)
실행: pip install -r requirements.txt && python app.py  ->  http://localhost:5000
"""
import hashlib
import hmac
import html
import os
import re
import secrets
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote, urlencode, urlparse

import requests
from flask import (Flask, Response, abort, jsonify, redirect, request, send_from_directory, session)

app = Flask(__name__, static_folder="static")

# ── 보안 설정 (Render > Environment 에서 입력) ─────────────────────────
# SITE_PASSWORD        : 접속 비밀번호 (선택)
# NAVER_CLIENT_ID/SECRET : 네이버 로그인 앱 키 (선택)
# ALLOWED_NAVER_IDS    : 입장을 허용할 네이버 고유 ID (쉼표로 구분)
# NID_AUT, NID_SES     : 성인 인증된 네이버 계정 쿠키 (로그인 보호가 켜진 경우에만 사용됨)
# SECRET_KEY           : 로그인 세션 서명용 임의 문자열 (권장)
# BASE_URL             : 사이트 주소 (예: https://내이름.onrender.com, 선택)
SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "")
NAVER_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
ALLOWED_NAVER_IDS = {x.strip() for x in os.environ.get("ALLOWED_NAVER_IDS", "").split(",") if x.strip()}
NAVER_OAUTH = bool(NAVER_CLIENT_ID and NAVER_CLIENT_SECRET)
GATED = bool(SITE_PASSWORD or NAVER_OAUTH)                       # 로그인 화면 사용 여부
CAN_LOGIN = bool(SITE_PASSWORD or (NAVER_OAUTH and ALLOWED_NAVER_IDS))  # 실제로 입장 가능한 수단이 있는지
NID_AUT = os.environ.get("NID_AUT", "")
NID_SES = os.environ.get("NID_SES", "")
AUTH_COOKIES = ({"NID_AUT": NID_AUT, "NID_SES": NID_SES}
                if CAN_LOGIN and NID_AUT and NID_SES else {})
app.secret_key = os.environ.get("SECRET_KEY") or hashlib.sha256(
    ("chzzk-dl|" + SITE_PASSWORD + NAVER_CLIENT_SECRET).encode()).hexdigest()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("RENDER") == "true",
                  PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30)
FAILS = {}
MSGS = {"state": "로그인 확인에 실패했어요. 다시 시도해 주세요.",
        "fail": "네이버 로그인 중 오류가 났어요. 잠시 후 다시 시도해 주세요.",
        "denied": "허용되지 않은 계정이에요."}

LOGIN_HTML = """<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>로그인</title>
<style>:root{--bg:#f6f7f5;--fg:#16191a;--card:#fff;--line:#dfe3e1;--accent:#00d68a;--err:#c0392b}
@media(prefers-color-scheme:dark){:root{--bg:#101312;--fg:#eef2f0;--card:#181c1b;--line:#2a302e;--accent:#00e595;--err:#ff7a6b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:"Pretendard","Apple SD Gothic Neo","Noto Sans KR",system-ui,sans-serif;padding:18px}
main{max-width:380px;margin:18vh auto 0}h1{font-size:1.4rem;margin:0 0 18px}
input,button,a.naver{display:block;font:inherit;width:100%;padding:13px 14px;border-radius:10px;border:1px solid var(--line);text-align:center;text-decoration:none}
input{background:var(--card);color:inherit;margin-bottom:10px;text-align:left}
button{background:var(--accent);color:#04251a;font-weight:700;border:0;cursor:pointer}
a.naver{background:#03c75a;color:#fff;font-weight:700;border:0;margin-bottom:14px}
.err{color:var(--err);min-height:1.4em;margin:0 0 10px;font-size:.92rem}</style></head><body><main>
<h1>치지직 클립 다운로더</h1><div class="err">{{msg}}</div>{{naver}}{{pw}}</main></body></html>"""


def render_login(msg=""):
    naver = '<a class="naver" href="/naver/login">네이버로 로그인</a>' if NAVER_OAUTH else ""
    pw = ('<form method="post" action="/login"><input type="password" name="pw" '
          'placeholder="비밀번호" autocomplete="current-password"><button>입장</button></form>'
          if SITE_PASSWORD else "")
    return (LOGIN_HTML.replace("{{msg}}", html.escape(msg))
            .replace("{{naver}}", naver).replace("{{pw}}", pw))


def redirect_uri():
    base = (os.environ.get("BASE_URL") or request.url_root).rstrip("/")
    if os.environ.get("RENDER") == "true" and base.startswith("http://"):
        base = "https://" + base[7:]
    return base + "/naver/callback"


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Referer": "https://chzzk.naver.com/",
           "Origin": "https://chzzk.naver.com"}

CLIP_URL_RE = re.compile(r"^https?://(?:www\.)?chzzk\.naver\.com/clips/([\w-]+)")
CLIP_INFO = "https://api.chzzk.naver.com/service/v1/play-info/clip/{}"
CLIP_DETAIL = "https://api.chzzk.naver.com/service/v1/clips/{}/detail"
PLAYBACK = ("https://apis.naver.com/neonplayer/vodplay/v2/playback/{}"
            "?key={}&sid=2099&env=real&lc=ko&cpl=ko")

# 다운로드 프록시가 접근할 수 있는 도메인 (SSRF 방지)
ALLOWED_SUFFIXES = (".naver.com", ".navercdn.com", ".pstatic.net", ".akamaized.net")


def host_allowed(url: str) -> bool:
    p = urlparse(url)
    h = (p.hostname or "").lower()
    return p.scheme == "https" and any(h.endswith(s) for s in ALLOWED_SUFFIXES)


def get_json(url):
    r = requests.get(url, headers=HEADERS, cookies=AUTH_COOKIES or None, timeout=10)
    r.raise_for_status()
    return r.json()


def parse_mpd(xml_text):
    """DASH MPD에서 영상 Representation(직접 MP4 BaseURL)만 추출. 해상도별 최고 bandwidth 1개."""
    root = ET.fromstring(xml_text)
    best = {}
    for aset in root.iter():
        if not aset.tag.endswith("AdaptationSet"):
            continue
        for rep in aset:
            if not rep.tag.endswith("Representation"):
                continue
            mime = rep.get("mimeType") or aset.get("mimeType") or ""
            is_video = (mime.startswith("video") or aset.get("contentType") == "video"
                        or bool(rep.get("height")))
            if not is_video or mime.startswith("audio"):
                continue
            base = next((c.text.strip() for c in rep
                         if c.tag.endswith("BaseURL") and c.text), None)
            if not base:
                continue
            h = int(rep.get("height") or 0)
            bw = int(rep.get("bandwidth") or 0)
            if h not in best or bw > best[h]["bandwidth"]:
                best[h] = {"height": h, "bandwidth": bw, "url": base}
    return sorted(best.values(), key=lambda x: (x["height"], x["bandwidth"]), reverse=True)


def fetch_playback(video_id, in_key):
    r = requests.get(PLAYBACK.format(video_id, in_key or ""),
                     headers={**HEADERS, "Accept": "application/dash+xml"},
                     cookies=AUTH_COOKIES or None, timeout=10)
    r.raise_for_status()
    return r.text


def safe_json(url):
    try:
        return get_json(url).get("content") or {}
    except Exception:
        return {}


OPEN_ENDPOINTS = ("login", "logout", "naver_login", "naver_callback")


@app.before_request
def gate():
    if not GATED or request.endpoint in OPEN_ENDPOINTS or session.get("ok"):
        return None
    if request.path.startswith("/api/"):
        return jsonify(error="로그인이 필요해요. 페이지를 새로고침해 주세요."), 401
    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not GATED:
        return redirect("/")
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    now = time.time()
    tries = FAILS[ip] = [t for t in FAILS.get(ip, []) if now - t < 600]
    msg = MSGS.get(request.args.get("m", ""), "")
    if request.method == "POST" and SITE_PASSWORD:
        pw = request.form.get("pw", "")
        if len(tries) >= 8:
            msg = "시도가 너무 많아요. 10분 뒤에 다시 해주세요."
        elif hmac.compare_digest(pw.encode(), SITE_PASSWORD.encode()):
            session["ok"] = True
            session.permanent = True
            FAILS.pop(ip, None)
            return redirect("/")
        else:
            tries.append(now)
            time.sleep(1)
            msg = "비밀번호가 맞지 않아요."
    return render_login(msg)


@app.get("/naver/login")
def naver_login():
    if not NAVER_OAUTH:
        abort(404)
    session["state"] = secrets.token_urlsafe(24)
    q = urlencode({"response_type": "code", "client_id": NAVER_CLIENT_ID,
                   "redirect_uri": redirect_uri(), "state": session["state"]})
    return redirect("https://nid.naver.com/oauth2.0/authorize?" + q)


@app.get("/naver/callback")
def naver_callback():
    if not NAVER_OAUTH:
        abort(404)
    state = session.pop("state", None)
    code = request.args.get("code", "")
    if not state or not code or not hmac.compare_digest(state, request.args.get("state", "")):
        return redirect("/login?m=state")
    try:
        t = requests.get("https://nid.naver.com/oauth2.0/token", timeout=10, params={
            "grant_type": "authorization_code", "client_id": NAVER_CLIENT_ID,
            "client_secret": NAVER_CLIENT_SECRET, "code": code, "state": state}).json()
        token = t.get("access_token")
        me = requests.get("https://openapi.naver.com/v1/nid/me", timeout=10,
                          headers={"Authorization": f"Bearer {token}"}).json()
        uid = (me.get("response") or {}).get("id")
    except Exception:
        return redirect("/login?m=fail")
    if not token or not uid:
        return redirect("/login?m=fail")
    if uid in ALLOWED_NAVER_IDS:
        session["ok"] = True
        session.permanent = True
        return redirect("/")
    if not ALLOWED_NAVER_IDS:  # 최초 설정 모드: 본인 ID 확인용
        return Response(
            "<meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<body style='font-family:sans-serif;padding:20px;line-height:1.6'>"
            "<h3>설정 모드</h3><p>내 네이버 ID:</p>"
            f"<p><b style='word-break:break-all'>{html.escape(uid)}</b></p>"
            "<p>Render &gt; Environment 에 <code>ALLOWED_NAVER_IDS</code> 로 위 값을 넣고 "
            "저장하면 이 계정으로만 입장할 수 있어요.</p>")
    return redirect("/login?m=denied")


@app.get("/logout")
def logout():
    session.clear()
    return redirect("/login")


ADULT_HTML = """<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>성인 인증 안내</title>
<style>:root{--bg:#f6f7f5;--fg:#16191a;--mute:#6b7473;--card:#fff;--line:#dfe3e1;--accent:#00d68a;--ok:#0a8f5b;--err:#c0392b}
@media(prefers-color-scheme:dark){:root{--bg:#101312;--fg:#eef2f0;--mute:#8f9a97;--card:#181c1b;--line:#2a302e;--accent:#00e595;--ok:#00e595;--err:#ff7a6b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:"Pretendard","Apple SD Gothic Neo","Noto Sans KR",system-ui,sans-serif;padding:18px;line-height:1.6}
main{max-width:520px;margin:5vh auto}h1{font-size:1.4rem;margin:0 0 14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin-bottom:12px}
.card h2{font-size:1rem;margin:0 0 6px}.mute{color:var(--mute);font-size:.9rem}
.ok{color:var(--ok);font-weight:700}.no{color:var(--err);font-weight:700}
a.btn{display:block;background:var(--accent);color:#04251a;font-weight:700;text-align:center;text-decoration:none;border-radius:10px;padding:12px;margin-top:8px}
a.sub{display:inline-block;color:var(--mute);margin-top:6px}ol{padding-left:20px;margin:6px 0}</style></head><body><main>
<h1>연령 제한 클립 · 성인 인증</h1>
<div class="card"><h2>현재 상태</h2>{{status}}</div>
<div class="card"><h2>1. 네이버에서 성인 인증 받기</h2>
<div class="mute">성인 인증은 네이버가 직접 처리해요. 이 사이트에서는 인증을 대신할 수 없어요. 실명 확인된 계정으로 성인 인증을 마쳐주세요.</div>
<a class="btn" href="https://chzzk.naver.com" target="_blank" rel="noopener">치지직 열기 (연령 제한 클립에서 인증 안내 확인)</a>
<a class="btn" href="https://help.naver.com" target="_blank" rel="noopener">네이버 고객센터</a></div>
<div class="card"><h2>2. 인증된 계정의 쿠키를 사이트에 등록</h2>
<ol><li>컴퓨터 크롬에서 인증된 계정으로 naver.com 로그인</li>
<li><b>F12</b> → <b>Application</b> → <b>Cookies</b> → naver.com</li>
<li><b>NID_AUT</b>, <b>NID_SES</b> 값을 복사</li>
<li>Render → Environment 에 같은 이름으로 입력하고 저장</li></ol>
<div class="mute">쿠키는 비밀번호와 같아요. 채팅이나 GitHub에는 올리지 마세요. 만료되면 새 값으로 바꿔주세요.</div></div>
<a class="btn" href="/">클립 다운로드 화면으로</a>
</main></body></html>"""


@app.get("/adult")
def adult():
    if AUTH_COOKIES:
        status = '<span class="ok">● 성인 인증 쿠키가 등록돼 있어요.</span><div class="mute">연령 제한 클립을 받을 수 있어요. 안 되면 쿠키가 만료됐을 수 있어요.</div>'
    elif NID_AUT and NID_SES:
        status = '<span class="no">● 쿠키는 있지만 사용되지 않아요.</span><div class="mute">보호를 위해 접속 비밀번호나 네이버 로그인 설정이 있어야 쿠키가 쓰여요.</div>'
    else:
        status = '<span class="no">● 쿠키가 등록되지 않았어요.</span><div class="mute">아래 순서대로 등록하면 연령 제한 클립을 받을 수 있어요.</div>'
    return ADULT_HTML.replace("{{status}}", status)


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/api/info")
def info():
    m = CLIP_URL_RE.match((request.args.get("url") or "").strip())
    if not m:
        return jsonify(error="치지직 클립 주소가 아니에요. https://chzzk.naver.com/clips/… 형식으로 넣어주세요."), 400
    clip_id = m.group(1)
    play = safe_json(CLIP_INFO.format(clip_id))
    detail = safe_json(CLIP_DETAIL.format(clip_id))  # 제목·썸네일은 여기에 있음
    video_id = play.get("videoId") or detail.get("videoId")
    if not video_id:
        msg = ("클립을 찾을 수 없어요. 삭제·비공개이거나 로그인 쿠키가 만료됐을 수 있어요."
               if AUTH_COOKIES else
               "클립을 찾을 수 없어요. 삭제·비공개이거나 성인 인증이 필요한 클립일 수 있어요.")
        return jsonify(error=msg), 404
    try:
        xml_text = fetch_playback(video_id, play.get("inKey"))
        qualities = parse_mpd(xml_text)
    except requests.RequestException:
        return jsonify(error="치지직 서버에서 정보를 가져오지 못했어요. 잠시 후 다시 시도해 주세요."), 502
    except ET.ParseError:
        return jsonify(error="재생 정보를 읽지 못했어요. 치지직 API가 바뀌었을 수 있어요."), 502
    if not qualities:
        return jsonify(error="이 클립은 직접 내려받을 수 있는 MP4가 없어요."), 422
    thumb = detail.get("thumbnailImageUrl") or play.get("thumbnailImageUrl") or ""
    if thumb.startswith("http://"):
        thumb = "https://" + thumb[7:]
    return jsonify(
        title=detail.get("clipTitle") or play.get("contentTitle") or clip_id,
        thumbnail=("/api/thumb?u=" + quote(thumb, safe="")) if thumb else "",
        duration=detail.get("duration") or play.get("duration"),
        qualities=[{"label": f"{q['height']}p" if q["height"] else "원본", "url": q["url"]}
                   for q in qualities])


@app.get("/api/thumb")
def thumb():
    url = request.args.get("u", "")
    if not host_allowed(url):
        abort(400)
    up = requests.get(url, headers=HEADERS, timeout=10)
    if up.status_code != 200:
        abort(404)
    return Response(up.content, headers={
        "Content-Type": up.headers.get("Content-Type", "image/jpeg"),
        "Cache-Control": "public, max-age=3600"})


@app.get("/api/debug")
def debug():
    """진단용: 어떤 화질/키가 내려오는지 확인 (inKey는 노출 안 함)."""
    m = CLIP_URL_RE.match((request.args.get("url") or "").strip())
    if not m:
        abort(400)
    play = safe_json(CLIP_INFO.format(m.group(1)))
    detail = safe_json(CLIP_DETAIL.format(m.group(1)))
    out = {"cookies_configured": bool(AUTH_COOKIES), "play_keys": sorted(play), "detail_keys": sorted(detail),
           "thumbnail_raw": detail.get("thumbnailImageUrl")}
    try:
        xml_text = fetch_playback(play.get("videoId") or detail.get("videoId"), play.get("inKey"))
        out["representations"] = [{"height": q["height"], "bandwidth": q["bandwidth"]}
                                  for q in parse_mpd(xml_text)]
        out["mpd_head"] = re.sub(r"https?://\S+", "<url>", xml_text)[:2500]
    except Exception as e:
        out["error"] = repr(e)
    return jsonify(out)


@app.get("/api/download")
def download():
    url = request.args.get("u", "")
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", request.args.get("name", "chzzk-clip"))
    name = name.strip(" .")[:100] or "chzzk-clip"
    for _ in range(4):  # 리다이렉트도 도메인 검사
        if not host_allowed(url):
            abort(400)
        up = requests.get(url, headers=HEADERS, stream=True, timeout=15, allow_redirects=False)
        if up.is_redirect:
            url = up.headers.get("Location", "")
            continue
        break
    else:
        abort(502)
    if up.status_code != 200:
        abort(502)
    h = {"Content-Type": "video/mp4",
         "Content-Disposition": f"attachment; filename=\"clip.mp4\"; filename*=UTF-8''{quote(name)}.mp4"}
    if up.headers.get("Content-Length"):
        h["Content-Length"] = up.headers["Content-Length"]
    return Response(up.iter_content(64 * 1024), headers=h)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
