"""Storage for the demo: one small SQLite database file (helpdesk.db).

Tickets, ticket history, the audit log, answer feedback and AI usage are saved here,
so they survive an app restart. SQLite is a database that lives in a single file,
built into Python, so there is nothing extra to install.
"""
import os
import json
import re
import sqlite3
import datetime
from zoneinfo import ZoneInfo

TOKYO = ZoneInfo("Asia/Tokyo")
DB_PATH = os.getenv("HELPDESK_DB", "helpdesk.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets  (ticket_id TEXT PRIMARY KEY, requester_email TEXT, title TEXT, category TEXT,
                                     priority TEXT, team TEXT, status TEXT, created TEXT, created_at TEXT,
                                     waiting_for TEXT, paused_since TEXT, paused_hours REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS history  (id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT, at TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS audit    (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, user TEXT, tool TEXT,
                                     args TEXT, result TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS feedback (msg_id TEXT PRIMARY KEY, at TEXT, user TEXT, question TEXT, answer TEXT,
                                     rating INTEGER);
CREATE TABLE IF NOT EXISTS self_help (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, user TEXT, question TEXT,
                                     outcome TEXT);
CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, user TEXT, started_at TEXT, updated_at TEXT,
                                     outcome TEXT, messages TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS usage    (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, user TEXT, kind TEXT,
                                     input_tokens INTEGER, output_tokens INTEGER);
"""


# Safety net in code: anything that looks like a password is removed BEFORE it is saved anywhere.
_SECRET = re.compile(r"((?:password|passcode|passwd|pwd|pin|パスワード)\s*(?:is|was|=|:|は)\s*)"
                     r"(?=\S*[0-9!@#$%^&*?_~-])(\S{4,})", re.IGNORECASE)   # e.g. "password is Summer2026!"


def redact(text):
    return _SECRET.sub(lambda m: m.group(1) + "[removed]", text) if isinstance(text, str) else text


def now():
    return datetime.datetime.now(TOKYO)


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


# ---------------------------------------------------------------- setup
def init():
    """Create the tables if needed, and load the starting tickets the first time."""
    with _conn() as c:
        c.executescript(SCHEMA)
        # Older helpdesk.db files have no created_at column (exact creation time): add it
        columns = [r[1] for r in c.execute("PRAGMA table_info(tickets)")]
        if "created_at" not in columns:
            c.execute("ALTER TABLE tickets ADD COLUMN created_at TEXT")
            c.execute("UPDATE tickets SET created_at = created || 'T09:00:00+09:00' WHERE created_at IS NULL")
        if "waiting_for" not in columns:              # who/what a waiting ticket is waiting for
            c.execute("ALTER TABLE tickets ADD COLUMN waiting_for TEXT")
        if "paused_since" not in columns:             # the resolution clock pauses while waiting for approval
            c.execute("ALTER TABLE tickets ADD COLUMN paused_since TEXT")
            c.execute("ALTER TABLE tickets ADD COLUMN paused_hours REAL DEFAULT 0")
            c.execute("UPDATE tickets SET paused_since=? WHERE status=?", (now().isoformat(timespec='seconds'),
                                                                         PAUSE_STATUS))
        empty = c.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0
    if empty:
        _seed()


def reset():
    """Delete everything and go back to the starting tickets in tickets.json."""
    with _conn() as c:
        for table in ("tickets", "history", "audit", "feedback", "usage", "self_help", "conversations"):
            c.execute(f"DELETE FROM {table}")
    _seed()


def _seed():
    with open("tickets.json", encoding="utf-8") as f:
        starting = json.load(f)
    with _conn() as c:
        for t in starting:
            c.execute("INSERT INTO tickets (ticket_id, requester_email, title, category, priority, team, status, "
                      "created, created_at, waiting_for, paused_since, paused_hours) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (t["ticket_id"], t["requester_email"], t["title"], t["category"],
                       t["priority"], t["team"], t["status"], t["created"],
                       t.get("created_at", t["created"] + "T09:00:00+09:00"), t.get("waiting_for"),
                       t.get("paused_since"), 0))
            for h in t.get("history", []):
                at, _, text = h.partition(": ")
                c.execute("INSERT INTO history (ticket_id, at, text) VALUES (?,?,?)", (t["ticket_id"], at, text))


# ---------------------------------------------------------------- tickets
def _with_history(c, row):
    t = dict(row)
    t["history"] = [dict(h) for h in c.execute(
        "SELECT at, text FROM history WHERE ticket_id=? ORDER BY id", (t["ticket_id"],))]
    return t


def list_tickets(requester_email=None):
    with _conn() as c:
        if requester_email:
            rows = c.execute("SELECT * FROM tickets WHERE requester_email=? ORDER BY ticket_id",
                             (requester_email,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM tickets ORDER BY ticket_id").fetchall()
        return [_with_history(c, r) for r in rows]


def get_ticket(ticket_id):
    with _conn() as c:
        row = c.execute("SELECT * FROM tickets WHERE UPPER(ticket_id)=?",
                        (ticket_id.strip().upper(),)).fetchone()
        return _with_history(c, row) if row else None


def create_ticket(requester_email, title, category, priority, team, status, first_note):
    with _conn() as c:
        numbers = [int(r[0].split("-")[1]) for r in c.execute("SELECT ticket_id FROM tickets")]
        new_id = f"T-{max(numbers, default=1000) + 1}"
        today = now().date().isoformat()
        title, first_note = redact(title), redact(first_note)
        c.execute("INSERT INTO tickets (ticket_id, requester_email, title, category, priority, team, status, "
                  "created, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  (new_id, requester_email, title, category, priority, team, status, today,
                   now().isoformat(timespec="seconds")))
        c.execute("INSERT INTO history (ticket_id, at, text) VALUES (?,?,?)", (new_id, today, first_note))
    return new_id


def add_history(ticket_id, text):
    with _conn() as c:
        c.execute("INSERT INTO history (ticket_id, at, text) VALUES (?,?,?)",
                  (ticket_id, now().date().isoformat(), redact(text)))


def clear_waiting(ticket_id):
    with _conn() as c:
        c.execute("UPDATE tickets SET waiting_for=NULL WHERE ticket_id=?", (ticket_id,))


PAUSE_STATUS = "Waiting for approval"             # while waiting for the user/manager, IT's clock is paused


def update_ticket(ticket_id, **fields):
    fields = {k: v for k, v in fields.items() if v}
    if "status" in fields:                            # start or stop the paused clock
        with _conn() as c:
            row = c.execute("SELECT status, paused_since, paused_hours FROM tickets WHERE ticket_id=?",
                            (ticket_id,)).fetchone()
        if row:
            new = fields["status"]
            if new == PAUSE_STATUS and not row["paused_since"]:
                fields["paused_since"] = now().isoformat(timespec="seconds")
            elif new != PAUSE_STATUS and row["paused_since"]:
                waited = now() - datetime.datetime.fromisoformat(row["paused_since"])
                fields["paused_hours"] = (row["paused_hours"] or 0) + waited.total_seconds() / 3600
                with _conn() as c:
                    c.execute("UPDATE tickets SET paused_since=NULL WHERE ticket_id=?", (ticket_id,))
    if fields:
        with _conn() as c:
            sets = ", ".join(f"{k}=?" for k in fields)
            c.execute(f"UPDATE tickets SET {sets} WHERE ticket_id=?", (*fields.values(), ticket_id))


# ---------------------------------------------------------------- audit log
def log_audit(user, tool, args, result, status):
    with _conn() as c:
        c.execute("INSERT INTO audit (at, user, tool, args, result, status) VALUES (?,?,?,?,?,?)",
                  (now().strftime("%Y-%m-%d %H:%M:%S"), user, tool,
                   redact(json.dumps(args, ensure_ascii=False)), redact(json.dumps(result, ensure_ascii=False)), status))


def list_audit(limit=100000):
    with _conn() as c:
        rows = c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r, args=json.loads(r["args"]), result=json.loads(r["result"])) for r in rows]


# ---------------------------------------------------------------- feedback
def save_feedback(msg_id, user, question, answer, rating):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO feedback VALUES (?,?,?,?,?,?)",
                  (msg_id, now().strftime("%Y-%m-%d %H:%M:%S"), user, redact(question), redact(answer), rating))


def list_feedback():
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM feedback ORDER BY at DESC")]


# ---------------------------------------------------------------- AI usage
def log_usage(user, kind, input_tokens, output_tokens):
    with _conn() as c:
        c.execute("INSERT INTO usage (at, user, kind, input_tokens, output_tokens) VALUES (?,?,?,?,?)",
                  (now().strftime("%Y-%m-%d %H:%M:%S"), user, kind, int(input_tokens or 0), int(output_tokens or 0)))


def usage_totals():
    with _conn() as c:
        rows = c.execute("SELECT kind, COUNT(*) AS calls, SUM(input_tokens) AS tin, SUM(output_tokens) AS tout "
                         "FROM usage GROUP BY kind").fetchall()
    return {r["kind"]: {"calls": r["calls"], "input": r["tin"] or 0, "output": r["tout"] or 0} for r in rows}


# ---------------------------------------------------------------- self-help outcomes ("solved" or "needed a ticket")
def log_self_help(user, question, outcome):
    with _conn() as c:
        c.execute("INSERT INTO self_help (at, user, question, outcome) VALUES (?,?,?,?)",
                  (now().strftime("%Y-%m-%d %H:%M:%S"), user, redact(question), outcome))


def self_help_totals():
    with _conn() as c:
        rows = c.execute("SELECT outcome, COUNT(*) FROM self_help GROUP BY outcome").fetchall()
    return {r[0]: r[1] for r in rows}


# ---------------------------------------------------------------- settings (e.g. conversation history on/off)
def get_setting(key, default):
    with _conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(key, value):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, str(value)))


# ---------------------------------------------------------------- conversation history (optional)
def save_conversation(conv_id, user, started_at, outcome, messages):
    """Saves the whole chat (passwords removed; screenshots are not stored, only noted)."""
    clean = [{"role": m["role"], "text": redact(m["text"]), "at": m.get("at", ""),
              "screenshot": bool(m.get("image"))} for m in messages]
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO conversations VALUES (?,?,?,?,?,?)",
                  (conv_id, user, started_at, now().strftime("%Y-%m-%d %H:%M:%S"), outcome,
                   json.dumps(clean, ensure_ascii=False)))


def list_conversations():
    with _conn() as c:
        rows = c.execute("SELECT * FROM conversations ORDER BY updated_at DESC").fetchall()
    return [dict(r, messages=json.loads(r["messages"])) for r in rows]


def delete_old_conversations(days):
    """Retention: conversations older than the chosen number of days are deleted automatically."""
    cutoff = (now() - datetime.timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as c:
        return c.execute("DELETE FROM conversations WHERE updated_at < ?", (cutoff,)).rowcount


def delete_all_conversations():
    with _conn() as c:
        c.execute("DELETE FROM conversations")
