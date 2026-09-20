from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
import os
import secrets
from pathlib import Path
from urllib.parse import quote

import psycopg2
from psycopg2.extras import DictCursor

app = Flask(__name__)
app.secret_key = os.environ.get(
    "FLASK_SECRET_KEY",
    "whot-arena-development-secret-change-this"
)

# Admin account
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "Whot")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Whot@2030")

BASE_DIR = Path(__file__).resolve().parent
DATABASE_URL = os.environ.get("DATABASE_URL")


class PGConnection:
    def __init__(self, url):
        if not url:
            raise RuntimeError("DATABASE_URL is not set.")

        self.conn = psycopg2.connect(
            url,
            cursor_factory=DictCursor
        )

    def execute(self, sql, params=None):
        sql = sql.replace("?", "%s")
        cur = self.conn.cursor()

        if params is None:
            cur.execute(sql)
        else:
            cur.execute(sql, params)

        return cur

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()


def get_db():
    return PGConnection(DATABASE_URL)



def ensure_challenge_tables():
    conn = get_db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS player_presence (
            user_id INTEGER PRIMARY KEY,
            last_seen TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS player_challenges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            challenger_id INTEGER NOT NULL,
            challenged_id INTEGER NOT NULL,
            stake INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            responded_at TIMESTAMP,
            FOREIGN KEY(challenger_id) REFERENCES users(id),
            FOREIGN KEY(challenged_id) REFERENCES users(id)
        )
    """)

    conn.commit()
    conn.close()


def init_db():
    # PostgreSQL schema is already created separately.
    # Keep this function for compatibility with the existing startup call.
    return


def ensure_challenge_tables():
    # Challenge tables already exist in PostgreSQL.
    return


@app.route("/challenge/incoming")
def incoming_challenges():
    if "user_id" not in session:
        return {"success": False, "challenges": []}, 401

    ensure_challenge_tables()
    conn = get_db()

    challenges = conn.execute("""
        SELECT
            c.id,
            c.stake,
            c.created_at,
            u.username AS challenger
        FROM player_challenges c
        JOIN users u ON u.id=c.challenger_id
        WHERE c.challenged_id=?
          AND c.status='pending'
        ORDER BY c.id DESC
        LIMIT 20
    """, (session["user_id"],)).fetchall()

    conn.close()

    return {
        "success": True,
        "challenges": [dict(x) for x in challenges]
    }


@app.route("/")
def home():
    return render_template("welcome.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        existing_device_token = request.cookies.get("whot_device")

        if existing_device_token:
            conn = get_db()
            device_account = conn.execute(
                "SELECT id FROM users WHERE device_token = ?",
                (existing_device_token,)
            ).fetchone()
            conn.close()

            if device_account:
                flash(
                    "ACCOUNT ALREADY EXISTS — This device is already linked to an account. Please log in instead.",
                    "account_exists"
                )
                return render_template(
                    "register.html",
                    referral_code=request.form.get("referral_code", "").strip().upper()
                )

        username = request.form.get("username", "").strip()
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        referral_code_input = request.form.get("referral_code", "").strip().upper()

        if not username or not phone or not password:
            flash("Please fill in all fields.", "error")
            return render_template(
                "register.html",
                referral_code=referral_code_input
            )

        if len(username) < 2:
            flash("Username must be at least 2 characters.", "error")
            return render_template(
                "register.html",
                referral_code=referral_code_input
            )

        if len(phone) < 10:
            flash("Enter a valid phone number.", "error")
            return render_template(
                "register.html",
                referral_code=referral_code_input
            )

        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
            return render_template(
                "register.html",
                referral_code=referral_code_input
            )

        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return render_template(
                "register.html",
                referral_code=referral_code_input
            )

        conn = get_db()

        existing = conn.execute(
            "SELECT id FROM users WHERE phone = ?",
            (phone,)
        ).fetchone()

        if existing:
            conn.close()
            flash(
                "ACCOUNT ALREADY EXISTS — This phone number is already registered. Please log in instead.",
                "account_exists"
            )
            return render_template(
                "register.html",
                referral_code=referral_code_input
            )

        # Validate referral code if supplied
        referrer = None

        if referral_code_input:
            referrer = conn.execute(
                "SELECT id, username, referral_code FROM users WHERE referral_code = ?",
                (referral_code_input,)
            ).fetchone()

            if not referrer:
                conn.close()
                flash("Invalid referral code.", "error")
                return render_template(
                    "register.html",
                    referral_code=referral_code_input
                )

        # Create a secure device token for this registration.
        device_token = secrets.token_urlsafe(32)

        password_hash = generate_password_hash(password)

        referral_code = "WA" + secrets.token_hex(4).upper()

        while conn.execute(
            "SELECT id FROM users WHERE referral_code = ?",
            (referral_code,)
        ).fetchone():
            referral_code = "WA" + secrets.token_hex(4).upper()

        cursor = conn.execute(
            """
            INSERT INTO users
            (username, phone, password_hash, balance, referral_code, referred_by, device_token)
            VALUES (?, ?, ?, 75, ?, ?, ?)
            """,
            (
                username,
                phone,
                password_hash,
                referral_code,
                referral_code_input or None,
                device_token
            )
        )

        new_user_id = cursor.lastrowid

        # Create referral record, but do NOT pay the ₦25 yet.
        if referrer and referrer["id"] != new_user_id:
            conn.execute(
                """
                INSERT INTO referrals
                (referrer_id, referred_id, referral_code)
                VALUES (?, ?, ?)
                """,
                (
                    referrer["id"],
                    new_user_id,
                    referral_code_input
                )
            )

        conn.commit()
        conn.close()

        response = redirect(url_for("login"))
        response.set_cookie(
            "whot_device",
            device_token,
            max_age=60 * 60 * 24 * 365 * 2,
            httponly=True,
            samesite="Lax",
            secure=False
        )

        flash("Account created successfully. You can now log in.", "success")
        return response

    # Automatically carry ?ref=CODE into the registration form
    referral_code = request.args.get("ref", "").strip().upper()

    return render_template(
        "register.html",
        referral_code=referral_code
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")

        conn = get_db()

        user = conn.execute(
            "SELECT * FROM users WHERE phone = ?",
            (phone,)
        ).fetchone()

        conn.close()

        if not user or not check_password_hash(user["password_hash"], password):
            flash("Incorrect phone number or password.", "error")
            return render_template("login.html")

        session.clear()
        session["user_id"] = user["id"]

        response = redirect(url_for("dashboard"))

        # Restore the device binding when the owner logs in.
        if user["device_token"]:
            response.set_cookie(
                "whot_device",
                user["device_token"],
                max_age=60 * 60 * 24 * 365 * 2,
                httponly=True,
                samesite="Lax",
                secure=False
            )

        return response

    return render_template("login.html")


@app.route("/referral")
def referral():
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()

    user = conn.execute(
        """
        SELECT id, username, referral_code
        FROM users
        WHERE id = ?
        """,
        (session["user_id"],)
    ).fetchone()

    if not user:
        conn.close()
        session.clear()
        return redirect(url_for("login"))

    referral_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM referrals
        WHERE referrer_id = ?
        """,
        (user["id"],)
    ).fetchone()[0]

    qualified_referrals = conn.execute(
        """
        SELECT COUNT(*)
        FROM referrals
        WHERE referrer_id = ?
        AND qualified = 1
        """,
        (user["id"],)
    ).fetchone()[0]

    referral_earnings = conn.execute(
        """
        SELECT COALESCE(SUM(reward_amount), 0)
        FROM referrals
        WHERE referrer_id = ?
        AND reward_paid = 1
        """,
        (user["id"],)
    ).fetchone()[0]

    conn.close()

    referral_link = url_for(
        "register",
        ref=user["referral_code"],
        _external=True
    )

    return render_template(
        "referral.html",
        user=user,
        referral_link=referral_link,
        referral_count=referral_count,
        qualified_referrals=qualified_referrals,
        referral_earnings=referral_earnings
    )


@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()

    user = conn.execute(
        "SELECT id, username, phone, balance, referral_code FROM users WHERE id = ?",
        (session["user_id"],)
    ).fetchone()

    if not user:
        conn.close()
        session.clear()
        return redirect(url_for("login"))

    referral_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM referrals
        WHERE referrer_id = ?
        """,
        (user["id"],)
    ).fetchone()[0]

    qualified_referrals = conn.execute(
        """
        SELECT COUNT(*)
        FROM referrals
        WHERE referrer_id = ?
        AND qualified = 1
        """,
        (user["id"],)
    ).fetchone()[0]

    referral_earnings = conn.execute(
        """
        SELECT COALESCE(SUM(reward_amount), 0)
        FROM referrals
        WHERE referrer_id = ?
        AND reward_paid = 1
        """,
        (user["id"],)
    ).fetchone()[0]

    deposit_total = conn.execute(
        """
        SELECT COALESCE(SUM(amount), 0)
        FROM deposits
        WHERE user_id = ?
        AND status = 'approved'
        """,
        (user["id"],)
    ).fetchone()[0]

    withdrawal_total = conn.execute(
        """
        SELECT COALESCE(SUM(amount), 0)
        FROM withdrawals
        WHERE user_id = ?
        AND status = 'approved'
        """,
        (user["id"],)
    ).fetchone()[0]

    entry_fees = conn.execute(
        """
        SELECT COALESCE(SUM(stake), 0)
        FROM game_history
        WHERE user_id = ?
        """,
        (user["id"],)
    ).fetchone()[0]

    losses = conn.execute(
        """
        SELECT COALESCE(SUM(stake), 0)
        FROM game_history
        WHERE user_id = ?
        AND outcome = 'loss'
        """,
        (user["id"],)
    ).fetchone()[0]

    wins = conn.execute(
        """
        SELECT COALESCE(SUM(winnings), 0)
        FROM game_history
        WHERE user_id = ?
        AND outcome = 'win'
        """,
        (user["id"],)
    ).fetchone()[0]

    conn.close()

    return render_template(
        "dashboard.html",
        user=user,
        referral_count=referral_count,
        qualified_referrals=qualified_referrals,
        referral_earnings=referral_earnings,
        deposit_total=deposit_total,
        withdrawal_total=withdrawal_total,
        entry_fees=entry_fees,
        losses=losses,
        wins=wins
    )


@app.route("/tournament")
def tournament():
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    user = conn.execute(
        "SELECT id, username, phone, balance FROM users WHERE id = ?",
        (session["user_id"],)
    ).fetchone()
    conn.close()

    if not user:
        session.clear()
        return redirect(url_for("login"))

    return render_template("tournament.html", user=user)


@app.route("/tournament/join", methods=["POST"])
def join_tournament():
    if "user_id" not in session:
        return {"success": False, "message": "Please login first."}, 401

    user_id = session["user_id"]
    entry_fee = 200
    max_players = 16

    conn = get_db()

    try:
        conn.execute("BEGIN IMMEDIATE")

        user = conn.execute(
            "SELECT id, username, balance FROM users WHERE id = ?",
            (user_id,)
        ).fetchone()

        if not user:
            conn.rollback()
            return {"success": False, "message": "Account not found."}, 404

        tournament = conn.execute("""
            SELECT id, entry_fee, max_players, status
            FROM tournaments
            WHERE status = 'open'
            ORDER BY start_at ASC, id ASC
            LIMIT 1
        """).fetchone()

        if not tournament:
            conn.rollback()
            return {
                "success": False,
                "message": "No tournament is currently open."
            }, 400

        entry_fee = int(tournament["entry_fee"])
        max_players = 16

        already_joined = conn.execute("""
            SELECT id
            FROM tournament_players
            WHERE tournament_id = ?
              AND user_id = ?
        """, (tournament["id"], user_id)).fetchone()

        if already_joined:
            conn.rollback()
            return {
                "success": False,
                "message": "You are already registered for this tournament."
            }, 400

        human_count = conn.execute("""
            SELECT COUNT(*)
            FROM tournament_players
            WHERE tournament_id = ?
              AND is_ai = 0
        """, (tournament["id"],)).fetchone()[0]

        if human_count >= max_players:
            conn.rollback()
            return {
                "success": False,
                "message": "This tournament is already full."
            }, 400

        updated = conn.execute("""
            UPDATE users
            SET balance = balance - ?
            WHERE id = ?
              AND balance >= ?
        """, (entry_fee, user_id, entry_fee))

        if updated.rowcount != 1:
            conn.rollback()
            return {
                "success": False,
                "message": "Insufficient balance. You need ₦200 to enter."
            }, 400

        used_slots = {
            row[0]
            for row in conn.execute("""
                SELECT slot_number
                FROM tournament_players
                WHERE tournament_id = ?
            """, (tournament["id"],)).fetchall()
        }

        slot_number = next(
            slot for slot in range(1, max_players + 1)
            if slot not in used_slots
        )

        conn.execute("""
            INSERT INTO tournament_players
                (tournament_id, user_id, username, is_ai, slot_number, status)
            VALUES (?, ?, ?, 0, ?, 'active')
        """, (
            tournament["id"],
            user["id"],
            user["username"],
            slot_number
        ))

        conn.commit()

        return {
            "success": True,
            "message": "Tournament joined successfully.",
            "tournament_id": tournament["id"],
            "slot_number": slot_number,
            "entry_fee": entry_fee
        }

    except Exception as e:
        conn.rollback()
        return {
            "success": False,
            "message": "Unable to join tournament."
        }, 500

    finally:
        conn.close()




@app.route("/matchmaking")
def matchmaking():
    if "user_id" not in session:
        return redirect(url_for("login"))

    try:
        stake = int(request.args.get("stake", "30"))
    except ValueError:
        stake = 30

    allowed_stakes = {30, 50, 100, 200, 500, 1000}
    if stake not in allowed_stakes:
        stake = 30

    conn = get_db()
    user = conn.execute(
        "SELECT balance FROM users WHERE id = ?",
        (session["user_id"],)
    ).fetchone()
    conn.close()

    if not user:
        return redirect(url_for("login"))

    if user["balance"] < stake:
        return redirect(url_for("dashboard"))

    return render_template("matchmaking.html", stake=stake)


@app.route("/matchmaking/start", methods=["POST"])
def matchmaking_start():
    if "user_id" not in session:
        return {"success": False, "message": "Please login first."}, 401

    try:
        stake = int(request.form.get("stake", "30"))
    except ValueError:
        return {"success": False, "message": "Invalid stake."}, 400

    if stake not in {30, 50, 100, 200, 500, 1000}:
        return {"success": False, "message": "Invalid stake."}, 400

    user_id = session["user_id"]
    conn = get_db()

    # Lock the SQLite transaction before changing the queue.
    # This prevents two players from claiming the same opponent.
    conn.execute("BEGIN IMMEDIATE")

    conn.execute(
        "DELETE FROM game_queue WHERE user_id = ? AND status = 'waiting'",
        (user_id,)
    )

    opponent = conn.execute(
        """
        SELECT id, user_id
        FROM game_queue
        WHERE stake = ?
          AND status = 'waiting'
          AND user_id != ?
        ORDER BY id ASC
        LIMIT 1
        """,
        (stake, user_id)
    ).fetchone()

    if opponent:
        match_id = secrets.token_urlsafe(16)

        # Charge both players exactly once when the real match is created.
        payer1 = conn.execute(
            "UPDATE users SET balance = balance - ? WHERE id = ? AND balance >= ?",
            (stake, user_id, stake)
        )
        payer2 = conn.execute(
            "UPDATE users SET balance = balance - ? WHERE id = ? AND balance >= ?",
            (stake, opponent["user_id"], stake)
        )

        if payer1.rowcount != 1 or payer2.rowcount != 1:
            conn.rollback()
            conn.close()
            return {
                "success": False,
                "message": "One player no longer has enough balance."
            }, 400

        conn.execute(
            """
            UPDATE game_queue
            SET status = 'matched', match_id = ?, opponent_id = ?
            WHERE id = ?
            """,
            (match_id, user_id, opponent["id"])
        )

        new_row = conn.execute(
            """
            INSERT INTO game_queue
            (user_id, stake, status, match_id, opponent_id)
            VALUES (?, ?, 'matched', ?, ?)
            """,
            (user_id, stake, match_id, opponent["user_id"])
        )

        conn.commit()
        conn.close()

        session["match_id"] = match_id
        session["current_stake"] = stake
        session["game_active"] = True
        session["game_payout_claimed"] = False
        session["match_type"] = "player"
        session["match_opponent_id"] = opponent["user_id"]

        return {
            "success": True,
            "matched": True,
            "type": "player",
            "match_id": match_id
        }

    conn.execute(
        """
        INSERT INTO game_queue (user_id, stake, status)
        VALUES (?, ?, 'waiting')
        """,
        (user_id, stake)
    )

    queue_id = conn.execute(
        "SELECT last_insert_rowid()"
    ).fetchone()[0]

    conn.commit()
    conn.close()

    session["match_queue_id"] = queue_id
    session["current_stake"] = stake

    return {
        "success": True,
        "matched": False,
        "type": "waiting",
        "queue_id": queue_id
    }


@app.route("/matchmaking/status")
def matchmaking_status():
    if "user_id" not in session:
        return {"success": False, "message": "Not logged in"}, 401

    queue_id = session.get("match_queue_id")

    if not queue_id:
        return {"success": False, "message": "No matchmaking request."}, 400

    conn = get_db()
    row = conn.execute(
        """
        SELECT status, match_id, opponent_id, stake
        FROM game_queue
        WHERE id = ? AND user_id = ?
        """,
        (queue_id, session["user_id"])
    ).fetchone()
    conn.close()

    if not row:
        return {"success": False, "message": "Match not found."}, 404

    if row["status"] == "matched":
        session["match_id"] = row["match_id"]
        session["current_stake"] = row["stake"]
        session["game_active"] = True
        session["game_payout_claimed"] = False
        session["match_type"] = "player"
        session["match_opponent_id"] = row["opponent_id"]

        return {
            "success": True,
            "matched": True,
            "type": "player",
            "match_id": row["match_id"]
        }

    return {
        "success": True,
        "matched": False
    }


@app.route("/matchmaking/ai", methods=["POST"])
def matchmaking_ai():
    if "user_id" not in session:
        return {"success": False, "message": "Not logged in"}, 401

    queue_id = session.get("match_queue_id")

    if not queue_id:
        return {"success": False, "message": "No matchmaking request."}, 400

    user_id = session["user_id"]
    conn = get_db()

    conn.execute("BEGIN IMMEDIATE")

    row = conn.execute(
        """
        SELECT status, stake, match_id, opponent_id
        FROM game_queue
        WHERE id = ? AND user_id = ?
        """,
        (queue_id, user_id)
    ).fetchone()

    if not row:
        conn.rollback()
        conn.close()
        return {"success": False, "message": "Match not found."}, 404

    # A real player arrived during the countdown.
    if row["status"] == "matched":
        conn.commit()
        conn.close()

        session["match_id"] = row["match_id"]
        session["current_stake"] = row["stake"]
        session["game_active"] = True
        session["game_payout_claimed"] = False
        session["match_type"] = "player"
        session["match_opponent_id"] = row["opponent_id"]

        return {
            "success": True,
            "matched": True,
            "type": "player",
            "match_id": row["match_id"]
        }

    stake = row["stake"]

    charged = conn.execute(
        """
        UPDATE users
        SET balance = balance - ?
        WHERE id = ? AND balance >= ?
        """,
        (stake, user_id, stake)
    )

    if charged.rowcount != 1:
        conn.rollback()
        conn.close()
        return {
            "success": False,
            "message": "Insufficient balance."
        }, 400

    conn.execute(
        """
        UPDATE game_queue
        SET status = 'ai'
        WHERE id = ? AND user_id = ? AND status = 'waiting'
        """,
        (queue_id, user_id)
    )

    conn.commit()
    conn.close()

    session["match_id"] = queue_id
    session["current_stake"] = stake
    session["game_active"] = True
    session["game_payout_claimed"] = False
    session["match_type"] = "ai"
    session["match_opponent_id"] = None

    return {
        "success": True,
        "matched": False,
        "type": "ai",
        "match_id": queue_id
    }

@app.route("/normal-game")
def normal_game():
    if "user_id" not in session:
        return redirect(url_for("login"))

    try:
        stake = int(request.args.get("stake", "30"))
    except ValueError:
        stake = 30

    allowed_stakes = {30, 50, 100, 200, 500, 1000}
    if stake not in allowed_stakes:
        stake = 30

    conn = get_db()
    user = conn.execute(
        "SELECT id, username, phone, balance FROM users WHERE id = ?",
        (session["user_id"],)
    ).fetchone()
    conn.close()

    if not user:
        session.clear()
        return redirect(url_for("login"))

    if user["balance"] < stake:
        return f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Insufficient Balance - WHOT Arena</title>
            <style>
                * {{
                    box-sizing: border-box;
                }}

                body {{
                    margin: 0;
                    min-height: 100vh;
                    font-family: Arial, Helvetica, sans-serif;
                    background:
                        radial-gradient(circle at 20% 10%, #173b9b 0, transparent 30%),
                        radial-gradient(circle at 90% 80%, #0b5bd3 0, transparent 28%),
                        linear-gradient(145deg, #020b2d, #061957 55%, #020a28);
                    color: white;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    padding: 22px 14px;
                    overflow-x: hidden;
                }}

                .page {{
                    width: 100%;
                    max-width: 520px;
                    text-align: center;
                }}

                .brand {{
                    margin-bottom: 18px;
                }}

                .logo {{
                    display: inline-block;
                    font-size: 32px;
                    font-weight: 1000;
                    letter-spacing: 1px;
                    color: #ffd21c;
                    text-shadow: 0 3px 18px rgba(255,210,28,.35);
                }}

                .arena {{
                    display: block;
                    color: #fff;
                    font-size: 17px;
                    font-weight: 900;
                    letter-spacing: 6px;
                    margin-top: -2px;
                }}

                .tagline {{
                    color: #8fc9ff;
                    font-size: 11px;
                    letter-spacing: 3px;
                    margin-top: 8px;
                }}

                .card {{
                    background: rgba(5, 20, 70, .94);
                    border: 1px solid #126bff;
                    border-radius: 28px;
                    padding: 30px 20px 24px;
                    box-shadow:
                        0 0 35px rgba(0, 102, 255, .28),
                        inset 0 0 30px rgba(0, 72, 180, .12);
                }}

                .wallet {{
                    width: 92px;
                    height: 92px;
                    margin: 0 auto 20px;
                    border-radius: 50%;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    font-size: 43px;
                    background: linear-gradient(145deg, #087cff, #0638b5);
                    border: 4px solid #1fa9ff;
                    box-shadow: 0 0 30px rgba(0, 157, 255, .5);
                    position: relative;
                }}

                .alert {{
                    position: absolute;
                    right: -3px;
                    bottom: -4px;
                    width: 31px;
                    height: 31px;
                    border-radius: 50%;
                    background: #ff3b3b;
                    color: white;
                    border: 3px solid #07194f;
                    font-size: 19px;
                    font-weight: 900;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                }}

                h1 {{
                    margin: 0;
                    font-size: 34px;
                    line-height: 1.05;
                    font-weight: 1000;
                }}

                h1 span {{
                    color: #ffd21c;
                }}

                .description {{
                    color: #d6e5ff;
                    line-height: 1.6;
                    font-size: 15px;
                    margin: 16px auto 24px;
                    max-width: 390px;
                }}

                .amounts {{
                    display: grid;
                    grid-template-columns: 1fr 1fr;
                    border: 1px solid #145ed8;
                    border-radius: 18px;
                    margin-bottom: 24px;
                    overflow: hidden;
                    background: rgba(3, 18, 67, .8);
                }}

                .amount {{
                    padding: 17px 10px;
                }}

                .amount + .amount {{
                    border-left: 1px solid #145ed8;
                }}

                .label {{
                    color: #a8c7f7;
                    font-size: 12px;
                    margin-bottom: 7px;
                }}

                .value {{
                    color: #ffd21c;
                    font-size: 23px;
                    font-weight: 1000;
                }}

                .button {{
                    display: block;
                    width: 100%;
                    padding: 17px 20px;
                    border-radius: 16px;
                    text-decoration: none;
                    font-weight: 900;
                    font-size: 16px;
                    margin-top: 12px;
                    transition: transform .15s ease;
                }}

                .button:active {{
                    transform: scale(.98);
                }}

                .primary {{
                    background: linear-gradient(135deg, #ffd21c, #ffad00);
                    color: #06143f;
                    box-shadow: 0 8px 22px rgba(255, 193, 7, .22);
                }}

                .secondary {{
                    background: transparent;
                    color: #dceaff;
                    border: 1px solid #1593ff;
                }}

                .footer {{
                    margin-top: 22px;
                    color: #8fb2e8;
                    font-size: 12px;
                    line-height: 1.8;
                }}

                .secure {{
                    color: #70e69a;
                    font-weight: 700;
                }}

                @media (max-width: 380px) {{
                    h1 {{
                        font-size: 29px;
                    }}

                    .card {{
                        padding: 25px 15px 20px;
                    }}
                }}
            </style>
        </head>

        <body>
            <main class="page">
                <div class="brand">
                    <div class="logo">WHOT</div>
                    <div class="arena">ARENA</div>
                    <div class="tagline">PLAY • WIN • EARN</div>
                </div>

                <section class="card">
                    <div class="wallet">
                        💰
                        <div class="alert">!</div>
                    </div>

                    <h1>Insufficient <span>Balance</span></h1>

                    <p class="description">
                        Your current balance is <strong>₦{user["balance"]:,}</strong>,
                        but this game requires <strong>₦{stake:,}</strong> to enter.
                    </p>

                    <div class="amounts">
                        <div class="amount">
                            <div class="label">YOUR BALANCE</div>
                            <div class="value">₦{user["balance"]:,}</div>
                        </div>

                        <div class="amount">
                            <div class="label">GAME ENTRY</div>
                            <div class="value">₦{stake:,}</div>
                        </div>
                    </div>

                    <a class="button primary" href="/dashboard">
                        🏠 &nbsp; Return to Dashboard
                    </a>

                    <a class="button secondary" href="/dashboard">
                        💳 &nbsp; Go to Wallet
                    </a>

                    <div class="footer">
                        <div class="secure">🛡 Secure • Fast • Trusted</div>
                        <div>Play More • Win More • Grow Your Account</div>
                        <div>© WHOT Arena</div>
                    </div>
                </section>
            </main>
        </body>
        </html>
        """


    # Create a unique game session.
    # Refreshing the same game will NOT deduct the stake again.
    game_id = session.get("game_id")
    game_stake = session.get("current_stake")

    if not game_id or game_stake != stake or not session.get("game_active"):
        game_id = secrets.token_urlsafe(16)

        conn = get_db()
        updated = conn.execute(
            "UPDATE users SET balance = balance - ? WHERE id = ? AND balance >= ?",
            (stake, session["user_id"], stake)
        )
        conn.commit()
        conn.close()

        if updated.rowcount != 1:
            return "Unable to start game: insufficient balance."

        session["game_id"] = game_id
        session["current_stake"] = stake
        session["game_active"] = True
        session["game_payout_claimed"] = False

        conn = get_db()
        conn.execute(
            """
            INSERT INTO game_history
                (user_id, game_id, stake, outcome, winnings)
            VALUES (?, ?, ?, 'active', 0)
            """,
            (session["user_id"], game_id, stake)
        )
        conn.commit()
        conn.close()

    return render_template("normal_game.html", user=user, stake=stake)


@app.route("/game-win")
def game_win():
    if "user_id" not in session:
        return {"success": False, "message": "Not logged in"}, 401

    if not session.get("game_active"):
        return {"success": False, "message": "No active game"}

    stake = session.get("current_stake")

    if not stake:
        return {"success": False, "message": "Game stake not found"}, 400

    if session.get("game_payout_claimed"):
        return {"success": False, "message": "Payout already claimed"}

    payout_table = {
        30: 50,
        50: 85,
        100: 180,
        200: 350,
        500: 850,
        1000: 1850
    }

    winnings = payout_table.get(stake, 0)

    if winnings <= 0:
        return {"success": False, "message": "Invalid stake"}, 400

    conn = get_db()

    updated = conn.execute(
        "UPDATE users SET balance = balance + ? WHERE id = ?",
        (winnings, session["user_id"])
    )

    if updated.rowcount == 1:
        conn.execute(
            """
            UPDATE game_history
            SET outcome = 'win',
                winnings = ?,
                completed_at = CURRENT_TIMESTAMP
            WHERE game_id = ?
              AND user_id = ?
              AND outcome = 'active'
            """,
            (winnings, session.get("game_id"), session["user_id"])
        )

    conn.commit()
    conn.close()

    if updated.rowcount != 1:
        return {"success": False, "message": "Payout failed"}, 500

    session["game_payout_claimed"] = True
    session["game_active"] = False

    return {
        "success": True,
        "winnings": winnings
    }


@app.route("/game-exit")
def game_exit():
    if "user_id" not in session:
        return redirect(url_for("login"))

    # Exiting an active AI game forfeits the already-deducted stake.
    if session.get("game_active"):
        conn = get_db()
        conn.execute(
            """
            UPDATE game_history
            SET outcome = 'loss',
                winnings = 0,
                completed_at = CURRENT_TIMESTAMP
            WHERE game_id = ?
              AND user_id = ?
              AND outcome = 'active'
            """,
            (session.get("game_id"), session["user_id"])
        )
        conn.commit()
        conn.close()

        session["game_active"] = False
        session["game_payout_claimed"] = True

    return redirect(url_for("dashboard"))


@app.route("/deposit", methods=["POST"])
def deposit():
    if "user_id" not in session:
        return {"success": False, "message": "Please login first."}, 401

    try:
        amount = int(request.form.get("amount", "0"))
    except ValueError:
        amount = 0

    sender_name = request.form.get("sender_name", "").strip()

    if amount < 100:
        return {"success": False, "message": "Minimum deposit is ₦100."}, 400

    if not sender_name:
        return {"success": False, "message": "Sender name is required."}, 400

    conn = get_db()
    conn.execute(
        """
        INSERT INTO deposits (user_id, amount, sender_name, status)
        VALUES (?, ?, ?, 'pending')
        """,
        (session["user_id"], amount, sender_name)
    )
    conn.commit()
    conn.close()

    return {
        "success": True,
        "message": "Deposit submitted successfully and is awaiting verification."
    }


@app.route("/admin/deposit/<int:deposit_id>/approve", methods=["POST"])
def approve_deposit(deposit_id):
    if session.get("admin_logged_in") != True:
        return redirect(url_for("admin_login"))

    conn = get_db()

    deposit = conn.execute(
        "SELECT id, user_id, amount, status FROM deposits WHERE id = ?",
        (deposit_id,)
    ).fetchone()

    if not deposit:
        conn.close()
        return "Deposit not found.", 404

    if deposit["status"] != "pending":
        conn.close()
        return redirect(url_for("admin_dashboard"))

    updated = conn.execute(
        """
        UPDATE deposits
        SET status = 'approved',
            reviewed_at = CURRENT_TIMESTAMP
        WHERE id = ? AND status = 'pending'
        """,
        (deposit_id,)
    )

    if updated.rowcount == 1:
        conn.execute(
            "UPDATE users SET balance = balance + ? WHERE id = ?",
            (deposit["amount"], deposit["user_id"])
        )

    conn.commit()
    conn.close()

    return redirect(url_for("admin_dashboard"))


@app.route("/admin/deposit/<int:deposit_id>/reject", methods=["POST"])
def reject_deposit(deposit_id):
    if session.get("admin_logged_in") != True:
        return redirect(url_for("admin_login"))

    reason = request.form.get("reason", "").strip()

    if not reason:
        reason = "Deposit rejected by administrator."

    conn = get_db()

    conn.execute(
        """
        UPDATE deposits
        SET status = 'rejected',
            rejection_reason = ?,
            reviewed_at = CURRENT_TIMESTAMP
        WHERE id = ? AND status = 'pending'
        """,
        (reason, deposit_id)
    )

    conn.commit()
    conn.close()

    return redirect(url_for("admin_dashboard"))

@app.route("/withdraw", methods=["POST"])
def withdraw():
    if "user_id" not in session:
        return {"success": False, "message": "Please login first."}, 401

    bank_name = request.form.get("bank_name", "").strip()
    account_name = request.form.get("account_name", "").strip()
    account_number = request.form.get("account_number", "").strip()

    try:
        amount = int(request.form.get("amount", "0"))
    except ValueError:
        amount = 0

    if not bank_name:
        return {"success": False, "message": "Bank name is required."}, 400

    if not account_name:
        return {"success": False, "message": "Account name is required."}, 400

    if len(account_number) != 10 or not account_number.isdigit():
        return {"success": False, "message": "Please enter a valid 10-digit account number."}, 400

    if amount < 200:
        return {"success": False, "message": "Minimum withdrawal is ₦200."}, 400

    conn = get_db()

    user = conn.execute(
        "SELECT balance FROM users WHERE id = ?",
        (session["user_id"],)
    ).fetchone()

    if not user:
        conn.close()
        return {"success": False, "message": "User account not found."}, 404

    if user["balance"] < amount:
        conn.close()
        return {"success": False, "message": "Insufficient balance."}, 400

    # Reserve the requested amount immediately.
    conn.execute(
        "UPDATE users SET balance = balance - ? WHERE id = ?",
        (amount, session["user_id"])
    )

    conn.execute(
        """
        INSERT INTO withdrawals
        (user_id, bank_name, account_name, account_number, amount, status)
        VALUES (?, ?, ?, ?, ?, 'pending')
        """,
        (
            session["user_id"],
            bank_name,
            account_name,
            account_number,
            amount
        )
    )

    conn.commit()
    conn.close()

    return {
        "success": True,
        "message": "Withdrawal submitted successfully and is awaiting admin approval."
    }


@app.route("/admin/withdraw/<int:withdrawal_id>/approve", methods=["POST"])
def admin_approve_withdrawal(withdrawal_id):
    if session.get("admin_logged_in") != True:
        return redirect(url_for("admin_login"))

    conn = get_db()

    withdrawal = conn.execute(
        "SELECT id, user_id, amount, status FROM withdrawals WHERE id = ?",
        (withdrawal_id,)
    ).fetchone()

    if withdrawal and withdrawal["status"] == "pending":
        conn.execute(
            """
            UPDATE withdrawals
            SET status = 'approved'
            WHERE id = ? AND status = 'pending'
            """,
            (withdrawal_id,)
        )
        conn.commit()

    conn.close()
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/withdraw/<int:withdrawal_id>/reject", methods=["POST"])
def admin_reject_withdrawal(withdrawal_id):
    if session.get("admin_logged_in") != True:
        return redirect(url_for("admin_login"))

    reason = request.form.get("reason", "Withdrawal rejected by admin.").strip()

    conn = get_db()

    withdrawal = conn.execute(
        """
        SELECT id, user_id, amount, status
        FROM withdrawals
        WHERE id = ?
        """,
        (withdrawal_id,)
    ).fetchone()

    if withdrawal and withdrawal["status"] == "pending":
        conn.execute(
            """
            UPDATE users
            SET balance = balance + ?
            WHERE id = ?
            """,
            (withdrawal["amount"], withdrawal["user_id"])
        )

        conn.execute(
            """
            UPDATE withdrawals
            SET status = 'rejected',
                rejection_reason = ?
            WHERE id = ? AND status = 'pending'
            """,
            (reason, withdrawal_id)
        )

        conn.commit()

    conn.close()
    return redirect(url_for("admin_dashboard"))



@app.route("/whatsapp")
def whatsapp_group():
    return redirect("https://chat.whatsapp.com/F0AmqD0RlYC5EqsMpeGvRz")

@app.route("/admin")
def admin_dashboard():
    if session.get("admin_logged_in") != True:
        return redirect(url_for("admin_login"))

    conn = get_db()

    total_users = conn.execute(
        "SELECT COUNT(*) FROM users"
    ).fetchone()[0]

    total_deposits = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM deposits WHERE status = 'approved'"
    ).fetchone()[0]

    total_withdrawals = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM withdrawals WHERE status = 'approved'"
    ).fetchone()[0]

    deposits = conn.execute("""
        SELECT deposits.id, deposits.amount, deposits.sender_name,
               deposits.status, deposits.rejection_reason,
               deposits.created_at, users.username
        FROM deposits
        JOIN users ON users.id = deposits.user_id
        ORDER BY deposits.id DESC
        LIMIT 50
    """).fetchall()

    withdrawals = conn.execute("""
        SELECT withdrawals.id, withdrawals.amount, withdrawals.status,
               withdrawals.bank_name, withdrawals.account_name,
               withdrawals.account_number, withdrawals.rejection_reason,
               withdrawals.created_at, users.username
        FROM withdrawals
        JOIN users ON users.id = withdrawals.user_id
        ORDER BY withdrawals.id DESC
        LIMIT 50
    """).fetchall()

    conn.close()

    return render_template(
        "admin/dashboard.html",
        total_users=total_users,
        total_deposits=total_deposits,
        total_withdrawals=total_withdrawals,
        deposits=deposits,
        withdrawals=withdrawals
    )


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")

        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session["admin_logged_in"] = True
            return redirect(url_for("admin_dashboard"))

        return render_template(
            "admin/login.html",
            error="Invalid admin username or password."
        )

    return render_template("admin/login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_logged_in", None)
    return redirect(url_for("admin_login"))


@app.route("/history")
def history():
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    user_id = session["user_id"]
    events = []

    def columns(table):
        try:
            return [x["name"] for x in conn.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()]
        except Exception:
            return []

    def pick(cols, names):
        for name in names:
            if name in cols:
                return name
        return None

    def money(value):
        try:
            return abs(float(value or 0))
        except Exception:
            return 0

    # ---------- DEPOSITS ----------
    dcols = columns("deposits")

    if dcols:
        uid = pick(dcols, ["user_id", "userid"])
        amount = pick(dcols, ["amount", "deposit_amount"])
        status = pick(dcols, ["status", "deposit_status"])
        created = pick(dcols, ["created_at", "date", "timestamp"])

        if uid and amount:
            rows = conn.execute(
                f"SELECT * FROM deposits WHERE {uid}=? "
                f"ORDER BY {created or 'rowid'} DESC LIMIT 100",
                (user_id,)
            ).fetchall()

            for r in rows:
                st = str(r[status]).lower() if status else "pending"
                label = "Approved" if st == "approved" else (
                    "Rejected" if st in ("rejected", "declined") else "Pending"
                )

                events.append({
                    "type": "deposit",
                    "title": "Deposit",
                    "amount": money(r[amount]),
                    "sign": "+",
                    "status": label,
                    "date": r[created] if created else "",
                    "sort": str(r[created] if created else "")
                })

    # ---------- WITHDRAWALS ----------
    wcols = columns("withdrawals")

    if wcols:
        uid = pick(wcols, ["user_id", "userid"])
        amount = pick(wcols, ["amount", "withdrawal_amount"])
        status = pick(wcols, ["status", "withdrawal_status"])
        created = pick(wcols, ["created_at", "date", "timestamp"])

        if uid and amount:
            rows = conn.execute(
                f"SELECT * FROM withdrawals WHERE {uid}=? "
                f"ORDER BY {created or 'rowid'} DESC LIMIT 100",
                (user_id,)
            ).fetchall()

            for r in rows:
                st = str(r[status]).lower() if status else "pending"
                label = "Approved" if st == "approved" else (
                    "Rejected" if st in ("rejected", "declined") else "Pending"
                )

                events.append({
                    "type": "withdrawal",
                    "title": "Withdrawal",
                    "amount": money(r[amount]),
                    "sign": "-",
                    "status": label,
                    "date": r[created] if created else "",
                    "sort": str(r[created] if created else "")
                })

    # ---------- GAME HISTORY ----------
    gcols = columns("game_history")

    if gcols:
        uid = pick(gcols, ["user_id", "userid"])
        stake = pick(gcols, [
            "stake", "entry_fee", "entry_amount",
            "game_entry", "amount", "bet"
        ])
        payout = pick(gcols, [
            "payout", "winnings", "win_amount",
            "prize", "reward"
        ])
        result = pick(gcols, [
            "result", "outcome", "status", "game_result"
        ])
        created = pick(gcols, [
            "created_at", "played_at", "date", "timestamp"
        ])

        if uid:
            rows = conn.execute(
                f"SELECT * FROM game_history WHERE {uid}=? "
                f"ORDER BY {created or 'rowid'} DESC LIMIT 100",
                (user_id,)
            ).fetchall()

            for r in rows:
                stake_value = money(r[stake]) if stake else 0
                payout_value = money(r[payout]) if payout else 0
                outcome = str(r[result]).lower() if result else ""

                date_value = r[created] if created else ""
                date_text = str(date_value)

                # Entry is shown separately when a stake exists.
                if stake_value > 0:
                    events.append({
                        "type": "game-entry",
                        "title": "Game Entry",
                        "amount": stake_value,
                        "sign": "-",
                        "status": "Completed",
                        "date": date_value,
                        "sort": date_text
                    })

                # Winning is shown separately when a payout exists.
                if payout_value > 0:
                    events.append({
                        "type": "game-win",
                        "title": "Game Win",
                        "amount": payout_value,
                        "sign": "+",
                        "status": "Won",
                        "date": date_value,
                        "sort": date_text
                    })

                # If the table has no separate payout but clearly records a win,
                # show the recorded amount as the winning amount.
                if (
                    payout_value == 0
                    and stake_value > 0
                    and any(x in outcome for x in ["win", "won", "victory"])
                ):
                    events.append({
                        "type": "game-win",
                        "title": "Game Win",
                        "amount": stake_value,
                        "sign": "+",
                        "status": "Won",
                        "date": date_value,
                        "sort": date_text
                    })

    conn.close()

    events.sort(key=lambda x: x["sort"], reverse=True)

    return render_template(
        "history.html",
        user=session.get("username", ""),
        events=events
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))




# ============================================================
# ============================================================
# WHOT ARENA — 30 MINUTE ALTERNATING TOURNAMENTS
# ============================================================

TOURNAMENT_ENTRY = 200
TURN_SECONDS = 20
READY_SECONDS = 30
TOURNAMENT_INTERVAL_MINUTES = 30

TOURNAMENT_TYPES = [
    {
        "players": 4,
        "first": 450,
        "second": 300,
        "fee": 50,
    },
    {
        "players": 8,
        "first": 900,
        "second": 500,
        "fee": 200,
    },
]

def tournament_now():
    from datetime import datetime, timedelta
    return datetime.utcnow() + timedelta(hours=1)


def ensure_tournament_columns():
    conn = get_db()

    cols = {
        r["name"]
        for r in conn.execute("PRAGMA table_info(tournaments)").fetchall()
    }

    additions = {
        "prize_first": "INTEGER NOT NULL DEFAULT 450",
        "prize_second": "INTEGER NOT NULL DEFAULT 300",
        "prize_third": "INTEGER NOT NULL DEFAULT 0",
        "arena_fee": "INTEGER NOT NULL DEFAULT 50",
        "current_round": "INTEGER NOT NULL DEFAULT 0",
        "round_deadline": "TIMESTAMP",
        "winner_paid": "INTEGER NOT NULL DEFAULT 0",
    }

    for name, definition in additions.items():
        if name not in cols:
            conn.execute(
                f"ALTER TABLE tournaments ADD COLUMN {name} {definition}"
            )

    match_cols = {
        r["name"]
        for r in conn.execute(
            "PRAGMA table_info(tournament_matches)"
        ).fetchall()
    }

    match_additions = {
        "turn_player_id": "INTEGER",
        "turn_deadline": "TIMESTAMP",
        "round_started_at": "TIMESTAMP",
        "ready_deadline": "TIMESTAMP",
        "p1_ready": "INTEGER NOT NULL DEFAULT 0",
        "p2_ready": "INTEGER NOT NULL DEFAULT 0",
        "match_started_at": "TIMESTAMP",
    }

    for name, definition in match_additions.items():
        if name not in match_cols:
            conn.execute(
                f"ALTER TABLE tournament_matches ADD COLUMN {name} {definition}"
            )

    conn.commit()
    conn.close()


def create_next_tournament():
    from datetime import timedelta

    ensure_tournament_columns()
    conn = get_db()
    now = tournament_now()

    active = conn.execute("""
        SELECT id
        FROM tournaments
        WHERE status IN ('open','running')
        LIMIT 1
    """).fetchone()

    if active:
        conn.close()
        return

    latest = conn.execute("""
        SELECT id, start_at, max_players
        FROM tournaments
        ORDER BY id DESC
        LIMIT 1
    """).fetchone()

    if latest:
        tournament_type = (
            1 if latest["max_players"] == 4 else 0
        )

        start_at = now + timedelta(minutes=TOURNAMENT_INTERVAL_MINUTES)
    else:
        tournament_type = 0
        start_at = now

    config = TOURNAMENT_TYPES[tournament_type]

    conn.execute("""
        INSERT INTO tournaments
        (
            entry_fee,
            prize,
            max_players,
            min_humans,
            status,
            start_at,
            prize_first,
            prize_second,
            prize_third,
            arena_fee,
            current_round,
            winner_paid
        )
        VALUES (?, ?, ?, 2, 'open', ?, ?, ?, 0, ?, 0, 0)
    """, (
        TOURNAMENT_ENTRY,
        config["first"],
        config["players"],
        start_at.isoformat(sep=" "),
        config["first"],
        config["second"],
        config["fee"],
    ))

    conn.commit()
    conn.close()


def fill_tournament_with_ai(conn, tournament_id):
    tournament = conn.execute("""
        SELECT max_players
        FROM tournaments
        WHERE id=?
    """, (tournament_id,)).fetchone()

    if not tournament:
        return

    max_players = tournament["max_players"]

    used = {
        r["slot_number"]
        for r in conn.execute("""
            SELECT slot_number
            FROM tournament_players
            WHERE tournament_id=?
        """, (tournament_id,)).fetchall()
    }

    for slot in range(1, max_players + 1):
        if slot in used:
            continue

        conn.execute("""
            INSERT INTO tournament_players
            (
                tournament_id,
                user_id,
                username,
                is_ai,
                slot_number,
                status
            )
            VALUES (?, NULL, ?, 1, ?, 'active')
        """, (
            tournament_id,
            f"Arena AI {slot}",
            slot,
        ))


def create_round(conn, tournament_id, round_number, players):
    for i in range(0, len(players), 2):
        p1 = players[i]
        p2 = players[i + 1]
        match_no = (i // 2) + 1

        conn.execute("""
            INSERT INTO tournament_matches
            (
                tournament_id,
                round_number,
                match_number,
                player1_id,
                player2_id,
                status,
                ready_deadline,
                p1_ready,
                p2_ready
            )
            VALUES
            (?, ?, ?, ?, ?, 'waiting',
             datetime('now', '+30 seconds'), 0, 0)
        """, (
            tournament_id,
            round_number,
            match_no,
            p1,
            p2,
        ))


def create_first_round(conn, tournament_id):
    exists = conn.execute("""
        SELECT id
        FROM tournament_matches
        WHERE tournament_id=?
        LIMIT 1
    """, (tournament_id,)).fetchone()

    if exists:
        return

    players = conn.execute("""
        SELECT id
        FROM tournament_players
        WHERE tournament_id=?
        ORDER BY slot_number
    """, (tournament_id,)).fetchall()

    ids = [p["id"] for p in players]

    if len(ids) not in (4, 8):
        return

    create_round(conn, tournament_id, 1, ids)


def start_due_tournament():
    ensure_tournament_columns()

    conn = get_db()
    now = tournament_now()

    tournament = conn.execute("""
        SELECT *
        FROM tournaments
        WHERE status='open'
        AND start_at <= ?
        ORDER BY id
        LIMIT 1
    """, (now.isoformat(sep=" "),)).fetchone()

    if not tournament:
        conn.close()
        return

    tid = tournament["id"]

    humans = conn.execute("""
        SELECT user_id
        FROM tournament_players
        WHERE tournament_id=?
        AND is_ai=0
    """, (tid,)).fetchall()

    if len(humans) < 2:
        for player in humans:
            conn.execute("""
                UPDATE users
                SET balance=balance+?
                WHERE id=?
            """, (TOURNAMENT_ENTRY, player["user_id"]))

        conn.execute("""
            UPDATE tournaments
            SET status='cancelled',
                completed_at=CURRENT_TIMESTAMP
            WHERE id=?
        """, (tid,))

        conn.commit()
        conn.close()
        return

    fill_tournament_with_ai(conn, tid)
    create_first_round(conn, tid)

    conn.execute("""
        UPDATE tournaments
        SET status='running',
            started_at=CURRENT_TIMESTAMP,
            current_round=1
        WHERE id=?
    """, (tid,))

    conn.commit()
    conn.close()


@app.route("/tournament/ready", methods=["POST"])
def tournament_ready():
    if "user_id" not in session:
        return {"success": False, "message": "Login required."}, 401

    ensure_tournament_columns()
    conn = get_db()

    match = conn.execute("""
        SELECT m.*, p1.user_id AS p1_user,
               p2.user_id AS p2_user
        FROM tournament_matches m
        JOIN tournament_players p1 ON p1.id=m.player1_id
        JOIN tournament_players p2 ON p2.id=m.player2_id
        WHERE m.status='waiting'
        AND (
            p1.user_id=? OR p2.user_id=?
        )
        ORDER BY m.id DESC
        LIMIT 1
    """, (
        session["user_id"],
        session["user_id"],
    )).fetchone()

    if not match:
        conn.close()
        return {"success": False, "message": "No waiting match."}

    if match["p1_user"] == session["user_id"]:
        conn.execute("""
            UPDATE tournament_matches
            SET p1_ready=1
            WHERE id=?
        """, (match["id"],))
    else:
        conn.execute("""
            UPDATE tournament_matches
            SET p2_ready=1
            WHERE id=?
        """, (match["id"],))

    conn.commit()

    updated = conn.execute("""
        SELECT *
        FROM tournament_matches
        WHERE id=?
    """, (match["id"],)).fetchone()

    if updated["p1_ready"] and updated["p2_ready"]:
        conn.execute("""
            UPDATE tournament_matches
            SET status='active',
                match_started_at=CURRENT_TIMESTAMP,
                turn_player_id=player1_id,
                turn_deadline=datetime('now', '+20 seconds')
            WHERE id=?
        """, (match["id"],))

        conn.commit()

    conn.close()

    return {
        "success": True,
        "message": "Ready status recorded."
    }


def tournament_timeout_check():
    ensure_tournament_columns()
    conn = get_db()

    # Players who don't enter the match within 30 seconds.
    waiting = conn.execute("""
        SELECT *
        FROM tournament_matches
        WHERE status='waiting'
        AND ready_deadline IS NOT NULL
        AND datetime(ready_deadline) <= datetime('now')
    """).fetchall()

    for match in waiting:
        p1_ready = bool(match["p1_ready"])
        p2_ready = bool(match["p2_ready"])

        if p1_ready and not p2_ready:
            winner = match["player1_id"]
            loser = match["player2_id"]
        elif p2_ready and not p1_ready:
            winner = match["player2_id"]
            loser = match["player1_id"]
        else:
            winner = None
            loser = None

        conn.execute("""
            UPDATE tournament_matches
            SET status='completed',
                winner_id=?,
                completed_at=CURRENT_TIMESTAMP
            WHERE id=?
            AND status='waiting'
        """, (winner, match["id"]))

        if loser:
            conn.execute("""
                UPDATE tournament_players
                SET status='eliminated'
                WHERE id=?
            """, (loser,))

    # AI automatically gets its turn.
    ai_matches = conn.execute("""
        SELECT m.*, p.is_ai
        FROM tournament_matches m
        JOIN tournament_players p
          ON p.id=m.turn_player_id
        WHERE m.status='active'
        AND p.is_ai=1
    """).fetchall()

    for match in ai_matches:
        conn.execute("""
            UPDATE tournament_matches
            SET turn_player_id =
                CASE
                    WHEN turn_player_id=player1_id
                    THEN player2_id
                    ELSE player1_id
                END,
                turn_deadline=datetime('now', '+20 seconds')
            WHERE id=?
        """, (match["id"],))

    # Human turn timeout.
    expired = conn.execute("""
        SELECT *
        FROM tournament_matches
        WHERE status='active'
        AND turn_deadline IS NOT NULL
        AND datetime(turn_deadline) <= datetime('now')
    """).fetchall()

    for match in expired:
        loser = match["turn_player_id"]

        winner = (
            match["player2_id"]
            if loser == match["player1_id"]
            else match["player1_id"]
        )

        conn.execute("""
            UPDATE tournament_matches
            SET winner_id=?,
                status='completed',
                completed_at=CURRENT_TIMESTAMP
            WHERE id=?
            AND status='active'
        """, (winner, match["id"]))

        conn.execute("""
            UPDATE tournament_players
            SET status='eliminated'
            WHERE id=?
        """, (loser,))

    conn.commit()
    conn.close()


def tournament_advance_rounds():
    ensure_tournament_columns()
    conn = get_db()

    tournaments = conn.execute("""
        SELECT *
        FROM tournaments
        WHERE status='running'
    """).fetchall()

    for tournament in tournaments:
        tid = tournament["id"]
        rnd = int(tournament["current_round"] or 1)

        matches = conn.execute("""
            SELECT *
            FROM tournament_matches
            WHERE tournament_id=?
            AND round_number=?
            ORDER BY match_number
        """, (tid, rnd)).fetchall()

        if not matches or any(m["status"] != "completed" for m in matches):
            continue

        winners = [
            m["winner_id"]
            for m in matches
            if m["winner_id"]
        ]

        if len(winners) == 1:
            winner_id = winners[0]

            conn.execute("""
                UPDATE users
                SET balance = balance + ?
                WHERE id = (
                    SELECT user_id
                    FROM tournament_players
                    WHERE id=?
                )
            """, (
                tournament["prize_first"],
                winner_id,
            ))

            # Find final loser for second prize.
            final_match = matches[0]
            loser_id = (
                final_match["player1_id"]
                if final_match["player1_id"] != winner_id
                else final_match["player2_id"]
            )

            conn.execute("""
                UPDATE users
                SET balance = balance + ?
                WHERE id = (
                    SELECT user_id
                    FROM tournament_players
                    WHERE id=?
                    AND user_id IS NOT NULL
                )
            """, (
                tournament["prize_second"],
                loser_id,
            ))

            conn.execute("""
                UPDATE tournaments
                SET status='completed',
                    completed_at=CURRENT_TIMESTAMP,
                    winner_paid=1
                WHERE id=?
            """, (tid,))

            continue

        if len(winners) < 2:
            continue

        create_round(
            conn,
            tid,
            rnd + 1,
            winners
        )

        conn.execute("""
            UPDATE tournaments
            SET current_round=?
            WHERE id=?
        """, (rnd + 1, tid))

    conn.commit()
    conn.close()


def run_tournament_maintenance():
    try:
        ensure_tournament_columns()
        create_next_tournament()
        start_due_tournament()
        tournament_timeout_check()
        tournament_advance_rounds()
    except Exception as e:
        print("TOURNAMENT MAINTENANCE ERROR:", e)


def tournament_scheduler():
    import time

    while True:
        run_tournament_maintenance()
        time.sleep(5)


def start_tournament_scheduler():
    import threading

    thread = threading.Thread(
        target=tournament_scheduler,
        daemon=True
    )
    thread.start()


@app.route("/tournament/status")
def tournament_status():
    ensure_tournament_columns()
    if "user_id" not in session:
        return {"success": False, "message": "Please login first."}, 401

    conn = get_db()

    tournament = conn.execute("""
        SELECT *
        FROM tournaments
        WHERE status IN ('open','running')
        ORDER BY id DESC
        LIMIT 1
    """).fetchone()

    if not tournament:
        conn.close()
        return {"success": True, "tournament": None, "players": [], "matches": []}

    players = conn.execute("""
        SELECT id, username, is_ai, slot_number, status
        FROM tournament_players
        WHERE tournament_id=?
        ORDER BY slot_number
    """, (tournament["id"],)).fetchall()

    matches = conn.execute("""
        SELECT
            m.id,
            m.round_number,
            m.match_number,
            m.status,
            m.turn_player_id,
            m.turn_deadline,
            m.winner_id,
            p1.username AS player1,
            p2.username AS player2,
            p1.is_ai AS player1_ai,
            p2.is_ai AS player2_ai
        FROM tournament_matches m
        LEFT JOIN tournament_players p1 ON p1.id=m.player1_id
        LEFT JOIN tournament_players p2 ON p2.id=m.player2_id
        WHERE m.tournament_id=?
        ORDER BY m.round_number, m.match_number
    """, (tournament["id"],)).fetchall()

    conn.close()

    return {
        "success": True,
        "tournament": {
            "id": tournament["id"],
            "status": tournament["status"],
            "start_at": tournament["start_at"],
            "current_round": tournament["current_round"],
            "entry_fee": tournament["entry_fee"],
            "first": tournament["prize_first"],
            "second": tournament["prize_second"],
            "third": tournament["prize_third"],
            "arena_fee": tournament["arena_fee"],
            "max_players": tournament["max_players"]
        },
        "players": [dict(x) for x in players],
        "matches": [dict(x) for x in matches]
    }


@app.route("/tournament/move", methods=["POST"])
def tournament_move():
    if "user_id" not in session:
        return {"success": False, "message": "Please login first."}, 401

    try:
        match_id = int(request.form.get("match_id", "0"))
    except Exception:
        return {"success": False, "message": "Invalid match."}, 400

    conn = get_db()

    match = conn.execute("""
        SELECT *
        FROM tournament_matches
        WHERE id=? AND status='active'
    """, (match_id,)).fetchone()

    if not match:
        conn.close()
        return {"success": False, "message": "Match is not active."}, 400

    current = conn.execute("""
        SELECT *
        FROM tournament_players
        WHERE id=?
    """, (match["turn_player_id"],)).fetchone()

    if not current or current["user_id"] != session["user_id"]:
        conn.close()
        return {"success": False, "message": "It is not your turn."}, 403

    other_id = (
        match["player2_id"]
        if match["turn_player_id"] == match["player1_id"]
        else match["player1_id"]
    )

    deadline = match["turn_deadline"]

    if deadline:
        try:
            from datetime import datetime, timezone
            deadline_dt = datetime.fromisoformat(
                deadline.replace("Z", "+00:00")
            )

            now = datetime.now(timezone.utc)

            if deadline_dt.tzinfo is None:
                deadline_dt = deadline_dt.replace(tzinfo=timezone.utc)

            if now >= deadline_dt:
                conn.execute("""
                    UPDATE tournament_matches
                    SET winner_id=?,
                        status='completed',
                        completed_at=CURRENT_TIMESTAMP
                    WHERE id=? AND status='active'
                """, (other_id, match_id))

                conn.execute("""
                    UPDATE tournament_players
                    SET status='eliminated'
                    WHERE id=?
                """, (current["id"],))

                conn.commit()
                conn.close()

                return {
                    "success": False,
                    "timeout": True,
                    "message": "20 seconds expired. You lost the match."
                }, 408
        except Exception:
            pass

    move = request.form.get("move", "").strip()

    # A move must be supplied by the game client.
    # The server still controls whose turn it is and the 20-second clock.
    if not move:
        conn.close()
        return {
            "success": False,
            "message": "No move supplied."
        }, 400

    # Record the turn and hand control to the opponent.
    conn.execute("""
        UPDATE tournament_matches
        SET turn_player_id=?,
            turn_deadline=datetime('now','+20 seconds')
        WHERE id=? AND status='active'
    """, (other_id, match_id))

    conn.commit()
    conn.close()

    return {
        "success": True,
        "message": "Move accepted.",
        "next_player_id": other_id,
        "seconds": 20
    }


@app.route("/tournament/bracket")
def tournament_bracket():
    conn = get_db()

    tournament = conn.execute("""
        SELECT *
        FROM tournaments
        ORDER BY id DESC
        LIMIT 1
    """).fetchone()

    if not tournament:
        conn.close()
        return {"success": False, "message": "No tournament found."}, 404

    matches = conn.execute("""
        SELECT
            m.id,
            m.round_number,
            m.match_number,
            m.status,
            m.turn_player_id,
            m.turn_deadline,
            m.winner_id,
            p1.username AS player1,
            p2.username AS player2
        FROM tournament_matches m
        LEFT JOIN tournament_players p1 ON p1.id=m.player1_id
        LEFT JOIN tournament_players p2 ON p2.id=m.player2_id
        WHERE m.tournament_id=?
        ORDER BY m.round_number, m.match_number
    """, (tournament["id"],)).fetchall()

    conn.close()

    round_names = {
        1: "SEMIFINAL",
        2: "FINAL"
    }

    if tournament["max_players"] == 8:
        round_names = {
            1: "QUARTERFINAL",
            2: "SEMIFINAL",
            3: "FINAL"
        }

    result = []

    for m in matches:
        item = dict(m)
        item["round_name"] = round_names.get(
            m["round_number"],
            "ROUND " + str(m["round_number"])
        )
        result.append(item)

    return {
        "success": True,
        "tournament": dict(tournament),
        "matches": result
    }

# Start every required tournament maintenance cycle.
def run_tournament_maintenance():
    try:
        ensure_tournament_columns()
        tournament_timeout_check()
        tournament_advance_rounds()
        create_next_tournament()
        start_due_tournament()
    except Exception as e:
        print("TOURNAMENT MAINTENANCE ERROR:", e)


def tournament_scheduler():
    import time

    while True:
        run_tournament_maintenance()
        time.sleep(5)


def start_tournament_scheduler():
    import threading
    import os

    if os.environ.get("WERKZEUG_RUN_MAIN") not in (None, "true"):
        return

    if getattr(app, "_tournament_scheduler_started", False):
        return

    app._tournament_scheduler_started = True

    thread = threading.Thread(
        target=tournament_scheduler,
        daemon=True
    )
    thread.start()


init_db()
start_tournament_scheduler()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
