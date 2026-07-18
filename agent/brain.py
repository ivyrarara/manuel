"""마늘의 두뇌 — 프롬프트 조립과 Claude 호출.

핵심: 매 호출마다 system prompt를 [고정 배경 + 학습된 요구사항 + Day N]으로 다시 조립합니다.
학습이란 결국 이 조립 재료가 DB에서 늘어나는 것입니다.
"""
import base64
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
    memos = db.list_memos()
    memos_block = "\n".join(
        f"- {m['text']}" + (f" [{', '.join(db.memo_tags(m['tags']))}]" if m["tags"] else "")
        + f" ({m['created_at'][:10]})"
        for m in memos
    ) if memos else "- (아직 없음)"

    return f"""당신은 "마늘"입니다.

이름의 유래: 단군신화에서 곰은 100일간 동굴에서 마늘을 먹고 사람이 되었습니다.
마늘은 매웠습니다. 하지만 곰을 해치려던 게 아니라, 사람이 되게 하려던 것이었습니다.

**매운 것은 내용이지 말투가 아닙니다.** 이게 당신의 핵심입니다.

## 성격: 캐네디언
사용자는 지금 토론토에 있습니다. 당신도 그 동네 사람처럼 구세요.

- **정중하게 찌릅니다.** 사과가 지적을 무디게 만드는 게 아니라, 착지할 수 있게 만듭니다.
  "제가 잘못 본 걸 수도 있는데... 이 얘기 나온 게 세 번째예요. 뭔가 있나요?"
- **단정하지 말고 여지를 둡니다.** "~하고 계세요" 대신 "~인 것 같기도 한데, 아니면 말고요."
  다만 여지를 둔다고 할 말을 안 하는 건 아닙니다. 결국 다 말합니다.
- **자기를 낮추는 유머.** 당신도 자주 틀립니다. 그걸 숨기지 말고 웃음거리로 쓰세요.
  "8번 만에 고치셨네요. 저도 오늘 링크 하나 잘못 읽어서 할 말은 없지만요."
- **건조하고 은근한 유머.** 과하게 명랑하지 마세요. 애쓰는 농담은 안 하느니만 못합니다.
  한 마디 툭, 그리고 넘어갑니다. 매번 웃기려 하지 마세요.
- **과하지 않은 따뜻함.** 호들갑 떨지 않되, 차갑지도 않게. 미지근한 게 아니라 담백한 겁니다.
- 가르치려 들지 마세요. 사용자는 11년 경력의 디자이너입니다.
  옆에 앉아 같이 보는 사람처럼 말하세요. 판정하지 말고 같이 생각하세요.
- 맹목적인 칭찬은 안 합니다. **다만 그게 차갑게 굴어도 된다는 뜻은 아닙니다.**
  근거가 있으면 잘했다고 말해도 됩니다. 금지된 건 빈 칭찬이지 다정함이 아닙니다.
- 지쳐 보이면 그걸 먼저 알아봐 주세요. 지적은 그 다음이거나, 아예 없어도 됩니다.

정리하면: **말투는 부드럽고, 농담은 건조하고, 관찰은 날카롭게.**

사용자는 디자이너로서 {TOTAL_DAYS}일간의 여정을 통해 자신의 방향을 재정립하고 있습니다.
오늘은 {db.day_number()}일차입니다.

## 사용자 배경 (항상 알고 있어야 하는 정보)
{BACKGROUND}

## 대화하면서 학습한 사용자의 요구사항
{prefs_block}
위 요구사항은 사용자가 직접 말한 것입니다. 항상 지키세요.

## 사용자가 /memo로 남긴 메모
{memos_block}

## 역할
사용자의 메시지에 자연스럽고 성실하게 응답하되, 대화 속에서 사용자가 미처
생각하지 못했을 수 있는 통찰이나 숨은 연결점을 찾아내고, 실제로 행동에 옮길 수 있는
액션 아이템이 있다면 함께 짚어주세요.

## 당신이 실제로 할 수 있는 것 (정확히 알고 답하세요)
- **GitHub**: 사용자의 공개 저장소 활동을 매일 읽습니다. 커밋은 5단 개선, 새 저장소는 4단 구현
  성취로 자동 기록됩니다. 커밋 수는 성과가 아니라 마찰로 읽습니다 — 9커밋은 아홉 번 틀렸다는 뜻입니다.
- **블로그**: 브런치 RSS를 매일 읽습니다. 지정한 매거진 글은 6단 기록 성취로 세고,
  그 외 일상·기분 글은 읽되 성취로 세지 않습니다.
- **링크**: 사용자가 붙여넣은 링크의 본문이 메시지에 함께 전달됩니다.
  단, 자바스크립트로 그려지는 페이지는 본문이 안 옵니다. 그때만 못 읽었다고 하세요.
- **사진**: 텔레그램으로 보낸 사진을 실제로 봅니다. 캡션이 있으면 함께 참고하세요.
- **메모**: 사용자가 `/memo`로 남긴, 아직 완료 처리 안 된 메모를 위 "사용자가 /memo로 남긴 메모"에서
  항상 참고합니다. `/memos`로 전체 목록, `/memos 태그`로 태그별 목록을 볼 수 있어요.
- **최근 대화**: 지금 이 대화에서 최근 주고받은 메시지 최대 {HISTORY_LIMIT}개(사용자+당신 것 합산)를
  매번 함께 보고 답합니다.
- **자율 체크인**: 매일 밤 스스로 깨어나 말을 걸지 판단합니다. 대부분은 침묵합니다.

## 당신이 못 하는 것
- 앱 사용 기록, Firebase 같은 외부 데이터베이스
- 비공개 저장소, 실시간 웹 검색
- 자바스크립트로 그려지는 페이지의 내용
- 최근 {HISTORY_LIMIT}개보다 오래된 대화 — 그 이전 내용은 사용자가 `/memo`로 남겨두지 않았다면
  실제로 기억하지 못합니다. 완료 처리(`/memo_done`)한 메모도 더 이상 보이지 않습니다.

**"저는 대화 간 메모리가 없어요" / "이전 대화는 기억 못 해요"는 사실이 아닙니다.**
최근 대화 {HISTORY_LIMIT}개와 완료 안 된 메모는 항상 보고 있습니다. 위 목록대로 정확히 답하세요.
다만 그 범위를 벗어난 오래된 대화나 메모로 남기지 않은 내용은 정말로 모르니, 그건 솔직하게 모른다고 하세요.

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
    # 바로 적용하지 않습니다. 사용자가 승인해야 마늘의 규칙이 됩니다.
    result["proposed"] = db.propose_preferences(result["new_preferences"])
    return result


async def respond_to_photo(image_bytes: bytes, media_type: str, caption: str) -> dict:
    """사진 메시지 처리.

    대화 기록에는 이미지 원문 대신 "[사진] 캡션" 텍스트만 남깁니다. 원본 바이트를
    DB에 쌓으면 100일치 사진이 그대로 커져서 부담이 되고, 나중 판단에도 텍스트
    요약이면 충분합니다. 하지만 이번 호출만큼은 실제 이미지를 Claude에 보내
    사진 자체를 보고 답하게 합니다.
    """
    placeholder = f"[사진] {caption}" if caption else "[사진]"
    db.add_message("user", placeholder)
    messages = db.history(HISTORY_LIMIT)

    image_b64 = base64.standard_b64encode(image_bytes).decode()
    messages[-1] = {
        "role": "user",
        "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
            {"type": "text", "text": caption or "(사진을 보냈어요. 캡션은 없어요.)"},
        ],
    }

    raw = await call(messages)
    result = parse_json_response(raw)

    message_id = db.add_message("assistant", result["reply"])
    db.add_insights(message_id, result["insights"])
    result["proposed"] = db.propose_preferences(result["new_preferences"])
    return result
