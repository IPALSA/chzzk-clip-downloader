"""치지직 클립 다운로더 (비공식 API 사용)
실행: pip install -r requirements.txt && python app.py  ->  http://localhost:5000
"""
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, quote

import requests
from flask import Flask, Response, abort, jsonify, request, send_from_directory

app = Flask(__name__, static_folder="static")

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
    r = requests.get(url, headers=HEADERS, timeout=10)
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
                     headers={**HEADERS, "Accept": "application/dash+xml"}, timeout=10)
    r.raise_for_status()
    return r.text


def safe_json(url):
    try:
        return get_json(url).get("content") or {}
    except Exception:
        return {}


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
        return jsonify(error="클립을 찾을 수 없어요. 삭제됐거나 비공개일 수 있어요."), 404
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
    out = {"play_keys": sorted(play), "detail_keys": sorted(detail),
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
