"""저장소 — SQLite 파일 하나.

시각 처리 원칙: 시간은 **Python이 로컬 타임존으로만** 찍습니다.
SQLite의 datetime('now')는 UTC를 반환하기 때문에, 그걸 Python의 로컬 시간과
섞어 쓰면 '최근 7일' 같은 계산이 타임존 차이만큼(한국은 9시간) 어긋납니다.

checkins 테이블이 이 프로젝트의 자산입니다. 코드가 아니라 이 테이블을 들고 갑니다.
"""
import collections
import hashlib
import re
import sqlite3
from datetime import date, datetime, timedelta

from .config import DB_PATH, SEED_PREFERENCES, START_DATE, TOTAL_DAYS, TZ

SCHEMA = """
create table if not exists messages (
    id         integer primary key autoincrement,
    role       text not null check (role in ('user', 'assistant')),
    content    text not null,
    created_at text not null
);

create table if not exists insights (
    id         integer primary key autoincrement,
    message_id integer references messages(id) on delete cascade,
    text       text not null,
    type       text not null check (type in ('learning', 'action', 'achievement')),
    depth      integer,       -- achievement일 때만: 사다리 1~6단
    done       integer not null default 0,
    created_at text not null
);

-- GitHub 활동. (저장소, 날짜) 단위로 묶어서 하루 9커밋도 성취 1건입니다.
create table if not exists github_activity (
    key        text primary key,   -- push:owner/repo:2026-07-16
    repo       text not null,
    date       text not null,
    commits    integer not null default 0,
    created_at text not null
);

-- 블로그에서 읽어온 글. 발행 = 사다리 6단 성취로 자동 기록됩니다.
create table if not exists blog_posts (
    guid       text primary key,
    title      text not null,
    link       text,
    published  text,
    content    text,              -- 글 원문. 분석 프롬프트를 고쳐도 옛 글에 다시 돌릴 수 있게.
    is_magazine integer not null default 1,  -- 성취로 세는 매거진 글인가
    created_at text not null
);

create table if not exists preferences (
    id         integer primary key autoincrement,
    text       text not null unique,
    status     text not null default 'active',  -- pending / active / rejected
    active     integer not null default 1,
    created_at text not null
);

-- /memo로 사용자가 직접 남긴 메모. 체크인 판단과 대화 시스템 프롬프트 양쪽에서 참고합니다.
create table if not exists memos (
    id         integer primary key autoincrement,
    text       text not null,
    tags       text,                    -- 쉼표로 구분된 태그들. 없으면 NULL.
    done       integer not null default 0,  -- 완료 처리해도 지우지 않고 남겨서 회고에 씁니다.
    created_at text not null
);

-- 자율 체크인 기록 — 100일 뒤 들고 갈 데이터.
create table if not exists checkins (
    id             integer primary key autoincrement,
    spoke          integer not null,
    trigger        text,          -- 무엇이 방아쇠였나 (avoidance/stale_action/drift/...)
    confidence     integer,       -- 얼마나 확신했나 (0~100)
    reason         text,          -- 판단 근거
    message        text,          -- 실제로 보낸 말
    unspoken       text,          -- 침묵했을 때, 말했다면 했을 말
    prompt_version text,          -- 어떤 버전의 마늘이 내린 판단인가
    raw_input      text,          -- 판단의 입력 원문 (그때 마늘이 본 것 전부)
    feedback       text,          -- 탭으로 받은 채점 결과
    feedback_at    text,
    created_at     text not null
);

-- 프롬프트 버전 원본. 해시만 있으면 나중에 해석이 불가능하므로 전문을 보관합니다.
create table if not exists prompt_versions (
    version     text primary key,
    template    text not null,
    preferences text not null,
    first_seen  text not null
);

create table if not exists meta (
    key   text primary key,
    value text not null
);
"""

TS_FORMAT = "%Y-%m-%d %H:%M:%S.%f"


def _now() -> str:
    """저장용 현재 시각. 이 함수가 시각의 유일한 출처입니다."""
    return datetime.now(TZ).strftime(TS_FORMAT)


def _cutoff(days: int) -> str:
    return (datetime.now(TZ) - timedelta(days=days)).strftime(TS_FORMAT)


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, TS_FORMAT)


def _today() -> date:
    """오늘 날짜. 반드시 사용자의 타임존 기준입니다.

    date.today()는 시스템 타임존을 봅니다. Railway 컨테이너는 UTC로 돌기 때문에,
    토론토 저녁 8시에 이미 UTC 자정이 지나 날짜가 넘어갑니다.
    그러면 Day 카운터가 저녁 8시마다 하루씩 올라갑니다.
    """
    return datetime.now(TZ).date()


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys = on")
    return conn


def _ensure_columns(conn):
    """이미 만들어진 테이블에 빠진 컬럼을 채웁니다.

    'create table if not exists'는 테이블이 있으면 그냥 넘어갑니다. 그래서 나중에
    스키마에 컬럼을 추가해도, 이미 돌고 있던 DB에는 반영되지 않습니다.
    그 상태로 insert하면 터집니다 — 그것도 새 데이터가 들어오는 순간에만 터지므로
    한참 뒤에야 발견됩니다. 켤 때마다 확인해서 조용히 메꿉니다.
    """
    wanted = {
        "blog_posts": [
            ("content", "text"),
            ("is_magazine", "integer not null default 1"),
        ],
        "checkins": [
            ("trigger", "text"),
            ("confidence", "integer"),
            ("unspoken", "text"),
            ("prompt_version", "text"),
            ("raw_input", "text"),
            ("feedback", "text"),
            ("feedback_at", "text"),
        ],
        "insights": [
            ("depth", "integer"),
        ],
        "preferences": [
            ("status", "text not null default 'active'"),
        ],
        "memos": [
            ("tags", "text"),
            ("done", "integer not null default 0"),
        ],
    }
    for table, columns in wanted.items():
        exists = conn.execute(
            "select name from sqlite_master where type = 'table' and name = ?", (table,)
        ).fetchone()
        if not exists:
            continue
        have = {row["name"] for row in conn.execute(f"pragma table_info({table})")}
        for name, decl in columns:
            if name not in have:
                conn.execute(f"alter table {table} add column {name} {decl}")
                print(f"[migration] {table}.{name} 추가됨")


def init():
    with connect() as conn:
        conn.executescript(SCHEMA)
        _ensure_columns(conn)
        for pref in SEED_PREFERENCES:
            conn.execute(
                "insert or ignore into preferences (text, status, created_at) "
                "values (?, 'active', ?)",
                (pref, _now()),
            )
        # date.today()는 컨테이너 시계(대개 UTC)를 봅니다. 사용자의 날짜와 다릅니다.
        start = START_DATE or _today().isoformat()
        conn.execute("insert or ignore into meta (key, value) values ('start_date', ?)", (start,))


def unclassified_posts() -> list[sqlite3.Row]:
    """매거진 판별 전에 저장된 글들. 다시 분류해야 합니다."""
    with connect() as conn:
        return conn.execute(
            "select guid, title, link from blog_posts where link is not null and link != ''"
        ).fetchall()


def set_post_magazine(guid: str, is_magazine: bool):
    with connect() as conn:
        conn.execute(
            "update blog_posts set is_magazine = ? where guid = ?", (int(is_magazine), guid)
        )


def _start_date() -> date:
    with connect() as conn:
        row = conn.execute("select value from meta where key = 'start_date'").fetchone()
    return date.fromisoformat(row["value"]) if row else _today()


def start_date_str() -> str:
    """100일 시작일 (YYYY-MM-DD). 외부 활동(GitHub 등)을 시작일 이전/이후로
    가를 때 씁니다 — 시작일 이후는 그 연동을 방금 켰어도 챌린지 안에서 일어난
    진짜 활동이니 "과거 기록이라 안 셈"으로 묻히면 안 됩니다."""
    return _start_date().isoformat()


def day_number() -> int:
    """Day N. 사용자가 있는 곳의 날짜 기준입니다.

    date.today()는 컨테이너 시계를 봅니다. Railway는 UTC로 돕니다.
    토론토 저녁 8시면 UTC는 이미 다음 날이라, 매일 밤 4시간씩 하루가 앞서갑니다.
    """
    return min(TOTAL_DAYS, max(1, (_today() - _start_date()).days + 1))


# ---------- 메시지 ----------

def add_message(role: str, content: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "insert into messages (role, content, created_at) values (?, ?, ?)",
            (role, content, _now()),
        )
        return cur.lastrowid


def history(limit: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "select role, content from messages order by id desc limit ?", (limit,)
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def days_since_last_user_message() -> int | None:
    with connect() as conn:
        row = conn.execute(
            "select created_at from messages where role = 'user' order by id desc limit 1"
        ).fetchone()
    if not row:
        return None
    return (datetime.now(TZ).replace(tzinfo=None) - _parse(row["created_at"])).days


# ---------- 통찰 ----------

def add_insights(message_id: int, insights: list[dict]):
    if not insights:
        return
    now = _now()
    with connect() as conn:
        conn.executemany(
            "insert into insights (message_id, text, type, depth, created_at) values (?, ?, ?, ?, ?)",
            [(message_id, i["text"], i["type"], i.get("depth"), now) for i in insights],
        )


def pending_actions() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "select id, text, created_at from insights "
            "where type = 'action' and done = 0 order by created_at"
        ).fetchall()


def complete_action(action_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "update insights set done = 1 where id = ? and type = 'action'", (action_id,)
        )
        return cur.rowcount > 0


def learnings_since(days: int) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "select text, created_at from insights "
            "where type = 'learning' and created_at >= ? order by created_at",
            (_cutoff(days),),
        ).fetchall()


# ---------- 선호 ----------

def preferences() -> list[str]:
    """마늘이 실제로 지키는 요구사항. 승인된 것만입니다.

    승인 전(pending)인 것은 여기 안 들어옵니다. 그래서 프롬프트 버전도
    사용자가 승인하는 순간에만 바뀝니다 — 지나가는 말이 데이터를 오염시키지 않습니다.
    """
    with connect() as conn:
        rows = conn.execute(
            "select text from preferences where active = 1 and status = 'active' order by id"
        ).fetchall()
    return [r["text"] for r in rows]


def seed_preferences(texts: list[str]):
    """초기 요구사항. 사용자가 직접 정한 것이므로 바로 승인 상태로 넣습니다."""
    with connect() as conn:
        for t in texts:
            conn.execute(
                "insert or ignore into preferences (text, status, created_at) "
                "values (?, 'active', ?)",
                (t, _now()),
            )


def propose_preferences(texts: list[str]) -> list[sqlite3.Row]:
    """마늘이 감지한 요구사항을 **승인 대기**로 넣습니다.

    바로 적용하지 않습니다. 지나가는 말이 영구 정책이 되면 안 되고,
    그 순간 프롬프트 버전까지 바뀌어 100일치 데이터 해석이 오염됩니다.
    한 번 거절한 것은 다시 묻지 않습니다.
    """
    proposed = []
    with connect() as conn:
        for t in texts:
            cur = conn.execute(
                "insert or ignore into preferences (text, status, created_at) "
                "values (?, 'pending', ?)",
                (t, _now()),
            )
            if cur.rowcount:
                row = conn.execute(
                    "select id, text from preferences where text = ?", (t,)
                ).fetchone()
                proposed.append(row)
    return proposed


def resolve_preference(pref_id: int, approved: bool) -> str | None:
    """승인 또는 거절. 요구사항 문구를 반환."""
    with connect() as conn:
        row = conn.execute(
            "select text from preferences where id = ? and status = 'pending'", (pref_id,)
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "update preferences set status = ? where id = ?",
            ("active" if approved else "rejected", pref_id),
        )
        return row["text"]


def forget_preference(pref_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute("update preferences set active = 0 where id = ?", (pref_id,))
        return cur.rowcount > 0


def list_preferences() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "select id, text from preferences where active = 1 and status = 'active' order by id"
        ).fetchall()


# ---------- 메모 ----------

def memo_tags(tags: str | None) -> list[str]:
    """저장된 tags 문자열("스케줄러,버그")을 리스트로. 없으면 빈 리스트."""
    return tags.split(",") if tags else []


def add_memo(text: str, tags: list[str] | None = None) -> int:
    with connect() as conn:
        cur = conn.execute(
            "insert into memos (text, tags, created_at) values (?, ?, ?)",
            (text, ",".join(tags) if tags else None, _now()),
        )
        return cur.lastrowid


def list_memos(tag: str | None = None) -> list[sqlite3.Row]:
    """완료 안 된 메모만 반환합니다. 완료된 건 여기 안 보이지만 DB에는 남아 회고에 씁니다."""
    with connect() as conn:
        rows = conn.execute(
            "select id, text, tags, created_at from memos where done = 0 order by id"
        ).fetchall()
    if tag:
        rows = [r for r in rows if tag in memo_tags(r["tags"])]
    return rows


def complete_memo(memo_id: int) -> bool:
    """삭제가 아니라 완료 처리. 원문은 남아서 나중에 회고에 쓸 수 있습니다."""
    with connect() as conn:
        cur = conn.execute("update memos set done = 1 where id = ? and done = 0", (memo_id,))
        return cur.rowcount > 0


def days_since(ts: str) -> int:
    return (datetime.now(TZ).replace(tzinfo=None) - _parse(ts)).days


# ---------- 프롬프트 버전 ----------

def register_prompt_version(template: str, prefs: list[str]) -> str:
    """판단 규칙의 지문(fingerprint)을 남깁니다.

    버전 = 판단 기준 템플릿 + 학습된 요구사항. 둘 중 하나만 바뀌어도 다른 마늘입니다.
    자동으로 계산되므로 사람이 버전을 매길 필요가 없습니다 — 지키지 않을 규율이니까요.
    """
    prefs_text = "\n".join(sorted(prefs))
    version = hashlib.sha256((template + "\n---\n" + prefs_text).encode()).hexdigest()[:8]
    with connect() as conn:
        conn.execute(
            "insert or ignore into prompt_versions (version, template, preferences, first_seen) "
            "values (?, ?, ?, ?)",
            (version, template, prefs_text, _now()),
        )
    return version


def prompt_version_history() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "select v.version, v.first_seen, "
            "  (select count(*) from checkins c where c.prompt_version = v.version) as checkins, "
            "  (select count(*) from checkins c where c.prompt_version = v.version and c.spoke = 1) as spoke "
            "from prompt_versions v order by v.first_seen"
        ).fetchall()


# ---------- 체크인 ----------

def log_checkin(
    spoke: bool,
    trigger: str | None,
    confidence: int | None,
    reason: str,
    message: str | None,
    unspoken: str | None,
    prompt_version: str,
    raw_input: str | None = None,
) -> int:
    """판단을 기록합니다.

    raw_input에는 그때 마늘이 본 것 전부가 들어갑니다. 판단만 남기면
    나중에 "v4가 v1보다 낫다"를 증명할 수 없습니다 — 같은 입력에 두 버전을
    돌려봐야 하는데 입력이 없으니까요. 원칙: 판단 전에 먼저 기록.
    """
    with connect() as conn:
        cur = conn.execute(
            "insert into checkins "
            "(spoke, trigger, confidence, reason, message, unspoken, prompt_version, raw_input, created_at) "
            "values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (int(spoke), trigger, confidence, reason, message, unspoken,
             prompt_version, raw_input, _now()),
        )
        return cur.lastrowid


def checkin_raw(checkin_id: int) -> sqlite3.Row | None:
    """나중에 다른 버전으로 다시 돌려보기 위한 원문 조회."""
    with connect() as conn:
        return conn.execute(
            "select c.id, c.raw_input, c.prompt_version, c.spoke, c.trigger, "
            "  c.confidence, c.reason, c.message, c.unspoken, c.feedback, "
            "  v.template, v.preferences "
            "from checkins c left join prompt_versions v on v.version = c.prompt_version "
            "where c.id = ?",
            (checkin_id,),
        ).fetchone()


def raw_coverage() -> dict:
    """원문이 몇 %나 남아있나. 이게 100%가 아니면 재현이 안 됩니다."""
    with connect() as conn:
        c = conn.execute(
            "select count(*) as total, "
            "  sum(case when raw_input is not null and raw_input != '' then 1 else 0 end) as with_raw "
            "from checkins"
        ).fetchone()
        b = conn.execute(
            "select count(*) as total, "
            "  sum(case when content is not null and content != '' then 1 else 0 end) as with_raw "
            "from blog_posts"
        ).fetchone()
    return {
        "checkins": {"total": c["total"], "with_raw": c["with_raw"] or 0},
        "posts": {"total": b["total"], "with_raw": b["with_raw"] or 0},
    }


def recent_checkins(limit: int = 5) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "select id, spoke, trigger, confidence, reason, message, feedback, created_at "
            "from checkins order by id desc limit ?",
            (limit,),
        ).fetchall()


def set_feedback(checkin_id: int, feedback: str) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "update checkins set feedback = ?, feedback_at = ? where id = ?",
            (feedback, _now(), checkin_id),
        )
        return cur.rowcount > 0


def unreviewed_silences(days: int = 7, limit: int = 3) -> list[sqlite3.Row]:
    """이번 주에 삼킨 말들 중 아직 채점 안 된 것.

    말한 것만 채점하면 데이터셋이 반쪽입니다. 안 한 말도 채점되어야
    '말했어야 했는데 안 한 경우'가 오답으로 잡힙니다.
    """
    with connect() as conn:
        return conn.execute(
            "select id, unspoken, reason, confidence, created_at from checkins "
            "where spoke = 0 and unspoken is not null and unspoken != '' "
            "and feedback is null and created_at >= ? "
            "order by confidence desc limit ?",
            (_cutoff(days), limit),
        ).fetchall()


def unanswered_checkin_streak() -> int:
    """마지막 사용자 메시지 이후 마늘이 일방적으로 말을 건 횟수."""
    with connect() as conn:
        row = conn.execute(
            "select created_at from messages where role = 'user' order by id desc limit 1"
        ).fetchone()
        if not row:
            return conn.execute(
                "select count(*) as n from checkins where spoke = 1"
            ).fetchone()["n"]
        return conn.execute(
            "select count(*) as n from checkins where spoke = 1 and created_at > ?",
            (row["created_at"],),
        ).fetchone()["n"]


# ---------- 성취 ----------

def add_achievement(text: str, depth: int, message_id: int | None = None) -> int:
    with connect() as conn:
        cur = conn.execute(
            "insert into insights (message_id, text, type, depth, created_at) "
            "values (?, ?, 'achievement', ?, ?)",
            (message_id, text, depth, _now()),
        )
        return cur.lastrowid


def achievements_since(days: int) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "select text, depth, created_at from insights "
            "where type = 'achievement' and created_at >= ? order by created_at",
            (_cutoff(days),),
        ).fetchall()


# GitHub 성취 문구는 항상 "{저장소} 개선..." 또는 "{저장소} PR 병합..."으로 시작합니다
# (agent/github.py의 _describe). 블로그·대화 성취와 구분해서 정리 대상만 골라냅니다.
_GITHUB_ACHIEVEMENT_RE = re.compile(r"^(\S+) (개선|PR 병합)")


def cleanup_github_achievements() -> dict:
    """일회성 정리 — 과거 GitHub 동기화 버그로 잘못 쌓인 성취를 바로잡습니다.

    1) manuel(마늘 자기 자신 저장소) 관련 성취는 전부 삭제합니다. 마늘을 고치는 건
       사용자의 성장이 아니라 봇 정비라 애초에 성취가 아니었습니다.
    2) "하루 1건" 규칙이 생기기 전에 같은 (저장소, 날짜)로 여러 건 쪼개져 기록된
       GitHub 성취는 하나로 합칩니다 — PR 병합이 있으면 그 문구를 남기고, 여러 건
       병합됐으면 "(N건 병합)"을 붙입니다.
    """
    with connect() as conn:
        removed_manuel = conn.execute(
            "delete from insights where type = 'achievement' and text like 'manuel %'"
        ).rowcount

        rows = conn.execute(
            "select id, text, created_at from insights where type = 'achievement' order by created_at"
        ).fetchall()

        groups = collections.defaultdict(list)
        for r in rows:
            m = _GITHUB_ACHIEVEMENT_RE.match(r["text"])
            if m:
                groups[(m.group(1), r["created_at"][:10])].append(r)

        merged = 0
        for (repo, date_str), items in groups.items():
            if len(items) <= 1:
                continue
            merges = [i for i in items if re.match(r"^\S+ PR 병합", i["text"])]
            # 예전 버그 있던 동기화는 GitHub API가 최신순으로 준 이벤트를 그 순서
            # 그대로 처리해서 기록했습니다 — 즉 가장 최근에 merge된 PR이 DB에는
            # 가장 먼저(=created_at이 가장 이른) 들어가 있습니다. 그래서 여기서도
            # merges[0](가장 이른 created_at)이 실제로는 가장 나중에 merge된 PR입니다.
            keep = merges[0] if merges else items[-1]
            text = keep["text"]
            if len(merges) > 1 and "건 병합" not in text:
                text = re.sub(r" PR 병합(\s*—|$)", f" PR 병합 ({len(merges)}건 병합)\\1", text, count=1)
            conn.execute("update insights set text = ? where id = ?", (text, keep["id"]))
            for i in items:
                if i["id"] != keep["id"]:
                    conn.execute("delete from insights where id = ?", (i["id"],))
                    merged += 1

    return {"removed_manuel": removed_manuel, "merged": merged}


def weekly_depth(weeks: int) -> list[dict]:
    """주차별 성취 개수와 최고 도달 깊이.

    페이스메이커가 보는 것. 개수가 아니라 max_depth가 핵심입니다.
    성취가 없는 주도 0으로 채워서 반환합니다 — 빈 주가 안 보이면 페이스를 못 잽니다.
    """
    rows = achievements_since(weeks * 7)
    today = _today()
    buckets = {}
    for i in range(weeks):
        start = today - timedelta(days=today.weekday() + 7 * i)
        buckets[start] = {"week_start": start, "count": 0, "max_depth": 0}

    for r in rows:
        d = _parse(r["created_at"]).date()
        start = d - timedelta(days=d.weekday())
        if start in buckets:
            b = buckets[start]
            b["count"] += 1
            b["max_depth"] = max(b["max_depth"], r["depth"] or 0)

    return sorted(buckets.values(), key=lambda b: b["week_start"])


# ---------- GitHub ----------

def github_activity_exists(key: str) -> bool:
    with connect() as conn:
        return conn.execute(
            "select 1 from github_activity where key = ?", (key,)
        ).fetchone() is not None


def add_github_activity(key: str, repo: str, date: str, commits: int) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "insert or ignore into github_activity (key, repo, date, commits, created_at) "
            "values (?, ?, ?, ?, ?)",
            (key, repo, date, commits, _now()),
        )
        return cur.rowcount > 0


def github_activity_count() -> int:
    with connect() as conn:
        return conn.execute("select count(*) as n from github_activity").fetchone()["n"]


def github_active_days(days: int) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "select date, repo, commits from github_activity "
            "where created_at >= ? order by date desc",
            (_cutoff(days),),
        ).fetchall()


# ---------- 블로그 ----------

def blog_post_exists(guid: str) -> bool:
    with connect() as conn:
        return conn.execute(
            "select 1 from blog_posts where guid = ?", (guid,)
        ).fetchone() is not None


def add_blog_post(guid: str, title: str, link: str, published: str,
                  content: str = "", is_magazine: bool = True) -> bool:
    """새 글이면 True. 이미 있으면 False.

    content는 글 원문입니다. 분석 결과만 저장하고 원문을 버리면,
    나중에 분석 프롬프트를 고쳐도 옛 글에는 다시 돌릴 수 없습니다.
    """
    with connect() as conn:
        cur = conn.execute(
            "insert or ignore into blog_posts "
            "(guid, title, link, published, content, is_magazine, created_at) "
            "values (?, ?, ?, ?, ?, ?, ?)",
            (guid, title, link, published, content, int(is_magazine), _now()),
        )
        return cur.rowcount > 0


def personal_posts_since(days: int, limit: int = 3) -> list[sqlite3.Row]:
    """매거진 밖의 글 — 일상과 기분. 성취는 아니지만 마늘이 알아야 할 맥락입니다."""
    with connect() as conn:
        return conn.execute(
            "select title, content, created_at from blog_posts "
            "where is_magazine = 0 and created_at >= ? order by created_at desc limit ?",
            (_cutoff(days), limit),
        ).fetchall()


def blog_post_count() -> int:
    with connect() as conn:
        return conn.execute("select count(*) as n from blog_posts").fetchone()["n"]


def feedback_summary() -> list[sqlite3.Row]:
    """방아쇠별 채점 결과. 10월에 들고 갈 표의 원본."""
    with connect() as conn:
        return conn.execute(
            "select trigger, feedback, count(*) as n from checkins "
            "where spoke = 1 and feedback is not null "
            "group by trigger, feedback order by trigger, n desc"
        ).fetchall()
