"""Отчёты в Excel: выгрузка по фильтрам, отчёт по подразделению и по мастеру."""
import io
from collections import Counter, defaultdict
from datetime import datetime

import config
import db

STATUS_LABEL = {"active": "Активный", "postponed": "Отложен", "done": "Завершён",
                "cancelled": "Отменён", "archived": "В архиве"}
ORDER = ["active", "postponed", "done", "cancelled", "archived"]


def _d(iso):
    try:
        return datetime.strptime(iso, "%Y-%m-%d").date()
    except Exception:
        return iso


def _dt(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return s or ""


def _fmt(d):
    return d.strftime("%d.%m.%Y") if hasattr(d, "strftime") else str(d)


def _fdt(s):
    v = _dt(s)
    return v.strftime("%d.%m.%Y %H:%M") if hasattr(v, "strftime") else ""


def _info(o, evs):
    """Достаёт из истории наряда всё, что нужно для таблицы."""
    def last(action):
        for e in reversed(evs):
            if e["action"] == action:
                return e
        return None

    def first(action):
        for e in evs:
            if e["action"] == action:
                return e
        return None

    moves = [e for e in evs if e["action"] == "Перенесён"]
    edits = [e for e in evs if e["action"].startswith("Изменено:")]
    returns = [e for e in evs if e["action"] == "Возвращён в работу"]
    changes = []
    for e in evs:
        if e["action"].startswith("Изменено:"):
            what = f"{e['action'][len('Изменено:'):].strip()}: {e['note'] or ''}"
        elif e["action"] == "Перенесён":
            what = f"Перенос: {e['note'] or ''}"
        else:
            continue
        changes.append(f"{_fdt(e['created_at'])} · {what} · {e['actor_name'] or ''}")
    return {
        "sent": first("Отправлен в чат мастеров"), "done": last("Завершён"), "post": last("Отложен"),
        "canc": last("Отменён"), "arch": last("Перемещён в архив"), "report": last("Отчёт о выполнении отправлен в чат"),
        "deleted": last("Удалён из архива"), "moves": moves, "edits": edits, "returns": returns, "changes": changes,
    }


def _who(e):
    return (e["actor_name"] or "") if e else ""


def _when(e):
    return _fdt(e["created_at"]) if e else ""


COLS = [
    ("№", 6), ("Подразделение", 16), ("Работа", 34), ("Описание", 34), ("ТМЦ", 24), ("Плановая дата", 14),
    ("Смена", 11), ("Статус", 14), ("Создал", 20), ("Создан", 17),
    ("Отправлен в чат мастеров", 20), ("Кто отправил", 18),
    ("Завершён", 17), ("Кто завершил", 18), ("Примечание по выполнению", 32),
    ("Отчёт о выполнении в чат", 20),
    ("Отложен", 17), ("Кто отложил", 18), ("Причина отложения", 28),
    ("Отменён", 17), ("Кто отменил", 18), ("Причина отмены", 28),
    ("Перемещён в архив", 17), ("Кто переместил", 18),
    ("Переносов", 10), ("Последний перенос", 36), ("Правок", 8), ("Все изменения и переносы", 60),
    ("Возвратов в работу", 11), ("Обновлён", 17),
]
DEL_COLS = [("Удалён из архива", 17), ("Кто удалил", 18)]


def _row(o, evs, with_deleted):
    i = _info(o, evs)
    done_note = (i["done"]["note"] if i["done"] and i["done"]["note"] else "") or (o.get("done_note") or "")
    post_reason = (i["post"]["note"] if i["post"] and i["post"]["note"] else "") or (o.get("postpone_reason") or "")
    canc_reason = (i["canc"]["note"] if i["canc"] and i["canc"]["note"] else "") or (o.get("cancel_reason") or "")
    last_move = i["moves"][-1]["note"] if i["moves"] else ""
    sent_when = _when(i["sent"]) or ("да" if o.get("sent_to_group") else "")
    row = [
        o["id"], o["department"], o["title"], o["details"] or "", o["tmc"] or "", _d(o["planned_date"]),
        o["shift"], STATUS_LABEL.get(o["status"], o["status"]), o["creator_name"] or "", _fdt(o["created_at"]),
        sent_when, _who(i["sent"]),
        _when(i["done"]), _who(i["done"]), done_note, _when(i["report"]),
        _when(i["post"]), _who(i["post"]), post_reason,
        _when(i["canc"]), _who(i["canc"]), canc_reason,
        _when(i["arch"]), _who(i["arch"]),
        len(i["moves"]), last_move, len(i["edits"]), "\n".join(i["changes"]),
        len(i["returns"]), _fdt(o["updated_at"]),
    ]
    if with_deleted:
        row += [_when(i["deleted"]), _who(i["deleted"])]
    return row


def _build(title, meta, orders, deleted, d1, d2, author, breakdowns):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    bold = Font(bold=True)
    fill = PatternFill("solid", fgColor="DDE7F3")
    all_ids = [o["id"] for o in orders] + [o["id"] for o in deleted]
    hist = db.history_for(all_ids)
    today = config.today().isoformat()
    total = len(orders)
    cnt = Counter(o["status"] for o in orders)
    overdue = sum(1 for o in orders if o["status"] in ("active", "postponed") and o["planned_date"] < today)
    moves = sum(1 for o in orders for e in hist.get(o["id"], []) if e["action"] == "Перенесён")
    edits = sum(1 for o in orders for e in hist.get(o["id"], []) if e["action"].startswith("Изменено:"))
    sent = sum(1 for o in orders if o.get("sent_to_group"))

    def widths(ws, ws_widths):
        for i, w in enumerate(ws_widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w

    def header(ws, row):
        ws.append(row)
        for c in ws[ws.max_row]:
            c.font = bold
            c.fill = fill
            c.alignment = Alignment(wrap_text=True, vertical="center")

    wb = Workbook()
    ws = wb.active
    ws.title = "Сводка"
    ws.append([title])
    ws["A1"].font = Font(bold=True, size=14)
    for k, v in meta:
        ws.append([k, v])
    ws.append(["Сформирован:", f"{config.now():%d.%m.%Y %H:%M}"])
    ws.append(["Сформировал:", author])
    ws.append([])
    header(ws, ["Показатель", "Значение"])
    ws.append(["Всего нарядов", total])
    for st in ORDER:
        if cnt[st]:
            ws.append([STATUS_LABEL[st], cnt[st]])
    ws.append(["Процент завершённых, %", round(cnt["done"] / total * 100, 1) if total else 0])
    ws.append(["Просрочено (не завершены, дата уже прошла)", overdue])
    ws.append(["Переносов всего", moves])
    ws.append(["Правок карточек всего", edits])
    ws.append(["Отправлено в чат мастеров", sent])
    ws.append(["Удалено из архива (лист «Удалённые»)", len(deleted)])
    for btitle, first_col, key in breakdowns:
        ws.append([])
        ws.append([btitle])
        ws.cell(ws.max_row, 1).font = bold
        header(ws, [first_col, "Всего", "Завершено", "Отменено", "Отложено", "Активных", "В архиве"])
        groups = defaultdict(list)
        for o in orders:
            groups[o[key] or "—"].append(o)
        for name, lst in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            c = Counter(x["status"] for x in lst)
            ws.append([name, len(lst), c["done"], c["cancelled"], c["postponed"], c["active"], c["archived"]])
    widths(ws, [46, 22, 12, 12, 12, 12, 12])

    def orders_sheet(name, lst, with_deleted):
        w = wb.create_sheet(name)
        cols = COLS + (DEL_COLS if with_deleted else [])
        header(w, [c for c, _ in cols])
        for o in lst:
            w.append(_row(o, hist.get(o["id"], []), with_deleted))
            w.cell(w.max_row, 6).number_format = "DD.MM.YYYY"
            for c in w[w.max_row]:
                c.alignment = Alignment(wrap_text=True, vertical="top")
        widths(w, [x for _, x in cols])
        w.freeze_panes = "D2"
        if lst:
            w.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{w.max_row}"

    orders_sheet("Наряды", orders, False)

    wd = wb.create_sheet("По дням")
    header(wd, ["Дата", "Всего", "Завершено", "Отменено", "Отложено", "Активных", "В архиве"])
    days = defaultdict(list)
    for o in orders:
        days[o["planned_date"]].append(o)
    for day in sorted(days):
        c = Counter(x["status"] for x in days[day])
        wd.append([_d(day), len(days[day]), c["done"], c["cancelled"], c["postponed"], c["active"], c["archived"]])
        wd.cell(wd.max_row, 1).number_format = "DD.MM.YYYY"
    widths(wd, [14, 10, 12, 12, 12, 12, 12])
    wd.freeze_panes = "A2"

    wh = wb.create_sheet("История")
    header(wh, ["№ наряда", "Подразделение", "Работа", "Дата и время", "Действие", "Пояснение", "Кто"])
    for o in orders + deleted:
        for e in hist.get(o["id"], []):
            wh.append([o["id"], o["department"], o["title"], _dt(e["created_at"]), e["action"],
                       e["note"] or "", e["actor_name"] or ""])
            wh.cell(wh.max_row, 4).number_format = "DD.MM.YYYY HH:MM"
            for c in wh[wh.max_row]:
                c.alignment = Alignment(wrap_text=True, vertical="top")
    widths(wh, [10, 16, 34, 17, 32, 50, 22])
    wh.freeze_panes = "A2"
    if wh.max_row > 1:
        wh.auto_filter.ref = f"A1:G{wh.max_row}"

    orders_sheet("Удалённые", deleted, True)

    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


def build_report(kind, label, orders, d1, d2, author, deleted=None):
    """kind: 'dept' (по подразделению) или 'master' (по мастеру)."""
    period = "за всё время" if not d1 and not d2 else f"{_fmt(_d(d1))} – {_fmt(_d(d2))}"
    title = f"Отчёт по подразделению: {label}" if kind == "dept" else f"Отчёт по мастеру: {label}"
    meta = [("Период (по плановой дате):", period)]
    if kind == "dept":
        breakdowns = [("По сменам", "Смена", "shift"), ("По мастерам (кто создал наряд)", "Мастер", "creator_name")]
    else:
        breakdowns = [("По сменам", "Смена", "shift"), ("По подразделениям", "Подразделение", "department")]
    return _build(title, meta, orders, deleted or [], d1, d2, author, breakdowns)


def build_export(orders, deleted, filters_text, author):
    """Выгрузка по фильтрам статистики."""
    meta = [("Фильтры:", filters_text)]
    breakdowns = [("По подразделениям", "Подразделение", "department"), ("По сменам", "Смена", "shift"),
                  ("По мастерам (кто создал наряд)", "Мастер", "creator_name")]
    return _build("Выгрузка нарядов", meta, orders, deleted, None, None, author, breakdowns)
