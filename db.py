import glob
import os
import shutil
import sqlite3
from collections import Counter
from datetime import datetime

import config

_conn = None


def conn():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
    return _conn


def now():
    return config.now().strftime("%Y-%m-%d %H:%M:%S")


def _add_col(table, col, ddl):
    cols = {r[1] for r in conn().execute(f"PRAGMA table_info({table})")}
    if col not in cols:
        conn().execute(ddl)


def init_db():
    c = conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(
            telegram_id INTEGER PRIMARY KEY,
            full_name TEXT, username TEXT,
            role TEXT, department TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            department TEXT NOT NULL,
            title TEXT NOT NULL,
            details TEXT, tmc TEXT,
            planned_date TEXT NOT NULL,
            shift TEXT NOT NULL,
            creator_id INTEGER, creator_name TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            cancel_reason TEXT,
            archived_deleted INTEGER NOT NULL DEFAULT 0,
            is_reminder_sent INTEGER NOT NULL DEFAULT 0,
            created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS order_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            action TEXT NOT NULL, note TEXT,
            actor_id INTEGER, actor_name TEXT,
            created_at TEXT
        );
        """
    )
    # миграции для баз, созданных старой версией
    _add_col("orders", "sent_to_group", "ALTER TABLE orders ADD COLUMN sent_to_group INTEGER NOT NULL DEFAULT 0")
    _add_col("users", "notify", "ALTER TABLE users ADD COLUMN notify INTEGER NOT NULL DEFAULT 1")
    _add_col("orders", "done_note", "ALTER TABLE orders ADD COLUMN done_note TEXT")
    _add_col("orders", "postpone_reason", "ALTER TABLE orders ADD COLUMN postpone_reason TEXT")
    c.execute("UPDATE orders SET status='postponed' WHERE status='continue'")
    # убираем «(ночь)» и «(день)» из названий смен в старых нарядах
    c.execute("UPDATE orders SET shift=REPLACE(REPLACE(shift,' (ночь)',''),' (день)','')")
    c.commit()


def list_backups():
    return sorted(glob.glob(os.path.join("backups", "naryadchik_*.sqlite3")))


def backup(keep=None, force=False):
    """Копия базы в папку backups. Хранятся последние `keep` копий (по умолчанию 20).
    force=True — заново сделать сегодняшнюю копию (свежие данные)."""
    keep = keep or config.BACKUP_KEEP
    if not os.path.exists(config.DB_PATH):
        return None
    os.makedirs("backups", exist_ok=True)
    dst = os.path.join("backups", f"naryadchik_{config.now():%Y%m%d}.sqlite3")
    if force and os.path.exists(dst):
        os.remove(dst)
    if not os.path.exists(dst):
        conn().commit()
        src = sqlite3.connect(config.DB_PATH)
        out = sqlite3.connect(dst)
        with out:
            src.backup(out)
        out.close()
        src.close()
    for f in list_backups()[:-keep]:
        try:
            os.remove(f)
        except OSError:
            pass
    return dst


def snapshot_bytes():
    """Свежая копия базы одним файлом (для скачивания админом)."""
    tmp = "naryadchik_export.tmp"
    src = sqlite3.connect(config.DB_PATH)
    out = sqlite3.connect(tmp)
    with out:
        src.backup(out)
    out.close()
    src.close()
    with open(tmp, "rb") as f:
        data = f.read()
    os.remove(tmp)
    return data


# ---------- пользователи ----------
def upsert_user(tg_id, full_name, username):
    c = conn()
    row = c.execute("SELECT 1 FROM users WHERE telegram_id=?", (tg_id,)).fetchone()
    if row:
        c.execute("UPDATE users SET full_name=?, username=? WHERE telegram_id=?", (full_name, username, tg_id))
        c.commit()
        return False
    c.execute("INSERT INTO users(telegram_id,full_name,username,created_at) VALUES(?,?,?,?)",
              (tg_id, full_name, username, now()))
    c.commit()
    return True


def get_user(tg_id):
    return conn().execute("SELECT * FROM users WHERE telegram_id=?", (tg_id,)).fetchone()


def list_users(limit=40):
    return conn().execute("SELECT * FROM users ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()


def set_role(tg_id, role, department=None):
    conn().execute("UPDATE users SET role=?, department=? WHERE telegram_id=?", (role, department, tg_id))
    conn().commit()


def set_notify(tg_id, value):
    conn().execute("UPDATE users SET notify=? WHERE telegram_id=?", (1 if value else 0, tg_id))
    conn().commit()


def masters_of(department):
    return conn().execute(
        "SELECT * FROM users WHERE role='master' AND department=? AND notify=1", (department,)).fetchall()


def creators():
    return [(r["creator_id"], r["creator_name"]) for r in conn().execute(
        "SELECT creator_id, creator_name FROM orders GROUP BY creator_id ORDER BY creator_name").fetchall()]


# ---------- наряды ----------
def create_order(department, title, details, tmc, planned_date, shift, creator_id, creator_name):
    t = now()
    cur = conn().execute(
        "INSERT INTO orders(department,title,details,tmc,planned_date,shift,creator_id,creator_name,"
        "status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,'active',?,?)",
        (department, title, details, tmc, planned_date, shift, creator_id, creator_name, t, t),
    )
    conn().commit()
    return cur.lastrowid


def get_order(oid):
    r = conn().execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    return dict(r) if r else None


def _where(statuses, department, creator_id, deleted):
    q = "FROM orders WHERE archived_deleted=?"
    p = [1 if deleted else 0]
    if statuses:
        q += " AND status IN (%s)" % ",".join("?" * len(statuses))
        p += list(statuses)
    if department:
        q += " AND department=?"
        p.append(department)
    if creator_id is not None:
        q += " AND creator_id=?"
        p.append(creator_id)
    return q, p


def list_orders(statuses, department=None, limit=30, newest_first=False, creator_id=None, deleted=0, offset=0):
    q, p = _where(statuses, department, creator_id, deleted)
    q = "SELECT * " + q + " ORDER BY " + ("id DESC" if newest_first else "planned_date ASC, id ASC")
    q += " LIMIT ? OFFSET ?"
    return [dict(r) for r in conn().execute(q, p + [limit, offset]).fetchall()]


def count_orders(statuses, department=None, creator_id=None, deleted=0):
    q, p = _where(statuses, department, creator_id, deleted)
    return conn().execute("SELECT COUNT(*) " + q, p).fetchone()[0]


def set_status(oid, status, cancel_reason=None):
    """Меняет статус. Причина отложения и примечание о выполнении сбрасываются, если они больше не актуальны."""
    conn().execute(
        "UPDATE orders SET status=?, cancel_reason=?, "
        "postpone_reason=CASE WHEN ?='postponed' THEN postpone_reason ELSE NULL END, "
        "done_note=CASE WHEN ?='done' THEN done_note ELSE NULL END, updated_at=? WHERE id=?",
        (status, cancel_reason, status, status, now(), oid))
    conn().commit()


def set_text(oid, column, value):
    assert column in ("done_note", "postpone_reason")
    conn().execute(f"UPDATE orders SET {column}=?, updated_at=? WHERE id=?", (value, now(), oid))
    conn().commit()


EDITABLE = {"title", "details", "tmc", "planned_date", "shift"}


def update_fields(oid, fields):
    fields = {k: v for k, v in fields.items() if k in EDITABLE}
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    if "planned_date" in fields:
        sets += ", is_reminder_sent=0"
    conn().execute(f"UPDATE orders SET {sets}, updated_at=? WHERE id=?", (*fields.values(), now(), oid))
    conn().commit()


def move_order(oid, planned_date, shift):
    conn().execute(
        "UPDATE orders SET planned_date=?, shift=?, status='active', cancel_reason=NULL, postpone_reason=NULL, "
        "is_reminder_sent=0, updated_at=? WHERE id=?", (planned_date, shift, now(), oid))
    conn().commit()


def mark_sent(oid):
    conn().execute("UPDATE orders SET sent_to_group=1, updated_at=? WHERE id=?", (now(), oid))
    conn().commit()


def soft_delete(oid):
    conn().execute("UPDATE orders SET archived_deleted=1, updated_at=? WHERE id=?", (now(), oid))
    conn().commit()


def undelete(oid):
    conn().execute("UPDATE orders SET archived_deleted=0, updated_at=? WHERE id=?", (now(), oid))
    conn().commit()


def reminder_candidates(today_iso):
    rows = conn().execute(
        "SELECT * FROM orders WHERE archived_deleted=0 AND is_reminder_sent=0 AND planned_date=? "
        "AND status IN ('active','postponed','archived') ORDER BY department, id", (today_iso,)).fetchall()
    return [dict(r) for r in rows]


def mark_reminded(ids):
    for i in ids:
        conn().execute("UPDATE orders SET is_reminder_sent=1 WHERE id=?", (i,))
    conn().commit()


# ---------- история ----------
def add_history(oid, action, note, actor_id, actor_name):
    conn().execute(
        "INSERT INTO order_history(order_id,action,note,actor_id,actor_name,created_at) VALUES(?,?,?,?,?,?)",
        (oid, action, note, actor_id, actor_name, now()),
    )
    conn().commit()


def get_history(oid, limit=15):
    return conn().execute(
        "SELECT * FROM order_history WHERE order_id=? ORDER BY id DESC LIMIT ?", (oid, limit)
    ).fetchall()


# ---------- статистика ----------
def query_orders(date_from=None, date_to=None, department=None, shift=None, creator_id=None, status=None,
                 deleted=False):
    q = "SELECT * FROM orders WHERE archived_deleted=?"
    p = [1 if deleted else 0]
    if date_from:
        q += " AND planned_date>=?"
        p.append(date_from)
    if date_to:
        q += " AND planned_date<=?"
        p.append(date_to)
    if department:
        q += " AND department=?"
        p.append(department)
    if shift:
        q += " AND shift=?"
        p.append(shift)
    if creator_id is not None:
        q += " AND creator_id=?"
        p.append(creator_id)
    if status:
        q += " AND status=?"
        p.append(status)
    q += " ORDER BY planned_date, id"
    return [dict(r) for r in conn().execute(q, p).fetchall()]


def history_days(ids, action):
    out = Counter()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        sql = ("SELECT substr(created_at,1,10) AS d, COUNT(*) AS c FROM order_history "
               "WHERE action=? AND order_id IN (%s) GROUP BY d" % ",".join("?" * len(chunk)))
        for r in conn().execute(sql, (action, *chunk)):
            out[r["d"]] += r["c"]
    return out


def history_counts(ids, action):
    """Сколько раз для каждого наряда было событие action (например, «Перенесён»)."""
    out = Counter()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        sql = ("SELECT order_id, COUNT(*) AS c FROM order_history WHERE action=? AND order_id IN (%s) "
               "GROUP BY order_id" % ",".join("?" * len(chunk)))
        for r in conn().execute(sql, (action, *chunk)):
            out[r["order_id"]] = r["c"]
    return out


def history_for(ids):
    """Вся история для списка нарядов: {order_id: [события по порядку]}."""
    out = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        sql = ("SELECT * FROM order_history WHERE order_id IN (%s) ORDER BY id" % ",".join("?" * len(chunk)))
        for r in conn().execute(sql, chunk):
            out.setdefault(r["order_id"], []).append(dict(r))
    return out


def moved_on(day_iso):
    """Наряды, у которых в этот день было событие «Перенесён» (по журналу), с их текущими данными."""
    rows = conn().execute(
        "SELECT o.* FROM order_history h JOIN orders o ON o.id=h.order_id "
        "WHERE h.action='Перенесён' AND substr(h.created_at,1,10)=? AND o.archived_deleted=0 "
        "ORDER BY h.id", (day_iso,)).fetchall()
    return [dict(r) for r in rows]
