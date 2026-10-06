# 치지직 클립 다운로더

## 로컬 실행
    pip install -r requirements.txt
    python app.py
https://chzzk-clip-downloader.onrender.com/ 접속 후 클립 주소 입력.

## 배포 (Render 등)
- Build: `pip install -r requirements.txt`
- Start: `gunicorn app:app --timeout 120`

## 동작 방식
클립 주소 → `play-info/clip/{id}`에서 videoId·inKey → neonplayer 재생 정보(DASH MPD) → 직접 MP4 주소 추출 → 서버가 중계해 다운로드.
비공식 API라 치지직이 바꾸면 동작이 멈출 수 있어요.
