"""마늘 — 텔레그램 에이전트.

프로세스 하나가 세 가지를 합니다:
  1. 사용자 메시지에 응답 (챗봇 부분)
  2. 스스로 깨어나서 말을 걸지 판단 (에이전트 부분)
  3. 자기 판단을 채점받아 기록 (자산이 되는 부분)

실행: python -m agent.main
"""
import logging
import os
import re
from collections import deque
from datetime import time as dtime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import InvalidToken
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

from . import blog, brain, checkin, db, github
from .config import (
    BACKUP_DAY, BACKUP_HOUR, BACKUP_MINUTE,
    BLOG_CHECK_HOUR, BLOG_CHECK_MINUTE, CHECKIN_HOUR, CHECKIN_MINUTE,
    GITHUB_CHECK_HOUR, GITHUB_CHECK_MINUTE, GITHUB_USER,
    FEEDBACK_LABELS, FEEDBACK_OPTIONS, MONDAY_ACTIONS, OWNER_CHAT_ID,
    CHECKIN_DAYS, SUNDAY_REVIEW, TELEGRAM_TOKEN,
    TOTAL_DAYS, TRIGGERS, TZ,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)

# httpx는 모든 요청 URL을 INFO로 남깁니다. 그런데 텔레그램 API는 주소 안에 토큰을 넣습니다:
#   POST https://api.telegram.org/bot<토큰>/getMe
# 그래서 로그를 남에게 보여주는 순간 봇이 탈취됩니다. WARNING으로 올려서 막습니다.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
# 스케줄러가 잡 등록할 때마다 남기는 잡음도 줄입니다.
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Application").setLevel(logging.WARNING)

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


def preference_keyboard(pref_id: int) -> InlineKeyboardMarkup:
    """승인 없이는 규칙이 되지 않습니다. 탭 한 번이면 충분하니 마찰도 거의 없습니다."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🧠 기억해", callback_data=f"pref:{pref_id}:y"),
        InlineKeyboardButton("🙅 아니", callback_data=f"pref:{pref_id}:n"),
    ]])


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
            lines.append(f"🧄 {'•' * i['depth']} {i['text']}")
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


# 주소에 쓸 수 있는 문자만 인정합니다.
# `[^\s]+` 로 잡으면 "brunch.co.kr/@ivyra/301이게" 처럼 띄어쓰기 없이 붙여 쓴
# 한글까지 주소로 삼아버립니다. 그러면 없는 주소를 부르게 되고, 조용히 실패합니다.
URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")


def _extract_url(text: str) -> str | None:
    urls = URL_RE.findall(text)
    if not urls:
        return None
    # 문장 끝에 붙은 마침표·쉼표·괄호는 주소가 아닙니다.
    return urls[0].rstrip(".,;:!?)'\"")


async def _with_link_content(text: str) -> str:
    """메시지에 링크가 있으면 읽어서 함께 넘깁니다.

    마늘은 링크를 클릭할 수 없습니다. 붙여넣은 글을 읽어주길 기대하는 건
    자연스러운데, 그걸 못 하면 "접근할 수 없어요"만 반복하게 됩니다.
    """
    url = _extract_url(text)
    if not url:
        return text

    article = await blog.fetch_url_text(url)
    if not article or len(article) < 100:
        log.warning("링크를 읽지 못했어요: %s", url)
        return text
    return f"{text}\n\n[아래는 사용자가 보낸 링크의 내용입니다]\n{article[:10000]}"


@owner_only
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return

    await context.bot.send_chat_action(chat_id=OWNER_CHAT_ID, action=ChatAction.TYPING)

    try:
        text = await _with_link_content(text)
        result = await brain.respond_to(text)
    except Exception:
        log.exception("응답 생성 실패")
        await update.message.reply_text("마늘이 응답하지 못했어요. 잠시 후 다시 시도해주세요.")
        return

    await update.message.reply_text(result["reply"] + format_insights(result["insights"]))

    # 지나가는 말이 영구 규칙이 되지 않도록, 승인을 받고 나서 적용합니다.
    for pref in result["proposed"]:
        await update.message.reply_text(
            f"🧠 이걸 규칙으로 기억할까요?\n\n“{pref['text']}”",
            reply_markup=preference_keyboard(pref["id"]),
        )


# 앨범(여러 장 한 번에 전송)이면 텔레그램이 사진마다 업데이트를 따로 보냅니다.
# 같은 media_group_id의 두 번째 장부터는 무시해서 첫 장만 봅니다.
_recent_media_groups: deque = deque(maxlen=50)


def _first_in_media_group(update: Update) -> bool:
    mgid = update.message.media_group_id
    if not mgid:
        return True
    if mgid in _recent_media_groups:
        return False
    _recent_media_groups.append(mgid)
    return True


@owner_only
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _first_in_media_group(update):
        return

    caption = (update.message.caption or "").strip()
    photo = update.message.photo[-1]  # 화질별로 여러 장 오는데, 제일 큰 것 하나만

    await context.bot.send_chat_action(chat_id=OWNER_CHAT_ID, action=ChatAction.TYPING)

    try:
        tg_file = await context.bot.get_file(photo.file_id)
        image_bytes = bytes(await tg_file.download_as_bytearray())
        # 텔레그램은 "사진"으로 보낸 이미지를 항상 JPEG로 재인코딩해서 줍니다.
        result = await brain.respond_to_photo(image_bytes, "image/jpeg", caption)
    except Exception:
        log.exception("사진 응답 생성 실패")
        await update.message.reply_text("사진을 보는 데 실패했어요. 잠시 후 다시 시도해주세요.")
        return

    await update.message.reply_text(result["reply"] + format_insights(result["insights"]))

    for pref in result["proposed"]:
        await update.message.reply_text(
            f"🧠 이걸 규칙으로 기억할까요?\n\n“{pref['text']}”",
            reply_markup=preference_keyboard(pref["id"]),
        )


@owner_only
async def on_preference(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """요구사항 승인/거절."""
    query = update.callback_query
    _, raw_id, code = query.data.split(":", 2)
    approved = code == "y"

    text = db.resolve_preference(int(raw_id), approved)
    if text is None:
        await query.answer("이미 처리했어요")
        return

    await query.answer("기억할게요" if approved else "잊을게요")
    label = "🧠 기억함 — 이제 규칙이에요" if approved else "🙅 안 기억함"
    await query.edit_message_reply_markup(
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data="noop")]])
    )


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


# 메시지 어디에 있든(맨 앞/뒤/중간) #단어 패턴을 태그로 뽑습니다.
# 앞쪽에만 붙어야 인식되는 방식이면 "내용 #태그"처럼 뒤에 붙이거나 문장 중간에
# 섞어 쓴 태그를 놓칩니다.
MEMO_TAG_RE = re.compile(r"#(\S+)")


def _parse_memo_args(args: list[str]) -> tuple[list[str], str]:
    """`#태그` 패턴을 위치 상관없이 전부 뽑고, 나머지를 본문으로 남깁니다."""
    raw = " ".join(args)
    tags = MEMO_TAG_RE.findall(raw)
    text = " ".join(MEMO_TAG_RE.sub("", raw).split())
    return tags, text


def _format_memo_tags(tags: str | None) -> str:
    parsed = db.memo_tags(tags)
    return ", ".join(parsed) if parsed else "-"


@owner_only
async def memo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tags, text = _parse_memo_args(context.args)
    if not text:
        await update.message.reply_text("사용법: /memo [#태그 ...] <내용>")
        return
    db.add_memo(text, tags)
    tag_str = f" [{', '.join(tags)}]" if tags else ""
    await update.message.reply_text(f"📌 메모했어요.{tag_str}")


@owner_only
async def memos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tag = context.args[0].lstrip("#") if context.args else None
    rows = db.list_memos(tag)
    if not rows:
        msg = f"'{tag}' 태그의 메모가 없어요." if tag else "아직 저장된 메모가 없어요. /memo <내용> 으로 남겨보세요."
        await update.message.reply_text(msg)
        return
    lines = [f"'{tag}' 태그 메모:" if tag else "저장된 메모:", ""]
    lines += [f"[{r['id']}] [{_format_memo_tags(r['tags'])}] {r['text']} ({r['created_at'][:10]})" for r in rows]
    lines += ["", "완료 처리: /memo_done <번호>"]
    await update.message.reply_text("\n".join(lines))


@owner_only
async def memo_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("사용법: /memo_done <번호>  (번호는 /memos 에서 확인)")
        return
    ok = db.complete_memo(int(context.args[0]))
    await update.message.reply_text("완료 처리했어요." if ok else "그 번호의 메모를 찾지 못했어요.")


@owner_only
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    silent = db.days_since_last_user_message()
    lines = [
        f"Day {db.day_number()} / {TOTAL_DAYS}",
        f"미완료 액션: {len(db.pending_actions())}개",
        f"최근 7일 배운 점: {len(db.learnings_since(7))}개",
        f"최근 7일 성취: {len(db.achievements_since(7))}건 (/pace 로 최근 5일 로그)",
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


async def _send_backup(bot, caption: str):
    """DB 백업 파일을 만들어 전송하고 지웁니다. 실패해도 조용히 죽지 않습니다.

    checkins 테이블(판단 원문·대화·성취·메모 전부)이 Railway 볼륨 한 곳에만
    있는 유일본이라, 이게 실패했는데 아무도 모르면 100일치가 통째로 위험해집니다.
    """
    path = None
    try:
        path = db.create_backup_copy()
        filename = f"maneul-backup-{db.today_str()}.db"
        with open(path, "rb") as f:
            await bot.send_document(
                chat_id=OWNER_CHAT_ID, document=f, filename=filename, caption=caption,
            )
    except Exception:
        log.exception("DB 백업 전송 실패")
        await bot.send_message(
            chat_id=OWNER_CHAT_ID,
            text="⚠️ 백업 전송에 실패했어요. 잠시 후 /backup 으로 다시 시도해주세요.",
        )
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass


@owner_only
async def backup_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """수동 백업. 아무 때나 지금 상태의 DB 파일을 받습니다."""
    await _send_backup(context.bot, "지금 상태의 백업이에요. 폰에 저장해두세요.")


async def job_backup(context: ContextTypes.DEFAULT_TYPE):
    """매월 자동 백업."""
    month = int(db.today_str()[5:7])
    await _send_backup(context.bot, f"{month}월 백업이에요. 폰에 저장해두세요.")


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
        mag = sum(1 for p in result["new"] if p["is_magazine"])
        lines.append(f"\n분류: 매거진 글 {mag}개 / 그 외 {len(result['new']) - mag}개")
        lines.append("(기존 글은 성취로 세지 않았어요 — 과거 글을 오늘 성취로 넣으면")
        lines.append("페이스 차트가 1일차부터 거짓말이 되니까요.)")
        lines.append("\n지금부터 매거진에 올리는 글이 6단 성취로 잡혀요.")
    else:
        mag = [p for p in result["new"] if p["is_magazine"]]
        personal = [p for p in result["new"] if not p["is_magazine"]]
        if mag:
            lines.append("\n매거진 글 — 6단 성취로 기록했어요:")
            lines += [f"🧄 {p['title']}" for p in mag]
        if personal:
            lines.append("\n매거진 밖 글 — 읽었지만 성취로 세지 않았어요:")
            lines += [f"📖 {p['title']}" for p in personal]
        if not result["new"]:
            lines.append("\n새 글은 없어요.")
    await update.message.reply_text("\n".join(lines))

    for post in result["recorded"][:1]:
        await send_post_insights(context, post)


@owner_only
async def pace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """성취 로그 — 최근 5일치만, 깊이는 🧄 뒤 점 개수로."""
    rows = db.achievements_since(5)
    if not rows:
        await update.message.reply_text("최근 5일간 성취 기록이 없어요. 뭔가 해냈으면 마늘한테 말해주세요.")
        return

    lines = [f"{r['created_at'][:10]}  🧄 {'•' * r['depth']}  {r['text']}" for r in rows]
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


@owner_only
async def reclassify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """매거진 판별 기능이 생기기 전에 저장된 글들을 다시 분류합니다."""
    posts = db.unclassified_posts()
    if not posts:
        await update.message.reply_text("분류할 글이 없어요.")
        return

    await update.message.reply_text(f"{len(posts)}개 글을 다시 분류할게요. 좀 걸려요...")
    mag, personal, failed = [], [], []

    for p in posts:
        html = await blog.fetch_raw_html(p["link"])
        if not html:
            failed.append(p["title"])
            continue
        is_mag = blog._is_magazine_post(html)
        db.set_post_magazine(p["guid"], is_mag)
        (mag if is_mag else personal).append(p["title"])

    lines = [f"🧄 매거진 글 {len(mag)}개 (성취로 셈)"]
    lines += [f"  · {t}" for t in mag[:5]]
    lines.append(f"\n📖 그 외 {len(personal)}개 (읽지만 성취 아님)")
    lines += [f"  · {t}" for t in personal[:5]]
    if failed:
        lines.append(f"\n⚠️ 판별 실패 {len(failed)}개 — 성취로 세지 않아요")
    await update.message.reply_text("\n".join(lines))


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

    if result["recorded"]:
        lines.append("\n새 활동을 성취로 기록했어요:")
        lines += [f"🧄 {t}" for t in result["recorded"]]
    if result.get("backlog"):
        lines.append(f"\n(그중 {result['backlog']}건은 100일 시작 전 활동이라 성취로 세지 않았어요.)")
    if not result["recorded"] and not result.get("backlog"):
        lines.append("\n새 활동은 없어요.")
    await update.message.reply_text("\n".join(lines))


@owner_only
async def github_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """일회성 정리 명령어. 과거 GitHub 동기화 버그로 잘못 쌓인 성취를 바로잡습니다."""
    result = db.cleanup_github_achievements()
    await update.message.reply_text(
        "정리했어요.\n"
        f"· manuel 관련 성취 {result['removed_manuel']}건 삭제\n"
        f"· 중복 기록된 성취 {result['merged']}건을 하나로 합침"
    )


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
    except Exception as e:
        # 침묵이 기본값인 에이전트는 죽어도 티가 나지 않습니다.
        # 고장난 마늘과 침묵을 선택한 마늘이 똑같아 보이면 100일이 통째로 날아갑니다.
        # 그래서 체크인이 실패하면 반드시 알립니다.
        log.exception("체크인 실패")
        try:
            await context.bot.send_message(
                chat_id=OWNER_CHAT_ID,
                text=(
                    "⚠️ 오늘 체크인이 실패했어요. 침묵이 아니라 고장이에요.\n\n"
                    f"{type(e).__name__}: {str(e)[:200]}\n\n"
                    "크레딧이 떨어졌거나(console.anthropic.com → Billing), "
                    "일시적인 오류일 수 있어요. /checkin 으로 다시 시도해보세요."
                ),
            )
        except Exception:
            log.exception("실패 알림조차 보내지 못함")


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
    app.add_handler(CommandHandler("memo", memo))
    app.add_handler(CommandHandler("memos", memos))
    app.add_handler(CommandHandler("memo_done", memo_done))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("data", data))
    app.add_handler(CommandHandler("backup", backup_now))
    app.add_handler(CommandHandler("pace", pace))
    app.add_handler(CommandHandler("blog", blog_check))
    app.add_handler(CommandHandler("github", github_check))
    app.add_handler(CommandHandler("github_cleanup", github_cleanup))
    app.add_handler(CommandHandler("reclassify", reclassify))
    app.add_handler(CommandHandler("checkin", checkin_now))
    app.add_handler(CallbackQueryHandler(on_noop, pattern=r"^noop$"))
    app.add_handler(CallbackQueryHandler(on_preference, pattern=r"^pref:"))
    app.add_handler(CallbackQueryHandler(on_feedback, pattern=r"^(fb|sr):"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))

    jq = app.job_queue
    jq.run_daily(job_blog, time=dtime(BLOG_CHECK_HOUR, BLOG_CHECK_MINUTE, tzinfo=TZ))
    jq.run_daily(job_github, time=dtime(GITHUB_CHECK_HOUR, GITHUB_CHECK_MINUTE, tzinfo=TZ))
    # 일요일은 회고가 나가므로 자율 체크인을 건너뜁니다.
    jq.run_daily(
        job_checkin,
        time=dtime(CHECKIN_HOUR, CHECKIN_MINUTE, tzinfo=TZ),
        days=CHECKIN_DAYS,
    )

    weekday, hour, minute = SUNDAY_REVIEW
    jq.run_daily(job_sunday_review, time=dtime(hour, minute, tzinfo=TZ), days=(weekday,))

    weekday, hour, minute = MONDAY_ACTIONS
    jq.run_daily(job_monday_actions, time=dtime(hour, minute, tzinfo=TZ), days=(weekday,))

    jq.run_monthly(job_backup, when=dtime(BACKUP_HOUR, BACKUP_MINUTE, tzinfo=TZ), day=BACKUP_DAY)

    log.info("마늘 시작. Day %s / %s", db.day_number(), TOTAL_DAYS)

    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except InvalidToken:
        # 라이브러리가 던지는 예외 메시지에는 토큰 원문이 들어 있습니다.
        # 그대로 두면 크래시할 때마다 로그에 토큰이 찍힙니다. 삼키고 안전한 안내만 남깁니다.
        log.error(
            "텔레그램 토큰이 거부됐어요. TELEGRAM_TOKEN을 확인하세요. "
            "(형태: 숫자:문자열, 콜론은 하나. 기존 값을 지우고 새로 붙여넣었는지 확인)"
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
