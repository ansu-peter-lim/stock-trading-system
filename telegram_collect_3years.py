import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient


# ============================================================
# 설정
# ============================================================

load_dotenv()

api_id = int(os.environ["TELEGRAM_API_ID"])
api_hash = os.environ["TELEGRAM_API_HASH"]

CHANNEL = "balanceasset"

# 과거 3년
YEARS_TO_COLLECT = 3

# 원문 저장 위치
SAVE_DIR = Path("data/raw/telegram/daily")

# Telegram 로그인 세션
SESSION_NAME = "telegram_test"


# ============================================================
# TOP30 판별
# ============================================================

def is_top30_message(text: str) -> bool:
    """
    메시지 본문에 '상승률 TOP30'이 포함되어 있으면
    TOP30 자료로 판단한다.

    제목 형식은 판단 기준으로 사용하지 않는다.
    """

    return "상승률 TOP30" in text


# ============================================================
# 파일명 생성
# ============================================================

def make_filename(message_date, message_id: int) -> str:
    """
    Telegram 메시지 날짜와 ID를 이용해
    중복되지 않는 파일명을 만든다.
    """

    date_text = message_date.strftime("%Y-%m-%d_%H-%M-%S")

    return f"{date_text}_{message_id}.txt"


# ============================================================
# 메인
# ============================================================

client = TelegramClient(
    SESSION_NAME,
    api_id,
    api_hash,
)


async def main():

    SAVE_DIR.mkdir(parents=True, exist_ok=True)

    channel = await client.get_entity(CHANNEL)

    now = datetime.now(timezone.utc)

    since = now - timedelta(
        days=365 * YEARS_TO_COLLECT
    )

    print("=" * 70)
    print("Telegram TOP30 과거 데이터 수집")
    print("=" * 70)
    print(f"채널       : {channel.title}")
    print(f"검색 시작   : {since}")
    print(f"검색 종료   : {now}")
    print(f"저장 위치   : {SAVE_DIR}")
    print("=" * 70)

    scanned_count = 0
    top30_count = 0
    saved_count = 0
    duplicate_count = 0

    async for message in client.iter_messages(channel):

        # ----------------------------------------------------
        # 3년보다 오래된 메시지에 도달하면 종료
        # ----------------------------------------------------

        if message.date < since:
            break

        scanned_count += 1

        # 진행 상황 표시
        if scanned_count % 500 == 0:
            print(
                f"[진행] 조회 {scanned_count:,}개 / "
                f"TOP30 {top30_count:,}개 / "
                f"저장 {saved_count:,}개"
            )

        # 텍스트가 없는 메시지는 건너뜀
        if not message.text:
            continue

        # ----------------------------------------------------
        # TOP30 여부 확인
        # ----------------------------------------------------

        if not is_top30_message(message.text):
            continue

        top30_count += 1

        # ----------------------------------------------------
        # 파일명 생성
        # ----------------------------------------------------

        filename = make_filename(
            message.date,
            message.id,
        )

        filepath = SAVE_DIR / filename

        # ----------------------------------------------------
        # 중복 확인
        # ----------------------------------------------------

        if filepath.exists():
            duplicate_count += 1
            continue

        # ----------------------------------------------------
        # 원문 저장
        # ----------------------------------------------------

        filepath.write_text(
            message.text,
            encoding="utf-8",
        )

        saved_count += 1

        print(
            f"[저장 {saved_count:,}] "
            f"{message.date.strftime('%Y-%m-%d')} "
            f"message_id={message.id}"
        )

    # ========================================================
    # 최종 결과
    # ========================================================

    print()
    print("=" * 70)
    print("수집 완료")
    print("=" * 70)
    print(f"조회 메시지       : {scanned_count:,}")
    print(f"TOP30 메시지      : {top30_count:,}")
    print(f"새로 저장         : {saved_count:,}")
    print(f"중복/이미 존재    : {duplicate_count:,}")
    print(f"저장 폴더         : {SAVE_DIR}")
    print("=" * 70)


with client:
    client.loop.run_until_complete(main())