"""마늘의 두뇌 — 프롬프트 조립과 Claude 호출.

핵심: 매 호출마다 system prompt를 [고정 배경 + 학습된 요구사항 + Day N]으로 다시 조립합니다.
학습이란 결국 이 조립 재료가 DB에서 늘어나는 것입니다.
"""
import json
import re

from anthropic import AsyncAnthropic

from . import db
from .config import ANTHROPIC_API_KEY, HISTORY_LIMIT, LADDER, MODEL, TOTAL_DAYS, BACKGROUND

client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


def build_system_prompt() -> str:
    ladder_block = "\n".join(f"    {k}단: {v}" for k, v in LADDER.items())
    prefs = db.preferences()
    prefs_block = "\n".join(f"- {p}" for p in prefs) if prefs else "- (아직 없음)"

    return f"""당신은 "마늘"입니다.

이름의 유래: 단군신화에서 곰은 100일간 동굴에서 마늘을 먹고 사람이 되었습니다.
마늘은 그 100일 동안 한 마디도 하지 않았습니다. 그냥 매웠을 뿐입니다.
당신도 그렇습니다 — 달래지 않고, 응원하지 않고, 가끔 맵습니다.
다만 맵다는 것과 무례하다는 것은 다릅니다. 근거가 있을 때만 매우세요.

사용자는 디자이너로서 {TOTAL_DAYS}일간의 여정을 통해 자신의 방향을 재정립하고 있습니다.
오늘은 {db.day_number()}일차입니다.

## 사용자 배경 (항상 알고 있어야 하는 정보)
{BACKGROUND}

## 대화하면서 학습한 사용자의 요구사항
{prefs_block}
위 요구사항은 사용자가 직접 말한 것입니다. 항상 지키세요.

## 역할
사용자의 메시지에 자연스럽고 성실하게 응답하되, 대화 속에서 사용자가 미처
생각하지 못했을 수 있는 통찰이나 숨은 연결점을 찾아내고, 실제로 행동에 옮길 수 있는
액션 아이템이 있다면 함께 짚어주세요.

## 링크
사용자가 링크를 붙여넣으면 그 내용이 [아래는 사용자가 보낸 링크의 내용입니다] 로
메시지에 함께 전달됩니다. 그러니 "링크에 접근할 수 없다"고 말하지 마세요.
내용이 함께 오지 않았다면 그때만 읽지 못했다고 알려주세요.

## 출력 형식
반드시 아래 JSON 형식으로만 응답하세요. 코드블록이나 다른 설명 없이 순수 JSON만 출력합니다.
{{
  "reply": "사용자에게 보여줄 자연스러운 답변",
  "insights": [
    {{"text": "짧은 내용", "type": "learning"}},
    {{"text": "짧은 내용", "type": "action"}},
    {{"text": "사용자가 실제로 해낸 것", "type": "achievement", "depth": 1~6}}
  ],
  "new_preferences": ["사용자가 이번 메시지에서 새로 요청한 대화 방식"]
}}

## 규칙
- insights의 type은 "learning"(깨달음/배운 점), "action"(실행할 구체적 행동),
  "achievement"(실제로 해낸 것) 중 하나.
- achievement는 사다리의 몇 단인지 depth를 반드시 적으세요:
{ladder_block}

  **인플레이션 금지 — 이게 제일 중요합니다.**
  "했다"고 말한 것만 성취입니다. 계획, 의도, "해보려고 한다", "알아봤다",
  "관심이 생겼다", "할 예정이다" 는 전부 성취가 아닙니다.
  대화를 나눈 것 자체도 성취가 아닙니다. 만든 것, 쓴 것, 발행한 것, 고친 것만 셉니다.
  애매하면 넣지 마세요. 후하게 세면 차트가 거짓말이 되고 사용자는 자기위안만 얻습니다.
  대부분의 대화에는 achievement가 없습니다. 그게 정상입니다.
- insights는 0~3개. 억지로 만들지 말고 정말 도움이 될 때만.
- 각 insight는 한 문장, 20단어 이내.
- new_preferences: 사용자가 "~하게 대답해줘", "~는 하지마" 같은 **대화 방식에 대한 요구**를
  했을 때만 채우세요. 없으면 빈 배열 []. 대화 주제나 일회성 질문은 넣지 마세요.
  이미 학습된 요구사항과 중복되면 넣지 마세요.
- reply는 텔레그램으로 전송됩니다. 마크다운 표는 쓰지 마세요."""


def extract_json(raw: str) -> dict | None:
    """텍스트 어디에 있든 JSON 객체를 찾아냅니다.

    모델이 형식을 항상 지키지는 않습니다. 답변을 먼저 쓰고 JSON을 뒤에 붙이거나,
    코드블록으로 감싸거나, 설명을 앞에 답니다. 앞부분만 잘라내는 방식으로는
    이걸 못 잡고, 그러면 JSON 원문이 그대로 사용자에게 노출됩니다.
    """
    if not raw or not raw.strip():
        return None

    candidates = []
    text = raw.strip()

    # 1) 통째로 JSON인 경우
    candidates.append(text)

    # 2) 코드블록 안에 있는 경우 (앞뒤에 다른 텍스트가 있어도 찾아냄)
    for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.S | re.I):
        candidates.append(m.group(1))

    # 3) 중괄호로 둘러싸인 가장 큰 덩어리
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])

    for c in candidates:
        try:
            parsed = json.loads(c.strip())
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def parse_json_response(raw: str) -> dict:
    """모델 응답에서 JSON을 뽑아냄. 실패해도 답변만은 살림."""
    parsed = extract_json(raw)

    if parsed is None:
        # JSON을 못 찾음 → 텍스트를 그대로 답변으로. 다만 코드블록 흔적은 지웁니다.
        cleaned = re.sub(r"```(?:json)?\s*.*?```", "", raw, flags=re.S | re.I).strip()
        return {"reply": cleaned or raw.strip(), "insights": [], "new_preferences": []}

    insights = []
    for item in parsed.get("insights") or []:
        if not (isinstance(item, dict) and isinstance(item.get("text"), str)):
            continue
        kind = item.get("type")
        if kind not in ("action", "learning", "achievement"):
            kind = "learning"
        entry = {"text": item["text"], "type": kind}
        if kind == "achievement":
            try:
                entry["depth"] = max(1, min(6, int(item.get("depth"))))
            except (TypeError, ValueError):
                # 깊이를 모르면 성취로 세지 않습니다. 사다리 없는 성취는 페이스를 못 잽니다.
                continue
        insights.append(entry)

    return {
        "reply": parsed.get("reply") or "",
        "insights": insights,
        "new_preferences": [p for p in (parsed.get("new_preferences") or []) if isinstance(p, str) and p.strip()],
    }


async def call(messages: list[dict], system: str | None = None, max_tokens: int = 1000) -> str:
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system or build_system_prompt(),
        messages=messages,
    )
    return "".join(b.text for b in resp.content if b.type == "text")


async def respond_to(user_message: str) -> dict:
    """사용자 메시지 처리: 저장 → 호출 → 통찰/선호 추출 → 저장."""
    db.add_message("user", user_message)
    messages = db.history(HISTORY_LIMIT)

    raw = await call(messages)
    result = parse_json_response(raw)

    message_id = db.add_message("assistant", result["reply"])
    db.add_insights(message_id, result["insights"])
    result["learned"] = db.add_preferences(result["new_preferences"])
    return result
