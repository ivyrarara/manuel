"""설정 — 마늘이 항상 알고 있어야 하는 것들."""
import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OWNER_CHAT_ID = int(os.environ["OWNER_CHAT_ID"])  # 이 사람 외에는 응답하지 않음

# 블로그 RSS. 비워두면 블로그 기능 전체가 조용히 꺼집니다.
#   네이버:  https://rss.blog.naver.com/아이디.xml
#   티스토리: https://이름.tistory.com/rss
#   브런치:  https://brunch.co.kr/rss/@@아이디
BLOG_RSS_URL = os.getenv("BLOG_RSS_URL", "").strip()
BLOG_CHECK_HOUR, BLOG_CHECK_MINUTE = 18, 40  # 체크인(19:00) 20분 전에 미리 읽어둠

# 성취로 인정할 매거진의 slug. 브런치 RSS는 작가 단위라 모든 글이 다 들어옵니다.
# 이 매거진 글만 6단 성취로 세고, 나머지(일상·기분 글)는 읽되 성취로 세지 않습니다.
# 비워두면 모든 글을 성취로 인정합니다.
BLOG_MAGAZINE_SLUG = os.getenv("BLOG_MAGAZINE_SLUG", "designer-renew").strip()

# GitHub 아이디. 비워두면 GitHub 기능 전체가 조용히 꺼집니다.
# 공개 저장소만 봅니다. 인증 없이 호출하므로 토큰이 필요 없습니다.
GITHUB_USER = os.getenv("GITHUB_USER", "").strip()
GITHUB_CHECK_HOUR, GITHUB_CHECK_MINUTE = 18, 40  # 체크인 20분 전에 오늘 커밋을 반영

# 성취 집계에서 뺄 저장소들 (쉼표 구분, owner 없이 저장소 이름만). 기본값은 마늘
# 자기 자신의 소스코드 저장소입니다 — 마늘을 고치는 건 사용자의 성장이 아니라
# 봇 정비라서, 100일 페이스 기록에 섞이면 안 됩니다.
GITHUB_EXCLUDE_REPOS = {
    r.strip() for r in os.getenv("GITHUB_EXCLUDE_REPOS", "manuel").split(",") if r.strip()
}

MODEL = "claude-sonnet-4-6"
# 사용자가 실제로 있는 곳 기준. 모든 스케줄과 저장 시각이 여기 맞춰집니다.
# 한국으로 돌아가면 이 값 하나만 Asia/Seoul 로 바꾸면 됩니다.
TZ = ZoneInfo(os.getenv("TIMEZONE", "America/Toronto"))
DB_PATH = os.getenv("DB_PATH", "maneul.db")

START_DATE = os.getenv("START_DATE")  # "2026-07-16" — 없으면 첫 실행일이 1일차
TOTAL_DAYS = int(os.getenv("TOTAL_DAYS", "100"))

HISTORY_LIMIT = 40  # Claude에게 넘길 최근 메시지 수

# 자율 체크인: 매일 이 시각에 "말을 걸까?"를 스스로 판단 (대부분은 침묵)
CHECKIN_HOUR, CHECKIN_MINUTE = 19, 0

# ⚠️ 요일 번호는 텔레그램 라이브러리(python-telegram-bot) 규칙을 따릅니다.
#    0=일 1=월 2=화 3=수 4=목 5=금 6=토
#    파이썬 표준 date.weekday()는 0=월 ~ 6=일 이라 정반대입니다. 헷갈리면 하루씩 밀립니다.
SUNDAY = 0
MONDAY = 1

# 고정 리마인더
SUNDAY_REVIEW = (SUNDAY, 19, 0)   # 일요일 19:00 — 이번 주 배운 것 + 삼킨 말 리뷰
MONDAY_ACTIONS = (MONDAY, 10, 0)  # 월요일 10:00 — 이번 주 액션 아이템

# 일요일 19:00에는 회고가 나가므로, 그날 자율 체크인은 건너뜁니다.
# 안 그러면 메시지 두 개가 동시에 도착합니다. ("잦은 알림 금지")
CHECKIN_DAYS = (1, 2, 3, 4, 5, 6)  # 월~토

BACKGROUND = """\
- 디자인전략기획 2년
- 이마트에서 PL 패키지 디자인 2년
- 아모레퍼시픽 에스쁘아에서 패키지/그래픽/비주얼 브랜딩 7년
- 2021년 삼성전자 가전UX팀으로 이직, 현재 육아휴직 중 (복귀 예정: 2027년 2월)

현재 상황:
- 가전UX팀에 온 이후 GUI다운 GUI를 단 한 번도 해본 적이 없음
- 그로 인해 팀 내 포지셔닝이 애매해진 상태

목표:
- 100일 후, 가전UX팀이 아니라 전사 조직인 디자인경영센터 그룹장에게 컨택할 예정.
  그 자리에서 자신을 어필하는 것이 이 100일의 실질적인 목표.
- 가설: 빅스비는 두뇌(추론)를 Gemini/Perplexity에 외주 줬고, 남은 건 성격과 브랜드다.
  삼성 기기가 먼저 말을 걸기 시작할 때 "언제 말하고 언제 침묵할지"를 정의하는 일 —
  그게 GUI 디자이너가 못 하고, 11년의 브랜딩 경력에서만 나올 수 있는 일이라는 것.
- 이 가설은 검증 중입니다. 확정된 것으로 취급하지 말고, 함께 다듬어야 할 재료로 다루세요.
- 마늘 자체가 그 증거물입니다. 100일간의 판단 기록이 컨택 때 들고 갈 자산입니다.

주의:
- 이 사람은 기획과 네이밍을 아주 잘합니다. 익숙한 영역이라 그쪽으로 굴러가기 쉽습니다.
  다만 그게 항상 회피인 것은 아닙니다. 순서를 정하는 것은 이 사람의 권한입니다.
- 터미널/코딩 경험이 없습니다. 기술적인 조언은 그 점을 전제로 하세요.
"""

# 초기 요구사항. 이후 대화에서 학습한 것들이 DB에 계속 추가됨.
SEED_PREFERENCES = [
    "맹목적인 칭찬보다는 객관적인 디렉션을 제시할 것",
    "너무 잦은 알림은 하지 말 것. 다만 게으름 피우지 않도록 동기를 줄 빈도는 유지할 것",
    "근거가 얇으면 침묵할 것. 회피처럼 보이는 것이 그냥 순서 정하기일 수 있음",
    "대화의 흐름을 강압적으로 가져가지 말 것. 순서를 정하는 것은 사용자의 권한임",
]


# 성취의 사다리 — 페이스는 "몇 개"가 아니라 "얼마나 깊이 갔나"로 잽니다.
# 한 주에 하나를 해도 그게 6단까지 갔으면 좋은 주.
# 열 개를 배우기만 했으면(1단) 나쁜 주. 육아휴직 중에 개수 할당량은 정직하지 않습니다.
LADDER = {
    1: "학습 — 알게 됨 (읽음, 배움, 이해함)",
    2: "활용 — 실제로 써봄",
    3: "확장 — 다른 도구나 AI로 옮겨봄",
    4: "구현 — 만들어냄",
    5: "개선 — 쓰면서 고침",
    6: "기록 — 남에게 내놓음 (블로그 발행)",
}

# 페이스 판단 기준: 이 주수만큼 연속으로 최고 깊이가 이 값 이하면 뒤처진 것.
PACE_WINDOW_WEEKS = 4
PACE_SHALLOW_DEPTH = 3

# 발화 방아쇠 — 어떤 이유로 입을 열었는지 분류. 100일 뒤 이게 분석의 축이 됩니다.
TRIGGERS = {
    "avoidance": "회피 — 특정 주제를 계속 겉돌고 있음",
    "stale_action": "방치 — 액션이 패턴으로 안 움직임",
    "drift": "이탈 — 방향이 스스로 말한 목표와 어긋남",
    "connection": "연결 — 흩어진 것들이 이어져 의미가 생김",
    "silence": "침묵 — 조용한 것 자체가 신호로 보임",
    "pace": "페이스 — 사다리를 못 올라가고 얕은 데서만 맴돎",
    "none": "해당 없음",
}

# 피드백 어휘 — 탭 한 번. 🌶️와 🙄의 차이가 이 프로젝트의 전부입니다.
FEEDBACK_OPTIONS = [
    ("on_target", "🎯 맞다"),
    ("painful_true", "🌶️ 아픈데 맞다"),
    ("obvious", "🤷 뻔하다"),
    ("bad_timing", "⏰ 맞는데 지금 아님"),
    ("pushy", "🙄 강압적"),
    ("wrong_fact", "❌ 사실이 틀림"),
]
FEEDBACK_LABELS = dict(FEEDBACK_OPTIONS)
