"""
Morandi Booking API — Flask + PostgreSQL (Neon)

環境變數：
  DATABASE_URL   — Neon PostgreSQL 連線字串（必填）
  ALLOWED_ORIGIN — 允許的前端網域，多個用逗號分隔，預設 *
  PORT           — 本機開發用的 port，Render 會自動注入
"""
import os
import json
import secrets
from contextlib import contextmanager

from flask import Flask, request, jsonify
from flask_cors import CORS
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)

# ---------- CORS ----------
_allowed = os.environ.get("ALLOWED_ORIGIN", "*").strip()
if _allowed in ("", "*"):
    CORS(app)
else:
    CORS(app, origins=[o.strip() for o in _allowed.split(",") if o.strip()])

DATABASE_URL = os.environ.get("DATABASE_URL")


# ---------- DB ----------
@contextmanager
def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not configured")
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS bookings (
    id          TEXT PRIMARY KEY,
    date        DATE NOT NULL,
    name        TEXT NOT NULL,
    start_time  TEXT NOT NULL,
    end_time    TEXT NOT NULL,
    contacts    JSONB NOT NULL DEFAULT '[]'::jsonb,
    note        TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bookings_date ON bookings(date);
"""


def init_db():
    if not DATABASE_URL:
        print("[init_db] DATABASE_URL not set, skipping")
        return
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        print("[init_db] schema ready")
    except Exception as exc:
        print(f"[init_db] failed: {exc}")


init_db()


# ---------- helpers ----------
def serialize(row):
    return {
        "id": row["id"],
        "date": row["date"].isoformat(),
        "name": row["name"],
        "start": row["start_time"],
        "end": row["end_time"],
        "contacts": row["contacts"] or [],
        "note": row["note"] or "",
        "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
    }


def validate_payload(d, require_date=True):
    required = ("name", "start", "end")
    if require_date:
        required = ("date",) + required
    for f in required:
        v = d.get(f)
        if not isinstance(v, str) or not v.strip():
            return f"missing or invalid field: {f}"
    if d["start"] >= d["end"]:
        return "end time must be after start time"
    contacts = d.get("contacts", [])
    if not isinstance(contacts, list) or len(contacts) == 0:
        return "at least one contact required"
    if not all(isinstance(c, str) and c.strip() for c in contacts):
        return "contacts must be non-empty strings"
    note = d.get("note", "")
    if not isinstance(note, str):
        return "note must be a string"
    if len(d["name"]) > 100 or len(note) > 1000:
        return "field too long"
    return None


# ---------- routes ----------
@app.route("/api/health")
def health():
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
        return {"ok": True, "db": True}
    except Exception as exc:
        return {"ok": True, "db": False, "error": str(exc)}, 200


@app.route("/api/bookings", methods=["GET"])
def list_bookings():
    date = request.args.get("date")
    frm = request.args.get("from")
    to = request.args.get("to")
    with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        if date:
            cur.execute(
                "SELECT * FROM bookings WHERE date = %s ORDER BY start_time",
                (date,),
            )
        elif frm and to:
            cur.execute(
                "SELECT * FROM bookings WHERE date BETWEEN %s AND %s "
                "ORDER BY date, start_time",
                (frm, to),
            )
        else:
            cur.execute(
                "SELECT * FROM bookings ORDER BY date DESC, start_time LIMIT 1000"
            )
        rows = cur.fetchall()
    return jsonify([serialize(r) for r in rows])


@app.route("/api/bookings", methods=["POST"])
def create_booking():
    data = request.get_json(silent=True) or {}
    err = validate_payload(data, require_date=True)
    if err:
        return {"error": err}, 400

    booking_id = secrets.token_urlsafe(8)
    with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO bookings (id, date, name, start_time, end_time, contacts, note)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
            RETURNING *
            """,
            (
                booking_id,
                data["date"],
                data["name"].strip(),
                data["start"],
                data["end"],
                json.dumps(data["contacts"]),
                data.get("note", "").strip(),
            ),
        )
        row = cur.fetchone()
    return jsonify(serialize(row)), 201


@app.route("/api/bookings/<booking_id>", methods=["PUT"])
def update_booking(booking_id):
    data = request.get_json(silent=True) or {}
    err = validate_payload(data, require_date=False)
    if err:
        return {"error": err}, 400

    with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            UPDATE bookings
            SET name=%s, start_time=%s, end_time=%s, contacts=%s::jsonb, note=%s
            WHERE id=%s
            RETURNING *
            """,
            (
                data["name"].strip(),
                data["start"],
                data["end"],
                json.dumps(data["contacts"]),
                data.get("note", "").strip(),
                booking_id,
            ),
        )
        row = cur.fetchone()
        if not row:
            return {"error": "booking not found"}, 404
    return jsonify(serialize(row))


@app.route("/api/bookings/<booking_id>", methods=["DELETE"])
def delete_booking(booking_id):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM bookings WHERE id=%s", (booking_id,))
        if cur.rowcount == 0:
            return {"error": "booking not found"}, 404
    return {"ok": True}


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=True,
    )
