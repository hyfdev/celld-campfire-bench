import functools
import json
import pathlib
import sqlite3
import zlib

ROOMS = 50
USERS = 8
ACTIVE_USERS = 4
SEED_MESSAGES = 1000
POST_TEXT = ("New message: 你好，世界 👋. https://example.com/new?a=1&b=2 "
             "<strong>quoted \"value\" & 'text'</strong>\n") * 4
POST_JSON = json.dumps({"body": POST_TEXT}, ensure_ascii=False, separators=(",", ":"))


def user_name(user):
    return f"User {user} — 云飞 <u{user}> & \"'"


def seed_body(room, message):
    return (f"Room {room}, message {message}: 你好，世界 👋\n"
            f"Link: https://example.com/path?q=a&room={room} "
            "<script>alert('quoted \"value\"')</script> & text\n") * 3


def initial_unread(room, user):
    return room * 2 + user * 3


def message_row(room, message, posts):
    if message <= SEED_MESSAGES:
        user = (message - 1) % 7 + 1
        text = seed_body(room, message)
        created = 1699900000 + message
    else:
        user, text = posts[message - SEED_MESSAGES - 1]
        created = 1700000000 + message - SEED_MESSAGES
    return {"id": message, "user_id": user, "user_name": user_name(user), "body": text, "created_at": created}


def escaped(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def page_bytes(room, user, json_response, posts=()):
    version = len(posts) + 1
    last = SEED_MESSAGES + len(posts)
    unread = initial_unread(room, user) + sum(sender != user for sender, _ in posts)
    messages = [message_row(room, i, posts) for i in range(last, last - 50, -1)]
    page = {"room_id": room, "version": version, "unread": unread, "messages": messages}
    if json_response:
        return json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode()
    output = [f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Room {room}</title></head><body><header><h1>Room {room}</h1><p>User: {escaped(user_name(user))}; unread: {unread}; version: {version}</p></header><main>']
    for message in messages:
        output.append(f'<article data-id="{message["id"]}"><h2>{escaped(message["user_name"])}</h2><time>{message["created_at"]}</time><p>{escaped(message["body"])}</p></article>')
    output.append(f'</main><form method="post" action="/rooms/{room}/messages"><textarea name="body"></textarea><button>Send</button></form></body></html>')
    return "".join(output).encode()


@functools.lru_cache(maxsize=400)
def baseline_page(room, user, json_response):
    return page_bytes(room, user, json_response)


def post_bytes(room, user, text, version):
    return json.dumps({"id": 1000 + version - 1, "room_id": room, "user_id": user,
                       "body": text, "created_at": 1700000000 + version - 1, "version": version},
                      ensure_ascii=False, separators=(",", ":")).encode()


def make_fixture(directory):
    directory.mkdir()
    for room in range(1, ROOMS + 1):
        with sqlite3.connect(directory / f"room-{room}.db") as db:
            db.executescript("""
                CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT NOT NULL, token TEXT NOT NULL UNIQUE);
                CREATE TABLE members(user_id INTEGER PRIMARY KEY, unread INTEGER NOT NULL);
                CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, body TEXT NOT NULL, created_at INTEGER NOT NULL);
                CREATE TABLE room_meta(id INTEGER PRIMARY KEY, version INTEGER NOT NULL);
                INSERT INTO room_meta VALUES(1, 1);
            """)
            db.executemany("INSERT INTO users VALUES(?, ?, ?)", [(u, user_name(u), f"token-{u}") for u in range(1, USERS + 1)])
            db.executemany("INSERT INTO members VALUES(?, ?)", [(u, initial_unread(room, u)) for u in range(1, 8)])
            db.executemany("INSERT INTO messages VALUES(?, ?, ?, ?)", [(i, (i - 1) % 7 + 1, seed_body(room, i), 1699900000 + i) for i in range(1, SEED_MESSAGES + 1)])
            db.executescript("""
                CREATE TRIGGER message_inserted AFTER INSERT ON messages BEGIN
                    UPDATE room_meta SET version = version + 1 WHERE id = 1;
                    UPDATE members SET unread = unread + 1 WHERE user_id != NEW.user_id;
                END;
            """)


def make_oracle(path, versions=32):
    reads = []
    for version in range(1, versions + 1):
        posts = ((1, POST_TEXT),) * (version - 1)
        for room in range(1, ROOMS + 1):
            for user in range(1, ACTIVE_USERS + 1):
                for json_response in (False, True):
                    data = page_bytes(room, user, json_response, posts)
                    reads.append({"room": room, "user": user, "json": json_response, "version": version,
                                  "length": len(data), "crc32": zlib.crc32(data)})
    path.write_text(json.dumps({"post_text": POST_TEXT, "post_json": POST_JSON, "reads": reads}, ensure_ascii=False, separators=(",", ":")))


def audit(directory, posts_by_room):
    total = 0
    for room in range(1, ROOMS + 1):
        posts = posts_by_room.get(room, ())
        with sqlite3.connect(directory / f"room-{room}.db") as db:
            assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert db.execute("SELECT count(*), min(id), max(id) FROM messages").fetchone() == (1000 + len(posts), 1, 1000 + len(posts))
            assert db.execute("SELECT version FROM room_meta WHERE id=1").fetchone() == (1 + len(posts),)
            members = db.execute("SELECT user_id, unread FROM members ORDER BY user_id").fetchall()
            assert members == [(u, initial_unread(room, u) + sum(sender != u for sender, _ in posts)) for u in range(1, 8)]
            rows = db.execute("SELECT id, user_id, body, created_at FROM messages ORDER BY id").fetchall()
            for row in rows:
                expected = message_row(room, row[0], posts)
                assert row == (expected["id"], expected["user_id"], expected["body"], expected["created_at"]), (room, row[0])
            assert db.execute("SELECT id, name, token FROM users ORDER BY id").fetchall() == [(u, user_name(u), f"token-{u}") for u in range(1, 9)]
            total += len(posts)
    return {"rooms_checked": ROOMS, "seed_rows_checked": ROOMS * SEED_MESSAGES, "post_rows_checked": total, "integrity": "ok"}
