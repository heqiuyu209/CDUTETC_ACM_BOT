import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import commands
import database


@pytest.fixture
def collection(monkeypatch, tmp_path):
    path = tmp_path / "registration.db"
    connections = []

    def connect():
        conn = sqlite3.connect(path, check_same_thread=False)
        connections.append(conn)
        return conn

    monkeypatch.setattr(database, "_connect", connect)
    monkeypatch.setattr(database, "now_str", lambda: "2026-11-01 08:00:00")
    database.init_db()
    cid = database.create_collection("g1", "报名", fields=["name", "student_id"])
    yield cid, path, connections
    for conn in connections:
        conn.close()


def saved(cid, member="member"):
    return json.loads(database.get_collection_registration(cid, member, "g1")["data_json"])


@pytest.mark.parametrize("extra", ["", "name Alice", 'name="unterminated', "name=Alice name=Bob", ",，"])
async def test_repeat_or_bad_input_preserves_existing_fields(collection, extra):
    cid = collection[0]
    original = {"name": "Alice", "student_id": "TEST001"}
    assert database.register_collection(cid, "g1", "member", original)[0]
    before = database.get_collection_registration(cid, "member", "g1")
    reply = await commands.handle_command(f"/报名 {cid} {extra}", "g1", "member")
    assert saved(cid) == original
    assert database.get_collection_registration(cid, "member", "g1") == before
    assert database.summarize_collection(cid)["registered"] == 1
    assert reply and "已更新" not in reply[0]["text"]


async def test_partial_update_and_explicit_field_clear(collection):
    cid = collection[0]
    database.register_collection(cid, "g1", "member", {"name": "Alice", "student_id": "TEST001"})
    await commands.handle_command(f'/报名 {cid} name="Alice Smith"', "g1", "member")
    assert saved(cid) == {"name": "Alice Smith", "student_id": "TEST001"}
    await commands.handle_command(f'/报名 {cid} student_id=""', "g1", "member")
    assert saved(cid) == {"name": "Alice Smith", "student_id": ""}


@pytest.mark.parametrize("fields", [None, {}])
def test_cancelled_registration_restores_original_data(collection, fields):
    cid = collection[0]
    original = {"name": "Alice", "student_id": "TEST001"}
    database.register_collection(cid, "g1", "member", original)
    database.cancel_collection_registration(cid, "g1", "member")
    assert database.register_collection(cid, "g1", "member", fields)[0]
    assert saved(cid) == original


@pytest.mark.parametrize("fields", [[], "bad", {"name": 123}, {"": "bad"}, {123: "bad"}, {"undeclared": "x"}])
def test_invalid_data_never_overwrites_registration(collection, fields):
    cid = collection[0]
    database.register_collection(cid, "g1", "member", {"name": "Alice"})
    assert not database.register_collection(cid, "g1", "member", fields)[0]
    assert saved(cid) == {"name": "Alice"}


def test_corrupt_existing_data_is_not_silently_replaced(collection):
    cid, path, _ = collection
    database.register_collection(cid, "g1", "member", {"name": "Alice"})
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE collection_responses SET data_json = '{bad' WHERE collection_id = ?", (cid,))
    conn.close()
    assert not database.register_collection(cid, "g1", "member", {"name": "Bob"})[0]
    assert database.get_collection_registration(cid, "member", "g1")["data_json"] == "{bad"


def test_capacity_is_atomic_under_independent_connections(collection):
    cid, path, _ = collection
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE collections SET capacity = 1 WHERE id = ?", (cid,))
    conn.close()
    barrier = threading.Barrier(8)

    def register(index):
        barrier.wait(timeout=5)
        return database.register_collection(cid, "g1", f"member-{index}", {"name": str(index)})

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(register, range(8)))
    assert sum(ok for ok, _ in results) == 1
    assert database.summarize_collection(cid)["registered"] == 1


def test_registration_rechecks_after_waiting_for_closing_transaction(collection, monkeypatch):
    cid, path, _ = collection
    original_connect = database._connect
    connected = threading.Event()

    def connect():
        conn = original_connect()
        connected.set()
        return conn

    monkeypatch.setattr(database, "_connect", connect)
    closer = sqlite3.connect(path)
    closer.execute("BEGIN IMMEDIATE")
    closer.execute("UPDATE collections SET status = 'closed' WHERE id = ?", (cid,))
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(database.register_collection, cid, "g1", "member", {"name": "Alice"})
            assert connected.wait(timeout=5)
            closer.commit()
            assert not future.result(timeout=5)[0]
    finally:
        closer.close()
    assert database.get_collection(cid)["status"] == "closed"
    assert database.summarize_collection(cid)["registered"] == 0


def test_concurrent_patches_preserve_each_others_fields(collection):
    cid = collection[0]
    database.register_collection(cid, "g1", "member", {"name": "Alice", "student_id": "TEST001"})
    barrier = threading.Barrier(2)

    def update(fields):
        barrier.wait(timeout=5)
        return database.register_collection(cid, "g1", "member", fields)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, [{"name": "Bob"}, {"student_id": "TEST002"}]))
    assert all(ok for ok, _ in results)
    assert saved(cid) == {"name": "Bob", "student_id": "TEST002"}


def test_closed_expired_and_other_group_do_not_modify_existing_data(collection):
    cid, path, _ = collection
    database.register_collection(cid, "g1", "member", {"name": "Alice"})
    assert not database.register_collection(cid, "g2", "member", {"name": "Bob"})[0]
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE collections SET deadline = '2026-11-01 08:00' WHERE id = ?", (cid,))
    conn.close()
    assert not database.register_collection(cid, "g1", "member", {"name": "Bob"})[0]
    assert saved(cid) == {"name": "Alice"}
