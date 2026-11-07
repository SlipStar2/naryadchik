import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

_g = os.getenv("GROUP_CHAT_ID", "").strip()
GROUP_CHAT_ID = int(_g) if _g.lstrip("-").isdigit() else None

DB_PATH = os.getenv("DB_PATH", "naryadchik.sqlite3").strip() or "naryadchik.sqlite3"

ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "1070461736").replace(" ", "").split(",") if x.isdigit()
}


def _parse_time(s):
    try:
        h, m = s.strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except Exception:
        pass
    return 8, 0


# Часовой пояс (например Asia/Yekaterinburg). Пусто = как на компьютере/сервере.
TZ_NAME = os.getenv("TIMEZONE", "").strip()
TZINFO = None
if TZ_NAME:
    try:
        from zoneinfo import ZoneInfo
        TZINFO = ZoneInfo(TZ_NAME)
    except Exception:
        print(f"Неверный TIMEZONE «{TZ_NAME}», используется время компьютера.")


def now():
    from datetime import datetime
    return datetime.now(TZINFO).replace(tzinfo=None) if TZINFO else datetime.now()


def today():
    return now().date()


REMINDER_TIME = os.getenv("REMINDER_TIME", "08:00").strip()
REMINDER_HM = _parse_time(REMINDER_TIME)

BACKUP_HM = _parse_time(os.getenv("BACKUP_TIME", "03:00"))
try:
    BACKUP_KEEP = max(1, int(os.getenv("BACKUP_KEEP", "20")))
except ValueError:
    BACKUP_KEEP = 20
# 1 = ежедневно присылать копию базы администраторам в Telegram (защита, если сломается компьютер)
BACKUP_SEND = os.getenv("BACKUP_SEND_TO_ADMIN", "0").strip() == "1"

# Списки можно менять прямо здесь
DEPARTMENTS = ["Дробление", "Сепарация", "Электрослужба", "Технология"]
SHIFTS = ["Смена 1", "Смена 2", "Смена 3", "Смена 4"]

# True: мастер может вернуть из архива только наряды, которые создал сам
MASTER_RESTORE_ONLY_OWN = True
