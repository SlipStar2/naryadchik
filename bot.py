import asyncio
import html
import io
import os
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)

import config
import db
import reports

logging.basicConfig(level=logging.INFO)

misc = Router()      # работает и в группах (/chatid)
router = Router()    # только личные чаты
router.message.filter(F.chat.type == "private")

ROLE_NAMES = {"admin": "Администратор", "resp": "Ответственный", "master": "Мастер"}
STATUS = {
    "active": ("🟢", "Активный"),
    "postponed": ("⏸", "Отложен"),
    "done": ("🔵", "Завершён"),
    "cancelled": ("❌", "Отменён"),
    "archived": ("🗄", "В архиве"),
}
WORKING = ("active", "postponed")
ARCHIVED = ("done", "cancelled", "archived")
ALL_STATUSES = ("active", "postponed", "done", "cancelled", "archived")
FIELDS = {
    "title": "Наименование работы",
    "details": "Описание",
    "tmc": "ТМЦ",
    "planned_date": "Плановая дата",
    "shift": "Смена",
}
SEP = "────────────────"
MORNING_HM = (7, 0)  # во сколько приходит утренняя сводка (часы, минуты)

HELP_TEXT = (
    "<b>Добро пожаловать в «Нарядчик»!</b>\n"
    "Бот предназначен для оформления и контроля производственных нарядов.\n\n"
    "<b>Как пользоваться:</b>\n"
    "1. Нажмите «Создать наряд».\n"
    "2. Выберите подразделение (мастеру оно подставляется само).\n"
    "3. Укажите выполняемую работу.\n"
    "4. При необходимости добавьте описание и ТМЦ (если не нужно — отправьте «-»).\n"
    "5. Укажите плановую дату (ДД.ММ.ГГГГ).\n"
    "6. Выберите смену.\n"
    "7. Проверьте данные и сохраните наряд.\n"
    "8. Чтобы мастера увидели наряд в рабочем чате, нажмите «📣 В чат мастерам».\n\n"
    "<b>Дополнительно:</b>\n"
    "• Каждый наряд получает уникальный номер.\n"
    "• «📅 Перенести» — новая дата и смена. Если срока нет — «⏸ Отложить» (нужно указать причину).\n"
    "• При завершении можно добавить примечание о выполненной работе (или «-»).\n"
    "• После завершения можно отчитаться в чат мастеров кнопкой «📣 Отчитаться в чат».\n"
    "• Завершённые и отменённые наряды попадают в архив. При отмене нужна причина.\n"
    "• Из архива наряд можно вернуть в работу.\n"
    "• На каждом шаге есть «Назад» — можно исправить опечатку.\n"
    "• Главное меню — кнопка «🏠 Меню» внизу.\n"
    "• Значки: 🔴 просрочен, 🟡 срок сегодня, 🟢 по плану, ⏸ отложен.\n\n"
    "❓ Нужна помощь или доступ? Обратитесь к администратору приложения в Telegram: @Slip_Star"
)
NO_ACCESS = ("У вас пока нет доступа. Обратитесь к администратору и сообщите ему свой Telegram ID "
             "(кнопка «👤 Мой доступ»).\nАдминистратор приложения: @Slip_Star")


# ====================== вспомогательное ======================
def esc(s):
    return html.escape(str(s))


def short(s, n):
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def fmt_date(iso):
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%d.%m.%Y")
    except Exception:
        return str(iso)


def parse_date(text):
    try:
        return datetime.strptime(text.strip(), "%d.%m.%Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def full_name(u):
    return " ".join(x for x in (u.first_name, u.last_name) if x) or (u.username or str(u.id))


@dataclass
class Ctx:
    id: int
    name: str
    role: Optional[str]
    dept: Optional[str]
    notify: bool = True


def ctx_of(u):
    is_new = db.upsert_user(u.id, full_name(u), u.username)
    row = db.get_user(u.id)
    role = "admin" if u.id in config.ADMIN_IDS else (row["role"] if row else None)
    dept = row["department"] if row else None
    notify = bool(row["notify"]) if row else True
    return Ctx(u.id, full_name(u), role, dept, notify), is_new


def is_boss(ctx):
    return ctx.role in ("admin", "resp")


def can_view(ctx, o):
    if is_boss(ctx):
        return True
    return ctx.role == "master" and ctx.dept is not None and ctx.dept == o["department"]


def can_restore(ctx, o):
    if is_boss(ctx):
        return True
    if not can_view(ctx, o):
        return False
    return (not config.MASTER_RESTORE_ONLY_OWN) or o["creator_id"] == ctx.id


def reply_menu(role):
    """Маленькое нижнее меню."""
    if role is None:
        rows = [["🏠 Меню", "👤 Мой доступ"]]
    else:
        rows = [["🏠 Меню", "📋 Мои наряды"], ["👤 Мой доступ", "⚙️ Настройки"]]
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t) for t in r] for r in rows],
                               resize_keyboard=True)


def btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def menu_kb(role):
    """Главное меню кнопками в чате — зависит от роли."""
    if role is None:
        rows = [[btn("👤 Мой доступ", "m:access"), btn("❓ Помощь", "m:help")]]
    elif role == "master":
        rows = [
            [btn("➕ Создать наряд", "m:create")],
            [btn("📋 Мои наряды", "m:mine"), btn("📋 Наряды отдела", "m:active")],
            [btn("🗄 Архив отдела", "m:archive"), btn("⚙️ Настройки", "m:settings")],
            [btn("👤 Мой доступ", "m:access"), btn("❓ Помощь", "m:help")],
        ]
    else:
        rows = [
            [btn("➕ Создать наряд", "m:create")],
            [btn("📋 Активные наряды", "m:active"), btn("📋 Мои наряды", "m:mine")],
            [btn("🗄 Архив", "m:archive"), btn("👁 Просмотр всех", "m:viewall")],
            [btn("📊 Статистика", "m:stats"), btn("🌙 Отчёт за смену", "m:shift")],
            [btn("👥 Пользователи", "m:users"), btn("⚙️ Настройки", "m:settings")],
            [btn("👤 Мой доступ", "m:access"), btn("❓ Помощь", "m:help")],
        ]
    return kb(rows)


async def safe_edit(msg, text, markup=None):
    try:
        await msg.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        pass


async def notify(bot, text):
    if config.GROUP_CHAT_ID is None:
        return False
    try:
        await bot.send_message(config.GROUP_CHAT_ID, text)
        return True
    except Exception as e:
        logging.warning("Не удалось отправить в группу: %s", e)
        return False


async def notify_order(bot, o, text):
    """Сообщение в группу — только если наряд уже был отправлен в чат мастеров."""
    if o and o.get("sent_to_group"):
        await notify(bot, text)


def urgency(o):
    """Значок срочности — только для активных нарядов."""
    if o.get("status") != "active":
        return None
    try:
        d = date.fromisoformat(o["planned_date"])
    except Exception:
        return None
    t = config.today()
    if isinstance(t, datetime):
        t = t.date()
    if d < t:
        return "🔴", f"Просрочен на {(t - d).days} дн."
    if d == t:
        return "🟡", "Срок сегодня"
    return "🟢", "По плану"


def status_text(o):
    u = urgency(o)
    if u:
        return f"{u[0]} {u[1]}"
    emoji, label = STATUS.get(o["status"], ("", o["status"]))
    return f"{emoji} {label}"


def card(o):
    emoji, label = STATUS.get(o["status"], ("", o["status"]))
    lines = [
        f"<b>Наряд №{o['id']}</b>",
        f"Статус: {status_text(o)}",
        f"Подразделение: {esc(o['department'])}",
        f"Работа: {esc(o['title'])}",
        f"Дата: {fmt_date(o['planned_date'])}",
        f"Смена: {esc(o['shift'])}",
        f"Создал: {esc(o.get('creator_name') or '—')}",
    ]
    if o.get("details"):
        lines.append(f"Описание: {esc(o['details'])}")
    if o.get("tmc"):
        lines.append(f"ТМЦ: {esc(o['tmc'])}")
    if o["status"] == "cancelled" and o.get("cancel_reason"):
        lines.append(f"Причина отмены: {esc(o['cancel_reason'])}")
    if o["status"] == "postponed" and o.get("postpone_reason"):
        lines.append(f"Причина отложения: {esc(o['postpone_reason'])}")
    if o["status"] == "done" and o.get("done_note"):
        lines.append(f"Примечание по выполнению: {esc(o['done_note'])}")
    if o["status"] in WORKING and "sent_to_group" in o:
        lines.append("📣 Отправлен в чат мастеров" if o["sent_to_group"] else "📭 В чат мастеров не отправлен")
    return "\n".join(lines)


def notif_body(o):
    emoji, label = STATUS.get(o["status"], ("", o["status"]))
    lines = [
        f"{emoji} <b>Наряд №{o['id']}</b>",
        f"Статус: {label}",
        f"Подразделение: {esc(o['department'])}",
        f"Работа: {esc(o['title'])}",
        f"Дата: {fmt_date(o['planned_date'])} | Смена: {esc(o['shift'])}",
    ]
    if o.get("details"):
        lines.append(f"Описание: {esc(o['details'])}")
    if o.get("tmc"):
        lines.append(f"ТМЦ: {esc(o['tmc'])}")
    if o["status"] == "postponed" and o.get("postpone_reason"):
        lines.append(f"Причина отложения: {esc(o['postpone_reason'])}")
    if o["status"] == "done" and o.get("done_note"):
        lines.append(f"Примечание по выполнению: {esc(o['done_note'])}")
    return "\n".join(lines)


def order_kb(o, ctx):
    oid, st = o["id"], o["status"]
    if st in WORKING:
        send_text = "📣 Отправить ещё раз" if o.get("sent_to_group") else "📣 В чат мастерам"
        rows = [
            [btn(send_text, f"o:send:{oid}"), btn("✏️ Изменить", f"o:edit:{oid}")],
            [btn("📅 Перенести", f"o:move:{oid}"),
             btn("⏸ Отложить", f"o:post:{oid}") if st == "active" else btn("🟢 В активные", f"o:act:{oid}")],
            [btn("✅ Завершить", f"o:done:{oid}"), btn("🚫 Отменить", f"o:cancel:{oid}")],
            [btn("🗄 В архив", f"o:arch:{oid}"), btn("📜 История", f"o:hist:{oid}")],
        ]
    else:
        row = []
        if can_restore(ctx, o):
            row.append(btn("↩️ Вернуть в работу", f"o:back:{oid}"))
        if is_boss(ctx):
            row.append(btn("🗑 Удалить", f"o:del:{oid}"))
        rows = [row] if row else []
        if st == "done":
            rows.append([btn("📣 Отчитаться в чат", f"o:report:{oid}")])
        rows.append([btn("📜 История", f"o:hist:{oid}")])
    return kb(rows)


def day_summary(ctx):
    """Короткая сводка над главным меню."""
    if ctx.role is None:
        return ""
    dept = ctx.dept if ctx.role == "master" else None
    if ctx.role == "master" and not dept:
        return ""
    t = config.today()
    if isinstance(t, datetime):
        t = t.date()
    iso = t.isoformat()
    try:
        work = db.list_orders(WORKING, dept, 500, False, creator_id=None)
        closed = db.query_orders(iso, iso, dept, None, None, "done")
    except Exception as e:
        logging.warning("Не удалось собрать сводку дня: %s", e)
        return ""
    overdue = sum(1 for o in work if o["status"] == "active" and o["planned_date"] < iso)
    today_n = sum(1 for o in work if o["status"] == "active" and o["planned_date"] == iso)
    post = sum(1 for o in work if o["status"] == "postponed")
    return (f"📊 <b>Сегодня, {t.strftime('%d.%m')}</b>\n"
            f"🔴 Просрочено: {overdue} · 🟡 На сегодня: {today_n}\n"
            f"⏸ Отложено: {post} · ✅ Выполнено: {len(closed)}")


def menu_text(ctx):
    s = day_summary(ctx)
    return (s + "\n\n" if s else "") + "🏠 <b>Главное меню</b>\nВыберите раздел:"


# ====================== старт и нижнее меню ======================
@misc.message(Command("chatid"))
async def cmd_chatid(m: Message):
    await m.answer(f"ID этого чата: <code>{m.chat.id}</code>")


@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    ctx, is_new = ctx_of(m.from_user)
    if is_new:
        await m.answer(HELP_TEXT)
    if ctx.role is None:
        await m.answer(NO_ACCESS, reply_markup=reply_menu(None))
    else:
        await m.answer("Нижнее меню обновлено 👇", reply_markup=reply_menu(ctx.role))
    await m.answer(menu_text(ctx), reply_markup=menu_kb(ctx.role))


# Нижние кнопки (и старые названия, чтобы старая клавиатура тоже работала)
MENU_TEXTS = {"🏠 Меню", "📋 Мои наряды", "👤 Мой доступ", "⚙️ Настройки", "➕ Создать наряд",
              "📋 Активные наряды", "🗄 Архив", "👥 Пользователи", "❓ Помощь"}


@router.message(F.text.in_(MENU_TEXTS))
async def reply_router(m: Message, state: FSMContext):
    await TEXT_ACTIONS[m.text](m, m.from_user, state)


@router.callback_query(F.data.startswith("m:"))
async def menu_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    fn = MENU_CB.get(cb.data[2:])
    if fn:
        await fn(cb.message, cb.from_user, state)


async def act_menu(msg, user, state):
    await state.clear()
    ctx, _ = ctx_of(user)
    await msg.answer(menu_text(ctx), reply_markup=menu_kb(ctx.role))


async def act_help(msg, user, state):
    await state.clear()
    await msg.answer(HELP_TEXT)


async def act_access(msg, user, state):
    await state.clear()
    ctx, _ = ctx_of(user)
    lines = ["<b>Ваш доступ:</b>", f"Ваше имя: {esc(ctx.name)}",
             f"Ваша роль: {ROLE_NAMES.get(ctx.role, 'Ожидает назначения доступа')}"]
    if ctx.dept and ctx.role == "master":
        lines.append(f"Подразделение: {esc(ctx.dept)}")
    lines.append(f"Telegram ID: <code>{ctx.id}</code>")
    await msg.answer("\n".join(lines))


# ====================== создание наряда ======================
class Create(StatesGroup):
    dept = State()
    title = State()
    details = State()
    tmc = State()
    date = State()
    shift = State()
    confirm = State()


STEP_ORDER = ["dept", "title", "details", "tmc", "date", "shift", "confirm"]


def first_step(ctx):
    return "title" if ctx.role == "master" else "dept"


def preview(data, ctx):
    lines = [
        "<b>Проверьте наряд</b>",
        "Номер: присваивается после сохранения",
        f"Подразделение: {esc(data.get('department'))}",
        f"Работа: {esc(data.get('title'))}",
    ]
    if data.get("details"):
        lines.append(f"Описание: {esc(data['details'])}")
    if data.get("tmc"):
        lines.append(f"ТМЦ: {esc(data['tmc'])}")
    lines += [f"Плановая дата: {fmt_date(data.get('planned_date'))}",
              f"Смена: {esc(data.get('shift'))}", f"Создал: {esc(ctx.name)}"]
    return "\n".join(lines)


async def show_step(target: Message, state: FSMContext, step: str, ctx: Ctx, edit=False):
    data = await state.get_data()
    await state.set_state(getattr(Create, step))
    nav = []
    if step != first_step(ctx):
        nav.append(btn("⬅️ Назад", "cr:back"))
    nav.append(btn("❌ Отмена", "cr:cancel"))

    if step == "dept":
        text = "Выберите подразделение:"
        rows = [[btn(d, f"cr:dept:{i}")] for i, d in enumerate(config.DEPARTMENTS)]
    elif step == "title":
        text = "Напишите, какую работу нужно выполнить (обязательно):"
        rows = []
    elif step == "details":
        text = "Описание / примечание. Если не нужно — отправьте «-»"
        rows = []
    elif step == "tmc":
        text = "ТМЦ / материалы. Если не нужны — отправьте «-»"
        rows = []
    elif step == "date":
        text = "Плановая дата в формате ДД.ММ.ГГГГ, например 06.11.2026:"
        rows = []
    elif step == "shift":
        text = "Выберите смену:"
        rows = [[btn(s, f"cr:shift:{i}")] for i, s in enumerate(config.SHIFTS)]
    else:
        text = preview(data, ctx)
        rows = [[btn("✅ Сохранить", "cr:save"), btn("❌ Отменить", "cr:cancel")]]
        nav = [btn("⬅️ Назад", "cr:back")]
    rows.append(nav)
    if edit:
        await safe_edit(target, text, kb(rows))
    else:
        await target.answer(text, reply_markup=kb(rows))


async def act_create(msg, user, state):
    await state.clear()
    ctx, _ = ctx_of(user)
    if ctx.role is None:
        return await msg.answer(NO_ACCESS)
    if ctx.role == "master" and not ctx.dept:
        return await msg.answer("Вам ещё не назначено подразделение. Обратитесь к администратору.")
    data = {"department": ctx.dept} if ctx.role == "master" else {}
    await state.set_data(data)
    await show_step(msg, state, first_step(ctx), ctx)


@router.callback_query(F.data == "cr:cancel", StateFilter(Create))
async def cr_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    await safe_edit(cb.message, "Создание наряда отменено.")


@router.callback_query(F.data == "cr:back", StateFilter(Create))
async def cr_back(cb: CallbackQuery, state: FSMContext):
    ctx, _ = ctx_of(cb.from_user)
    cur = (await state.get_state()).split(":")[1]
    idx = STEP_ORDER.index(cur) - 1
    first = STEP_ORDER.index(first_step(ctx))
    await cb.answer()
    if idx < first:
        return
    await show_step(cb.message, state, STEP_ORDER[idx], ctx, edit=True)


@router.callback_query(F.data.startswith("cr:dept:"), Create.dept)
async def cr_dept(cb: CallbackQuery, state: FSMContext):
    ctx, _ = ctx_of(cb.from_user)
    if not is_boss(ctx):
        return await cb.answer("Недоступно", show_alert=True)
    await state.update_data(department=config.DEPARTMENTS[int(cb.data.split(":")[2])])
    await cb.answer()
    await show_step(cb.message, state, "title", ctx, edit=True)


@router.message(Create.title, F.text)
async def cr_title(m: Message, state: FSMContext):
    ctx, _ = ctx_of(m.from_user)
    t = m.text.strip()
    if len(t) < 3:
        return await m.answer("Слишком коротко. Опишите работу подробнее:")
    await state.update_data(title=t)
    await show_step(m, state, "details", ctx)


@router.message(Create.details, F.text)
async def cr_details(m: Message, state: FSMContext):
    ctx, _ = ctx_of(m.from_user)
    t = m.text.strip()
    await state.update_data(details="" if t == "-" else t)
    await show_step(m, state, "tmc", ctx)


@router.message(Create.tmc, F.text)
async def cr_tmc(m: Message, state: FSMContext):
    ctx, _ = ctx_of(m.from_user)
    t = m.text.strip()
    await state.update_data(tmc="" if t == "-" else t)
    await show_step(m, state, "date", ctx)


@router.message(Create.date, F.text)
async def cr_date(m: Message, state: FSMContext):
    ctx, _ = ctx_of(m.from_user)
    iso = parse_date(m.text)
    if not iso:
        return await m.answer("Неверная дата. Нужен формат ДД.ММ.ГГГГ, например 06.11.2026. Попробуйте ещё раз:")
    await state.update_data(planned_date=iso)
    await show_step(m, state, "shift", ctx)


@router.callback_query(F.data.startswith("cr:shift:"), Create.shift)
async def cr_shift(cb: CallbackQuery, state: FSMContext):
    ctx, _ = ctx_of(cb.from_user)
    await state.update_data(shift=config.SHIFTS[int(cb.data.split(":")[2])])
    await cb.answer()
    await show_step(cb.message, state, "confirm", ctx, edit=True)


@router.callback_query(F.data == "cr:save", Create.confirm)
async def cr_save(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()  # защита от двойного нажатия
    ctx, _ = ctx_of(cb.from_user)
    ok = is_boss(ctx) or (ctx.role == "master" and ctx.dept == data.get("department"))
    if not ok or data.get("department") not in config.DEPARTMENTS:
        return await cb.answer("Нет прав", show_alert=True)
    oid = db.create_order(data["department"], data["title"], data.get("details", ""), data.get("tmc", ""),
                          data["planned_date"], data["shift"], ctx.id, ctx.name)
    db.add_history(oid, "Создан", None, ctx.id, ctx.name)
    o = db.get_order(oid)
    await cb.answer("Сохранено")
    await safe_edit(cb.message,
                    "✅ Наряд сохранён. В рабочий чат он пока не отправлен — нажмите «📣 В чат мастерам», "
                    "когда будете готовы.\n\n" + card(o), order_kb(o, ctx))


# ====================== списки ======================
async def send_list(msg, user, statuses, newest_first, empty, mine=False, archive=False):
    ctx, _ = ctx_of(user)
    if ctx.role is None:
        return await msg.answer(NO_ACCESS)
    dept, creator = None, None
    if mine:
        creator = ctx.id
    elif ctx.role == "master":
        if not ctx.dept:
            return await msg.answer("Вам ещё не назначено подразделение.")
        dept = ctx.dept
    orders = db.list_orders(statuses, dept, 30, newest_first, creator_id=creator)
    if not orders:
        return await msg.answer(empty)
    if archive:
        total = db.count_orders(statuses, dept, creator)
        await msg.answer(f"🗄 Архив: {total}", reply_markup=kb([[btn("☑️ Выбрать несколько", "x:start")]]))
    for o in orders:
        await msg.answer(card(o), reply_markup=order_kb(o, ctx))
    if len(orders) == 30:
        await msg.answer("Показаны первые 30 нарядов.")


async def act_active(msg, user, state):
    await state.clear()
    await send_list(msg, user, WORKING, False, "Активных нарядов нет.")


async def act_mine(msg, user, state):
    await state.clear()
    await send_list(msg, user, WORKING, False, "У вас нет активных нарядов, созданных вами.", mine=True)


async def act_archive(msg, user, state):
    await state.clear()
    await send_list(msg, user, ARCHIVED, True, "Архив пуст.", archive=True)


# ====================== действия с нарядом ======================
class Cancel(StatesGroup):
    reason = State()


class Done(StatesGroup):
    note = State()


class Postpone(StatesGroup):
    reason = State()


async def refresh(cb: CallbackQuery, oid, ctx, extra=""):
    o = db.get_order(oid)
    await safe_edit(cb.message, card(o) + extra, order_kb(o, ctx))
    return o


async def send_to_group(bot, o, ctx):
    """Отправка наряда в рабочий чат мастеров по кнопке."""
    text = (f"📣 <b>Наряд №{o['id']} запланирован</b>\n👤 Отправил: {esc(ctx.name)}\n"
            f"{SEP}\n{notif_body(o)}")
    return await notify(bot, text)


@router.callback_query(F.data.startswith("o:"))
async def order_action(cb: CallbackQuery, state: FSMContext):
    _, act, sid = cb.data.split(":")
    oid = int(sid)
    ctx, _n = ctx_of(cb.from_user)
    if act == "abort":
        await state.clear()
        await cb.answer()
        return await safe_edit(cb.message, "Действие отменено.")
    if act == "cont":  # старые кнопки прошлой версии
        act = "post"
    o = db.get_order(oid)
    if not o or o["archived_deleted"] or not can_view(ctx, o):
        return await cb.answer("Нет доступа или наряд удалён", show_alert=True)
    st = o["status"]

    if act == "open":
        await cb.answer()
        return await cb.message.answer(card(o), reply_markup=order_kb(o, ctx))

    if act == "hist":
        rows = db.get_history(oid)
        lines = [f"<b>История наряда №{oid}</b>"]
        for r in reversed(rows):
            t = datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M")
            note = f" — {esc(r['note'])}" if r["note"] else ""
            lines.append(f"{t} · {esc(r['action'])}{note} · {esc(r['actor_name'])}")
        await cb.answer()
        return await cb.message.answer("\n".join(lines))

    if act == "report":
        if st != "done":
            return await cb.answer("Отчитаться можно только по завершённому наряду", show_alert=True)
        if config.GROUP_CHAT_ID is None:
            return await cb.answer("Рабочий чат не подключён (GROUP_CHAT_ID в .env)", show_alert=True)
        if await notify(cb.bot, report_text(o, ctx)):
            db.mark_sent(oid)
            db.add_history(oid, "Отчёт о выполнении отправлен в чат", None, ctx.id, ctx.name)
            return await cb.answer("Отчёт отправлен в чат мастеров ✅", show_alert=True)
        return await cb.answer("Не удалось отправить. Бот добавлен в группу?", show_alert=True)

    if act in ("done", "arch", "post", "act", "cancel", "edit", "move", "send") and st not in WORKING:
        await cb.answer("Наряд уже не в работе", show_alert=True)
        return await refresh(cb, oid, ctx)

    if act == "send":
        if config.GROUP_CHAT_ID is None:
            return await cb.answer("Рабочий чат не подключён (GROUP_CHAT_ID в .env)", show_alert=True)
        if await send_to_group(cb.bot, o, ctx):
            db.mark_sent(oid)
            db.add_history(oid, "Отправлен в чат мастеров", None, ctx.id, ctx.name)
            await cb.answer("Отправлено в чат мастеров ✅")
            await refresh(cb, oid, ctx)
        else:
            await cb.answer("Не удалось отправить. Бот добавлен в группу?", show_alert=True)

    elif act == "done":
        await state.set_state(Done.note)
        await state.set_data({"order_id": oid})
        await cb.answer()
        await cb.message.answer(
            f"✅ Завершение наряда №{oid}.\nДобавьте примечание по выполненной работе. "
            "Если не требуется — отправьте «-»", reply_markup=kb([[btn("Не завершать", "o:abort:0")]]))

    elif act == "arch":
        db.set_status(oid, "archived")
        db.add_history(oid, "Перемещён в архив", None, ctx.id, ctx.name)
        await cb.answer("В архиве")
        o = await refresh(cb, oid, ctx)
        await notify_order(cb.bot, o, f"🗄 <b>Наряд №{oid} ПЕРЕМЕЩЁН В АРХИВ</b>\n👤 Ответственный: {esc(ctx.name)}\n{SEP}\n{notif_body(o)}")

    elif act == "post":
        await state.set_state(Postpone.reason)
        await state.set_data({"order_id": oid})
        await cb.answer()
        await cb.message.answer(
            f"⏸ Отложение наряда №{oid}.\nВведите причину отложения наряда (например: отсутствие ТМЦ):",
            reply_markup=kb([[btn("Не откладывать", "o:abort:0")]]))

    elif act == "act":
        db.set_status(oid, "active")
        db.add_history(oid, "Снова активный", None, ctx.id, ctx.name)
        await cb.answer("Готово")
        o = await refresh(cb, oid, ctx)
        await notify_order(cb.bot, o, f"🟢 <b>Наряд №{oid} СНОВА АКТИВЕН</b>\n👤 Ответственный: {esc(ctx.name)}\n{SEP}\n{notif_body(o)}")

    elif act == "cancel":
        await state.set_state(Cancel.reason)
        await state.update_data(order_id=oid)
        await cb.answer()
        await cb.message.answer(f"Напишите причину отмены наряда №{oid}:",
                                reply_markup=kb([[btn("Не отменять", "o:abort:0")]]))

    elif act == "edit":
        await cb.answer()
        await start_edit(cb.message, state, o)

    elif act == "move":
        await state.set_state(Move.date)
        await state.set_data({"order_id": oid})
        await cb.answer()
        await cb.message.answer(
            f"📅 Перенос наряда №{oid}.\nТекущая дата: {fmt_date(o['planned_date'])}, {esc(o['shift'])}.\n\n"
            "Напишите новую дату в формате ДД.ММ.ГГГГ:", reply_markup=kb([[btn("❌ Отмена", "mv:cancel")]]))

    elif act == "back":
        if st not in ARCHIVED:
            await cb.answer("Наряд уже в работе", show_alert=True)
            return await refresh(cb, oid, ctx)
        if not can_restore(ctx, o):
            return await cb.answer("Вернуть в работу может только автор наряда, ответственный или администратор",
                                   show_alert=True)
        db.set_status(oid, "active")
        db.add_history(oid, "Возвращён в работу", None, ctx.id, ctx.name)
        await cb.answer("Возвращён в работу")
        o = await refresh(cb, oid, ctx)
        await notify_order(cb.bot, o, f"↩️ <b>Наряд №{oid} ВОЗВРАЩЁН В РАБОТУ</b>\n👤 Ответственный: {esc(ctx.name)}\n{SEP}\n{notif_body(o)}")

    elif act == "del":
        if not is_boss(ctx) or st not in ARCHIVED:
            return await cb.answer("Нет прав", show_alert=True)
        await cb.answer()
        await cb.message.answer(f"Удалить наряд №{oid} из архива? История сохранится.",
                                reply_markup=kb([[btn("🗑 Да, удалить", f"o:delyes:{oid}"), btn("Нет", "o:abort:0")]]))

    elif act == "delyes":
        if not is_boss(ctx) or st not in ARCHIVED:
            return await cb.answer("Нет прав", show_alert=True)
        db.soft_delete(oid)
        db.add_history(oid, "Удалён из архива", None, ctx.id, ctx.name)
        await cb.answer("Удалено")
        await safe_edit(cb.message, f"🗑 Наряд №{oid} удалён из архива.")

    else:
        await cb.answer()


@router.message(Cancel.reason, F.text)
async def cancel_reason(m: Message, state: FSMContext):
    data = await state.get_data()
    reason = m.text.strip()
    if len(reason) < 3 or reason == "-":
        return await m.answer("Причина обязательна. Напишите, почему наряд отменяется:")
    await state.clear()
    ctx, _ = ctx_of(m.from_user)
    oid = data["order_id"]
    o = db.get_order(oid)
    if not o or o["status"] not in WORKING or not can_view(ctx, o):
        return await m.answer("Наряд недоступен или уже не в работе.")
    db.set_status(oid, "cancelled", reason)
    db.add_history(oid, "Отменён", reason, ctx.id, ctx.name)
    o = db.get_order(oid)
    await m.answer("🚫 Наряд отменён и перемещён в архив.\n\n" + card(o), reply_markup=order_kb(o, ctx))
    await notify_order(m.bot, o, f"🚫 <b>Наряд №{oid} ОТМЕНЁН</b>\n👤 Ответственный: {esc(ctx.name)}\n"
                                 f"📝 Причина: {esc(reason)}\n{SEP}\n{notif_body(o)}")


def report_text(o, ctx):
    return f"✅ <b>Наряд №{o['id']} ВЫПОЛНЕН</b>\n👤 Отчитался: {esc(ctx.name)}\n{SEP}\n{notif_body(o)}"


@router.message(Done.note, F.text)
async def done_note(m: Message, state: FSMContext):
    data = await state.get_data()
    note = m.text.strip()
    await state.clear()
    ctx, _ = ctx_of(m.from_user)
    oid = data["order_id"]
    o = db.get_order(oid)
    if not o or o["status"] not in WORKING or not can_view(ctx, o):
        return await m.answer("Наряд недоступен или уже не в работе.")
    note = "" if note == "-" else note
    db.set_status(oid, "done")
    if note:
        db.set_text(oid, "done_note", note)
    db.add_history(oid, "Завершён", note or None, ctx.id, ctx.name)
    o = db.get_order(oid)
    await m.answer("✅ Наряд завершён и перемещён в архив.\nМожно отчитаться в чат мастеров кнопкой ниже.\n\n"
                   + card(o), reply_markup=order_kb(o, ctx))


@router.message(Postpone.reason, F.text)
async def postpone_reason(m: Message, state: FSMContext):
    data = await state.get_data()
    reason = m.text.strip()
    if len(reason) < 3 or reason == "-":
        return await m.answer("Причина обязательна. Напишите, почему наряд откладывается (например: отсутствие ТМЦ):")
    await state.clear()
    ctx, _ = ctx_of(m.from_user)
    oid = data["order_id"]
    o = db.get_order(oid)
    if not o or o["status"] != "active" or not can_view(ctx, o):
        return await m.answer("Наряд недоступен или уже отложен.")
    db.set_status(oid, "postponed")
    db.set_text(oid, "postpone_reason", reason)
    db.add_history(oid, "Отложен", reason, ctx.id, ctx.name)
    o = db.get_order(oid)
    await m.answer("⏸ Наряд отложен.\n\n" + card(o), reply_markup=order_kb(o, ctx))
    await notify_order(m.bot, o, f"⏸ <b>Наряд №{oid} ОТЛОЖЕН</b>\n👤 Ответственный: {esc(ctx.name)}\n"
                                 f"📝 Причина: {esc(reason)}\n{SEP}\n{notif_body(o)}")


# ====================== перенос на другой день ======================
class Move(StatesGroup):
    date = State()
    shift = State()


@router.callback_query(F.data == "mv:cancel", StateFilter(Move))
async def mv_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    await safe_edit(cb.message, "Перенос отменён.")


@router.message(Move.date, F.text)
async def mv_date(m: Message, state: FSMContext):
    iso = parse_date(m.text)
    if not iso:
        return await m.answer("Неверная дата. Нужен формат ДД.ММ.ГГГГ. Попробуйте ещё раз:")
    data = await state.get_data()
    o = db.get_order(data["order_id"])
    await state.update_data(new_date=iso)
    await state.set_state(Move.shift)
    rows = [[btn(f"Оставить: {o['shift']}", "mv:s:k")]]
    rows += [[btn(s, f"mv:s:{i}")] for i, s in enumerate(config.SHIFTS)]
    rows.append([btn("❌ Отмена", "mv:cancel")])
    await m.answer(f"Новая дата: {fmt_date(iso)}. Выберите смену:", reply_markup=kb(rows))


@router.callback_query(F.data.startswith("mv:s:"), Move.shift)
async def mv_shift(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    ctx, _ = ctx_of(cb.from_user)
    o = db.get_order(data["order_id"])
    if not o or o["status"] not in WORKING or not can_view(ctx, o):
        return await cb.answer("Наряд недоступен", show_alert=True)
    key = cb.data.split(":")[2]
    new_shift = o["shift"] if key == "k" else config.SHIFTS[int(key)]
    old = f"{fmt_date(o['planned_date'])} / {o['shift']}"
    new = f"{fmt_date(data['new_date'])} / {new_shift}"
    db.move_order(o["id"], data["new_date"], new_shift)
    db.add_history(o["id"], "Перенесён", f"{old} → {new}", ctx.id, ctx.name)
    o2 = db.get_order(o["id"])
    await cb.answer("Перенесено")
    await safe_edit(cb.message, "📅 Наряд перенесён.\n\n" + card(o2), order_kb(o2, ctx))
    await notify_order(cb.bot, o2, f"📅 <b>Наряд №{o['id']} ПЕРЕНЕСЁН</b>\n👤 Ответственный: {esc(ctx.name)}\n"
                                   f"📝 Было: {esc(old)}\n📝 Стало: {esc(new)}\n{SEP}\n{notif_body(o2)}")


# ====================== редактирование (через черновик) ======================
class Edit(StatesGroup):
    pick = State()
    value = State()
    confirm = State()


def show_val(field, v):
    if field == "planned_date":
        return fmt_date(v)
    return str(v) if v else "—"


async def start_edit(msg: Message, state: FSMContext, o):
    draft = {f: o[f] for f in FIELDS}
    await state.set_state(Edit.pick)
    await state.set_data({"order_id": o["id"], "draft": draft})
    await edit_pick_screen(msg, state, o)


async def edit_pick_screen(msg: Message, state: FSMContext, o=None, edit=False):
    data = await state.get_data()
    o = o or db.get_order(data["order_id"])
    view = {**o, **data["draft"]}
    rows = [[btn(label, f"e:f:{f}")] for f, label in FIELDS.items()]
    rows.append([btn("✅ Завершить редактирование", "e:done")])
    rows.append([btn("❌ Выйти без сохранения", "e:discard")])
    text = f"✏️ Редактирование наряда №{o['id']}\n\n{card(view)}\n\nЧто изменить?"
    await state.set_state(Edit.pick)
    if edit:
        await safe_edit(msg, text, kb(rows))
    else:
        await msg.answer(text, reply_markup=kb(rows))


@router.callback_query(F.data == "e:discard", StateFilter(Edit))
async def e_discard(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    await safe_edit(cb.message, "Редактирование отменено, изменения не сохранены.")


@router.callback_query(F.data.startswith("e:f:"), Edit.pick)
async def e_field(cb: CallbackQuery, state: FSMContext):
    field = cb.data.split(":")[2]
    await cb.answer()
    if field == "shift":
        rows = [[btn(s, f"e:s:{i}")] for i, s in enumerate(config.SHIFTS)]
        rows.append([btn("⬅️ Назад", "e:pick")])
        return await safe_edit(cb.message, "Выберите смену:", kb(rows))
    await state.set_state(Edit.value)
    await state.update_data(field=field)
    hints = {
        "title": "Напишите новое наименование работы:",
        "details": "Напишите новое описание (или «-», чтобы убрать):",
        "tmc": "Напишите новый список ТМЦ (или «-», чтобы убрать):",
        "planned_date": "Напишите новую дату в формате ДД.ММ.ГГГГ:",
    }
    await safe_edit(cb.message, hints[field], kb([[btn("⬅️ Назад", "e:pick")]]))


@router.callback_query(F.data == "e:pick", StateFilter(Edit))
async def e_back_pick(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await edit_pick_screen(cb.message, state, edit=True)


@router.callback_query(F.data.startswith("e:s:"), Edit.pick)
async def e_shift(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    draft = data["draft"]
    draft["shift"] = config.SHIFTS[int(cb.data.split(":")[2])]
    await state.update_data(draft=draft)
    await cb.answer()
    await edit_pick_screen(cb.message, state, edit=True)


@router.message(Edit.value, F.text)
async def e_value(m: Message, state: FSMContext):
    data = await state.get_data()
    field, draft = data["field"], data["draft"]
    t = m.text.strip()
    if field == "title":
        if len(t) < 3:
            return await m.answer("Слишком коротко. Попробуйте ещё раз:")
        draft[field] = t
    elif field == "planned_date":
        iso = parse_date(t)
        if not iso:
            return await m.answer("Неверная дата. Нужен формат ДД.ММ.ГГГГ. Попробуйте ещё раз:")
        draft[field] = iso
    else:
        draft[field] = "" if t == "-" else t
    await state.update_data(draft=draft)
    await edit_pick_screen(m, state)


def diff_lines(o, draft):
    out = []
    for f, label in FIELDS.items():
        old, new = o.get(f) or "", draft.get(f) or ""
        if old != new:
            out.append((f, label, show_val(f, old), show_val(f, new)))
    return out


@router.callback_query(F.data == "e:done", Edit.pick)
async def e_done(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    o = db.get_order(data["order_id"])
    ch = diff_lines(o, data["draft"])
    await cb.answer()
    if not ch:
        return await safe_edit(cb.message, "Вы ничего не изменили.\n\nЧто изменить?",
                               kb([[btn(label, f"e:f:{f}")] for f, label in FIELDS.items()] +
                                  [[btn("❌ Выйти без сохранения", "e:discard")]]))
    await state.set_state(Edit.confirm)
    txt = "<b>Проверьте изменения</b>\n" + "\n".join(
        f"• {esc(label)}: {esc(a)} → {esc(b)}" for _, label, a, b in ch)
    txt += "\n\n" + card({**o, **data["draft"]})
    await safe_edit(cb.message, txt, kb([[btn("✅ Сохранить", "e:save"), btn("❌ Отменить изменения", "e:discard")]]))


@router.callback_query(F.data == "e:save", Edit.confirm)
async def e_save(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()  # защита от двойного нажатия
    ctx, _ = ctx_of(cb.from_user)
    o = db.get_order(data["order_id"])
    if not o or o["status"] not in WORKING or not can_view(ctx, o):
        return await cb.answer("Наряд недоступен", show_alert=True)
    ch = diff_lines(o, data["draft"])
    if not ch:
        await cb.answer()
        return await safe_edit(cb.message, "Изменений нет.")
    db.update_fields(o["id"], {f: data["draft"][f] for f, *_ in ch})
    for f, label, a, b in ch:
        db.add_history(o["id"], f"Изменено: {label}", f"{a} → {b}", ctx.id, ctx.name)
    o2 = db.get_order(o["id"])
    await cb.answer("Сохранено")
    await safe_edit(cb.message, "✅ Изменения сохранены.\n\n" + card(o2), order_kb(o2, ctx))
    changes = "\n".join(f"• {esc(label)}: {esc(a)} → {esc(b)}" for _, label, a, b in ch)
    await notify_order(cb.bot, o2, f"🔄 <b>Наряд №{o['id']} ИЗМЕНЁН</b>\n👤 Изменил: {esc(ctx.name)}\n"
                                   f"📝 Что изменено:\n{changes}\n{SEP}\n{notif_body(o2)}")


# ====================== просмотр всех (только чтение) ======================
PAGE = 12
KIND_LABEL = {"a": "🟢 Активные", "r": "🗄 Архивные", "d": "🗑 Удалённые"}


def view_menu(ctx):
    rows = [[btn(KIND_LABEL["a"], "v:a:x:0"), btn(KIND_LABEL["r"], "v:r:x:0")]]
    if ctx.role == "admin":
        rows.append([btn(KIND_LABEL["d"], "v:d:x:0")])
    return "👁 <b>Просмотр всех нарядов</b> (только чтение)\nВыберите раздел:", kb(rows)


async def act_viewall(msg, user, state):
    await state.clear()
    ctx, _ = ctx_of(user)
    if not is_boss(ctx):
        return await msg.answer("Недоступно.")
    text, markup = view_menu(ctx)
    await msg.answer(text, reply_markup=markup)


async def show_view(msg, kind, d, page):
    statuses = WORKING if kind == "a" else (ARCHIVED if kind == "r" else ALL_STATUSES)
    deleted = 1 if kind == "d" else 0
    dept = None if d == "x" else config.DEPARTMENTS[int(d)]
    total = db.count_orders(statuses, dept, None, deleted)
    pages = max(1, (total + PAGE - 1) // PAGE)
    page = min(max(page, 0), pages - 1)
    orders = db.list_orders(statuses, dept, PAGE, kind != "a", None, deleted, page * PAGE)
    title = f"{KIND_LABEL[kind]} · {dept or 'все подразделения'} · всего {total}"
    lines = [f"<b>{title}</b>"]
    for o in orders:
        emoji = STATUS[o["status"]][0]
        lines.append(f"{emoji} №{o['id']} · {esc(o['department'])} · {esc(short(o['title'], 32))} · "
                     f"{fmt_date(o['planned_date'])}")
    if not orders:
        lines.append("Ничего нет.")
    lines.append("\nНажмите на номер, чтобы открыть карточку.")
    rows, row = [], []
    for o in orders:
        row.append(btn(f"№{o['id']}", f"v:card:{o['id']}"))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(btn("◀️", f"v:{kind}:{d}:{page - 1}"))
        nav.append(btn(f"{page + 1}/{pages}", f"v:{kind}:{d}:{page}"))
        if page < pages - 1:
            nav.append(btn("▶️", f"v:{kind}:{d}:{page + 1}"))
        rows.append(nav)
    drow = [btn(("✓ " if d == "x" else "") + "Все", f"v:{kind}:x:0")]
    for i, name in enumerate(config.DEPARTMENTS):
        drow.append(btn(("✓ " if d == str(i) else "") + short(name, 12), f"v:{kind}:{i}:0"))
    rows.append(drow[:3])
    rows.append(drow[3:])
    rows.append([btn("⬅️ Разделы", "v:menu:x:0")])
    await safe_edit(msg, "\n".join(lines), kb(rows))


@router.callback_query(F.data.startswith("v:"))
async def view_cb(cb: CallbackQuery):
    ctx, _ = ctx_of(cb.from_user)
    if not is_boss(ctx):
        return await cb.answer("Недоступно", show_alert=True)
    p = cb.data.split(":")
    if p[1] == "menu":
        await cb.answer()
        text, markup = view_menu(ctx)
        return await safe_edit(cb.message, text, markup)
    if p[1] == "card":
        o = db.get_order(int(p[2]))
        if not o:
            return await cb.answer("Не найден", show_alert=True)
        await cb.answer()
        extra = "\n🗑 Удалён из архива" if o["archived_deleted"] else ""
        rows = [[btn("📜 История", f"v:hist:{o['id']}")]]
        if o["archived_deleted"]:
            if ctx.role == "admin":
                rows.append([btn("♻️ Восстановить в архив", f"v:restore:{o['id']}")])
        else:
            rows.append([btn("⚙️ Действия", f"o:open:{o['id']}")])
        return await cb.message.answer(card(o) + extra, reply_markup=kb(rows))
    if p[1] == "hist":
        oid = int(p[2])
        lines = [f"<b>История наряда №{oid}</b>"]
        for r in reversed(db.get_history(oid)):
            t = datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M")
            note = f" — {esc(r['note'])}" if r["note"] else ""
            lines.append(f"{t} · {esc(r['action'])}{note} · {esc(r['actor_name'])}")
        await cb.answer()
        return await cb.message.answer("\n".join(lines))
    if p[1] == "restore":
        if ctx.role != "admin":
            return await cb.answer("Только администратор", show_alert=True)
        oid = int(p[2])
        db.undelete(oid)
        db.add_history(oid, "Восстановлен в архив", None, ctx.id, ctx.name)
        await cb.answer("Восстановлен")
        return await safe_edit(cb.message, f"♻️ Наряд №{oid} восстановлен в архив.")
    if p[1] in KIND_LABEL:
        if p[1] == "d" and ctx.role != "admin":
            return await cb.answer("Только администратор", show_alert=True)
        await cb.answer()
        await show_view(cb.message, p[1], p[2], int(p[3]))
    else:
        await cb.answer()


# ====================== выбор нескольких нарядов в архиве ======================
async def sel_screen(msg, state, user):
    ctx, _ = ctx_of(user)
    data = await state.get_data()
    sel = set(data.get("sel", []))
    dept = ctx.dept if ctx.role == "master" else None
    orders = db.list_orders(ARCHIVED, dept, 30, True)
    rows = [[btn(("☑️ " if o["id"] in sel else "☐ ") + f"№{o['id']} {short(o['title'], 26)}", f"x:t:{o['id']}")]
            for o in orders]
    act = [btn(f"↩️ Вернуть ({len(sel)})", "x:back")]
    if is_boss(ctx):
        act.append(btn(f"🗑 Удалить ({len(sel)})", "x:del"))
    rows.append(act)
    rows.append([btn("✖️ Закрыть", "x:close")])
    await safe_edit(msg, "☑️ <b>Выберите наряды из архива</b> (последние 30):", kb(rows))


@router.callback_query(F.data.startswith("x:"))
async def sel_cb(cb: CallbackQuery, state: FSMContext):
    ctx, _ = ctx_of(cb.from_user)
    if ctx.role is None:
        return await cb.answer("Нет доступа", show_alert=True)
    p = cb.data.split(":")
    act = p[1]
    data = await state.get_data()
    sel = list(data.get("sel", []))
    if act == "start":
        await state.update_data(sel=[])
        await cb.answer()
        return await sel_screen(await cb.message.answer("…"), state, cb.from_user)
    if act == "t":
        oid = int(p[2])
        sel = [i for i in sel if i != oid] if oid in sel else sel + [oid]
        await state.update_data(sel=sel)
        await cb.answer()
        return await sel_screen(cb.message, state, cb.from_user)
    if act == "close":
        await state.update_data(sel=[])
        await cb.answer()
        return await safe_edit(cb.message, "Выбор закрыт.")
    if not sel:
        return await cb.answer("Сначала отметьте наряды", show_alert=True)
    if act == "back":
        done, sent = [], []
        for oid in sel:
            o = db.get_order(oid)
            if o and not o["archived_deleted"] and o["status"] in ARCHIVED and can_restore(ctx, o):
                db.set_status(oid, "active")
                db.add_history(oid, "Возвращён в работу", "массовое действие", ctx.id, ctx.name)
                done.append(oid)
                if o["sent_to_group"]:
                    sent.append(oid)
        await state.update_data(sel=[])
        await cb.answer(f"Возвращено: {len(done)}")
        await safe_edit(cb.message, "↩️ Возвращены в работу: " + (", ".join(f"№{i}" for i in done) or "ничего"))
        if sent:
            await notify(cb.bot, f"↩️ <b>Возвращены в работу:</b> {', '.join(f'№{i}' for i in sent)}\n"
                                 f"👤 Ответственный: {esc(ctx.name)}")
    elif act == "del":
        if not is_boss(ctx):
            return await cb.answer("Нет прав", show_alert=True)
        await cb.answer()
        await safe_edit(cb.message, f"Удалить из архива выбранные наряды ({len(sel)})? История сохранится.",
                        kb([[btn("🗑 Да, удалить", "x:delyes"), btn("Нет", "x:close")]]))
    elif act == "delyes":
        if not is_boss(ctx):
            return await cb.answer("Нет прав", show_alert=True)
        n = 0
        for oid in sel:
            o = db.get_order(oid)
            if o and not o["archived_deleted"] and o["status"] in ARCHIVED:
                db.soft_delete(oid)
                db.add_history(oid, "Удалён из архива", "массовое действие", ctx.id, ctx.name)
                n += 1
        await state.update_data(sel=[])
        await cb.answer(f"Удалено: {n}")
        await safe_edit(cb.message, f"🗑 Удалено из архива: {n}")
    else:
        await cb.answer()


# ====================== пользователи ======================
async def act_users(msg, user, state):
    await state.clear()
    ctx, _ = ctx_of(user)
    if not is_boss(ctx):
        return await msg.answer("Недоступно.")
    lines, rows = ["<b>Пользователи:</b>"], []
    for u in db.list_users():
        role = "admin" if u["telegram_id"] in config.ADMIN_IDS else u["role"]
        dept = f", {esc(u['department'])}" if u["department"] and role == "master" else ""
        uname = f" @{esc(u['username'])}" if u["username"] else ""
        lines.append(f"• {esc(u['full_name'])}{uname} · <code>{u['telegram_id']}</code> · "
                     f"{ROLE_NAMES.get(role, 'нет доступа')}{dept}")
        rows.append([btn(u["full_name"] or str(u["telegram_id"]), f"u:{u['telegram_id']}")])
    if ctx.role == "admin":
        lines.append("\nНажмите на имя, чтобы назначить роль.")
    await msg.answer("\n".join(lines), reply_markup=kb(rows) if ctx.role == "admin" else None)


@router.callback_query(F.data.startswith("u:"))
async def user_action(cb: CallbackQuery):
    ctx, _ = ctx_of(cb.from_user)
    if ctx.role != "admin":
        return await cb.answer("Только администратор", show_alert=True)
    parts = cb.data.split(":")
    if len(parts) == 2:
        uid = int(parts[1])
        u = db.get_user(uid)
        if not u:
            return await cb.answer("Не найден", show_alert=True)
        await cb.answer()
        rows = [[btn("Администратор", f"u:r:{uid}:admin"), btn("Ответственный", f"u:r:{uid}:resp")],
                [btn("Мастер", f"u:r:{uid}:master"), btn("Убрать доступ", f"u:r:{uid}:none")]]
        return await cb.message.answer(f"Роль для: {esc(u['full_name'])}", reply_markup=kb(rows))
    kind, uid = parts[1], int(parts[2])
    if kind == "r":
        role = parts[3]
        if role == "master":
            await cb.answer()
            rows = [[btn(d, f"u:d:{uid}:{i}")] for i, d in enumerate(config.DEPARTMENTS)]
            return await safe_edit(cb.message, "Выберите подразделение мастера:", kb(rows))
        db.set_role(uid, None if role == "none" else role, None)
        await cb.answer("Готово")
        await safe_edit(cb.message, f"✅ Роль обновлена: {ROLE_NAMES.get(role, 'нет доступа')}")
    elif kind == "d":
        dept = config.DEPARTMENTS[int(parts[3])]
        db.set_role(uid, "master", dept)
        await cb.answer("Готово")
        await safe_edit(cb.message, f"✅ Назначен мастером: {esc(dept)}")


# ====================== статистика и Excel ======================
PERIODS = ["today", "yesterday", "week", "month", "all", "custom"]
PERIOD_LABEL = {"today": "Сегодня", "yesterday": "Вчера", "week": "7 дней", "month": "30 дней",
                "all": "Всё время", "custom": "Свой период"}
STATUS_CYCLE = [None, "active", "postponed", "done", "cancelled", "archived"]
DEFAULT_F = {"period": "week", "dept": None, "shift": None, "status": None, "master": None,
             "from": None, "to": None}


class StatsRange(StatesGroup):
    value = State()


def cycle(lst, cur):
    i = lst.index(cur) if cur in lst else -1
    return lst[(i + 1) % len(lst)]


def period_range(f):
    t = config.today()
    p = f["period"]
    if p == "today":
        return t.isoformat(), t.isoformat()
    if p == "yesterday":
        y = (t - timedelta(days=1)).isoformat()
        return y, y
    if p == "week":
        return (t - timedelta(days=6)).isoformat(), t.isoformat()
    if p == "month":
        return (t - timedelta(days=29)).isoformat(), t.isoformat()
    if p == "custom":
        return f.get("from"), f.get("to")
    return None, None


async def get_f(state):
    d = await state.get_data()
    return {**DEFAULT_F, **(d.get("stats") or {})}


def fetch_stats_orders(f):
    d1, d2 = period_range(f)
    return db.query_orders(d1, d2, f["dept"], f["shift"], f["master"], f["status"])


def stats_text(f, orders):
    d1, d2 = period_range(f)
    per = PERIOD_LABEL[f["period"]]
    if f["period"] in ("custom",) and d1 and d2:
        per = f"{fmt_date(d1)} – {fmt_date(d2)}"
    master = "Все"
    if f["master"] is not None:
        master = next((n for i, n in db.creators() if i == f["master"]), str(f["master"]))
    st = STATUS[f["status"]][1] if f["status"] else "Все"
    lines = ["📊 <b>Статистика</b> (по плановой дате)",
             f"Период: {per} · Подр.: {f['dept'] or 'Все'} · Смена: {f['shift'] or 'Все'}",
             f"Статус: {st} · Мастер: {esc(master)}", "",
             f"<b>Всего нарядов: {len(orders)}</b>"]
    by_status = Counter(o["status"] for o in orders)
    for s in ALL_STATUSES:
        if by_status.get(s):
            lines.append(f"{STATUS[s][0]} {STATUS[s][1]}: {by_status[s]}")
    for title, key in (("По подразделениям", "department"), ("По сменам", "shift"),
                       ("По мастерам (кто создал)", "creator_name")):
        c = Counter(o[key] or "—" for o in orders)
        if c:
            lines.append(f"\n<b>{title}:</b>")
            for name, n in c.most_common(10):
                lines.append(f"• {esc(name)}: {n}")
    if orders:
        ids = [o["id"] for o in orders]
        created = Counter(o["created_at"][:10] for o in orders if o.get("created_at"))
        done = db.history_days(ids, "Завершён")
        days = sorted(set(created) | set(done), reverse=True)[:10]
        if days:
            lines.append("\n<b>Динамика (создано / завершено):</b>")
            for dd in days:
                lines.append(f"• {fmt_date(dd)}: {created.get(dd, 0)} / {done.get(dd, 0)}")
    return "\n".join(lines)


def stats_kb(f):
    st = STATUS[f["status"]][1] if f["status"] else "Все"
    return kb([
        [btn(f"📆 {PERIOD_LABEL[f['period']]}", "s:period"), btn(f"🏭 {short(f['dept'] or 'Все', 14)}", "s:dept")],
        [btn(f"🕐 {short(f['shift'] or 'Все', 14)}", "s:shift"), btn(f"📌 {st}", "s:status")],
        [btn("👷 Мастер", "s:master"), btn("📅 Свой период", "s:custom")],
        [btn("📥 Excel", "s:xls"), btn("🔄 Сброс", "s:reset")],
        [btn("📑 Отчёт по подразделению", "rp:dept")],
        [btn("📑 Отчёт по мастеру", "rp:master")],
    ])


async def show_stats(msg, state, edit):
    f = await get_f(state)
    await state.update_data(stats=f)
    text, markup = stats_text(f, fetch_stats_orders(f)), stats_kb(f)
    if edit:
        await safe_edit(msg, text, markup)
    else:
        await msg.answer(text, reply_markup=markup)


async def act_stats(msg, user, state):
    ctx, _ = ctx_of(user)
    if not is_boss(ctx):
        return await msg.answer("Недоступно.")
    await state.set_state(None)
    await show_stats(msg, state, edit=False)


def filters_text(f):
    d1, d2 = period_range(f)
    per = PERIOD_LABEL[f["period"]]
    if d1 and d2 and f["period"] in ("custom",):
        per = f"{fmt_date(d1)} – {fmt_date(d2)}"
    master = "Все"
    if f["master"] is not None:
        master = next((n for i, n in db.creators() if i == f["master"]), str(f["master"]))
    st = STATUS[f["status"]][1] if f["status"] else "Все"
    return (f"период: {per}; подразделение: {f['dept'] or 'Все'}; смена: {f['shift'] or 'Все'}; "
            f"статус: {st}; мастер: {master}")


def fetch_stats_deleted(f):
    d1, d2 = period_range(f)
    return db.query_orders(d1, d2, f["dept"], f["shift"], f["master"], f["status"], deleted=True)


@router.callback_query(F.data.startswith("s:"))
async def stats_cb(cb: CallbackQuery, state: FSMContext):
    ctx, _ = ctx_of(cb.from_user)
    if not is_boss(ctx):
        return await cb.answer("Недоступно", show_alert=True)
    f = await get_f(state)
    act = cb.data.split(":")[1]
    if act == "period":
        f["period"] = cycle([p for p in PERIODS if p != "custom"], f["period"] if f["period"] != "custom" else "all")
    elif act == "dept":
        f["dept"] = cycle([None] + config.DEPARTMENTS, f["dept"])
    elif act == "shift":
        f["shift"] = cycle([None] + config.SHIFTS, f["shift"])
    elif act == "status":
        f["status"] = cycle(STATUS_CYCLE, f["status"])
    elif act == "master":
        f["master"] = cycle([None] + [i for i, _n in db.creators()], f["master"])
    elif act == "reset":
        f = dict(DEFAULT_F)
    elif act == "custom":
        await state.update_data(stats=f)
        await state.set_state(StatsRange.value)
        await cb.answer()
        return await cb.message.answer("Напишите период в формате ДД.ММ.ГГГГ-ДД.ММ.ГГГГ, "
                                       "например 01.11.2026-30.11.2026")
    elif act == "xls":
        orders = fetch_stats_orders(f)
        deleted = fetch_stats_deleted(f)
        if not orders and not deleted:
            return await cb.answer("По этим фильтрам нарядов нет", show_alert=True)
        await cb.answer("Формирую файл…")
        data = reports.build_export(orders, deleted, filters_text(f), ctx.name)
        name = f"naryadi_{config.now():%Y%m%d_%H%M}.xlsx"
        return await cb.message.answer_document(
            BufferedInputFile(data, filename=name),
            caption=f"Нарядов в файле: {len(orders)} · удалённых: {len(deleted)}")
    await state.update_data(stats=f)
    await cb.answer()
    await show_stats(cb.message, state, edit=True)


@router.message(StatsRange.value, F.text)
async def stats_range(m: Message, state: FSMContext):
    ctx, _ = ctx_of(m.from_user)
    if not is_boss(ctx):
        return
    parts = [x.strip() for x in m.text.replace("–", "-").replace("—", "-").split("-")]
    d1 = parse_date(parts[0]) if len(parts) == 2 else None
    d2 = parse_date(parts[1]) if len(parts) == 2 else None
    if not d1 or not d2 or d1 > d2:
        return await m.answer("Не понял период. Пример: 01.11.2026-30.11.2026")
    f = await get_f(state)
    f.update(period="custom", **{"from": d1, "to": d2})
    await state.update_data(stats=f)
    await state.set_state(None)
    await show_stats(m, state, edit=False)


# ====================== настройки ======================
async def act_settings(msg, user, state):
    await state.clear()
    ctx, _ = ctx_of(user)
    if ctx.role is None:
        return await msg.answer(NO_ACCESS)
    lines = ["⚙️ <b>Настройки</b>", f"🔔 Личные напоминания: {'включены' if ctx.notify else 'выключены'}"]
    rows = [[btn("🔕 Выключить напоминания" if ctx.notify else "🔔 Включить напоминания", "st:notify")]]
    if is_boss(ctx):
        lines += [f"📣 Рабочий чат: {'подключён' if config.GROUP_CHAT_ID else 'не подключён'}",
                  f"⏰ Время напоминаний: {config.REMINDER_TIME}",
                  "🏭 Подразделения: " + ", ".join(config.DEPARTMENTS),
                  "🕐 Смены: " + ", ".join(config.SHIFTS)]
    if ctx.role == "admin":
        bks = db.list_backups()
        last = os.path.basename(bks[-1]) if bks else "ещё нет"
        lines.append(f"💾 Автокопия: ежедневно в {config.BACKUP_HM[0]:02d}:{config.BACKUP_HM[1]:02d}, "
                     f"хранится {config.BACKUP_KEEP}. Последняя: {last}")
        rows.append([btn("💾 Создать копию сейчас", "st:mkbackup")])
        rows.append([btn("📥 Скачать копию базы", "st:backup")])
        rows.append([btn("⏰ Отправить напоминания сейчас", "st:remind")])
        rows.append([btn("☀️ Утренняя сводка сейчас", "st:morning")])
    await msg.answer("\n".join(lines), reply_markup=kb(rows))


@router.callback_query(F.data.startswith("st:"))
async def settings_cb(cb: CallbackQuery):
    ctx, _ = ctx_of(cb.from_user)
    if ctx.role is None:
        return await cb.answer("Нет доступа", show_alert=True)
    act = cb.data.split(":")[1]
    if act == "notify":
        db.set_notify(ctx.id, not ctx.notify)
        await cb.answer("Готово")
        await safe_edit(cb.message, "Настройка изменена. Откройте «⚙️ Настройки» ещё раз, чтобы увидеть.")
    elif act == "backup" and ctx.role == "admin":
        await cb.answer()
        name = f"naryadchik_{config.now():%Y%m%d_%H%M}.sqlite3"
        await cb.message.answer_document(BufferedInputFile(db.snapshot_bytes(), filename=name),
                                         caption="Копия базы данных")
    elif act == "remind" and ctx.role == "admin":
        n = await run_reminders(cb.bot)
        await cb.answer(f"Отправлено нарядов в напоминаниях: {n}", show_alert=True)
    elif act == "morning" and ctx.role == "admin":
        n = await run_morning(cb.bot)
        await cb.answer(f"Утренняя сводка отправлена: {n}", show_alert=True)
    elif act == "mkbackup" and ctx.role == "admin":
        try:
            path = db.backup(force=True)
            await cb.answer(f"Копия создана: {os.path.basename(path)}", show_alert=True)
        except Exception as e:
            await cb.answer(f"Не удалось: {e}", show_alert=True)
    else:
        await cb.answer("Нет прав", show_alert=True)


# ====================== отчёты в Excel (по подразделению / по мастеру) ======================
RP_PERIODS = [("today", "Сегодня"), ("yesterday", "Вчера"), ("week", "7 дней"), ("month", "30 дней"),
              ("thismonth", "Этот месяц"), ("prevmonth", "Прошлый месяц"), ("all", "Всё время"),
              ("custom", "Свой период")]


class RepRange(StatesGroup):
    value = State()


def rp_range(kind):
    t = config.today()
    if kind == "today":
        return t.isoformat(), t.isoformat()
    if kind == "yesterday":
        y = (t - timedelta(days=1)).isoformat()
        return y, y
    if kind == "week":
        return (t - timedelta(days=6)).isoformat(), t.isoformat()
    if kind == "month":
        return (t - timedelta(days=29)).isoformat(), t.isoformat()
    if kind == "thismonth":
        first = t.replace(day=1)
        last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        return first.isoformat(), last.isoformat()
    if kind == "prevmonth":
        last = t.replace(day=1) - timedelta(days=1)
        return last.replace(day=1).isoformat(), last.isoformat()
    return None, None


def rp_period_kb():
    rows, row = [], []
    for k, label in RP_PERIODS:
        row.append(btn(label, f"rp:p:{k}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([btn("❌ Отмена", "rp:cancel")])
    return kb(rows)


async def send_report(msg, state, ctx, d1, d2):
    data = await state.get_data()
    rpt = data.get("rpt")
    if not rpt:
        return await msg.answer("Отчёт устарел. Откройте «📊 Статистика» и начните заново.")
    kw = {"department": rpt["key"]} if rpt["kind"] == "dept" else {"creator_id": rpt["key"]}
    orders = db.query_orders(d1, d2, **kw)
    deleted = db.query_orders(d1, d2, deleted=True, **kw)
    if not orders and not deleted:
        return await msg.answer("За выбранный период нарядов нет.")
    xlsx = reports.build_report(rpt["kind"], rpt["label"], orders, d1, d2, ctx.name, deleted)
    safe = "".join(ch for ch in rpt["label"] if ch.isalnum() or ch in "-_ ").strip().replace(" ", "_") or "report"
    per = "all" if not d1 else f"{d1.replace('-', '')}-{d2.replace('-', '')}"
    await msg.answer_document(BufferedInputFile(xlsx, filename=f"otchet_{safe}_{per}.xlsx"),
                              caption=f"Отчёт: {rpt['label']} · нарядов: {len(orders)} · удалённых: {len(deleted)}")


@router.callback_query(F.data.startswith("rp:"))
async def report_cb(cb: CallbackQuery, state: FSMContext):
    ctx, _ = ctx_of(cb.from_user)
    if not is_boss(ctx):
        return await cb.answer("Недоступно", show_alert=True)
    p = cb.data.split(":")
    act = p[1]
    if act == "cancel":
        await state.set_state(None)
        await cb.answer()
        return await safe_edit(cb.message, "Отчёт отменён.")
    if act == "dept":
        await cb.answer()
        rows = [[btn(d, f"rp:d:{i}")] for i, d in enumerate(config.DEPARTMENTS)]
        rows.append([btn("❌ Отмена", "rp:cancel")])
        return await cb.message.answer("📑 Отчёт по подразделению. Выберите подразделение:", reply_markup=kb(rows))
    if act == "master":
        people = db.creators()[:40]
        if not people:
            return await cb.answer("Пока нет ни одного наряда", show_alert=True)
        await cb.answer()
        rows = [[btn(short(n or str(i), 40), f"rp:m:{i}")] for i, n in people]
        rows.append([btn("❌ Отмена", "rp:cancel")])
        return await cb.message.answer("📑 Отчёт по мастеру. Выберите мастера:", reply_markup=kb(rows))
    if act == "d":
        dept = config.DEPARTMENTS[int(p[2])]
        await state.update_data(rpt={"kind": "dept", "key": dept, "label": dept})
    elif act == "m":
        uid = int(p[2])
        name = next((n for i, n in db.creators() if i == uid), str(uid))
        await state.update_data(rpt={"kind": "master", "key": uid, "label": name or str(uid)})
    elif act == "p":
        data = await state.get_data()
        if not data.get("rpt"):
            return await cb.answer("Отчёт устарел. Начните заново из «📊 Статистика».", show_alert=True)
        if p[2] == "custom":
            await state.set_state(RepRange.value)
            await cb.answer()
            return await cb.message.answer("Напишите период в формате ДД.ММ.ГГГГ-ДД.ММ.ГГГГ, "
                                           "например 01.11.2026-30.11.2026")
        await cb.answer("Формирую отчёт…")
        d1, d2 = rp_range(p[2])
        return await send_report(cb.message, state, ctx, d1, d2)
    await cb.answer()
    await safe_edit(cb.message, "За какой период нужен отчёт?", rp_period_kb())


@router.message(RepRange.value, F.text)
async def report_range(m: Message, state: FSMContext):
    ctx, _ = ctx_of(m.from_user)
    if not is_boss(ctx):
        return
    parts = [x.strip() for x in m.text.replace("–", "-").replace("—", "-").split("-")]
    d1 = parse_date(parts[0]) if len(parts) == 2 else None
    d2 = parse_date(parts[1]) if len(parts) == 2 else None
    if not d1 or not d2 or d1 > d2:
        return await m.answer("Не понял период. Пример: 01.11.2026-30.11.2026")
    await state.set_state(None)
    await send_report(m, state, ctx, d1, d2)


# ====================== напоминания ======================
async def run_reminders(bot):
    """Напоминание о работах, плановая дата которых — сегодня. Один наряд — одно напоминание."""
    today = config.today().isoformat()
    cands = db.reminder_candidates(today)
    if not cands:
        return 0
    delivered = set()
    # 1) сводка в рабочий чат (только наряды, уже отправленные в чат)
    in_chat = [o for o in cands if o["sent_to_group"]]
    if in_chat and config.GROUP_CHAT_ID is not None:
        lines = ["⏰ <b>Напоминание: запланированные работы на сегодня</b>"]
        for o in in_chat:
            lines.append(f"\n📅 Наряд №{o['id']} ({esc(o['department'])})\nРабота: {esc(o['title'])}\n"
                         f"Смена: {esc(o['shift'])}")
        if await notify(bot, "\n".join(lines)):
            delivered.update(o["id"] for o in in_chat)
    # 2) личные сообщения мастерам подразделений
    by_dept = {}
    for o in cands:
        by_dept.setdefault(o["department"], []).append(o)
    for dept, orders in by_dept.items():
        for u in db.masters_of(dept):
            lines = ["⏰ <b>Напоминание: запланированные работы на сегодня</b>"]
            rows = []
            for o in orders:
                lines.append(f"\n📅 Наряд №{o['id']} ({esc(dept)})\nРабота: {esc(o['title'])}\nСмена: {esc(o['shift'])}"
                             f"\nСтатус: {STATUS[o['status']][0]} {STATUS[o['status']][1]}")
                row = [btn(f"🔍 Открыть №{o['id']}", f"o:open:{o['id']}")]
                if o["status"] == "archived":
                    row.append(btn("↩️ Вернуть в работу", f"o:back:{o['id']}"))
                rows.append(row)
            try:
                await bot.send_message(u["telegram_id"], "\n".join(lines), reply_markup=kb(rows))
                delivered.update(o["id"] for o in orders)
            except Exception as e:
                logging.warning("Напоминание мастеру %s не доставлено: %s", u["telegram_id"], e)
    db.mark_reminded(list(delivered))
    return len(delivered)


async def daily_backup(bot=None):
    """Ежедневная копия базы. При сбое — сообщение администраторам. По желанию копия уходит админам в Telegram."""
    try:
        path = db.backup(force=True)
    except Exception as e:
        logging.warning("Резервная копия не создана: %s", e)
        if bot:
            for aid in config.ADMIN_IDS:
                try:
                    await bot.send_message(aid, f"⚠️ Не удалось создать резервную копию базы: {esc(e)}")
                except Exception:
                    pass
        return
    if bot and config.BACKUP_SEND and path:
        with open(path, "rb") as f:
            data = f.read()
        for aid in config.ADMIN_IDS:
            try:
                await bot.send_document(aid, BufferedInputFile(data, filename=os.path.basename(path)),
                                        caption="💾 Ежедневная копия базы «Нарядчика»")
            except Exception as e:
                logging.warning("Копия не отправлена админу %s: %s", aid, e)


# ====================== отчёт за смену и утренняя сводка ======================
def user_link(uid, name):
    """Кликабельное имя — по нажатию открывается личный чат с человеком."""
    label = esc(name or uid or "—")
    if not uid:
        return label
    uname = None
    try:
        u = db.get_user(uid)
        uname = u["username"] if u else None
    except Exception:
        uname = None
    href = f"https://t.me/{uname}" if uname else f"tg://user?id={uid}"
    return f'<a href="{href}">{label}</a>'


def order_line(o):
    return (f"• №{o['id']} {esc(short(o['title'], 45))} · {esc(o['department'])}, {esc(o['shift'])} · "
            f"{user_link(o.get('creator_id'), o.get('creator_name'))}")


def group_block(title, orders, extra=None, limit=10):
    if not orders:
        return []
    lines = ["", f"<b>{title} ({len(orders)})</b>"]
    for o in orders[:limit]:
        lines.append(order_line(o))
        e = extra(o) if extra else None
        if e:
            lines.append("    " + e)
    if len(orders) > limit:
        lines.append(f"…и ещё {len(orders) - limit}")
    return lines


def fit_lines(lines, limit=3800):
    """Telegram не принимает сообщения длиннее ~4096 символов — лишнее убираем целыми строками."""
    cut = False
    while len("\n".join(lines)) > limit and len(lines) > 2:
        lines.pop()
        cut = True
    if cut:
        lines.append("…показано не всё — полный список в «📊 Статистика».")
    return "\n".join(lines)


def open_buttons(orders, limit=12):
    bs = [btn(f"🔍 №{o['id']}", f"o:open:{o['id']}") for o in orders[:limit]]
    return [bs[i:i + 4] for i in range(0, len(bs), 4)]


def note_extra(o):
    n = o.get("done_note")
    return f"📝 {esc(short(n, 80))}" if n and n != "-" else None


def post_extra(o):
    n = o.get("postpone_reason")
    return f"Причина: {esc(short(n, 80))}" if n else None


def cancel_extra(o):
    n = o.get("cancel_reason")
    return f"Причина: {esc(short(n, 80))}" if n else None


def shift_report_view(day):
    """Вечерний отчёт: что выполнено, что нет — за выбранный день (по плановой дате)."""
    iso = day.isoformat()
    orders = sorted(db.query_orders(iso, iso), key=lambda o: o["id"])
    by = {}
    for o in orders:
        by.setdefault(o["status"], []).append(o)
    done, active, post = by.get("done", []), by.get("active", []), by.get("postponed", [])
    canc, arch = by.get("cancelled", []), by.get("archived", [])
    moved = {}
    for o in db.moved_on(iso):  # перенесённые в этот день на другую дату (по журналу)
        if o["planned_date"] != iso:
            moved[o["id"]] = o
    moved = sorted(moved.values(), key=lambda o: o["id"])
    lines = [f"🌙 <b>Отчёт за день · {fmt_date(iso)}</b>"]
    if not orders and not moved:
        lines += ["", "На эту дату нарядов нет."]
    else:
        lines.append(f"Всего: {len(orders)} · ✅ {len(done)} · ❌ {len(active)} · ⏸ {len(post)} · 🔁 {len(moved)}")
        lines += group_block("✅ Выполнено", done, note_extra)
        lines += group_block("❌ Не выполнено", active)
        lines += group_block("⏸ Отложено", post, post_extra)
        lines += group_block("🔁 Перенесено", moved,
                             lambda o: f"→ {fmt_date(o['planned_date'])}, {esc(o['shift'])}")
        lines += group_block("🚫 Отменено", canc, cancel_extra)
        if arch:
            lines += ["", f"🗄 В архиве: {len(arch)}"]
        lines += ["", "💬 Нажмите на имя мастера, чтобы написать ему."]
    rows = open_buttons(active + post + moved)
    today = config.today()
    if isinstance(today, datetime):
        today = today.date()
    rows.append([btn("◀️ Вчера", "sr:y")] if day == today else [btn("Сегодня ▶️", "sr:t")])
    return fit_lines(lines), kb(rows)


async def act_shift_report(msg, user, state):
    await state.clear()
    ctx, _ = ctx_of(user)
    if not is_boss(ctx):
        return await msg.answer("Недоступно.")
    today = config.today()
    if isinstance(today, datetime):
        today = today.date()
    text, markup = shift_report_view(today)
    await msg.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("sr:"))
async def shift_report_cb(cb: CallbackQuery):
    ctx, _ = ctx_of(cb.from_user)
    if not is_boss(ctx):
        return await cb.answer("Недоступно", show_alert=True)
    today = config.today()
    if isinstance(today, datetime):
        today = today.date()
    day = today - timedelta(days=1) if cb.data == "sr:y" else today
    text, markup = shift_report_view(day)
    await cb.answer()
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        pass


def morning_view():
    today = config.today()
    if isinstance(today, datetime):
        today = today.date()
    iso = today.isoformat()
    work = db.list_orders(WORKING, None, 500, False, creator_id=None)
    overdue = sorted((o for o in work if o["status"] == "active" and o["planned_date"] < iso),
                     key=lambda o: o["planned_date"])
    today_o = [o for o in work if o["status"] == "active" and o["planned_date"] == iso]
    post = [o for o in work if o["status"] == "postponed"]
    lines = [f"☀️ <b>Сводка на {fmt_date(iso)}</b>"]
    if not (overdue or today_o or post):
        lines += ["", "Просроченных нарядов и работ на сегодня нет 👍"]
    lines += group_block("🔴 Просрочено", overdue, lambda o: f"Срок был {fmt_date(o['planned_date'])}")
    lines += group_block("🟡 На сегодня", today_o)
    lines += group_block("⏸ Отложено", post, post_extra)
    rows = open_buttons(overdue + today_o)
    return fit_lines(lines), (kb(rows) if rows else None)


def morning_recipients():
    rec = {}
    for u in db.list_users():
        if u["telegram_id"] in config.ADMIN_IDS or u["role"] == "resp":
            try:
                rec[u["telegram_id"]] = bool(u["notify"])
            except Exception:
                rec[u["telegram_id"]] = True
    for aid in config.ADMIN_IDS:
        rec.setdefault(aid, True)
    return [i for i, on in rec.items() if on]


async def run_morning(bot):
    """Утренняя сводка администраторам и ответственным (кто не отключил напоминания)."""
    text, markup = morning_view()
    sent = 0
    for uid in morning_recipients():
        try:
            await bot.send_message(uid, text, reply_markup=markup)
            sent += 1
        except Exception as e:
            logging.warning("Утренняя сводка %s не доставлена: %s", uid, e)
    return sent


# ====================== связка меню и запуск ======================
TEXT_ACTIONS = {
    "🏠 Меню": act_menu, "📋 Мои наряды": act_mine, "👤 Мой доступ": act_access, "⚙️ Настройки": act_settings,
    "➕ Создать наряд": act_create, "📋 Активные наряды": act_active, "🗄 Архив": act_archive,
    "👥 Пользователи": act_users, "❓ Помощь": act_help,
}
MENU_CB = {
    "create": act_create, "active": act_active, "mine": act_mine, "archive": act_archive, "viewall": act_viewall,
    "stats": act_stats, "shift": act_shift_report, "users": act_users, "settings": act_settings, "access": act_access, "help": act_help,
}


@router.callback_query(F.data.startswith(("cr:", "e:", "mv:")))
async def stale_buttons(cb: CallbackQuery):
    await cb.answer("Эта кнопка устарела. Начните заново из меню.", show_alert=True)


async def main():
    if not config.BOT_TOKEN:
        print("Не указан BOT_TOKEN. Откройте файл .env и вставьте токен.")
        return
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    db.init_db()
    db.backup()
    bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(misc)
    dp.include_router(router)

    @dp.errors()
    async def on_error(event):
        logging.exception("Ошибка при обработке обновления: %s", event.exception)
        cbq = getattr(event.update, "callback_query", None)
        if cbq:
            try:
                await cbq.answer("Что-то пошло не так. Попробуйте ещё раз.", show_alert=True)
            except Exception:
                pass
        return True

    h, mi = config.REMINDER_HM
    sched = AsyncIOScheduler(timezone=config.TZINFO) if config.TZINFO else AsyncIOScheduler()
    sched.add_job(run_reminders, CronTrigger(hour=h, minute=mi), args=[bot], id="reminders",
                  misfire_grace_time=3600, coalesce=True)
    bh, bm = config.BACKUP_HM
    sched.add_job(daily_backup, CronTrigger(hour=bh, minute=bm), args=[bot], id="backup",
                  misfire_grace_time=3600, coalesce=True)
    mh, mm = MORNING_HM
    sched.add_job(run_morning, CronTrigger(hour=mh, minute=mm), args=[bot], id="morning",
                  misfire_grace_time=3600, coalesce=True)
    sched.start()
    # Если бот был выключен в нужное время — проверяем сразу (дубли исключены флагом is_reminder_sent)
    if (config.now().hour, config.now().minute) >= (h, mi):
        asyncio.create_task(run_reminders(bot))

    print("Бот запущен. Остановить: Ctrl+C")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
