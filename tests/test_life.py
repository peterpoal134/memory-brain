"""Independent local-life contracts; no network, model or factual-memory writes."""
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from xiaojiu_brain.life import LifeLayer
from xiaojiu_brain.storage import Store


def epoch(value):
    return dt.datetime.fromisoformat(value).timestamp()


class LifeTests(unittest.TestCase):
    NOW = epoch("2026-05-02T10:01:00+08:00")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "brain.db"
        self.store = Store(self.path)
        self.settings = {
            "life_enabled": False,
            "life_context_enabled": True,
            "life_interval_seconds": 1200,
            "life_wake_time": "08:00",
            "life_sleep_time": "23:30",
        }
        self.layers = []
        self.life = self.layer("owner")

    def tearDown(self):
        for layer in self.layers:
            layer.stop()
        self.store.close()
        self.tmp.cleanup()

    def layer(self, owner, timezone="Asia/Shanghai"):
        layer = LifeLayer(self.store, owner, timezone,
                          settings=lambda: dict(self.settings))
        self.layers.append(layer)
        return layer

    def enable(self):
        self.settings["life_enabled"] = True

    def events(self, layer=None):
        return (layer or self.life).events()["items"]

    def tasks(self, layer=None):
        return (layer or self.life).tasks()["items"]

    def books(self, layer=None):
        return (layer or self.life).books()["items"]

    def core_counts(self):
        return {name: self.store.conn.execute("SELECT count(*) FROM " + name).fetchone()[0]
                for name in ("messages", "documents", "jobs")}

    def ingest(self, message_id, *, owner="owner", conversation="chat",
               text="合成私人原话", recorded_at=None):
        return self.store.ingest(owner, conversation, message_id, "user", text,
                                 recorded_at=self.NOW if recorded_at is None else recorded_at)

    def tick(self, when=None, layer=None):
        return (layer or self.life).tick(self.NOW if when is None else when)

    def test_off_creates_no_automatic_tick_or_chat_events(self):
        raw = self.ingest("one")
        baseline = self.core_counts()
        self.assertFalse(self.tick()["ran"])
        self.life.note_chat(raw["id"], "chat")
        self.assertEqual(self.events(), [])
        self.assertEqual(self.core_counts(), baseline)
        self.assertEqual(self.life.context("chat"), "")

    def test_toggle_is_read_live_without_reconstructing_layer(self):
        self.assertFalse(self.tick()["ran"])
        self.enable()
        self.assertTrue(self.tick()["ran"])
        count = len(self.events())
        self.settings["life_enabled"] = False
        self.assertFalse(self.tick(self.NOW + 1200)["ran"])
        self.assertEqual(len(self.events()), count)
        self.assertEqual(self.life.context("chat"), "")

    def test_same_tick_is_idempotent_across_new_layer_and_store_restart(self):
        self.enable()
        self.assertTrue(self.tick()["ran"])
        original = self.events()
        again = self.layer("owner")
        self.assertFalse(self.tick(layer=again)["ran"])
        self.assertEqual(self.events(again), original)
        for layer in self.layers:
            layer.stop()
        self.store.close()
        self.store = Store(self.path)
        reopened = self.layer("owner")
        self.assertFalse(self.tick(layer=reopened)["ran"])
        self.assertEqual(self.events(reopened), original)

    def test_resuming_after_downtime_does_not_manufacture_missing_ticks(self):
        self.enable()
        self.assertTrue(self.tick()["ran"])
        before = len(self.events())
        self.assertTrue(self.tick(self.NOW + 20 * 86400)["ran"])
        self.assertEqual(len(self.events()), before + 1)
        self.assertFalse(self.tick(self.NOW + 20 * 86400)["ran"])

    def test_sleep_window_crosses_midnight_in_configured_timezone(self):
        self.enable()
        for moment, phase in [
            ("2026-05-02T23:29:00+08:00", "awake"),
            ("2026-05-02T23:30:00+08:00", "rest"),
            ("2026-05-03T07:59:00+08:00", "rest"),
            ("2026-05-03T08:00:00+08:00", "awake"),
        ]:
            with self.subTest(moment=moment):
                self.assertEqual(self.life.snapshot(now=epoch(moment))["rhythm"]["phase"], phase)

    def test_awake_window_can_itself_cross_midnight(self):
        self.settings.update(life_wake_time="22:00", life_sleep_time="07:00")
        for moment, phase in [
            ("2026-05-02T21:59:00+08:00", "rest"),
            ("2026-05-02T22:00:00+08:00", "awake"),
            ("2026-05-03T06:59:00+08:00", "awake"),
            ("2026-05-03T07:00:00+08:00", "rest"),
        ]:
            with self.subTest(moment=moment):
                self.assertEqual(self.life.snapshot(now=epoch(moment))["rhythm"]["phase"], phase)

    def test_books_read_actual_contiguous_spans_once_per_tick(self):
        self.enable()
        text = "".join("第%03d段原文。" % i for i in range(150))
        book = self.life.add_book({"title": "合成本地读物", "text": text})
        self.assertNotIn("text", self.books()[0], "book list leaked private full text")
        first = self.tick()
        source = first["event"]["source"]
        self.assertEqual(source["kind"], "book_excerpt")
        self.assertEqual(source["book_id"], book["id"])
        self.assertEqual(source["start"], 0)
        self.assertGreater(source["end"], source["start"])
        self.assertLessEqual(source["end"] - source["start"], 500)
        self.assertIn(text[source["start"]:source["end"]], first["event"]["text"])
        self.assertIs(source["program_extraction"], True)
        self.assertIs(source["model_understanding"], False)
        cursor = self.books()[0]["cursor"]
        self.assertEqual(cursor, source["end"])
        self.assertFalse(self.tick()["ran"])
        self.assertEqual(self.books()[0]["cursor"], cursor)
        second = self.tick(self.NOW + 1200)
        second_source = second["event"]["source"]
        self.assertEqual(second_source["start"], cursor)
        self.assertLessEqual(second_source["end"], len(text))
        self.assertIn(text[cursor:second_source["end"]], second["event"]["text"])

    def test_finished_book_does_not_repeat_or_overrun_last_excerpt(self):
        self.enable()
        text = "短篇只有这十一字左右。"
        self.life.add_book({"title": "短篇", "text": text})
        self.tick()
        self.tick(self.NOW + 1200)
        book = self.books()[0]
        excerpts = [e for e in self.events() if e["source"]["kind"] == "book_excerpt"]
        self.assertEqual(book["cursor"], len(text))
        self.assertEqual(len(excerpts), 1)
        self.assertEqual(excerpts[0]["source"]["end"], len(text))

    def test_book_deletion_removes_its_excerpt_events(self):
        self.enable()
        book = self.life.add_book({"title": "待删私有读物", "text": "只在本机的书摘标记"})
        self.tick()
        self.assertTrue(self.events())
        self.life.delete_book(book["id"])
        self.assertEqual(self.books(), [])
        self.assertFalse(any(e["source"].get("book_id") == book["id"] for e in self.events()))

    def test_context_has_only_state_and_this_conversations_pending_tasks(self):
        self.enable()
        self.life.add_event({"text": "不应注入的私密事件", "conversation_id": "chat-a"})
        self.life.add_task({"title": "本会话待办标记", "conversation_id": "chat-a"})
        done = self.life.add_task({"title": "已完成待办标记", "conversation_id": "chat-a"})
        self.life.update_task(done["id"], {"status": "done"})
        self.life.add_task({"title": "他会话待办标记", "conversation_id": "chat-b"})
        self.life.add_book({"title": "不能暴露的书名", "text": "不能暴露的书正文"})
        self.tick()
        context = self.life.context("chat-a", max_chars=700)
        self.assertIn("本会话待办标记", context)
        for value in ["不应注入的私密事件", "已完成待办标记", "他会话待办标记",
                      "不能暴露的书名", "不能暴露的书正文"]:
            self.assertNotIn(value, context)
        self.assertLessEqual(len(context), 700)
        self.settings["life_context_enabled"] = False
        self.assertEqual(self.life.context("chat-a"), "")

    def test_owner_records_cannot_be_read_or_mutated_by_another_owner(self):
        other = self.layer("different-owner")
        event = self.life.add_event({"text": "owner-only-event"})
        task = self.life.add_task({"title": "owner-only-task"})
        book = self.life.add_book({"title": "owner-only-book", "text": "private book contents"})
        self.assertEqual(self.events(other), [])
        self.assertEqual(self.tasks(other), [])
        self.assertEqual(self.books(other), [])
        for method, ident in [(other.delete_event, event["id"]),
                              (other.delete_task, task["id"]), (other.delete_book, book["id"])]:
            try:
                method(ident)
            except (ValueError, PermissionError, KeyError):
                pass
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(len(self.tasks()), 1)
        self.assertEqual(len(self.books()), 1)

    def test_manual_event_replay_is_idempotent_and_body_collision_rejected(self):
        payload = {"text": "已整理本地书架", "conversation_id": "chat",
                   "importance": .7, "idempotency_key": "manual-event-1"}
        first = self.life.add_event(payload)
        replayed = self.life.add_event(dict(payload))
        self.assertEqual(first["id"], replayed["id"])
        self.assertEqual(len(self.events()), 1)
        with self.assertRaises(ValueError):
            self.life.add_event({**payload, "text": "冲突的另一个正文"})
        self.assertEqual(self.events()[0]["text"], payload["text"])

    def test_task_validation_rejects_bad_status_text_or_date_without_partial_write(self):
        task = self.life.add_task({"title": "有效任务", "conversation_id": "chat"})
        for title in ["", "  ", "字" * 301, None, 123]:
            with self.subTest(title=repr(title)[:50]), self.assertRaises(ValueError):
                self.life.add_task({"title": title})
        for due_at in ["不是日期", "2026-02-30T12:00:00", float("nan"), float("inf"), True, {}]:
            with self.subTest(due_at=repr(due_at)), self.assertRaises(ValueError):
                self.life.update_task(task["id"], {"title": "不应部分保存", "due_at": due_at})
        for status in ["", "not-a-status", True, 1]:
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.life.update_task(task["id"], {"title": "不应部分保存", "status": status})
        self.assertEqual(len(self.tasks()), 1)
        self.assertEqual(self.tasks()[0]["title"], "有效任务")
        self.assertEqual(self.tasks()[0]["status"], "pending")

    def test_naive_task_date_uses_local_timezone_not_host_timezone(self):
        local = self.life.add_task({"title": "本地时间任务", "due_at": "2026-05-03T09:30:00"})
        utc = self.life.add_task({"title": "相同绝对时刻", "due_at": "2026-05-03T01:30:00+00:00"})
        self.assertEqual(local["due_at"], utc["due_at"])
        self.assertEqual(local["due_at"], epoch("2026-05-03T09:30:00+08:00"))

    def test_chat_observation_uses_real_sources_and_ten_minute_aggregation(self):
        self.enable()
        first = self.ingest("first", text="绝不能复制的原话甲")
        second = self.ingest("second", text="绝不能复制的原话乙", recorded_at=self.NOW + 4 * 60)
        third = self.ingest("third", text="另一个时间窗", recorded_at=self.NOW + 15 * 60)
        counts = self.core_counts()
        self.life.note_chat(first["id"], "chat", recorded_at=self.NOW + 100 * 86400)
        self.life.note_chat(first["id"], "chat")
        self.life.note_chat(second["id"], "chat")
        observed = [e for e in self.events() if e["source"]["kind"] == "chat_observed"]
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["source_count"], 2)
        self.assertEqual(set(observed[0]["source_message_ids"]), {first["id"], second["id"]})
        self.life.note_chat(third["id"], "chat")
        self.assertEqual(len([e for e in self.events() if e["source"]["kind"] == "chat_observed"]), 2)
        serialized = json.dumps(self.events(), ensure_ascii=False)
        self.assertNotIn("绝不能复制的原话甲", serialized)
        self.assertNotIn("绝不能复制的原话乙", serialized)
        self.assertEqual(self.core_counts(), counts)

    def test_chat_source_owner_and_conversation_cannot_be_forged(self):
        self.enable()
        foreign = self.ingest("foreign", owner="other")
        own = self.ingest("mine", conversation="actual-chat")
        for ident, conversation in [(foreign["id"], "chat"), (own["id"], "wrong-chat"), (999999, "chat")]:
            try:
                self.life.note_chat(ident, conversation)
            except (ValueError, PermissionError, KeyError):
                pass
        self.assertEqual(self.events(), [])

    def test_deleting_observed_original_recomputes_count_then_hides_empty_event(self):
        self.enable()
        first = self.ingest("one")
        second = self.ingest("two", recorded_at=self.NOW + 60)
        self.life.note_chat(first["id"], "chat")
        self.life.note_chat(second["id"], "chat")
        self.store.delete_message("owner", first["id"])
        self.assertEqual(self.events()[0]["source_count"], 1)
        self.assertEqual(self.events()[0]["source_message_ids"], [second["id"]])
        self.store.delete_message("owner", second["id"])
        self.assertEqual(self.events(), [])

    def test_journal_is_deterministic_local_record_not_invented_history(self):
        with patch("xiaojiu_brain.life.time.time", return_value=self.NOW):
            self.life.add_event({"text": "已整理本地书架", "conversation_id": "chat"})
            self.life.add_task({"title": "等待修补书套", "conversation_id": "chat"})
            baseline = self.core_counts()
            first = self.life.journal("2026-05-02")
            second = self.life.journal("2026-05-02")
        self.assertEqual(first, second)
        self.assertEqual(first["kind"], "deterministic_journal")
        self.assertEqual(first["timezone"], "Asia/Shanghai")
        self.assertEqual(first["date"], "2026-05-02")
        self.assertIn("已整理本地书架", first["text"])
        self.assertEqual(len(first["events"]), 1)
        self.assertEqual(self.core_counts(), baseline)
        self.assertEqual(len(self.events()), 1, "journal generation wrote itself as a new event")

    def test_life_tables_are_in_online_sqlite_backup(self):
        self.life.add_event({"text": "backup-event"})
        self.life.add_task({"title": "backup-task"})
        self.life.add_book({"title": "backup-book", "text": "backup-source"})
        path = Path(self.tmp.name) / "snapshot.db"
        result = self.store.backup(path)
        self.assertEqual(result["integrity"], "ok")
        restored = Store(path)
        try:
            layer = LifeLayer(restored, "owner", "Asia/Shanghai", settings=lambda: dict(self.settings))
            try:
                self.assertEqual(layer.events()["items"][0]["text"], "backup-event")
                self.assertEqual(layer.tasks()["items"][0]["title"], "backup-task")
                self.assertEqual(layer.books()["items"][0]["title"], "backup-book")
            finally:
                layer.stop()
        finally:
            restored.close()

    def test_concurrent_ticks_commit_only_one_event_and_one_cursor_advance(self):
        self.enable()
        text = "并发书摘正文" * 200
        self.life.add_book({"title": "并发读物", "text": text})
        second_layer = self.layer("owner")
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: self.tick(layer=self.life if i % 2 else second_layer), range(8)))
        self.assertEqual(sum(result["ran"] for result in results), 1)
        excerpts = [e for e in self.events() if e["source"]["kind"] == "book_excerpt"]
        self.assertEqual(len(excerpts), 1)
        self.assertEqual(self.books()[0]["cursor"], excerpts[0]["source"]["end"])
        self.assertLessEqual(self.books()[0]["cursor"], 500)

    def test_all_life_operations_leave_factual_memory_and_jobs_untouched(self):
        before = self.core_counts()
        event = self.life.add_event({"text": "本地模拟事件"})
        task = self.life.add_task({"title": "本地模拟待办"})
        book = self.life.add_book({"title": "本地模拟书", "text": "实际本地字句"})
        self.life.update_task(task["id"], {"status": "done"})
        self.enable()
        self.tick()
        self.life.snapshot(now=self.NOW)
        self.life.context("manual")
        self.life.journal("2026-05-02")
        self.life.delete_event(event["id"])
        self.life.delete_task(task["id"])
        self.life.delete_book(book["id"])
        self.assertEqual(self.core_counts(), before)

    def test_background_start_stop_restart_keeps_tick_idempotent(self):
        self.enable()
        observed = threading.Event()
        real_tick = self.life.tick

        def tick_at_fixed_time():
            result = real_tick(self.NOW)
            observed.set()
            return result

        with patch.object(self.life, "tick", side_effect=tick_at_fixed_time):
            self.assertIs(self.life.start(), self.life)
            self.assertIs(self.life.start(), self.life)
            try:
                self.assertTrue(observed.wait(1), "background life worker did not tick")
            finally:
                self.assertTrue(self.life.stop(timeout=1))
            self.assertTrue(self.life.stop(timeout=1))
            self.assertEqual(len(self.events()), 1)
            observed.clear()
            self.life.start()
            try:
                self.assertTrue(observed.wait(1))
            finally:
                self.assertTrue(self.life.stop(timeout=1))
            self.assertEqual(len(self.events()), 1, "restart repeated the already committed tick")

    def needs(self, when, layer=None):
        state = (layer or self.life).snapshot(now=when)
        self.assertIn("rule_simulation", json.dumps(state), "needs must identify their rule-only provenance")
        result = state["needs"]
        for key in ("energy", "boredom", "social", "curiosity"):
            self.assertIsInstance(result[key], (int, float))
            self.assertNotIsInstance(result[key], bool)
            self.assertGreaterEqual(result[key], 0)
            self.assertLessEqual(result[key], 1)
        return result

    def test_needs_project_time_without_recording_an_invented_activity(self):
        self.enable()
        wake = epoch("2026-05-02T08:00:00+08:00")
        self.tick(wake)
        events_before = self.events()
        core_before = self.core_counts()
        noon = self.needs(epoch("2026-05-02T12:00:00+08:00"))
        afternoon = self.needs(epoch("2026-05-02T16:00:00+08:00"))
        self.assertLess(afternoon["energy"], noon["energy"])
        for key in ("boredom", "social", "curiosity"):
            self.assertGreater(afternoon[key], noon[key])
        early_rest = self.needs(epoch("2026-05-03T00:30:00+08:00"))
        late_rest = self.needs(epoch("2026-05-03T05:30:00+08:00"))
        self.assertGreater(late_rest["energy"], early_rest["energy"])
        self.assertEqual(self.events(), events_before)
        self.assertEqual(self.core_counts(), core_before)

    def test_real_chat_and_book_have_distinct_bounded_activity_effects(self):
        self.enable()
        wake = epoch("2026-05-02T08:00:00+08:00")
        moment = epoch("2026-05-02T14:00:00+08:00")
        self.tick(wake)
        idle = self.needs(moment)
        raw = self.ingest("needs-chat", text="真正发生的合成聊天", recorded_at=moment)
        self.life.note_chat(raw["id"], "chat")
        after_chat = self.needs(moment)
        self.assertLess(after_chat["social"], idle["social"])
        self.assertLess(after_chat["boredom"], idle["boredom"])
        self.assertGreater(after_chat["curiosity"], idle["curiosity"])
        self.assertLess(after_chat["energy"], idle["energy"])
        book = self.life.add_book({"title": "真实本地书摘", "text": "确实存在的本地原文。"})
        self.tick(moment)
        after_book = self.needs(moment)
        self.assertGreater(after_book["curiosity"], after_chat["curiosity"])
        self.assertEqual(after_book["social"], after_chat["social"])
        self.assertLess(after_book["energy"], after_chat["energy"])
        self.store.delete_message("owner", raw["id"])
        self.assertEqual(self.needs(moment)["social"], idle["social"],
                         "deleted source still counted as verified chat activity")
        self.life.delete_book(book["id"])
        self.assertEqual(self.needs(moment)["curiosity"], idle["curiosity"],
                         "deleted book still counted as a verified reading activity")

    def test_needs_are_bounded_and_do_not_reset_on_database_restart(self):
        self.enable()
        self.tick(self.NOW)
        moment = self.NOW + 4 * 3600
        expected = self.needs(moment)
        started = self.store.conn.execute("SELECT started_at FROM life_runtime WHERE owner=?", ("owner",)).fetchone()[0]
        self.needs(self.NOW + 100 * 365 * 86400)
        for layer in self.layers:
            layer.stop()
        self.store.close()
        self.store = Store(self.path)
        with patch("xiaojiu_brain.life.time.time", return_value=moment):
            reopened = self.layer("owner")
        self.assertEqual(self.needs(moment, reopened), expected)
        self.assertEqual(self.store.conn.execute("SELECT started_at FROM life_runtime WHERE owner=?", ("owner",)).fetchone()[0], started)
        self.assertEqual(len(self.events(reopened)), 1)

    def test_old_runtime_without_started_at_migrates_without_resetting_last_tick(self):
        old = Store(Path(self.tmp.name) / "old-life.db")
        try:
            old.conn.executescript("""
                CREATE TABLE life_runtime(owner TEXT PRIMARY KEY,last_tick_at REAL,
                    current_event_id TEXT,error TEXT);
            """)
            old.conn.execute("INSERT INTO life_runtime VALUES(?,?,NULL,NULL)", ("legacy", self.NOW))
            with patch("xiaojiu_brain.life.time.time", return_value=self.NOW + 3600):
                layer = LifeLayer(old, "legacy", "Asia/Shanghai", settings=lambda: dict(self.settings))
            try:
                row = old.conn.execute("SELECT started_at,last_tick_at FROM life_runtime WHERE owner='legacy'").fetchone()
                self.assertEqual(row["last_tick_at"], self.NOW)
                self.assertEqual(row["started_at"], self.NOW)
                early = self.needs(self.NOW + 3600, layer)
                late = self.needs(self.NOW + 7200, layer)
                self.assertGreater(late["boredom"], early["boredom"])
                again = LifeLayer(old, "legacy", "Asia/Shanghai", settings=lambda: dict(self.settings))
                try:
                    self.assertEqual(again.snapshot(now=self.NOW + 7200)["needs"], late)
                    self.assertEqual(again.events()["items"], [])
                finally:
                    again.stop()
            finally:
                layer.stop()
        finally:
            old.close()

    def test_paused_needs_can_project_without_context_or_catchup_events(self):
        self.enable()
        self.tick(self.NOW)
        initial = self.needs(self.NOW)
        events_before = self.events()
        self.settings["life_enabled"] = False
        later = self.needs(self.NOW + 6 * 3600)
        self.assertGreater(later["social"], initial["social"])
        self.assertEqual(self.life.context("chat"), "")
        self.assertFalse(self.tick(self.NOW + 20 * 86400)["ran"])
        self.assertEqual(self.events(), events_before)
        self.settings["life_enabled"] = True
        self.assertTrue(self.tick(self.NOW + 20 * 86400)["ran"])
        self.assertEqual(len(self.events()), len(events_before) + 1,
                         "resuming invented events for the stopped interval")

    def test_migration_does_not_treat_a_backdated_manual_note_as_activation_time(self):
        self.enable()
        self.tick(self.NOW)
        note=self.life.add_event({'text':'合成历史手工记录'})
        self.store.conn.execute('UPDATE life_events SET occurred_at=? WHERE id=?',
            (self.NOW-365*86400,note['id']))
        self.store.conn.execute('UPDATE life_runtime SET started_at=NULL WHERE owner=?',('owner',))
        reopened=self.layer('owner')
        self.assertEqual(reopened.snapshot(now=self.NOW)['started_at'],self.NOW)


if __name__ == "__main__":
    unittest.main()
