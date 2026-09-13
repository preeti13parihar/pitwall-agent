"""Small durable document store. Postgres in Render; SQLite for local development."""
import json, os, sqlite3, threading
from contextlib import contextmanager
lock = threading.RLock()

@contextmanager
def connection():
    url = os.getenv('DATABASE_URL')
    if url:
        import psycopg
        with psycopg.connect(url) as c:
            yield c, '%s'
    else:
        with lock:
            c = sqlite3.connect(os.getenv('SQLITE_PATH', 'pitwall.db'))
            try:
                yield c, '?'
                c.commit()
            finally:
                c.close()

def init():
    with connection() as (c, p):
        c.execute('CREATE TABLE IF NOT EXISTS documents (key TEXT PRIMARY KEY, body TEXT NOT NULL)')

def get(key, default=None):
    with connection() as (c, p):
        row = c.execute(f'SELECT body FROM documents WHERE key={p}', (key,)).fetchone()
    return json.loads(row[0]) if row else default

def put(key, value):
    with connection() as (c, p):
        c.execute(f'INSERT INTO documents(key,body) VALUES({p},{p}) ON CONFLICT(key) DO UPDATE SET body=excluded.body', (key, json.dumps(value)))

def create(key, value):
    with connection() as (c, p):
        cur = c.execute(f'INSERT INTO documents(key,body) VALUES({p},{p}) ON CONFLICT(key) DO NOTHING', (key, json.dumps(value)))
        return cur.rowcount == 1

def all_jobs():
    with connection() as (c, p):
        rows = c.execute("SELECT body FROM documents WHERE key LIKE 'job:%'").fetchall()
    return sorted([json.loads(r[0]) for r in rows], key=lambda x:x['created'], reverse=True)
