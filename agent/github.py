"""GitHub — 만든 것과 고친 것을 자동으로 성취로 기록합니다.

Events API를 씁니다: https://api.github.com/users/{user}/events/public
호출 한 번으로 **모든 공개 저장소**의 활동이 다 옵니다. 저장소를 하나하나
등록할 필요가 없어서, 100일 동안 새 앱을 만들어도 설정을 안 바꿔도 됩니다.

⚠️ Events API의 PushEvent.payload는 더 이상 commits 배열을 안 줍니다(GitHub이
제거했습니다). before/head SHA만 오므로, 실제 커밋 개수·메시지는 Compare API
(`/repos/{repo}/compare/{before}...{head}`)로 따로 가져와야 합니다.
PullRequestEvent.payload.pull_request도 축약판이라 title이 없어서, PR 병합은
`/repos/{repo}/pulls/{number}`을 따로 불러 제목을 가져옵니다. 이 두 개를 몰랐던
채로 짜면 이벤트는 받아오면서도 실제 활동은 전부 0건으로 보입니다.

인플레이션 방지: 하루에 커밋을 9번 해도 **성취는 1건**입니다.
커밋 수를 세면 숫자만 예뻐집니다. 사다리는 깊이를 재지 횟수를 재지 않습니다.

그리고 커밋 수는 **성과가 아니라 마찰**입니다. 원하는 수정이 안 돼서 반복하는 거니까요.
1커밋은 알고 있었다는 뜻, 9커밋은 아홉 번 틀렸다는 뜻입니다.
그래서 커밋 수는 성취의 크기가 아니라 그날의 고생을 나타내는 값으로 씁니다.

한계: Events API는 최근 ~90일, 300개까지만 보관합니다. 하루 한 번 도는 데는 충분합니다.
인증 없이 시간당 60회 — 이벤트 조회 1회 + 그날 푸시/병합 건수만큼 추가 호출이 붙지만,
하루 1~2회 도는 데는 충분히 여유롭습니다.
"""
import collections
import logging

import httpx

from . import db
from .config import GITHUB_EXCLUDE_REPOS, GITHUB_USER

log = logging.getLogger("maneul.github")

API = "https://api.github.com/users/{user}/events/public"
HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "maneul-agent"}
TIMEOUT = 15


def enabled() -> bool:
    return bool(GITHUB_USER)


async def fetch_events(client: httpx.AsyncClient) -> dict:
    """반환: {"ok": bool, "error": str|None, "events": [...]}"""
    if not enabled():
        return {"ok": False, "error": "GITHUB_USER가 설정되지 않았어요.", "events": []}

    url = API.format(user=GITHUB_USER)
    try:
        r = await client.get(url, params={"per_page": 100}, headers=HEADERS)
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


async def _fetch_compare_commits(
    client: httpx.AsyncClient, repo: str, before: str, head: str
) -> list[str] | None:
    """이 푸시로 새로 들어온 커밋 메시지들. 실패하면 None(있었다는 것만 알고 내용은 모름).

    before == head면 새 커밋이 없는 푸시(태그 이동 등)이므로 빈 리스트를 바로 반환합니다.
    """
    if not before or not head or before == head:
        return []
    url = f"https://api.github.com/repos/{repo}/compare/{before}...{head}"
    try:
        r = await client.get(url, headers=HEADERS)
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception:
        return None
    commits = data.get("commits") or []
    return [c.get("commit", {}).get("message", "") for c in commits if c.get("commit")]


async def _fetch_pr_title(client: httpx.AsyncClient, repo: str, number: int) -> str | None:
    """PullRequestEvent에는 제목이 없어서 PR 상세를 따로 불러옵니다. 실패하면 None."""
    url = f"https://api.github.com/repos/{repo}/pulls/{number}"
    try:
        r = await client.get(url, headers=HEADERS)
        if r.status_code != 200:
            return None
        return (r.json().get("title") or "").strip() or None
    except Exception:
        return None


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


def _group_events(events: list) -> tuple[dict, dict, dict]:
    """이벤트를 종류별로 묶습니다 (API 호출 없이, 순수 분류만).

    push/merge 전부 (저장소, 날짜) 단위로 묶습니다 — 그날 커밋을 몇 번 하고
    PR을 몇 개 merge했든, 성취는 하루 1건입니다. merge_prs는 (저장소, 날짜) ->
    그날 merge된 PR 번호 목록(최신순)입니다.

    GITHUB_EXCLUDE_REPOS에 있는 저장소는 아예 걸러냅니다 — 마늘 자기 자신을
    고치는 건 사용자의 성장이 아니라 봇 정비이므로, 성취로 셀 대상이 아닙니다.
    """
    push_refs = collections.defaultdict(list)
    creates = {}
    merge_prs = collections.defaultdict(list)

    for e in events:
        repo = (e.get("repo") or {}).get("name")
        created = e.get("created_at") or ""
        if not repo or not created:
            continue
        if repo.split("/")[-1] in GITHUB_EXCLUDE_REPOS:
            continue
        date = created[:10]
        etype = e.get("type")
        payload = e.get("payload") or {}

        if etype == "PushEvent":
            before, head = payload.get("before"), payload.get("head")
            if before and head:
                push_refs[(repo, date)].append((before, head))

        elif etype == "CreateEvent":
            if payload.get("ref_type") == "repository":
                creates[(repo, date)] = True

        elif etype == "PullRequestEvent":
            # Events API는 병합을 action="merged"로 줍니다 (webhook의
            # action="closed" + pull_request.merged=true 형태가 아닙니다).
            if payload.get("action") == "merged":
                pr = payload.get("pull_request") or {}
                number = pr.get("number")
                if number is not None:
                    merge_prs[(repo, date)].append(number)

    return push_refs, creates, merge_prs


async def _digest(client: httpx.AsyncClient, events: list) -> list[dict]:
    """이벤트를 묶고, 부족한 정보(커밋 메시지·PR 제목)를 추가로 가져와 완성합니다.

    반환: [{"key","repo","date","kind","commits","message","merged_count"}...]

    (저장소, 날짜) 단위로 최대 1건만 만듭니다 — 커밋을 몇 번 하고 PR을 몇 개
    merge했든 그날의 성취는 1건입니다. 그날 merge된 PR이 있으면 제목을
    최우선으로 씁니다(가장 설명적이니까). 없으면 커밋 메시지 중에서 고릅니다.
    """
    push_refs, creates, merge_prs = _group_events(events)

    items = []
    for (repo, date) in creates:
        items.append({
            "key": f"create:{repo}:{date}", "repo": repo, "date": date,
            "kind": "create", "commits": 0, "message": "", "merged_count": 0,
        })

    for (repo, date) in set(push_refs) | set(merge_prs):
        messages: list[str] = []
        commit_count = 0
        lookup_failed = False
        for before, head in push_refs.get((repo, date), []):
            result = await _fetch_compare_commits(client, repo, before, head)
            if result is None:
                # Compare API 실패 — 몰라도 최소 1건은 있었다고 침. 실제 작업을
                # "조회 실패"라는 이유로 0건 처리해버리면 안 됩니다.
                lookup_failed = True
                commit_count += 1
            else:
                commit_count += len(result)
                messages += result

        pr_titles = []
        for number in merge_prs.get((repo, date), []):
            title = await _fetch_pr_title(client, repo, number)
            pr_titles.append(title or f"PR #{number}")

        if commit_count == 0 and not lookup_failed and not pr_titles:
            continue  # before == head뿐이었던, 확실히 새 커밋도 merge도 없던 날

        message = pr_titles[0] if pr_titles else _pick_message(messages)
        items.append({
            "key": f"push:{repo}:{date}", "repo": repo, "date": date,
            "kind": "push", "commits": commit_count, "message": message,
            "merged_count": len(pr_titles),
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

    if item.get("merged_count"):
        # PR 제목은 _digest에서 이미 항상 채워서 넘깁니다 (없으면 "PR #N"으로 대체).
        # 하루에 여러 PR을 merge해도 성취는 1건이라, 개수만 살짝 표시합니다.
        count = f" ({item['merged_count']}건 병합)" if item["merged_count"] > 1 else ""
        return f"{short} PR 병합{count} — {item['message']}", 5  # 개선

    detail = f" — {item['message']}" if item["message"] else ""
    friction = f" ({item['commits']}번 만에)" if item["commits"] > 2 else ""
    return f"{short} 개선{friction}{detail}", 5  # 개선


async def sync() -> dict:
    """새 활동을 성취로 기록.

    100일 시작일 이전 활동은 성취로 세지 않습니다 — 그걸 오늘 성취로 넣으면
    페이스 차트가 1일차부터 거짓말이 됩니다. 반대로 시작일 이후 활동은, 이번이
    이 계정에 대한 사상 첫 동기화라도(= 방금 GITHUB_USER를 연결했어도) 이미
    챌린지 안에서 일어난 진짜 활동이므로 곧바로 성취로 셉니다 — "첫 동기화는
    과거 기록 취급"이라는 규칙이 오늘 한 일까지 묻어버리면 안 됩니다.
    """
    first_run = db.github_activity_count() == 0
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        result = await fetch_events(client)
        if not result["ok"]:
            return {**result, "new": [], "recorded": []}
        items = await _digest(client, result["events"])

    new = [i for i in items if not db.github_activity_exists(i["key"])]

    for i in new:
        db.add_github_activity(i["key"], i["repo"], i["date"], i["commits"])

    start = db.start_date_str()
    countable = [i for i in new if i["date"] >= start]
    backlog = len(new) - len(countable)

    recorded = []
    for i in countable:
        text, depth = _describe(i)
        db.add_achievement(text, depth=depth)
        recorded.append(text)

    if backlog:
        log.info("GitHub 활동 %d건은 시작일(%s) 이전 기록이라 성취로 세지 않음", backlog, start)
    if recorded:
        log.info("GitHub 활동 %d건을 성취로 기록", len(recorded))

    return {
        **result, "new": new, "recorded": recorded,
        "backlog": backlog, "first_run": first_run, "total": len(items),
    }
