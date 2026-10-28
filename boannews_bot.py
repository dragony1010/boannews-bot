"""
보안뉴스 최신 기사 -> 디스코드 알림 봇
- RSS로 새 기사를 찾아 웹훅으로 전송합니다.
- ANTHROPIC_API_KEY가 설정돼 있을 때만 AI 요약을 붙입니다. (없으면 완전 무료로 동작)
- 필요 패키지: pip install requests feedparser
  (AI 요약까지 쓸 때만: pip install trafilatura anthropic)
- 실행 예:
    export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
    export ANTHROPIC_API_KEY="sk-ant-..."
    python boannews_bot.py            # 계속 실행 (INTERVAL 초마다 확인)
    RUN_ONCE=1 python boannews_bot.py # 한 번만 확인 (cron / GitHub Actions용)
"""
import os
import re
import json
import time
import html

import requests
import feedparser

FEED_URL = os.getenv("FEED_URL", "http://www.boannews.com/media/news_rss.xml")
WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
SEEN_FILE = os.getenv("SEEN_FILE", "seen.json")
INTERVAL = int(os.getenv("INTERVAL", "600"))
FIRST_RUN_POST = int(os.getenv("FIRST_RUN_POST", "3"))
MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")  # 요약용으로는 빠르고 저렴한 모델이면 충분
HEADERS = {"User-Agent": "Mozilla/5.0 (BoanNews-Discord-Bot)"}

# 포럼 채널용 설정
# FORUM=1      -> 기사마다 새 포럼 게시물 생성 (기사 제목이 게시물 제목)
# THREAD_ID=.. -> 이미 만든 게시물(스레드) 하나에 계속 올리기
# 둘 다 없으면 일반 텍스트 채널로 전송
FORUM = os.getenv("FORUM") == "1"
THREAD_ID = os.getenv("THREAD_ID", "").strip()

USE_AI = bool(os.getenv("ANTHROPIC_API_KEY"))
if USE_AI:  # API 키가 있을 때만 요약 관련 라이브러리를 불러옴
    import trafilatura
    import anthropic
    client = anthropic.Anthropic()

SYSTEM_PROMPT = """당신은 보안 뉴스 큐레이터입니다. 주어진 기사를 한국어로 요약하세요.
형식:
- 핵심 내용 3줄 이내 불릿(각 줄 '• '로 시작)
- 마지막 줄에 '🎯 시사점: ' 뒤에 실무자가 주목할 점 한 문장
기사에 없는 내용은 추측하지 말고, 본문이 부실하면 확인된 내용만 짧게 쓰세요.
원문 문장을 길게 그대로 옮기지 말고 자신의 말로 요약하세요."""


def load_seen():
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen[-500:], f, ensure_ascii=False)


def clean(text, limit=300):
    text = html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def fetch_entries():
    r = requests.get(FEED_URL, headers=HEADERS, timeout=15)
    r.raise_for_status()
    print("피드 응답:", r.status_code, r.headers.get("Content-Type"), "주소:", r.url)
    return feedparser.parse(r.content).entries


def fetch_article_text(url):
    """기사 페이지에서 본문만 자동 추출 (사이트별 선택자 없이 동작)"""
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        r.encoding = r.apparent_encoding  # EUC-KR 등 인코딩 자동 판별
        text = trafilatura.extract(r.text, include_comments=False, include_tables=False)
        return (text or "")[:8000]  # 너무 긴 본문은 잘라서 토큰 비용 절약
    except Exception as ex:
        print("본문 추출 실패:", ex)
        return ""


def summarize(title, body):
    if not body:
        return None
    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"제목: {title}\n\n본문:\n{body}"}],
        )
        return "".join(b.text for b in msg.content if b.type == "text").strip()
    except Exception as ex:
        print("요약 실패:", ex)
        return None


def post_to_discord(entry, summary):
    embed = {
        "title": clean(entry.get("title"), 250),
        "url": entry.get("link"),
        # 요약에 실패하면 RSS에 들어있는 짧은 설명으로 대체
        "description": (summary or clean(entry.get("summary")))[:4000],
        "color": 0xC62828,
        "footer": {"text": f"보안뉴스 · {entry.get('published', '')}" + (" · AI 요약" if summary else "")},
    }
    payload = {"embeds": [embed]}
    params = {}
    if THREAD_ID:
        params["thread_id"] = THREAD_ID
    elif FORUM:
        payload["thread_name"] = clean(entry.get("title"), 95)  # 게시물 제목은 최대 100자
    while True:
        resp = requests.post(WEBHOOK_URL, params=params, json=payload, timeout=15)
        if resp.status_code == 429:
            time.sleep(float(resp.json().get("retry_after", 2)))
            continue
        resp.raise_for_status()
        break
    time.sleep(1)


def check_once():
    seen = load_seen()
    entries = fetch_entries()
    new = [e for e in entries if e.get("link") and e.get("link") not in seen]
    to_post = new[:FIRST_RUN_POST] if not seen else new
    print(f"피드 기사 {len(entries)}개 / 기존 기록 {len(seen)}개 / 새 기사 {len(new)}개")

    for e in reversed(to_post):
        summary = None
        if USE_AI:
            body = fetch_article_text(e.get("link"))
            summary = summarize(e.get("title", ""), body)
        post_to_discord(e, summary)
        print("posted:", e.get("title"))
        seen.append(e.get("link"))
        save_seen(seen)  # 기사마다 저장해서 중간에 죽어도 중복 전송 방지
        if USE_AI:
            time.sleep(2)  # 본문을 가져올 때는 사이트에 연속 요청하지 않도록 간격 두기

    seen.extend(e.get("link") for e in reversed(new) if e.get("link") not in seen)
    save_seen(seen)


def main():
    if os.getenv("RUN_ONCE"):
        check_once()
        return
    while True:
        try:
            check_once()
        except Exception as ex:
            print("error:", ex)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
