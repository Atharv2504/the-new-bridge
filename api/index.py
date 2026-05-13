import json
import os
import sys
from datetime import date, datetime, timedelta
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from psycopg2 import IntegrityError

# Load .env from project root
env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env')
load_dotenv(env_path, override=True)
print(f'Loaded .env from: {env_path}')
print(f'DATABASE_URL configured: {bool(os.environ.get("DATABASE_URL"))}')
print(f'DATABASE_SSLMODE: {os.environ.get("DATABASE_SSLMODE", "NOT SET")}')

sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from api.aws_env import validate_required_aws_env
from api.database import connect, init_schema
from api.dynamo_session import DynamoDBSessionInterface

from boto3_services import (
    ensure_rating_record,
    generate_ai_summary,
    get_region,
    publish_notification as _publish_notification,
    update_rating_metrics,
    upload_verification_docs,
)


def publish_notification(message, subject='BridgeTheGap Notification'):
    try:
        return _publish_notification(message, subject=subject)
    except Exception as e:
        print(f'AWS notification skipped: {e}')
        return False


def safe_ensure_rating_record(user_id):
    try:
        ensure_rating_record(user_id)
    except Exception as e:
        print(f'AWS rating record skipped for user {user_id}: {e}')

parent_dir = os.path.dirname(os.path.dirname(__file__))
template_dir = os.path.join(parent_dir, 'templates')
static_dir = os.path.join(parent_dir, 'static')

app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'super_secret_dummy_key_for_flash_messages')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

validate_required_aws_env()
app.session_interface = DynamoDBSessionInterface(
    os.environ['AWS_DYNAMODB_SESSION_TABLE_NAME'],
    get_region(),
)

ALL_SECTORS = [
    'Healthcare',
    'Technology',
    'Defense & Aerospace',
    'Finance & Banking',
    'Law & Legal Services',
    'Education & Academia',
    'Engineering',
    'Arts & Entertainment',
]

NEWS_SEED = [
    {
        'title': 'The Future of AI in Healthcare Diagnostics',
        'sector': 'Healthcare',
        'content': 'Recent studies show AI algorithms can detect anomalies in X-rays with 95% accuracy.',
        'mentor_take': 'Dr. Mitchell says: "AI is a tool to augment, not replace, human connection."',
    },
    {
        'title': 'Zero Trust in Defense Cloud Migrations',
        'sector': 'Defense & Aerospace',
        'content': 'Agencies are accelerating secure cloud adoption with continuous verification models.',
        'mentor_take': 'Col. James says: "Assume breach—design every layer for resilience and auditability."',
    },
    {
        'title': 'Regulatory Shifts in Cross-Border Finance',
        'sector': 'Finance & Banking',
        'content': 'New frameworks aim to harmonize reporting while preserving competitive markets.',
        'mentor_take': 'Elena Ruiz says: "Compliance is a product feature, not a bolt-on afterthought."',
    },
]


def _init_app_db():
    init_schema()
    conn = connect()
    cur = None
    try:
        cur = conn.cursor()
        cur.execute('SELECT COUNT(*) AS c FROM news_articles')
        if (cur.fetchone() or {}).get('c', 0) == 0:
            now = datetime.utcnow().isoformat()
            for a in NEWS_SEED:
                cur.execute(
                    '''INSERT INTO news_articles (title, sector, content, mentor_take, author_id, created_at)
                       VALUES (%s, %s, %s, %s, NULL, %s)''',
                    (a['title'], a['sector'], a['content'], a['mentor_take'], now),
                )
            conn.commit()
    except Exception as e:
        conn.rollback()
        print(f'Database seed error: {e}')
        raise
    finally:
        if cur is not None:
            cur.close()
        conn.close()


try:
    print('[DEBUG] Starting database initialization...')
    _init_app_db()
    print('[DEBUG] Database initialization successful!')
except Exception as e:
    import traceback
    print(f'[ERROR] Database initialization error: {e}')
    print(f'[ERROR] Traceback: {traceback.format_exc()}')


def get_db():
    try:
        return connect()
    except Exception as e:
        import traceback
        print(f'Database connection error: {e}')
        print(f'Traceback: {traceback.format_exc()}')
        return None


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in to access this page.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)

    return decorated_function


def row_to_user_dict(row):
    if row is None:
        return None
    d = dict(row)
    d['time_credits'] = d.get('time_credits') or 0
    if d.get('tech_help_mode') is None:
        d['tech_help_mode'] = d.get('reverse_mentor_mode', 1)
    return d


@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('profile'))
    return render_template('landing.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        conn = get_db()
        if not conn:
            flash('Database connection error.', 'danger')
            return render_template('login.html')
        try:
            cur = conn.cursor()
            cur.execute(
                'SELECT * FROM users WHERE email = %s AND password = %s', (email, password)
            )
            user = cur.fetchone()
        finally:
            conn.close()
        if user:
            session['user_id'] = user['id']
            session['user_role'] = user['role']
            session['user_name'] = user['name']
            publish_notification(
                f'User {user["id"]} ({user["name"]}) logged in as {user["role"]}.',
                subject='BridgeTheGap: Login',
            )
            flash('Login successful!', 'success')
            if not user['verified']:
                return redirect(url_for('setup_profile'))
            return redirect(url_for('profile'))
        flash('Invalid email or password.', 'danger')
    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        password = request.form['password']
        role = request.form['role']
        quiz_answers = json.dumps(
            {'q1': request.form.get('q1'), 'q2': request.form.get('q2'), 'q3': request.form.get('q3')}
        )
        conn = get_db()
        if not conn:
            flash('Database connection error.', 'danger')
            return render_template('register.html')
        try:
            cur = conn.cursor()
            cur.execute(
                'INSERT INTO users (name, email, password, role, quiz_answers) VALUES (%s, %s, %s, %s, %s) RETURNING id',
                (name, email, password, role, quiz_answers),
            )
            user_id = cur.fetchone()['id']
            conn.commit()
            safe_ensure_rating_record(user_id)
            publish_notification(
                f'New user registered: id={user_id}, email={email}, role={role}.',
                subject='BridgeTheGap: Registration',
            )
            session['user_id'] = user_id
            session['user_role'] = role
            session['user_name'] = name
            flash('Registration successful. Please complete your verification profile.', 'success')
            return redirect(url_for('setup_profile'))
        except IntegrityError:
            conn.rollback()
            flash('Email already exists.', 'danger')
        finally:
            conn.close()
    return render_template('register.html')


@app.route('/logout')
def logout():
    uid = session.get('user_id')
    uname = session.get('user_name')
    if uid:
        publish_notification(
            f'User {uid} ({uname}) logged out.',
            subject='BridgeTheGap: Logout',
        )
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('index'))


@app.route('/setup_profile', methods=['GET', 'POST'])
@login_required
def setup_profile():
    user_id = session['user_id']
    role = session['user_role']
    conn = get_db()
    if not conn:
        flash('Database connection error.', 'danger')
        return render_template('setup_profile.html', role=role, sectors=ALL_SECTORS)
    try:
        if request.method == 'POST':
            phone = request.form.get('phone', '')
            govt_id_file = request.files.get('govt_id')
            if govt_id_file and govt_id_file.filename != '':
                try:
                    s3_link = upload_verification_docs(govt_id_file, user_id)
                    if s3_link:
                        cur = conn.cursor()
                        cur.execute(
                            'INSERT INTO documents (user_id, doc_type, s3_link) VALUES (%s, %s, %s)',
                            (user_id, 'Govt ID', s3_link),
                        )
                except Exception as e:
                    print(f'File upload error: {e}')
                    flash('Error uploading file. Please try again.', 'danger')
            cur = conn.cursor()
            if role == 'student':
                learning_goals = request.form.get('learning_goals', '')
                cur.execute(
                    'UPDATE users SET phone = %s, learning_goals = %s, verified = 1 WHERE id = %s',
                    (phone, learning_goals, user_id),
                )
            else:
                sector = request.form.get('sector', '')
                legacy_bio = request.form.get('legacy_bio', '')
                achievements = request.form.get('achievements', '')
                education = request.form.get('education', '')
                cur.execute(
                    '''UPDATE users SET phone = %s, sector = %s, legacy_bio = %s, achievements = %s,
                       education = %s, verified = 1 WHERE id = %s''',
                    (phone, sector, legacy_bio, achievements, education, user_id),
                )
            conn.commit()
            publish_notification(
                f'User {user_id} completed profile verification (role={role}).',
                subject='BridgeTheGap: Profile verified',
            )
            flash('Profile and Verification Vault updated successfully!', 'success')
            return redirect(url_for('profile'))
        return render_template('setup_profile.html', role=role, sectors=ALL_SECTORS)
    finally:
        conn.close()


@app.route('/profile')
@login_required
def profile():
    user_id = session['user_id']
    conn = get_db()
    if not conn:
        flash('Database connection error.', 'danger')
        return redirect(url_for('index'))
    try:
        cur = conn.cursor()
        cur.execute('SELECT * FROM users WHERE id = %s', (user_id,))
        user = cur.fetchone()
        cur.execute('SELECT * FROM documents WHERE user_id = %s', (user_id,))
        documents = cur.fetchall()
    finally:
        conn.close()
    u = row_to_user_dict(user)
    learning_goals = u['learning_goals'].split('\n') if u.get('learning_goals') else []
    achievements = u['achievements'].split('\n') if u.get('achievements') else []
    return render_template(
        'profile.html',
        user=u,
        documents=documents,
        learning_goals=learning_goals,
        achievements=achievements,
    )


def _session_party(sess, uid):
    if sess['student_id'] == uid:
        return 'student'
    if sess['retiree_id'] == uid:
        return 'retiree'
    return None


def _load_session(conn, session_id, uid):
    cur = conn.cursor()
    cur.execute('SELECT * FROM sessions WHERE id = %s', (session_id,))
    s = cur.fetchone()
    if not s or _session_party(s, uid) is None:
        return None, None
    cur.execute('SELECT name FROM users WHERE id = %s', (s['student_id'],))
    st = cur.fetchone()
    cur.execute('SELECT name FROM users WHERE id = %s', (s['retiree_id'],))
    rt = cur.fetchone()
    data = {
        'id': s['id'],
        'subject': s['subject'] or '',
        'student_name': st['name'] if st else '',
        'mentor_name': rt['name'] if rt else '',
        'status': s['status'],
    }
    return s, data


@app.route('/tracker')
@login_required
def tracker():
    uid = session['user_id']
    role = session['user_role']
    conn = get_db()
    if not conn:
        flash('Database connection error.', 'danger')
        return redirect(url_for('index'))
    try:
        cur = conn.cursor()
        cur.execute('SELECT * FROM users WHERE id = %s', (uid,))
        user_row = cur.fetchone()
        u = row_to_user_dict(user_row)

        sessions = []
        if role == 'student':
            cur.execute(
                '''SELECT s.*, r.name AS retiree_name FROM sessions s
                   JOIN users r ON r.id = s.retiree_id WHERE s.student_id = %s ORDER BY s.date, s.time''',
                (uid,),
            )
            rows = cur.fetchall()
            for r in rows:
                sessions.append(
                    {
                        'id': r['id'],
                        'retiree_name': r['retiree_name'],
                        'date': r['date'],
                        'time': r['time'],
                        'subject': r['subject'],
                        'status': r['status'],
                        'initiator_role': r['initiator_role'],
                        'key_takeaways': r['key_takeaways'],
                    }
                )
        else:
            cur.execute(
                '''SELECT s.*, st.name AS student_name FROM sessions s
                   JOIN users st ON st.id = s.student_id WHERE s.retiree_id = %s ORDER BY s.date, s.time''',
                (uid,),
            )
            rows = cur.fetchall()
            for r in rows:
                cur.execute(
                    'SELECT avg_rating, review_count FROM users WHERE id = %s', (r['student_id'],)
                )
                st_m = cur.fetchone()
                sessions.append(
                    {
                        'id': r['id'],
                        'student_name': r['student_name'],
                        'date': r['date'],
                        'time': r['time'],
                        'subject': r['subject'],
                        'status': r['status'],
                        'initiator_role': r['initiator_role'],
                        'avg_rating': (st_m['avg_rating'] or 0) if st_m else 0,
                        'review_count': (st_m['review_count'] or 0) if st_m else 0,
                        'key_takeaways': r['key_takeaways'],
                    }
                )

        attended = len([x for x in sessions if x['status'] == 'completed'])
        pending_sessions = [
            x for x in sessions if x['status'] == 'pending' and x.get('initiator_role') == 'student'
        ]
        upcoming_sessions = [x for x in sessions if x['status'] == 'accepted']
        completed_sessions = [x for x in sessions if x['status'] == 'completed']

        if role == 'student':
            pending_sessions = [x for x in sessions if x['status'] == 'pending']

        tracker_user = {
            'type': role,
            'reverse_mentor_mode': u.get('reverse_mentor_mode', 1),
            'mentor_mode': u.get('mentor_mode', 1),
            'sessions': sessions,
            'sessions_attended': attended,
            'pending_sessions': pending_sessions if role == 'retiree' else [],
            'upcoming_sessions': upcoming_sessions if role == 'retiree' else [],
            'completed_sessions': completed_sessions if role == 'retiree' else [],
        }
        return render_template('tracker.html', user=tracker_user)
    finally:
        conn.close()


@app.route('/toggle_reverse_mentor_mode', methods=['POST'])
@login_required
def toggle_reverse_mentor_mode():
    uid = session['user_id']
    conn = get_db()
    if not conn:
        return redirect(url_for('tracker'))
    try:
        cur = conn.cursor()
        cur.execute('SELECT reverse_mentor_mode FROM users WHERE id = %s', (uid,))
        row = cur.fetchone()
        newv = 0 if (row and row['reverse_mentor_mode']) else 1
        cur.execute(
            'UPDATE users SET reverse_mentor_mode = %s, tech_help_mode = %s WHERE id = %s',
            (newv, newv, uid),
        )
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for('tracker'))


@app.route('/toggle_mentor_mode', methods=['POST'])
@login_required
def toggle_mentor_mode():
    uid = session['user_id']
    conn = get_db()
    if not conn:
        return redirect(url_for('tracker'))
    try:
        cur = conn.cursor()
        cur.execute('SELECT mentor_mode FROM users WHERE id = %s', (uid,))
        row = cur.fetchone()
        newv = 0 if (row and row['mentor_mode']) else 1
        cur.execute('UPDATE users SET mentor_mode = %s WHERE id = %s', (newv, uid))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for('tracker'))


@app.route('/toggle_tech_help', methods=['POST'])
@login_required
def toggle_tech_help():
    uid = session['user_id']
    conn = get_db()
    if not conn:
        return redirect(url_for('profile'))
    try:
        cur = conn.cursor()
        cur.execute('SELECT tech_help_mode FROM users WHERE id = %s', (uid,))
        row = cur.fetchone()
        cur_val = row['tech_help_mode'] if row and row['tech_help_mode'] is not None else 1
        newv = 0 if cur_val else 1
        cur.execute(
            'UPDATE users SET tech_help_mode = %s, reverse_mentor_mode = %s WHERE id = %s',
            (newv, newv, uid),
        )
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for('profile'))


@app.route('/news')
@login_required
def news():
    sector = request.args.get('sector')
    conn = get_db()
    if not conn:
        flash('Database connection error.', 'danger')
        return redirect(url_for('index'))
    try:
        cur = conn.cursor()
        if sector:
            cur.execute(
                'SELECT * FROM news_articles WHERE sector = %s ORDER BY id DESC', (sector,)
            )
        else:
            cur.execute('SELECT * FROM news_articles ORDER BY id DESC')
        rows = cur.fetchall()
    finally:
        conn.close()
    articles = [dict(r) for r in rows]
    return render_template(
        'news.html',
        articles=articles,
        sectors=ALL_SECTORS,
        current_filter=sector,
        user_role=session['user_role'],
    )


@app.route('/post_news', methods=['GET', 'POST'])
@login_required
def post_news():
    if session['user_role'] != 'retiree':
        flash('Only mentors may post news.', 'warning')
        return redirect(url_for('news'))
    if request.method == 'POST':
        conn = get_db()
        if not conn:
            flash('Database connection error.', 'danger')
            return render_template('post_news.html', sectors=ALL_SECTORS)
        try:
            cur = conn.cursor()
            now = datetime.utcnow().isoformat()
            cur.execute(
                '''INSERT INTO news_articles (title, sector, content, mentor_take, author_id, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s)''',
                (
                    request.form['title'],
                    request.form['sector'],
                    request.form['content'],
                    request.form['mentor_take'],
                    session['user_id'],
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        publish_notification(f"New Industry Pulse article: {request.form['title']}")
        flash('Article published.', 'success')
        return redirect(url_for('news'))
    return render_template('post_news.html', sectors=ALL_SECTORS)


@app.route('/booking')
@login_required
def booking():
    sector = request.args.get('sector')
    role = session['user_role']
    uid = session['user_id']
    conn = get_db()
    if not conn:
        flash('Database connection error.', 'danger')
        return redirect(url_for('index'))
    try:
        cur = conn.cursor()
        if role == 'student':
            q = '''SELECT id, name, sector, avg_rating, review_count FROM users
                   WHERE role = 'retiree' AND mentor_mode = 1'''
            params = ()
            if sector:
                q += ' AND sector = %s'
                params = (sector,)
            cur.execute(q, params)
            cand = cur.fetchall()
            retirees = [
                {
                    'id': r['id'],
                    'name': r['name'],
                    'role': 'Mentor',
                    'sector': r['sector'],
                    'avg_rating': r['avg_rating'] or 0,
                    'review_count': r['review_count'] or 0,
                    'match_percent': _vibe_match(conn, uid, r['id']),
                }
                for r in cand
            ]
        else:
            q = '''SELECT id, name, sector, avg_rating, review_count FROM users
                   WHERE role = 'student' AND reverse_mentor_mode = 1'''
            params = ()
            if sector:
                q += ' AND sector = %s'
                params = (sector,)
            cur.execute(q, params)
            cand = cur.fetchall()
            retirees = [
                {
                    'id': r['id'],
                    'name': r['name'],
                    'role': 'Reverse Mentor',
                    'sector': r['sector'] or 'Technology',
                    'avg_rating': r['avg_rating'] or 0,
                    'review_count': r['review_count'] or 0,
                    'match_percent': _vibe_match(conn, uid, r['id']),
                }
                for r in cand
            ]

        booked_times = {}
        cur.execute(
            "SELECT retiree_id, date, time FROM sessions WHERE status IN ('pending','accepted')"
        )
        for r in cur.fetchall():
            rid = r['retiree_id']
            key = f"{r['date']}_{r['time']}"
            booked_times.setdefault(rid, []).append(key)
        cur.execute(
            "SELECT student_id, date, time FROM sessions WHERE status IN ('pending','accepted')"
        )
        for r in cur.fetchall():
            sid = r['student_id']
            key = f"{r['date']}_{r['time']}"
            booked_times.setdefault(sid, []).append(key)
    finally:
        conn.close()
    return render_template(
        'booking.html',
        retirees=retirees,
        sectors=ALL_SECTORS,
        current_filter=sector,
        user_role=role,
        current_date=date.today().isoformat(),
        booked_times=booked_times,
    )


def _vibe_match(conn, a_id, b_id):
    cur = conn.cursor()
    cur.execute('SELECT quiz_answers FROM users WHERE id = %s', (a_id,))
    ra = cur.fetchone()
    cur.execute('SELECT quiz_answers FROM users WHERE id = %s', (b_id,))
    rb = cur.fetchone()
    if not ra or not rb or not ra['quiz_answers'] or not rb['quiz_answers']:
        return None
    try:
        ja = json.loads(ra['quiz_answers'])
        jb = json.loads(rb['quiz_answers'])
        same = sum(1 for k in ja if k in jb and ja[k] == jb[k])
        total = max(len(ja), 1)
        return int(100 * same / total)
    except Exception:
        return None


@app.route('/book_session/<int:target_id>', methods=['POST'])
@login_required
def book_session(target_id):
    uid = session['user_id']
    role = session['user_role']
    d = request.form['date']
    t = request.form['time']
    subj = request.form['subject']
    conn = get_db()
    if not conn:
        flash('Database connection error.', 'danger')
        return redirect(url_for('booking'))
    try:
        cur = conn.cursor()
        if role == 'student':
            cur.execute(
                '''INSERT INTO sessions (student_id, retiree_id, date, time, subject, status, initiator_role)
                   VALUES (%s, %s, %s, %s, %s, 'pending', 'student')''',
                (uid, target_id, d, t, subj),
            )
        else:
            cur.execute(
                '''INSERT INTO sessions (student_id, retiree_id, date, time, subject, status, initiator_role)
                   VALUES (%s, %s, %s, %s, %s, 'pending', 'retiree')''',
                (target_id, uid, d, t, subj),
            )
        conn.commit()
    finally:
        conn.close()
    publish_notification('A new mentorship session has been requested on BridgeTheGap.')
    flash('Session request submitted.', 'success')
    return redirect(url_for('booking'))


@app.route('/accept_session/<int:session_id>', methods=['POST'])
@login_required
def accept_session(session_id):
    uid = session['user_id']
    conn = get_db()
    if not conn:
        return redirect(url_for('tracker'))
    try:
        cur = conn.cursor()
        cur.execute('SELECT * FROM sessions WHERE id = %s', (session_id,))
        s = cur.fetchone()
        if s and _session_party(s, uid):
            cur.execute("UPDATE sessions SET status = 'accepted' WHERE id = %s", (session_id,))
            conn.commit()
            publish_notification(
                f'Session {session_id} was accepted by user {uid}.',
                subject='BridgeTheGap: Session accepted',
            )
    finally:
        conn.close()
    return redirect(url_for('tracker'))


@app.route('/deny_session/<int:session_id>', methods=['POST'])
@login_required
def deny_session(session_id):
    uid = session['user_id']
    conn = get_db()
    if not conn:
        return redirect(url_for('tracker'))
    try:
        cur = conn.cursor()
        cur.execute('SELECT * FROM sessions WHERE id = %s', (session_id,))
        s = cur.fetchone()
        if s and _session_party(s, uid):
            cur.execute("UPDATE sessions SET status = 'denied' WHERE id = %s", (session_id,))
            conn.commit()
            publish_notification(
                f'Session {session_id} was denied by user {uid}.',
                subject='BridgeTheGap: Session denied',
            )
    finally:
        conn.close()
    return redirect(url_for('tracker'))


@app.route('/meeting/<int:session_id>')
@login_required
def meeting(session_id):
    uid = session['user_id']
    conn = get_db()
    if not conn:
        return redirect(url_for('tracker'))
    try:
        _, session_data = _load_session(conn, session_id, uid)
    finally:
        conn.close()
    if not session_data:
        flash('Session not found.', 'danger')
        return redirect(url_for('tracker'))
    return render_template('meeting.html', session_data=session_data, user_role=session['user_role'])


@app.route('/rate_session/<int:session_id>', methods=['GET', 'POST'])
@login_required
def rate_session(session_id):
    uid = session['user_id']
    role = session['user_role']
    conn = get_db()
    if not conn:
        flash('Database connection error.', 'danger')
        return redirect(url_for('tracker'))
    try:
        s, session_data = _load_session(conn, session_id, uid)
        if not s:
            flash('Session not found.', 'danger')
            return redirect(url_for('tracker'))

        if request.method == 'GET':
            if s['status'] == 'accepted':
                cur = conn.cursor()
                cur.execute("UPDATE sessions SET status = 'completed' WHERE id = %s", (session_id,))
                conn.commit()
            return render_template('rate_session.html', session_data=session_data, user_role=role)

        rating = int(request.form.get('rating') or 0)
        if rating < 1 or rating > 5:
            flash('Invalid rating.', 'danger')
            return redirect(url_for('rate_session', session_id=session_id))

        ratee_id = s['retiree_id'] if role == 'student' else s['student_id']
        col = 'student_rating' if role == 'student' else 'retiree_rating'
        if col not in ('student_rating', 'retiree_rating'):
            flash('Invalid rating.', 'danger')
            return redirect(url_for('tracker'))
        cur = conn.cursor()
        cur.execute(f'SELECT {col} AS rcol FROM sessions WHERE id = %s', (session_id,))
        existing = cur.fetchone()
        if existing and existing['rcol'] is not None:
            flash('You have already rated this session.', 'info')
            return redirect(url_for('tracker'))

        cur.execute(f'UPDATE sessions SET {col} = %s WHERE id = %s', (rating, session_id))
        cur.execute('SELECT avg_rating, review_count FROM users WHERE id = %s', (ratee_id,))
        ur = cur.fetchone()
        old_avg = ur['avg_rating'] or 0
        old_cnt = ur['review_count'] or 0
        new_cnt = old_cnt + 1
        new_avg = (old_avg * old_cnt + rating) / new_cnt if old_cnt else float(rating)
        cur.execute(
            'UPDATE users SET avg_rating = %s, review_count = %s WHERE id = %s',
            (new_avg, new_cnt, ratee_id),
        )
        conn.commit()
    finally:
        conn.close()
    update_rating_metrics(ratee_id, new_avg, new_cnt)
    publish_notification(f'Session {session_id} received a new {rating}-star rating.')
    flash('Thank you for your feedback.', 'success')
    return redirect(url_for('tracker'))


@app.route('/masterclass')
@login_required
def masterclass():
    conn = get_db()
    masterclasses = []
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(
                '''SELECT m.*, u.name AS host_name FROM masterclasses m
                   JOIN users u ON u.id = m.host_id
                   ORDER BY m.date ASC, m.time ASC LIMIT 50'''
            )
            masterclasses = cur.fetchall()
        finally:
            conn.close()
    return render_template(
        'masterclass.html', user_role=session['user_role'], masterclasses=masterclasses
    )


@app.route('/host_masterclass', methods=['GET', 'POST'])
@login_required
def host_masterclass():
    if session['user_role'] != 'retiree':
        return redirect(url_for('masterclass'))
    if request.method == 'POST':
        conn = get_db()
        if not conn:
            flash('Database connection error.', 'danger')
            return render_template('host_masterclass.html', current_date=date.today().isoformat())
        try:
            cur = conn.cursor()
            now = datetime.utcnow().isoformat()
            cur.execute(
                '''INSERT INTO masterclasses (host_id, title, max_students, date, time, description, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id''',
                (
                    session['user_id'],
                    request.form['title'],
                    int(request.form['max_students']),
                    request.form['date'],
                    request.form['time'],
                    request.form['description'],
                    now,
                ),
            )
            mid = cur.fetchone()['id']
            conn.commit()
            publish_notification(
                f'Masterclass #{mid} scheduled by user {session["user_id"]}: {request.form["title"]}',
                subject='BridgeTheGap: Masterclass scheduled',
            )
            flash('Masterclass scheduled and saved.', 'success')
            return redirect(url_for('masterclass'))
        finally:
            conn.close()
    return render_template('host_masterclass.html', current_date=date.today().isoformat())


@app.route('/api/generate_summary/<int:session_id>', methods=['POST'])
@login_required
def api_generate_summary(session_id):
    uid = session['user_id']
    conn = get_db()
    if not conn:
        return jsonify({'error': 'database unavailable'}), 503
    try:
        s, _ = _load_session(conn, session_id, uid)
    finally:
        conn.close()
    if not s:
        return jsonify({'error': 'not found'}), 404
    body = request.get_json(silent=True) or {}
    transcript = body.get('transcript') or 'Session transcript simulation.'
    subject = s['subject'] or 'the session'
    try:
        summary = generate_ai_summary(session_id, transcript=transcript, subject=subject)
    except Exception as e:
        return jsonify({'error': str(e)}), 502
    conn = get_db()
    if not conn:
        publish_notification(
            f'Bedrock summary generated for session {session_id} (RDS update skipped — DB unavailable).',
            subject='BridgeTheGap: AI summary',
        )
        return jsonify({'summary': summary}), 200
    try:
        cur = conn.cursor()
        cur.execute(
            'UPDATE sessions SET key_takeaways = %s WHERE id = %s', (summary[:2000], session_id)
        )
        conn.commit()
    finally:
        conn.close()
    publish_notification(
        f'Bedrock summary stored for session {session_id}.',
        subject='BridgeTheGap: AI summary',
    )
    return jsonify({'summary': summary})


@app.route('/api/save_homework/<int:session_id>', methods=['POST'])
@login_required
def api_save_homework(session_id):
    if session['user_role'] != 'retiree':
        return jsonify({'ok': False}), 403
    uid = session['user_id']
    conn = get_db()
    if not conn:
        return jsonify({'error': 'database unavailable'}), 503
    try:
        s, _ = _load_session(conn, session_id, uid)
        if not s:
            return jsonify({'error': 'not found'}), 404
        hw = (request.get_json(silent=True) or {}).get('homework') or ''
        cur = conn.cursor()
        cur.execute('UPDATE sessions SET homework = %s WHERE id = %s', (hw, session_id))
        conn.commit()
    finally:
        conn.close()
    publish_notification(
        f'Homework/action items updated for session {session_id} by mentor.',
        subject='BridgeTheGap: Homework saved',
    )
    return jsonify({'ok': True})
