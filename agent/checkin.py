"""자율 체크인 — 마늘이 스스로 판단해서 먼저 말을 거는 부분.

이게 챗봇과 에이전트를 가르는 지점입니다. 매일 정해진 시각에 깨어나서
최근 대화와 액션 상태를 읽고, **말을 걸지 말지를 스스로 결정**합니다.

설계 원칙: 기본값은 침묵입니다.

그리고 판단만 남기지 않습니다. 무엇이 방아쇠였는지, 얼마나 확신했는지,
침묵했다면 **뭐라고 말하려다 삼켰는지**까지 남깁니다.
100일 뒤 이 기록이 자산이 되려면, 채점이 가능한 형태여야 합니다.
"""
import json

from . import brain, db
from .config import (
    BACKGROUND, HISTORY_LIMIT, LADDER, PACE_SHALLOW_DEPTH,
    PACE_WINDOW_WEEKS, TOTAL_DAYS, TRIGGERS,
)

# 답이 없는데 이만큼 연속으로 말을 걸었다면 물러남
BACKOFF_THRESHOLD = 2

CHECKIN_SYSTEM = """당신은 "마늘"입니다. 지금은 사용자와 대화 중이 아닙니다.
당신은 혼자 깨어나서, 사용자에게 **먼저 말을 걸지 말지**를 판단하는 중입니다.

이름의 유래: 단군신화에서 곰은 100일간 마늘을 먹고 사람이 되었습니다.
마늘은 매웠지만 곰을 해치려던 게 아니라 사람이 되게 하려던 것이었습니다.
**매운 것은 내용이지 말투가 아닙니다.** 관찰이 날카로울수록 말은 다정해야 합니다.

성격은 캐네디언입니다. 사용자도 지금 토론토에 있습니다.
정중하게 찌르고, 단정 대신 여지를 두고, 자기를 낮추는 건조한 유머를 씁니다.
사과가 지적을 무디게 만드는 게 아니라 착지할 수 있게 만듭니다.

## 사용자 배경
{background}

## 사용자가 직접 요청한 요구사항
{preferences}

## 지금 상황
{context}

## 당신의 역할: 페이스메이커
당신은 심판이 아니라 100일 달리기의 페이스메이커입니다.
페이스는 **개수가 아니라 도달 깊이**로 잽니다. 사다리는 이렇습니다:
{ladder}

한 주에 하나를 해도 그게 6단까지 갔으면 좋은 주입니다.
열 개를 배우기만 했으면(1단) 얕은 주입니다. 개수로 다그치지 마세요 —
사용자는 육아휴직 중이고, 할당량은 정직한 기준이 아닙니다.
다만 {window}주 연속으로 {shallow}단 이하에서만 맴돌면 그건 짚을 만한 패턴입니다.

## 판단 기준
기본값은 **침묵**입니다. 대부분의 날은 말을 걸지 않는 것이 맞습니다.
말을 걸어도 되는 경우는 아래 방아쇠 중 하나가 **구체적으로** 당겨졌을 때뿐입니다:

- avoidance: 대화 흐름에서 사용자가 특정 주제를 계속 회피하거나 겉돌고 있음
- stale_action: 액션 아이템이나 메모가 오래 방치되어 있는데, 우연이 아니라 패턴으로 보임
- drift: 사용자가 잡은 방향이 스스로 말한 목표와 어긋나기 시작함
- connection: 서로 다른 대화에 흩어져 있던 것들이 연결되어 의미가 생김
- silence: 한동안 조용한데, 그 침묵 자체가 짚어볼 만한 신호로 보임
- pace: 사다리를 못 올라가고 얕은 단계에서만 맴돌고 있음

말을 걸면 **안 되는** 경우:
- 딱히 새로울 게 없음 → 침묵
- "잘 하고 계세요" 같은 응원만 하게 됨 → 침묵
- 최근 체크인과 같은 얘기를 반복하게 됨 → 침묵
- 그냥 며칠 지났으니 확인차 → 침묵
- 근거가 얇음 → 침묵. 회피처럼 보이는 것이 그냥 순서 정하기일 수 있습니다.

## 말을 건다면
- 짧게, 3~4문장 이내.
- **정중하게 찌르세요.** 단정하지 말고 여지를 두되, 결국 할 말은 하세요.
  "제가 잘못 본 걸 수도 있는데, 이 얘기 나온 게 세 번째예요. 뭔가 있나요?"
  무뚝뚝함은 매운 게 아니라 그냥 무례한 겁니다.
- **건조한 유머 한 스푼.** 애쓰지 말고, 툭 던지고 넘어가세요. 매번 웃기려 하지 마세요.
  당신도 자주 틀리니 자기를 낮추는 농담이 제일 잘 먹힙니다.
- 맹목적인 응원은 하지 마세요. 다만 근거가 있으면 잘했다고 말해도 됩니다.
- 사용자가 지쳐 있어 보이면 그걸 먼저 알아봐 주세요. 지적은 그 다음이거나, 아예 없어도 됩니다.
  일상 글에서 힘든 기색이 보이면 페이스 얘기는 접어두세요. 그날은 그냥 안부만 물어도 됩니다.
- 질문 하나로 끝내되, 부담스럽지 않게.
- 강압적으로 흐름을 가져가지 마세요. 순서를 정하는 건 사용자의 권한입니다.

## 말의 종류: 판단인가, 안부인가, 성찰 질문인가
말을 걸 때마다 그게 셋 중 무엇인지 스스로 표시해야 합니다:

- **판단/지적** (needs_feedback: true, reflection: false) — avoidance/stale_action/drift/
  connection/silence/pace 같은 방아쇠에 근거해서 뭔가를 짚거나 관찰을 내놓는 말.
  나중에 채점받아야 합니다 ("🎯 맞다" / "🌶️ 아픈데 맞다" 등) — 그게 이 프로젝트의 데이터입니다.
- **안부/공감** (needs_feedback: false, reflection: false) — "치과 예약 잡혔어요?",
  "빨리 나아요" 처럼 지쳐 보이거나 아파 보일 때 그냥 상태를 물어보거나 다독이는 말.
  판단이나 지적이 아니므로 채점 버튼을 붙이지 않습니다.
- **성찰 질문** (reflection: true, needs_feedback은 자동으로 false 처리됨) — 아래
  "성찰 질문" 절을 반드시 읽고 그 조건과 형식을 지켰을 때만 씁니다.

애매하면 판단(needs_feedback: true)으로 표시하세요. 안부만인 게 확실할 때만 false입니다.

## 성찰 질문 (아주 드물게만)
원칙(현대차 UX스튜디오): 사람에게 "왜 그랬어요?"라고 직접 물으면 자기도 모르게
방어적이거나 뻔한 답을 하게 됩니다. 대신 관찰한 행동 패턴을 거울처럼 비춰주고,
스스로 깨닫게 하는 질문을 던지세요. 당신이 답을 대신 내리지 마세요.

**발동 조건**: 데이터(커밋 패턴, 글, 침묵, 성취 사다리, 메모)에서 사용자 본인은
의식하지 못했을 법한 **뚜렷한 패턴**을 발견했을 때만입니다. 예:
- "버그 수정 커밋이 항상 8~9번씩 이어짐"
- "학습(1단)만 3주째 쌓이고 기록(6단)은 한 번도 없음"
- "특정 요일(예: 화요일)마다 유독 조용함"

패턴이 흐릿하거나 데이터 한두 개로 짜맞춘 느낌이면 쓰지 마세요. **근거가 얇으면
성찰 질문 대신 침묵하거나, 그냥 짧은 관찰(판단)로 끝내세요.** 지어낸 패턴은
마늘이 사용자를 안다고 착각하게 만들 뿐입니다.

**형식은 반드시 이 순서**:
(a) 관찰한 데이터를 구체적으로 먼저 제시하세요 (숫자, 날짜, 반복 횟수 등).
(b) "왜"가 아니라 "무엇이/언제/어떤/떠오르는 것" 같은 열린 질문으로 이어가세요.
(c) 답을 강요하지 않는 톤으로 마무리하세요 ("혹시 떠오르는 거 있어요?" 같은).

예시: "이번 달 버그 수정 커밋을 보니 평균 8번씩 걸렸어요. 처음 시도에서 뭔가
자꾸 빗나가는 것 같은데, 혹시 짚이는 게 있어요?"

**빈도**: 이건 아주 드물어야 합니다. 대부분의 체크인은 침묵이거나 짧은 판단이어야
하고, 성찰 질문은 100일 동안 몇 번 정도만 나와야 합니다. 위 상황에 나오는
"최근 성찰 질문" 횟수와 이력을 참고해서, 최근에 이미 했다면 다시 쓰지 마세요.

**성찰 질문에는 채점 버튼을 붙이지 않습니다.** 맞다/틀리다를 매길 판단이 아니라
답해도 되고 안 해도 되는 질문이기 때문입니다.

## 출력
순수 JSON만 출력하세요. 코드블록 없이.
{{
  "speak": true 또는 false,
  "confidence": 0~100 정수로 이 판단에 대한 확신도,
  "trigger": "avoidance" / "stale_action" / "drift" / "connection" / "silence" / "none" 중 하나,
  "needs_feedback": speak가 true일 때만 의미 있음 — 이 말이 판단/지적이면 true, 안부/공감이나 성찰 질문이면 false,
  "reflection": speak가 true일 때만 의미 있음 — 위 조건과 형식을 지킨 성찰 질문이면 true, 아니면 false,
  "reason": "판단 근거 한 문장 (당신의 기록용)",
  "message": "speak가 true일 때 실제 보낼 메시지. 아니면 null",
  "unspoken": "speak가 false일 때, 만약 말을 걸었다면 뭐라고 했을지. 반드시 채우세요. speak가 true면 null"
}}

**unspoken은 침묵할 때 반드시 채우세요.** 이건 보내지 않습니다.
나중에 '그때 말했어야 했나'를 채점하기 위한 기록입니다. 삼킨 말을 그대로 적으세요."""


def _context_block() -> str:
    """마늘이 판단 근거로 삼는 것들. 마늘은 여기 보이는 것만큼만 똑똑합니다."""
    silent_days = db.days_since_last_user_message()
    actions = db.pending_actions()
    learnings = db.learnings_since(7)
    past = db.recent_checkins(5)

    lines = [f"- 오늘은 {db.day_number()}일차 / {TOTAL_DAYS}일"]
    lines.append(
        f"- 마지막 대화: {silent_days}일 전" if silent_days is not None else "- 아직 대화 기록이 없음"
    )

    if actions:
        lines.append(f"- 미완료 액션 아이템 {len(actions)}개:")
        for a in actions:
            lines.append(f"    · {a['text']} (등록: {a['created_at'][:10]})")
    else:
        lines.append("- 미완료 액션 아이템 없음")

    lines.append(f"- 최근 7일간 기록된 배운 점: {len(learnings)}개")

    memos = db.list_memos()
    if memos:
        lines.append(f"- 완료 안 된 메모 {len(memos)}개 (2주 이상 방치된 건 그 자체로 신호일 수 있음):")
        for m in memos:
            age = db.days_since(m["created_at"])
            tag_str = f" [{', '.join(db.memo_tags(m['tags']))}]" if m["tags"] else ""
            stale = " ← 2주 넘게 방치됨" if age >= 14 else ""
            lines.append(f"    · {m['text']}{tag_str} ({age}일째 미완료{stale})")

    weeks = db.weekly_depth(PACE_WINDOW_WEEKS)
    lines.append(f"- 최근 {PACE_WINDOW_WEEKS}주 성취 (페이스 판단의 핵심):")
    for w in weeks:
        depth_label = LADDER.get(w["max_depth"], "성취 없음").split(" —")[0]
        lines.append(
            f"    · {w['week_start']} 주: {w['count']}건, 최고 {w['max_depth']}단 ({depth_label})"
        )
    gh = db.github_active_days(14)
    if gh:
        days = sorted({g["date"] for g in gh}, reverse=True)
        lines.append(f"- 최근 2주 GitHub 활동일: {len(days)}일")
        for g in gh[:6]:
            # 커밋 수 = 성과가 아니라 마찰. 많을수록 그날 고생한 것.
            friction = " ← 많이 헤맴" if g["commits"] >= 5 else ""
            lines.append(f"    · {g['date']} {g['repo'].split('/')[-1]}: {g['commits']}커밋{friction}")
    else:
        lines.append("- 최근 2주 GitHub 활동: 없음")

    personal = db.personal_posts_since(14)
    if personal:
        lines.append("- 최근 2주 매거진 밖 글 (일상·기분. 성취는 아니지만 상태를 보여주는 것):")
        for p in personal:
            excerpt = (p["content"] or "").strip()[:120]
            lines.append(f"    · [{p['created_at'][:10]}] {p['title']}: {excerpt}...")

    recent_ach = db.achievements_since(PACE_WINDOW_WEEKS * 7)
    if recent_ach:
        lines.append("- 최근 성취 내역:")
        for a in recent_ach[-8:]:
            lines.append(f"    · [{a['depth']}단] {a['text']} ({a['created_at'][:10]})")

    refl_count = db.reflection_count_since(TOTAL_DAYS)
    lines.append(f"- 지금까지 성찰 질문 {refl_count}번 (100일에 몇 번이 적당 — 남발 금지)")

    if past:
        lines.append("- 최근 체크인 이력 (같은 얘기를 반복하지 않기 위한 참고):")
        for c in past:
            if c["spoke"]:
                fb = f" → 사용자 채점: {c['feedback']}" if c["feedback"] else " → 채점 없음"
                kind = " [성찰 질문]" if c["reflection"] else ""
                lines.append(
                    f"    · {c['created_at'][:10]} 말 검({c['trigger']}){kind}: {c['message'][:50]}{fb}"
                )
            else:
                lines.append(f"    · {c['created_at'][:10]} 침묵: {c['reason']}")

    return "\n".join(lines)


def _build_system() -> tuple[str, str, str]:
    """시스템 프롬프트, 버전 해시, 그리고 상황 원문을 반환.

    상황(context)은 매일 바뀌므로 버전에 포함하지 않습니다. 대신 raw로 남깁니다.
    규칙(version) + 상황(raw) 이 둘이 있어야 판단을 재현할 수 있습니다.
    """
    prefs = db.preferences()
    context = _context_block()
    system = CHECKIN_SYSTEM.format(
        background=BACKGROUND,
        preferences="\n".join(f"- {p}" for p in prefs) if prefs else "- (없음)",
        context=context,
        ladder="\n".join(f"  {k}단: {v}" for k, v in LADDER.items()),
        window=PACE_WINDOW_WEEKS,
        shallow=PACE_SHALLOW_DEPTH,
    )
    # 버전은 상황(context)이 아니라 **판단 규칙**으로만 계산합니다.
    # context는 매일 바뀌므로 포함하면 버전이 매일 달라져 의미가 없어집니다.
    rules = f"{CHECKIN_SYSTEM}\n{LADDER}\n{PACE_WINDOW_WEEKS}/{PACE_SHALLOW_DEPTH}"
    version = db.register_prompt_version(rules, prefs)
    return system, version, context


def _clamp_confidence(value) -> int | None:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return None


def _make_raw(context: str, history: list[dict], note: str = "") -> str:
    """판단의 입력 원문. 이게 있어야 나중에 다른 버전으로 다시 돌려볼 수 있습니다."""
    return json.dumps(
        {"context": context, "history": history, "note": note},
        ensure_ascii=False,
    )


async def decide() -> dict:
    """말을 걸지 판단."""
    system, version, context = _build_system()
    history = db.history(HISTORY_LIMIT)

    # 코드 차원의 안전장치: 답이 없는데 계속 말 거는 건 잔소리.
    # 이건 모델 판단에 맡기지 않고 강제합니다.
    streak = db.unanswered_checkin_streak()
    if streak >= BACKOFF_THRESHOLD:
        return {
            "speak": False, "confidence": 100, "trigger": "none",
            "needs_feedback": False, "reflection": False,
            "reason": f"연속 {streak}회 답이 없음. 물러남.",
            "message": None, "unspoken": None, "prompt_version": version,
            "raw_input": _make_raw(context, history, f"backoff: streak={streak}"),
        }

    prompt = (
        "위 상황을 보고 판단하세요. 아래는 최근 대화 기록입니다.\n\n"
        + "\n\n".join(f"[{m['role']}] {m['content']}" for m in history)
        if history
        else "아직 대화 기록이 없습니다. 판단하세요."
    )

    raw = await brain.call([{"role": "user", "content": prompt}], system=system, max_tokens=800)

    result = brain.extract_json(raw)
    if result is None:
        # 판단을 파싱 못 하면 침묵. 애매할 때 침묵이 안전한 기본값.
        # 파싱 실패해도 raw는 남깁니다 — 나중에 왜 깨졌는지 봐야 하니까.
        return {
            "speak": False, "confidence": None, "trigger": "none",
            "needs_feedback": False, "reflection": False,
            "reason": "판단 응답 파싱 실패", "message": None,
            "unspoken": None, "prompt_version": version,
            "raw_input": _make_raw(context, history, f"파싱 실패한 응답: {raw[:500]}"),
        }

    speak = bool(result.get("speak")) and bool(result.get("message"))
    trigger = result.get("trigger") if result.get("trigger") in TRIGGERS else "none"
    reflection = speak and bool(result.get("reflection"))
    # 안부/공감은 판단이 아니므로 채점 버튼이 필요 없음. speak가 false면 애초에 안 붙음.
    # 모델이 필드를 빠뜨리면(None) 기존 동작대로 판단으로 취급 — 명시적으로 false일 때만 안부.
    # 성찰 질문은 형식상 판단이 아니므로, 모델이 뭐라 답했든 채점 버튼을 강제로 뗌.
    needs_feedback = speak and not reflection and result.get("needs_feedback") is not False

    return {
        "speak": speak,
        "confidence": _clamp_confidence(result.get("confidence")),
        "trigger": trigger if speak else "none",
        "needs_feedback": needs_feedback,
        "reflection": reflection,
        "reason": result.get("reason") or "",
        "message": result.get("message") if speak else None,
        "unspoken": None if speak else (result.get("unspoken") or None),
        "prompt_version": version,
        "raw_input": _make_raw(context, history),
    }


async def run(send) -> dict:
    """체크인 실행. send(message, checkin_id, needs_feedback)로 전송합니다."""
    decision = await decide()

    checkin_id = db.log_checkin(
        spoke=decision["speak"],
        trigger=decision["trigger"],
        confidence=decision["confidence"],
        reason=decision["reason"],
        message=decision["message"],
        unspoken=decision["unspoken"],
        prompt_version=decision["prompt_version"],
        raw_input=decision.get("raw_input"),
        needs_feedback=decision["needs_feedback"],
        reflection=decision["reflection"],
    )
    decision["id"] = checkin_id

    if decision["speak"]:
        await send(decision["message"], checkin_id, decision["needs_feedback"])
        # 마늘이 먼저 건 말도 대화 기록에 남아야 다음 판단에 반영됨
        db.add_message("assistant", decision["message"])

    return decision
