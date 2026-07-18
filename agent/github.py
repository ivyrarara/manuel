"""GitHub — 만든 것과 고친 것을 자동으로 성취로 기록합니다.

Events API를 씁니다: https://api.github.com/users/{user}/events/public
호출 한 번으로 **모든 공개 저장소**의 활동이 다 옵니다. 저장소를 하나하나
등록할 필요가 없어서, 100일 동안 새 앱을 만들어도 설정을 안 바꿔도 됩니다.

인플레이션 방지: 하루에 커밋을 9번 해도 **성취는 1건**입니다.
커밋 수를 세면 숫자만 예뻐집니다. 사다리는 깊이를 재지 횟수를 재지 않습니다.

그리고 커밋 수는 **성과가 아니라 마찰**입니다. 원하는 수정이 안 돼서 반복하는 거니까요.
1커밋은 알고 있었다는 뜻, 9커밋은 아홉 번 틀렸다는 뜻입니다.
그래서 커밋 수는 성취의 크기가 아니라 그날의 고생을 나타내는 값으로 씁니다.

한계: Events API는 최근 ~90일, 300개까지만 보관합니다. 하루 한 번 도는 데는 충분합니다.
인증 없이 시간당 60회 — 하루 1~2회 호출이면 여유롭습니다.
"""
import asyncio
import collections
import logging

import httpx

from . import db
from .config import GITHUB_USER

log = logging.getLogger("maneul.github")

API = "https://api.github.com/users/{user}/events/public"
TIMEOUT = 15


def enabled() -> bool:
    return bool(GITHUB_USER)


async def fetch_events() -> dict:
    """반환: {"ok": bool, "error": str|None, "events": [...]}"""
    if not enabled():
        return {"ok": False, "error": "GITHUB_USER가 설정되지 않았어요.", "events": []}

    url = API.format(user=GITHUB_USER)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.get(
                url,
                params={"per_page": 100},
                headers={"Accept": "application/vnd.github+json", "User-Agent": "maneul-agent"},
            )
    except Exception as e:
        return {"ok": False, "error": f"GitHub에 연결하지 못했어요: {e}", "events": []}

    if r.status_code == 404:
        return {"ok": False, "error": f"'{GITHUB_USER}' 사용자를 찾을 수 없어요. 아이디를 확인해주세요.", "events": []}
    if r.status_code == 403:
        remaining = r.headers.get("X-RateLimit-Remaining", "?")
        return {"ok": False, "error": f"GitHub 호출 제한에 걸렸어요 (남은 횟수 {remaining}). 잠시 후 다시요.", "events": []}
    if r.status_code != 200:
        return {"ok": False, "error": f"GitHub이 {r.status_code}를 반환했어요.", "events": []}

    try:
        events = r.json()
    except Exception:
        return {"ok": False, "error": "GitHub 응답을 읽지 못했어요.", "events": []}

    if not isinstance(events, list):
        return {"ok": False, "error": "GitHub 응답 형식이 예상과 달라요.", "events": []}

    return {"ok": True, "error": None, "events": events}


# GitHub 웹 업로드가 자동으로 붙이는 문구들. 성취 문구로 쓰면 아무 의미가 없습니다.
GENERIC_MESSAGES = (
    "add files via upload", "update", "create", "delete", "initial commit",
    "rename", "upload", "add file", "commit",
)


def _extract_message(raw: str) -> str | None:
    """커밋 메시지 한 줄을 성취 문구 후보로 정리. 못 쓰면 None.

    "Merge branch 'x' into y" 같은 순수 브랜치 병합은 실제 작업이 아니므로 버립니다.
    "Merge pull request #N from owner/branch" 는 GitHub 웹에서 PR을 merge하면
    자동으로 붙는 첫 줄일 뿐이고, 그 다음 줄에 진짜 PR 제목이 옵니다 — 그 부분은
    실제 작업 내용이므로 살립니다. 이 구분을 안 하면 PR을 merge할 때마다
    그날 한 일이 통째로 버려집니다.
    """
    if not raw:
        return None
    first_line, _, rest = raw.partition("\n")
    first_line = first_line.strip()
    lowered = first_line.lower()
    if lowered.startswith("merge pull request"):
        body = rest.strip().split("\n")[0].strip()
        return body or None
    if lowered.startswith("merge"):
        return None
    return first_line or None


def _pick_message(messages: list[str]) -> str:
    """그날의 커밋 중 가장 설명적인 것 하나.

    웹 업로드를 쓰면 메시지가 전부 'Add files via upload'입니다.
    그걸 성취로 남기면 100일 뒤에 의미 없는 기록만 쌓입니다.
    사람이 직접 쓴 메시지가 하나라도 있으면 그걸 씁니다.
    """
    cleaned = [c for c in (_extract_message(m) for m in messages) if c]
    meaningful = [
        m for m in cleaned
        if not any(m.lower().startswith(g) for g in GENERIC_MESSAGES)
    ]
    if meaningful:
        return meaningful[0][:80]  # 가장 최근 것 (이벤트는 최신순으로 옴)
    return ""


def _digest(events: list) -> list[dict]:
    """이벤트를 묶습니다. 반환: [{"key","repo","date","kind","commits","message"}...]

    push/create는 (저장소, 날짜) 단위 — 하루 9커밋도 성취 1건.
    merge(PR 병합)는 PR 번호 단위로 따로 묶습니다. 같은 날 (저장소, 날짜) 묶음이
    이미 기록된 뒤에 또 다른 PR을 merge해도, push 쪽 dedup 키는 그날 하루로
    고정돼 있어서 다시 안 잡힙니다. merge를 PR 번호로 독립적으로 추적해야
    "그날 이미 뭔가 기록됐다"는 이유로 나중에 merge한 PR이 조용히 묻히지 않습니다.
    """
    pushes = collections.defaultdict(lambda: {"commits": 0, "messages": []})
    creates = {}
    merges = {}

    for e in events:
        repo = (e.get("repo") or {}).get("name")
        created = e.get("created_at") or ""
        if not repo or not created:
            continue
        date = created[:10]
        etype = e.get("type")

        if etype == "PushEvent":
            commits = (e.get("payload") or {}).get("commits") or []
            bucket = pushes[(repo, date)]
            bucket["commits"] += len(commits)
            bucket["messages"] += [c.get("message", "") for c in commits if c.get("message")]

        elif etype == "CreateEvent":
            if (e.get("payload") or {}).get("ref_type") == "repository":
                creates[(repo, date)] = True

        elif etype == "PullRequestEvent":
            payload = e.get("payload") or {}
            pr = payload.get("pull_request") or {}
            number = pr.get("number")
            if payload.get("action") == "closed" and pr.get("merged") and number is not None:
                merges[(repo, number)] = {
                    "title": (pr.get("title") or f"PR #{number}").strip(),
                    "date": (pr.get("merged_at") or created)[:10],
                }

    items = []
    for (repo, date) in creates:
        items.append({
            "key": f"create:{repo}:{date}", "repo": repo, "date": date,
            "kind": "create", "commits": 0, "message": "",
        })
    for (repo, date), b in pushes.items():
        if not b["commits"]:
            continue
        items.append({
            "key": f"push:{repo}:{date}", "repo": repo, "date": date,
            "kind": "push", "commits": b["commits"], "message": _pick_message(b["messages"]),
        })
    for (repo, number), m in merges.items():
        items.append({
            "key": f"merge:{repo}:{number}", "repo": repo, "date": m["date"],
            "kind": "merge", "commits": 0, "message": m["title"],
        })

    return sorted(items, key=lambda i: i["date"])


def _describe(item: dict) -> tuple[str, int]:
    """성취 문구와 사다리 단수.

    커밋 수는 **성과가 아니라 마찰**입니다. 1커밋은 알고 있었다는 뜻이고,
    9커밋은 아홉 번 틀렸다는 뜻입니다. 같은 '개선 1건'이지만 과정이 다릅니다.
    그래서 "커밋 8개"(많이 했다)가 아니라 "8번 만에"(고생했다)로 적습니다.
    숫자를 세는 것과 그 숫자가 뭘 뜻하는지 아는 건 다른 일입니다.
    """
    short = item["repo"].split("/")[-1]
    if item["kind"] == "create":
        return f"새 저장소 만듦: {short}", 4  # 구현

    if item["kind"] == "merge":
        # PR 제목은 _digest에서 이미 항상 채워서 넘깁니다 (없으면 "PR #N"으로 대체).
        return f"{short} PR 병합 — {item['message']}", 5  # 개선

    detail = f" — {item['message']}" if item["message"] else ""
    friction = f" ({item['commits']}번 만에)" if item["commits"] > 2 else ""
    return f"{short} 개선{friction}{detail}", 5  # 개선


async def sync() -> dict:
    """새 활동을 성취로 기록.

    첫 실행은 과거 활동을 성취로 세지 않습니다 — 오늘 성취로 넣으면
    페이스 차트가 1일차부터 거짓말이 됩니다.
    """
    first_run = db.github_activity_count() == 0
    result = await fetch_events()
    if not result["ok"]:
        return {**result, "new": [], "recorded": []}

    items = _digest(result["events"])
    new = [i for i in items if not db.github_activity_exists(i["key"])]

    for i in new:
        db.add_github_activity(i["key"], i["repo"], i["date"], i["commits"])

    if first_run:
        log.info("GitHub 첫 동기화: 과거 활동 %d건을 성취로 세지 않고 넘어감", len(new))
        return {**result, "new": new, "recorded": [], "first_run": True, "total": len(items)}

    recorded = []
    for i in new:
        text, depth = _describe(i)
        db.add_achievement(text, depth=depth)
        recorded.append(text)

    if recorded:
        log.info("GitHub 활동 %d건을 성취로 기록", len(recorded))
    return {**result, "new": new, "recorded": recorded, "first_run": False, "total": len(items)}
