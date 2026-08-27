"""ChatStore 会话消息存储单元测试。"""

from __future__ import annotations

from src.memory.chat_store import ChatStore


def _new_store(tmp_path) -> ChatStore:
    return ChatStore(str(tmp_path / "chat.db"))


def test_add_and_get_roundtrip(tmp_path):
    store = _new_store(tmp_path)
    mid1 = store.add("s1", "user", "chat", "研究 transformer 的来历")
    mid2 = store.add("s1", "assistant", "chat", "你想了解哪个阶段？")
    assert mid1 == "M-1"
    assert mid2 == "M-2"
    msgs = store.get_messages("s1")
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["kind"] == "chat"
    assert [m["message_id"] for m in msgs] == ["M-1", "M-2"]


def test_message_ids_are_sorted_by_numeric_sequence(tmp_path):
    store = _new_store(tmp_path)
    for index in range(12):
        store.add("s1", "user", "chat", f"message-{index + 1}")

    assert [item["message_id"] for item in store.get_messages("s1")] == [
        f"M-{index}" for index in range(1, 13)
    ]


def test_session_isolation(tmp_path):
    store = _new_store(tmp_path)
    store.add("s1", "user", "chat", "A")
    store.add("s2", "user", "chat", "B")
    assert len(store.get_messages("s1")) == 1
    assert len(store.get_messages("s2")) == 1
    # 每 session 独立递增
    assert store.add("s1", "user", "chat", "C") == "M-2"
    assert store.add("s2", "user", "chat", "D") == "M-2"


def test_list_sessions_title_is_first_user_message_preview(tmp_path):
    store = _new_store(tmp_path)
    store.add("s1", "user", "chat", "研究transformer的来历与发展历史")
    store.add("s1", "assistant", "chat", "需要确认范围")
    store.add("s2", "assistant", "chat", "先来一条助手消息")
    sessions = store.list_sessions()
    assert len(sessions) == 2
    by_id = {s["session_id"]: s for s in sessions}
    # title = 首条 user 消息前 15 字
    assert by_id["s1"]["title"] == "研究transformer的来历与发展历史"[:15]
    assert by_id["s1"]["count"] == 2
    # s2 无 user 消息 → title 回退为 session_id
    assert by_id["s2"]["title"] == "s2"
    # 按 last_update 倒序
    assert sessions[0]["session_id"] == "s2"  # 后写入的


def test_list_sessions_empty(tmp_path):
    store = _new_store(tmp_path)
    assert store.list_sessions() == []


def test_persistence_across_instances(tmp_path):
    db = str(tmp_path / "chat.db")
    ChatStore(db).add("s1", "user", "chat", "问题")
    second = ChatStore(db)
    msgs = second.get_messages("s1")
    assert len(msgs) == 1
    assert msgs[0]["content"] == "问题"


# ---------------------------------------------------------------------------
# 会话管理：重命名 / 置顶 / 排序 / 删除
# ---------------------------------------------------------------------------

def test_set_meta_rename(tmp_path):
    store = _new_store(tmp_path)
    store.add("s1", "user", "chat", "首条消息")
    store.set_meta("s1", title="自定义标题")
    sessions = store.list_sessions()
    assert sessions[0]["title"] == "自定义标题"


def test_meta_title_overrides_first_message(tmp_path):
    store = _new_store(tmp_path)
    store.add("s1", "user", "chat", "很长的第一条消息内容超过十五个字了")
    store.set_meta("s1", title="短标题")
    sessions = store.list_sessions()
    assert sessions[0]["title"] == "短标题"


def test_pin_puts_session_on_top(tmp_path):
    store = _new_store(tmp_path)
    store.add("s1", "user", "chat", "旧会话")
    store.add("s2", "user", "chat", "新会话")  # s2 更新，默认排前
    assert store.list_sessions()[0]["session_id"] == "s2"
    store.set_meta("s1", pinned=True)
    sessions = store.list_sessions()
    assert sessions[0]["session_id"] == "s1"
    assert sessions[0]["pinned"] is True
    # 取消置顶恢复原排序
    store.set_meta("s1", pinned=False)
    assert store.list_sessions()[0]["session_id"] == "s2"


def test_sort_order_drives_drag_reorder(tmp_path):
    store = _new_store(tmp_path)
    for sid in ("a", "b", "c"):
        store.add(sid, "user", "chat", f"消息{sid}")
    store.set_sort_order(["c", "a", "b"])
    sessions = store.list_sessions()
    assert [s["session_id"] for s in sessions] == ["c", "a", "b"]


def test_delete_session_removes_chat_and_meta(tmp_path):
    store = _new_store(tmp_path)
    store.add("s1", "user", "chat", "消息1")
    store.add("s1", "assistant", "chat", "回复")
    store.add("s2", "user", "chat", "其他会话")
    store.set_meta("s1", title="标题", pinned=True)
    n = store.delete_session("s1")
    assert n == 2
    assert store.get_messages("s1") == []
    assert store.get_messages("s2")  # 其他会话不受影响
    sessions = store.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "s2"
