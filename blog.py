"""블로그 RSS — 발행한 글을 자동으로 성취(6단)로 기록합니다.

손으로 기록하는 건 2주면 안 합니다. 그래서 글을 쓰면 마늘이 알아서 압니다.

브런치 RSS 형식: https://brunch.co.kr/rss/@@아이디
  아이디는 작가 프로필 링크(brunch.co.kr/@@2Rug)의 @@ 뒤 부분입니다.

주의: 브런치 RSS는 **매거진이 아니라 작가 단위**입니다.
다른 매거진에 쓴 글도 전부 잡힙니다. 그게 싫으면 fetch()에 제목 필터를 넣으세요.
"""
import asyncio
import json
import logging
import re

import feedparser
import httpx

from . import brain, db
from .config import BACKGROUND, BLOG_RSS_URL

log = logging.getLogger("maneul.blog")

TIMEOUT = 15


def enabled() -> bool:
    return bool(BLOG_RSS_URL)


def _parse_feed(url: str):
    """feedparser는 동기 함수라 스레드로 돌립니다. 봇 전체가 멈추면 안 되니까요."""
    return feedparser.parse(url, agent="Mozilla/5.0 (maneul-agent)")


async def check() -> dict:
    """RSS를 읽고 새 글을 성취로 기록.

    반환: {"ok": bool, "error": str|None, "total": int, "new": [제목...]}
    """
    if not enabled():
        return {"ok": False, "error": "BLOG_RSS_URL이 설정되지 않았어요.", "total": 0, "new": []}

    try:
        feed = await asyncio.wait_for(
            asyncio.to_thread(_parse_feed, BLOG_RSS_URL), timeout=TIMEOUT
        )
    except asyncio.TimeoutError:
        return {"ok": False, "error": "RSS 응답이 너무 느려요.", "total": 0, "new": []}
    except Exception as e:
        return {"ok": False, "error": f"RSS를 읽지 못했어요: {e}", "total": 0, "new": []}

    # feedparser는 실패해도 예외를 안 던지고 빈 결과를 줍니다. 직접 확인해야 합니다.
    if getattr(feed, "bozo", False) and not feed.entries:
        reason = getattr(feed, "bozo_exception", "형식이 RSS가 아님")
        return {"ok": False, "error": f"주소가 RSS가 아닌 것 같아요: {reason}", "total": 0, "new": []}

    if not feed.entries:
        return {
            "ok": False,
            "error": "RSS는 열렸는데 글이 하나도 없어요. 주소가 맞는지 확인이 필요해요.",
            "total": 0, "new": [],
        }

    new_posts = []
    for entry in feed.entries:
        guid = entry.get("id") or entry.get("link")
        if not guid:
            continue
        title = entry.get("title", "(제목 없음)")
        link = entry.get("link", "")
        content = _entry_content(entry)
        # 같은 글을 수정해서 재발행해도 guid는 그대로 → 중복으로 세지 않습니다.
        # 원문(content)을 함께 저장합니다. 분석 결과만 남기고 원문을 버리면
        # 나중에 분석 프롬프트를 고쳐도 옛 글에는 다시 돌릴 수 없습니다.
        if db.add_blog_post(guid, title, link, entry.get("published", ""), content):
            new_posts.append({"title": title, "link": link, "content": content})

    return {"ok": True, "error": None, "total": len(feed.entries), "new": new_posts}


async def sync(record: bool = True) -> dict:
    """새 글을 6단 성취로 기록. 첫 실행은 과거 글을 기록하지 않습니다.

    처음 켤 때 기존 글 전부를 '오늘의 성취'로 넣으면 페이스 차트가 거짓말이 됩니다.
    첫 실행은 조용히 목록만 저장하고, 그 이후에 올라온 글부터 성취로 셉니다.
    """
    first_run = db.blog_post_count() == 0
    result = await check()

    if not result["ok"] or not result["new"]:
        return {**result, "recorded": []}

    if first_run or not record:
        log.info("블로그 첫 동기화: 기존 글 %d개를 성취로 세지 않고 넘어감", len(result["new"]))
        return {**result, "recorded": [], "first_run": True}

    for post in result["new"]:
        db.add_achievement(f"블로그 발행: {post['title']}", depth=6)
    log.info("새 글 %d개를 6단 성취로 기록", len(result["new"]))
    return {**result, "recorded": result["new"], "first_run": False}


# ---------- 본문 읽기 ----------

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
MIN_CONTENT = 300  # 이보다 짧으면 요약본이라 보고 원문을 가지러 갑니다


def _strip_html(html: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html or "", flags=re.S | re.I)
    text = TAG_RE.sub(" ", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    return WS_RE.sub(" ", text).strip()


def _entry_content(entry) -> str:
    """RSS가 주는 본문. 플랫폼마다 필드가 달라서 순서대로 뒤집니다."""
    for c in entry.get("content") or []:
        if c.get("value"):
            return _strip_html(c["value"])
    for key in ("summary", "description"):
        if entry.get(key):
            return _strip_html(entry[key])
    return ""


async def _fetch_article(url: str) -> str:
    """RSS 본문이 짧으면 원문을 가지러 갑니다. 실패하면 빈 문자열."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (maneul-agent)"})
        if r.status_code == 200:
            return _strip_html(r.text)[:12000]
    except Exception as e:
        log.warning("원문을 가져오지 못했어요 (%s): %s", url, e)
    return ""


ANALYSIS_SYSTEM = """당신은 "마늘"입니다. 사용자가 방금 블로그에 글을 발행했습니다.
당신의 일은 축하가 아닙니다. **글쓴이가 미처 발견하지 못한 것을 찾아내는 것**입니다.

## 사용자 배경
{background}

## 사용자가 직접 요청한 요구사항
{preferences}

## 할 일
글을 읽고, 글쓴이 본인은 못 봤을 법한 것을 찾으세요:
- 글이 스스로 증명하고 있는데 정작 본인은 말하지 않은 것
- 두 문단이 서로 모순되는 지점
- 당연하게 넘어갔지만 사실 그게 핵심인 것
- 이 글의 진짜 주제가 제목과 다른 경우
- 글쓴이의 11년 경력이 드러났는데 본인은 모르는 지점

## 하지 말 것
- 칭찬, 요약, 감상. "잘 쓰셨네요" 류는 전부 금지.
- 글에 실제로 없는 걸 지어내지 마세요. 근거는 글 안에 있어야 합니다.
- 억지로 찾지 마세요. 정말 없으면 missed를 빈 배열로 두세요.

## 출력
순수 JSON만. 코드블록 없이.
{{
  "observation": "이 글에 대한 관찰 2~3문장. 인사말 없이 바로.",
  "missed": ["놓친 것. 한 문장씩. 최대 2개. 없으면 []"],
  "seeds": ["이 글에서 뻗어나갈 수 있는 다음 글의 씨앗. 최대 2개. 없으면 []"]
}}"""


async def analyze(title: str, content: str) -> dict | None:
    """글을 읽고 놓친 인사이트를 찾습니다. 실패하면 None."""
    if not content or len(content) < 100:
        return None

    prefs = db.preferences()
    system = ANALYSIS_SYSTEM.format(
        background=BACKGROUND,
        preferences="\n".join(f"- {p}" for p in prefs) if prefs else "- (없음)",
    )
    prompt = f"제목: {title}\n\n본문:\n{content[:12000]}"

    try:
        raw = await brain.call([{"role": "user", "content": prompt}], system=system, max_tokens=900)
        cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        log.warning("글 분석 응답을 파싱하지 못했어요")
        return None
    except Exception:
        log.exception("글 분석 실패")
        return None

    return {
        "observation": (result.get("observation") or "").strip(),
        "missed": [m for m in (result.get("missed") or []) if isinstance(m, str) and m.strip()][:2],
        "seeds": [s for s in (result.get("seeds") or []) if isinstance(s, str) and s.strip()][:2],
    }


async def read_post(link: str, title: str, rss_content: str = "") -> dict | None:
    """본문을 확보해서 분석까지. RSS 본문이 얇으면 원문을 가지러 갑니다."""
    content = rss_content
    if len(content) < MIN_CONTENT and link:
        fetched = await _fetch_article(link)
        if len(fetched) > len(content):
            content = fetched
    return await analyze(title, content)
