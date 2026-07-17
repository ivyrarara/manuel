"""마늘 — 텔레그램 에이전트.

프로세스 하나가 세 가지를 합니다:
  1. 사용자 메시지에 응답 (챗봇 부분)
  2. 스스로 깨어나서 말을 걸지 판단 (에이전트 부분)
  3. 자기 판단을 채점받아 기록 (자산이 되는 부분)

실행: python -m agent.main
"""
import logging
from datetime import time as dtime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

from . import blog, brain, checkin, db, github
from .config import (
    BLOG_CHECK_HOUR, BLOG_CHECK_MINUTE, CHECKIN_HOUR, CHECKIN_MINUTE,
    GITHUB_CHECK_HOUR, GITHUB_CHECK_MINUTE, GITHUB_USER,
    FEEDBACK_LABELS, FEEDBACK_OPTIONS, LADDER, MONDAY_ACTIONS, OWNER_CHAT_ID,
    PACE_WINDOW_WEEKS, SUNDAY_REVIEW, TELEGRAM_TOKEN, TOTAL_DAYS, TRIGGERS, TZ,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("maneul")


def owner_only(handler):
    """이 봇은 한 사람만 씁니다. 토큰이 유출돼도 남이 못 씁니다."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        if not chat or chat.id != OWNER_CHAT_ID:
            log.warning("허가되지 않은 chat_id 접근: %s", chat.id if chat else "?")
            return
        return await handler(update, context)
    return wrapper


def feedback_keyboard(checkin_id: int) -> InlineKeyboardMarkup:
    """탭 한 번으로 채점. 타이핑 0. 마늘이 먼저 말 걸 때만 붙습니다."""
    buttons = [
        InlineKeyboardButton(label, callback_data=f"fb:{checkin_id}:{code}")
        for code, label in FEEDBACK_OPTIONS
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)


def silence_keyboard(checkin_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🗣️ 말했어야 함", callback_data=f"sr:{checkin_id}:should_have_spoken"),
        InlineKeyboardButton("🤐 침묵이 맞음", callback_data=f"sr:{checkin_id}:correct_silence"),
    ]])


def format_insights(insights: list[dict]) -> str:
    if not insights:
        return ""
    lines = ["", "———"]
    for i in insights:
        if i["type"] == "achievement":
            lines.append(f"🧄 [{i['depth']}단] {i['text']}")
        elif i["type"] == "action":
            lines.append(f"✓ {i['text']}")
        else:
            lines.append(f"💡 {i['text']}")
    return "\n".join(lines)


# ---------- 핸들러 ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """chat_id 확인용. 환경변수 설정 전에 이걸로 본인 id를 알아냅니다."""
    await update.message.reply_text(
        f"마늘입니다.\n\n당신의 chat_id: {update.effective_chat.id}\n"
        f"이 숫자를 OWNER_CHAT_ID 에 넣어주세요."
    )


@owner_only
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return

    await context.bot.send_chat_action(chat_id=OWNER_CHAT_ID, action=ChatAction.TYPING)

    try:
        result = await brain.respond_to(text)
    except Exception:
        log.exception("응답 생성 실패")
        await update.message.reply_text("마늘이 응답하지 못했어요. 잠시 후 다시 시도해주세요.")
        return

    await update.message.reply_text(result["reply"] + format_insights(result["insights"]))

    if result["learned"]:
        learned = "\n".join(f"· {p}" for p in result["learned"])
        await update.message.reply_text(f"🧠 기억했어요:\n{learned}\n\n/forget 으로 지울 수 있어요.")


@owner_only
async def on_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """버튼 탭 처리. 채점하고, 누른 결과를 메시지에 남깁니다."""
    query = update.callback_query
    kind, raw_id, code = query.data.split(":", 2)
    checkin_id = int(raw_id)

    db.set_feedback(checkin_id, code)

    if kind == "fb":
        label = FEEDBACK_LABELS.get(code, code)
    else:
        label = "🗣️ 말했어야 함" if code == "should_have_spoken" else "🤐 침묵이 맞음"

    await query.answer("기록했어요")
    # 버튼을 지우고 고른 것만 남김 — 다시 못 누르게, 그리고 뭘 골랐는지 보이게
    await query.edit_message_reply_markup(
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"✓ {label}", callback_data="noop")]])
    )


async def on_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


@owner_only
async def actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db.pending_actions()
    if not rows:
        await update.message.reply_text("미완료 액션 아이템이 없어요.")
        return
    lines = [f"미완료 액션 {len(rows)}개:", ""]
    lines += [f"{r['id']}. {r['text']}" for r in rows]
    lines += ["", "완료하려면: /done <번호>"]
    await update.message.reply_text("\n".join(lines))


@owner_only
async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("사용법: /done <번호>  (번호는 /actions 에서 확인)")
        return
    ok = db.complete_action(int(context.args[0]))
    await update.message.reply_text("완료 처리했어요." if ok else "그 번호의 액션을 찾지 못했어요.")


@owner_only
async def prefs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db.list_preferences()
    if not rows:
        await update.message.reply_text("아직 학습된 요구사항이 없어요.")
        return
    lines = ["마늘이 학습한 요구사항:", ""]
    lines += [f"{r['id']}. {r['text']}" for r in rows]
    lines += ["", "지우려면: /forget <번호>"]
    await update.message.reply_text("\n".join(lines))


@owner_only
async def forget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("사용법: /forget <번호>  (번호는 /prefs 에서 확인)")
        return
    ok = db.forget_preference(int(context.args[0]))
    await update.message.reply_text("잊었어요." if ok else "그 번호의 요구사항을 찾지 못했어요.")


@owner_only
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    silent = db.days_since_last_user_message()
    lines = [
        f"Day {db.day_number()} / {TOTAL_DAYS}",
        f"미완료 액션: {len(db.pending_actions())}개",
        f"최근 7일 배운 점: {len(db.learnings_since(7))}개",
        f"최근 7일 성취: {len(db.achievements_since(7))}건 (/pace 로 자세히)",
        f"마지막 대화: {silent}일 전" if silent is not None else "대화 기록 없음",
    ]
    recent = db.recent_checkins(3)
    if recent:
        lines += ["", "최근 체크인:"]
        for c in recent:
            state = f"말 검 ({c['trigger']})" if c["spoke"] else "침묵"
            conf = f" {c['confidence']}%" if c["confidence"] is not None else ""
            fb = f" [{FEEDBACK_LABELS.get(c['feedback'], c['feedback'])}]" if c["feedback"] else ""
            lines.append(f"· {c['created_at'][:10]} {state}{conf}{fb} — {c['reason']}")
    await update.message.reply_text("\n".join(lines))


@owner_only
async def data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """10월에 들고 갈 표의 미리보기. 지금은 비어있는 게 정상입니다."""
    versions = db.prompt_version_history()
    summary = db.feedback_summary()

    cov = db.raw_coverage()
    lines = ["📊 지금까지 쌓인 것", ""]
    c, b = cov["checkins"], cov["posts"]
    ok = (c["total"] == c["with_raw"]) and (b["total"] == b["with_raw"])
    lines.append(f"{'✅' if ok else '⚠️'} 원문 보존: 판단 {c['with_raw']}/{c['total']}, 글 {b['with_raw']}/{b['total']}")
    lines.append("원문이 있어야 나중에 다른 버전으로 다시 돌려볼 수 있어요.")
    lines.append("")

    if versions:
        lines.append("프롬프트 버전:")
        for v in versions:
            lines.append(f"· {v['version']} ({v['first_seen'][:10]}~) 판단 {v['checkins']}회, 발화 {v['spoke']}회")
    else:
        lines.append("프롬프트 버전: 아직 없음")

    lines.append("")
    if summary:
        lines.append("방아쇠별 채점:")
        for row in summary:
            trigger_label = TRIGGERS.get(row["trigger"], row["trigger"])
            fb_label = FEEDBACK_LABELS.get(row["feedback"], row["feedback"])
            lines.append(f"· {trigger_label.split(' —')[0]} → {fb_label}: {row['n']}회")
    else:
        lines.append("방아쇠별 채점: 아직 없음 (마늘이 말을 걸고, 버튼을 눌러야 쌓여요)")

    await update.message.reply_text("\n".join(lines))


@owner_only
async def checkin_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """체크인 판단을 지금 강제로 돌려봄. 침묵을 골라도 이유와 삼킨 말을 보여줍니다."""
    async def send(msg, checkin_id):
        await context.bot.send_message(
            chat_id=OWNER_CHAT_ID, text=msg, reply_markup=feedback_keyboard(checkin_id)
        )

    decision = await checkin.run(send)
    if not decision["speak"]:
        conf = f" (확신도 {decision['confidence']}%)" if decision["confidence"] is not None else ""
        text = f"(침묵을 선택했어요{conf} — {decision['reason']})"
        if decision["unspoken"]:
            text += f"\n\n삼킨 말:\n“{decision['unspoken']}”"
        await update.message.reply_text(text)



@owner_only
async def blog_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """RSS 주소가 실제로 되는지 지금 확인. 추측으로 100일 날리지 않기 위한 명령어."""
    if not blog.enabled():
        await update.message.reply_text(
            "블로그가 연결 안 됐어요.\n\n"
            "Railway → Variables → BLOG_RSS_URL 에 RSS 주소를 넣어주세요.\n"
            "브런치라면: https://brunch.co.kr/rss/@@아이디"
        )
        return

    await update.message.reply_text("블로그 읽어볼게요...")
    result = await blog.sync()

    if not result["ok"]:
        await update.message.reply_text(
            f"❌ 실패했어요.\n\n{result['error']}\n\n"
            f"주소: {blog.BLOG_RSS_URL}\n\n"
            f"브런치는 작가 프로필(brunch.co.kr/@@아이디)의 @@ 뒷부분을 써야 해요."
        )
        return

    lines = [f"✅ 연결됐어요. 글 {result['total']}개가 보여요."]
    if result.get("first_run"):
        lines.append(f"\n기존 글 {len(result['new'])}개는 성취로 세지 않았어요.")
        lines.append("과거 글을 오늘 성취로 넣으면 페이스 차트가 거짓말이 되니까요.")
        lines.append("지금부터 올리는 글이 6단 성취로 잡혀요.")
    elif result["recorded"]:
        lines.append("\n새 글을 6단 성취로 기록했어요:")
        lines += [f"🧄 {p['title']}" for p in result["recorded"]]
    else:
        lines.append("\n새 글은 없어요.")
    await update.message.reply_text("\n".join(lines))

    for post in result["recorded"][:1]:
        await send_post_insights(context, post)


@owner_only
async def pace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """페이스 — 개수가 아니라 도달 깊이."""
    weeks = db.weekly_depth(PACE_WINDOW_WEEKS)
    lines = [f"🏃 페이스 (Day {db.day_number()} / {TOTAL_DAYS})", ""]
    for w in weeks:
        if w["max_depth"]:
            label = LADDER[w["max_depth"]].split(" —")[0]
            bar = "█" * w["max_depth"] + "·" * (6 - w["max_depth"])
            lines.append(f"{w['week_start']}  {bar}  {w['max_depth']}단 {label} ({w['count']}건)")
        else:
            lines.append(f"{w['week_start']}  ······  성취 없음")

    totals = db.achievement_totals()
    if totals:
        lines += ["", "누적:"]
        for t in totals:
            if t["depth"]:
                lines.append(f"· {t['depth']}단 {LADDER[t['depth']].split(' —')[0]}: {t['n']}건")
    else:
        lines += ["", "아직 성취 기록이 없어요. 뭔가 해냈으면 마늘한테 말해주세요."]
    await update.message.reply_text("\n".join(lines))


async def send_post_insights(context: ContextTypes.DEFAULT_TYPE, post: dict):
    """발행한 글을 읽고 놓친 것을 짚어줍니다.

    글 1개라는 숫자보다 이게 중요합니다. 숫자는 세면 그만이지만,
    놓친 통찰은 다음 글의 시작이 됩니다.
    """
    try:
        await context.bot.send_chat_action(chat_id=OWNER_CHAT_ID, action=ChatAction.TYPING)
        result = await blog.read_post(post["link"], post["title"], post.get("content", ""))
    except Exception:
        log.exception("글 분석 실패")
        return

    if not result:
        log.info("글 본문을 읽지 못해 분석을 건너뜀: %s", post["title"])
        return

    lines = [f"「{post['title']}」 읽었어요.", ""]
    if result["observation"]:
        lines.append(result["observation"])
    if result["missed"]:
        lines += ["", "놓치신 것 같은 지점:"]
        lines += [f"💡 {m}" for m in result["missed"]]
    if result["seeds"]:
        lines += ["", "다음 글이 될 수 있는 것:"]
        lines += [f"🌱 {s}" for s in result["seeds"]]

    if len(lines) <= 2:
        return  # 할 말이 없으면 침묵

    text = "\n".join(lines)
    await context.bot.send_message(chat_id=OWNER_CHAT_ID, text=text)

    # 대화 기록에 남겨야 다음 판단에 반영됨
    mid = db.add_message("assistant", text)
    if result["missed"]:
        db.add_insights(mid, [{"text": m, "type": "learning"} for m in result["missed"]])


async def job_blog(context: ContextTypes.DEFAULT_TYPE):
    """체크인 전에 블로그를 먼저 읽어둡니다. 그래야 오늘 쓴 글이 판단에 반영돼요."""
    if not blog.enabled():
        return
    try:
        result = await blog.sync()
        if not result["ok"]:
            log.warning("블로그 확인 실패: %s", result["error"])
            return
        if not result["recorded"]:
            return

        titles = "\n".join(f"🧄 {p['title']}" for p in result["recorded"])
        await context.bot.send_message(
            chat_id=OWNER_CHAT_ID,
            text=f"블로그에 새 글이 올라왔네요. 6단 성취로 기록했어요.\n\n{titles}",
        )
        # 가장 최근 글 하나만 분석 — 여러 개면 알림이 시끄러워집니다
        await send_post_insights(context, result["recorded"][-1])
    except Exception:
        log.exception("블로그 잡 실패")



@owner_only
async def github_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """GitHub 연결 확인. 추측으로 100일 날리지 않기 위한 명령어."""
    if not github.enabled():
        await update.message.reply_text(
            "GitHub이 연결 안 됐어요.\n\n"
            "Railway → Variables → GITHUB_USER 에 아이디를 넣어주세요. (예: ivyrarara)"
        )
        return

    await update.message.reply_text("GitHub 읽어볼게요...")
    result = await github.sync()

    if not result["ok"]:
        await update.message.reply_text(f"❌ 실패했어요.\n\n{result['error']}")
        return

    lines = [f"✅ 연결됐어요. 최근 활동 {result['total']}건이 보여요."]
    repos = sorted({i["repo"] for i in result["new"]})
    if repos:
        lines.append("\n저장소:")
        lines += [f"· {r}" for r in repos]

    if result.get("first_run"):
        lines.append(f"\n과거 활동 {len(result['new'])}건은 성취로 세지 않았어요.")
        lines.append("지금부터의 커밋이 5단 개선 성취로 잡혀요.")
    elif result["recorded"]:
        lines.append("\n새 활동을 성취로 기록했어요:")
        lines += [f"🧄 {t}" for t in result["recorded"]]
    else:
        lines.append("\n새 활동은 없어요.")
    await update.message.reply_text("\n".join(lines))


async def job_github(context: ContextTypes.DEFAULT_TYPE):
    """체크인 전에 GitHub을 읽어둡니다. 오늘 커밋이 오늘 판단에 반영되도록."""
    if not github.enabled():
        return
    try:
        result = await github.sync()
        if result["ok"] and result["recorded"]:
            items = "\n".join(f"🧄 {t}" for t in result["recorded"])
            await context.bot.send_message(
                chat_id=OWNER_CHAT_ID,
                text=f"GitHub에서 오늘 작업을 봤어요. 성취로 기록했어요.\n\n{items}",
            )
        elif not result["ok"]:
            log.warning("GitHub 확인 실패: %s", result["error"])
    except Exception:
        log.exception("GitHub 잡 실패")


# ---------- 스케줄 작업 ----------

async def job_checkin(context: ContextTypes.DEFAULT_TYPE):
    async def send(msg, checkin_id):
        await context.bot.send_message(
            chat_id=OWNER_CHAT_ID, text=msg, reply_markup=feedback_keyboard(checkin_id)
        )

    try:
        decision = await checkin.run(send)
        log.info(
            "체크인: %s (%s, %s%%) — %s",
            "말 검" if decision["speak"] else "침묵",
            decision["trigger"], decision["confidence"], decision["reason"],
        )
    except Exception:
        log.exception("체크인 실패")


async def job_sunday_review(context: ContextTypes.DEFAULT_TYPE):
    """이번 주 배운 것 + 삼킨 말 채점.

    말한 것만 채점하면 데이터가 반쪽입니다. 안 한 말도 채점되어야
    계측이 양쪽으로 닫힙니다.
    """
    rows = db.learnings_since(7)
    if not rows:
        text = "이번 주는 기록이 없네요. 5분만 돌아볼까요? 뭐가 걸리적거렸나요?"
    else:
        items = "\n".join(f"💡 {r['text']}" for r in rows)
        text = f"이번 주에 배운 것 {len(rows)}가지예요.\n\n{items}\n\n이 중에 다음 주로 가져갈 건 뭘까요?"
    await context.bot.send_message(chat_id=OWNER_CHAT_ID, text=text)
    db.add_message("assistant", text)

    silences = db.unreviewed_silences(days=7, limit=3)
    if not silences:
        return

    await context.bot.send_message(
        chat_id=OWNER_CHAT_ID,
        text="그리고 이번 주에 제가 삼킨 말들이에요. 말했어야 했던 게 있나요?",
    )
    for s in silences:
        conf = f" (당시 확신도 {s['confidence']}%)" if s["confidence"] is not None else ""
        await context.bot.send_message(
            chat_id=OWNER_CHAT_ID,
            text=f"{s['created_at'][:10]}{conf}\n“{s['unspoken']}”",
            reply_markup=silence_keyboard(s["id"]),
        )


async def job_monday_actions(context: ContextTypes.DEFAULT_TYPE):
    rows = db.pending_actions()
    if not rows:
        text = "새 주가 시작됐어요. 이번 주 목표를 하나만 정해볼까요?"
    else:
        items = "\n".join(f"{r['id']}. {r['text']}" for r in rows)
        text = f"아직 실행 안 한 액션 {len(rows)}개예요.\n\n{items}\n\n이번 주에 뭐부터 할까요?"
    await context.bot.send_message(chat_id=OWNER_CHAT_ID, text=text)
    db.add_message("assistant", text)


def main():
    db.init()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("actions", actions))
    app.add_handler(CommandHandler("done", done))
    app.add_handler(CommandHandler("prefs", prefs))
    app.add_handler(CommandHandler("forget", forget))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("data", data))
    app.add_handler(CommandHandler("pace", pace))
    app.add_handler(CommandHandler("blog", blog_check))
    app.add_handler(CommandHandler("github", github_check))
    app.add_handler(CommandHandler("checkin", checkin_now))
    app.add_handler(CallbackQueryHandler(on_noop, pattern=r"^noop$"))
    app.add_handler(CallbackQueryHandler(on_feedback, pattern=r"^(fb|sr):"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    jq = app.job_queue
    jq.run_daily(job_blog, time=dtime(BLOG_CHECK_HOUR, BLOG_CHECK_MINUTE, tzinfo=TZ))
    jq.run_daily(job_github, time=dtime(GITHUB_CHECK_HOUR, GITHUB_CHECK_MINUTE, tzinfo=TZ))
    jq.run_daily(job_checkin, time=dtime(CHECKIN_HOUR, CHECKIN_MINUTE, tzinfo=TZ))

    weekday, hour, minute = SUNDAY_REVIEW
    jq.run_daily(job_sunday_review, time=dtime(hour, minute, tzinfo=TZ), days=(weekday,))

    weekday, hour, minute = MONDAY_ACTIONS
    jq.run_daily(job_monday_actions, time=dtime(hour, minute, tzinfo=TZ), days=(weekday,))

    log.info("마늘 시작. Day %s / %s", db.day_number(), TOTAL_DAYS)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
