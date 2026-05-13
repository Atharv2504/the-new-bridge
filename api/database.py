"""AWS RDS (PostgreSQL) connection and schema."""

import os
import urllib.parse

import psycopg2
from psycopg2.extras import RealDictCursor


def get_database_url():
    url = os.environ.get('DATABASE_URL')
    if url:
        if url.startswith('postgres://'):
            url = url.replace('postgres://', 'postgresql://', 1)
        ssl = os.environ.get('DATABASE_SSLMODE') or os.environ.get('PGSSLMODE')
        if ssl and 'sslmode=' not in url:
            sep = '&' if '?' in url else '?'
            url = f'{url}{sep}sslmode={urllib.parse.quote(ssl)}'
        return url

    host = os.environ.get('RDS_HOSTNAME') or os.environ.get('PGHOST')
    user = os.environ.get('RDS_USERNAME') or os.environ.get('PGUSER')
    password = os.environ.get('RDS_PASSWORD') or os.environ.get('PGPASSWORD')
    port = os.environ.get('RDS_PORT') or os.environ.get('PGPORT') or '5432'
    name = os.environ.get('RDS_DB_NAME') or os.environ.get('PGDATABASE')
    if not all([host, user, password, name]):
        raise RuntimeError(
            'Set DATABASE_URL or RDS_HOSTNAME, RDS_USERNAME, RDS_PASSWORD, RDS_DB_NAME for AWS RDS.'
        )
    password_q = urllib.parse.quote_plus(password)
    base = f'postgresql://{user}:{password_q}@{host}:{port}/{name}'
    ssl = os.environ.get('DATABASE_SSLMODE') or os.environ.get('PGSSLMODE')
    if ssl:
        base = f'{base}?sslmode={urllib.parse.quote(ssl)}'
    return base


def connect():
    return psycopg2.connect(get_database_url(), cursor_factory=RealDictCursor)


def init_schema():
    statements = [
        """
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('student', 'retiree')),
        phone TEXT,
        sector TEXT,
        learning_goals TEXT,
        legacy_bio TEXT,
        achievements TEXT,
        education TEXT,
        verified INTEGER DEFAULT 0,
        mentor_mode INTEGER DEFAULT 1,
        reverse_mentor_mode INTEGER DEFAULT 1,
        avg_rating DOUBLE PRECISION DEFAULT 0,
        review_count INTEGER DEFAULT 0,
        quiz_answers TEXT,
        time_credits INTEGER DEFAULT 0,
        tech_help_mode INTEGER DEFAULT 1
    )
    """,
        """
    CREATE TABLE IF NOT EXISTS sessions (
        id SERIAL PRIMARY KEY,
        student_id INTEGER REFERENCES users(id),
        retiree_id INTEGER REFERENCES users(id),
        date TEXT NOT NULL,
        time TEXT NOT NULL,
        subject TEXT,
        status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'denied', 'completed')),
        initiator_role TEXT,
        key_takeaways TEXT,
        homework TEXT,
        student_rating INTEGER,
        retiree_rating INTEGER
    )
    """,
        """
    CREATE TABLE IF NOT EXISTS documents (
        id SERIAL PRIMARY KEY,
        user_id INTEGER REFERENCES users(id),
        doc_type TEXT,
        s3_link TEXT
    )
    """,
        """
    CREATE TABLE IF NOT EXISTS news_articles (
        id SERIAL PRIMARY KEY,
        title TEXT NOT NULL,
        sector TEXT NOT NULL,
        content TEXT NOT NULL,
        mentor_take TEXT NOT NULL,
        author_id INTEGER REFERENCES users(id),
        created_at TEXT
    )
    """,
        """
    CREATE TABLE IF NOT EXISTS masterclasses (
        id SERIAL PRIMARY KEY,
        host_id INTEGER NOT NULL REFERENCES users(id),
        title TEXT NOT NULL,
        max_students INTEGER NOT NULL,
        date TEXT NOT NULL,
        time TEXT NOT NULL,
        description TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    ]
    conn = connect()
    conn.autocommit = True
    cur = conn.cursor()
    for stmt in statements:
        cur.execute(stmt)
    cur.close()
    conn.close()
