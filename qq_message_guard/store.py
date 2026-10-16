from __future__ import annotations

import asyncio
import json
import time

import aiosqlite

from .config import Deferred, GuardError
from .muting import member_key, observation_key


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = asyncio.Lock()
        self.db = None

    async def open(self):
        self.db = await aiosqlite.connect(self.path, timeout=10)
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript("""
            PRAGMA auto_vacuum=INCREMENTAL;
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL UNIQUE, scope TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, state TEXT NOT NULL,
                flagged INTEGER NOT NULL DEFAULT 0, notified INTEGER NOT NULL DEFAULT 0,
                human TEXT NOT NULL DEFAULT '', action_ready INTEGER NOT NULL DEFAULT 0,
                notify_enabled INTEGER NOT NULL DEFAULT 0, settings_revision TEXT NOT NULL,
                data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS case_work ON cases(state,created);
            CREATE INDEX IF NOT EXISTS case_scope ON cases(scope,created);
            CREATE INDEX IF NOT EXISTS case_actions ON cases(action_ready,state,created);
            CREATE INDEX IF NOT EXISTS case_alerts ON cases(settings_revision,notify_enabled,notified,created);
            CREATE INDEX IF NOT EXISTS case_retention ON cases(updated);
            CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY, case_id INTEGER, at REAL NOT NULL, ok INTEGER NOT NULL,
                model TEXT NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
                elapsed_ms REAL NOT NULL, error TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS call_time ON calls(at);
            CREATE TABLE IF NOT EXISTS operations (
                id INTEGER PRIMARY KEY, case_id INTEGER, kind TEXT NOT NULL, account TEXT NOT NULL,
                at REAL NOT NULL, state TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
                UNIQUE(case_id,kind));
            CREATE INDEX IF NOT EXISTS op_account ON operations(account,at);
            CREATE TABLE IF NOT EXISTS reads (account TEXT NOT NULL, at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS read_account ON reads(account,at);
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS garbage (file TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, mid TEXT NOT NULL,
                user TEXT NOT NULL, at REAL NOT NULL, text TEXT NOT NULL,
                UNIQUE(scope,mid));
            CREATE INDEX IF NOT EXISTS history_scope ON history(scope,id);
            CREATE INDEX IF NOT EXISTS history_sender ON history(scope,user,id);
            CREATE TABLE IF NOT EXISTS screenings (
                id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, state TEXT NOT NULL,
                stage TEXT NOT NULL, flagged INTEGER NOT NULL DEFAULT 0,
                case_ids TEXT NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS screening_retention ON screenings(updated);
            CREATE TABLE IF NOT EXISTS image_cache (
                key TEXT PRIMARY KEY, source_case INTEGER NOT NULL, expires REAL NOT NULL,
                accessed REAL NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS image_cache_expiry ON image_cache(expires);
            CREATE TABLE IF NOT EXISTS mute_strikes (
                case_id INTEGER PRIMARY KEY, scope TEXT NOT NULL, user TEXT NOT NULL, at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS mute_strike_member ON mute_strikes(scope,user,at);
        """)
        columns = {row["name"] for row in await self._rows("PRAGMA table_info(calls)")}
        if "stage" not in columns:
            await self.db.execute("ALTER TABLE calls ADD COLUMN stage TEXT NOT NULL DEFAULT 'detail'")
        if "screening_id" not in columns:
            await self.db.execute("ALTER TABLE calls ADD COLUMN screening_id INTEGER")
        history_columns = {row["name"] for row in await self._rows("PRAGMA table_info(history)")}
        if "sender_name" not in history_columns:
            await self.db.execute("ALTER TABLE history ADD COLUMN sender_name TEXT NOT NULL DEFAULT ''")
        await self.db.execute("PRAGMA user_version=6")
        await self.call("recover", time.time())

    async def close(self):
        if self.db:
            await self.db.close()
            self.db = None

    async def call(self, name, *args):
        async with self.lock:
            try:
                result = await getattr(self, name)(*args)
                await self.db.commit()
                return result
            except BaseException:
                await self.db.rollback()
                raise

    async def _row(self, sql, args=()):
        async with self.db.execute(sql, args) as cursor:
            return await cursor.fetchone()

    async def _rows(self, sql, args=()):
        async with self.db.execute(sql, args) as cursor:
            return await cursor.fetchall()

    @staticmethod
    def _case(row):
        return {**dict(row), "data": json.loads(row["data"])} if row else None

    async def get(self, key, default=None):
        row = await self._row("SELECT value FROM state WHERE key=?", (key,))
        return json.loads(row[0]) if row else default

    async def put(self, key, value):
        await self.db.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (key, json.dumps(value)))

    async def history_context(self, message, group_count, sender_count):
        """Receive order, with no age filter. Background never contains the current message."""
        scope, mid = message.scope, message.mid
        recent = await self._rows(
            "SELECT * FROM history WHERE scope=? AND mid!=? ORDER BY id DESC LIMIT ?",
            (scope, mid, group_count),
        )
        author = await self._rows(
            "SELECT * FROM history WHERE scope=? AND user=? AND mid!=? ORDER BY id DESC LIMIT ?",
            (scope, message.user, mid, sender_count),
        )
        selected = {row["id"]: dict(row) for row in [*recent, *author]}
        quote = None
        if message.reply and message.reply != mid:
            quote = await self._row("SELECT * FROM history WHERE scope=? AND mid=?", (scope, message.reply))
            if quote:
                selected[quote["id"]] = dict(quote)
                selected[quote["id"]]["quoted"] = True
        return sorted(selected.values(), key=lambda row: row["id"]), bool(quote)

    async def remember(self, message, capacity):
        text = message.text[:500]
        if message.images:
            text += " [图片未识别]"
        if message.unsupported:
            text += " [其他内容未识别]"
        inserted = await self.db.execute(
            "INSERT OR IGNORE INTO history(scope,mid,user,at,text,sender_name) VALUES(?,?,?,?,?,?)",
            (message.scope, message.mid, message.user, message.at, text, message.sender_name),
        )
        # Fixed count cache persists across reloads; no wall-clock cutoff.
        await self.db.execute(
            "DELETE FROM history WHERE scope=? AND id NOT IN"
            " (SELECT id FROM history WHERE scope=? ORDER BY id DESC LIMIT ?)",
            (message.scope, message.scope, capacity),
        )
        return inserted.rowcount > 0

    async def recover(self, now):
        await self.db.execute(
            "UPDATE screenings SET state='incomplete',updated=? WHERE state='running'", (now,)
        )
        for row in await self._rows("SELECT id FROM cases WHERE state IN ('queued','auditing')"):
            await self.finish(row["id"], "incomplete", {"reason": "插件重载中断，未完成审核"}, now)
        for row in await self._rows("SELECT * FROM operations WHERE state='submitted'"):
            await self.operation(row["id"], "unknown", "进程中断，已提交请求的结果需要人工核对", now)
            await self.put("pause:" + row["account"], "上次提交结果不明，请核对后使用恢复命令")
        for row in await self._rows("SELECT id FROM operations WHERE state='reserved'"):
            await self.operation(row["id"], "not_sent", "进程中断，请求尚未提交", now)
        # Repair the split bookkeeping left by versions before 0.9.1. Do not
        # replay operations or reconstruct historical mute counters.
        for row in await self._rows(
            "SELECT o.* FROM operations o JOIN cases c ON c.id=o.case_id"
            " WHERE o.kind='recall' AND o.state='confirmed' AND c.state!='recalled'"
        ):
            await self.finish(
                row["case_id"],
                "recalled",
                {"reason": "撤回接口已确认成功，启动时校正记录"},
                now,
                flagged=True,
            )
        for row in await self._rows(
            "SELECT o.* FROM operations o JOIN cases c ON c.id=o.case_id"
            " WHERE o.kind='mute' AND o.state IN ('not_sent','failed','unknown')"
            " AND json_extract(c.data,'$.mute_status') IN ('planned','reserved','submitted')"
        ):
            await self.patch(row["case_id"], {"mute_status": row["state"], "mute_reason": row["detail"]})
        for row in await self._rows(
            "SELECT * FROM cases WHERE action_ready=1 OR json_extract(data,'$.actions_pending')=1"
        ):
            case = self._case(row)
            if case["data"].get("auto_ready") or case["data"].get("actions_pending"):
                await self.finish(
                    case["id"],
                    case["state"],
                    {
                        "auto_ready": False,
                        "actions_pending": False,
                        "reason": case["data"].get("reason", "")
                        if case["state"] in ("recalled", "unknown", "failed")
                        else "重载取消了尚未提交的自动操作，请人工复核",
                    },
                    now,
                    flagged=bool(case["flagged"]),
                )

    async def add(self, message, payload, now, capacity, initial="queued", reason=""):
        if await self._row("SELECT 1 FROM cases WHERE event_key=?", (message.event_key,)):
            return None
        pending = await self._row("SELECT count(*) FROM cases WHERE state IN ('queued','auditing')")
        if initial == "queued" and pending[0] >= capacity:
            initial, reason = "incomplete", "审核队列已满，未完成审核"
        if initial != "queued":
            payload = self._clean_media_refs({**payload, "reason": reason})
        cursor = await self.db.execute(
            "INSERT INTO cases(event_key,scope,created,updated,state,notify_enabled,settings_revision,data,flagged)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (
                message.event_key,
                message.scope,
                now,
                now,
                initial,
                int(payload.get("notification_enabled", False)),
                payload["settings_revision"],
                json.dumps(payload, ensure_ascii=False),
                int(
                    initial != "queued"
                    and payload.get("keyword_review", False)
                    and bool(payload.get("keyword_matches"))
                ),
            ),
        )
        return await self.case(cursor.lastrowid)

    async def case(self, cid):
        return self._case(await self._row("SELECT * FROM cases WHERE id=?", (cid,)))

    async def next_case(self, served):
        rows = await self._rows("SELECT * FROM cases WHERE state='queued' ORDER BY created,id LIMIT 1000")
        if not rows:
            return None
        # Oldest message from the least recently served group. Fairness is across groups,
        # while each group's own order stays chronological.
        row = min(rows, key=lambda r: (served.get(r["scope"], 0), r["created"], r["id"]))
        await self.db.execute("UPDATE cases SET state='auditing' WHERE id=?", (row["id"],))
        return await self.case(row["id"])

    async def next_work(self, served, settings, now, estimates=None):
        from .batching import choose_work

        rows = await self._rows("SELECT * FROM cases WHERE state='queued' ORDER BY created,id LIMIT 1000")
        cases, wait = choose_work([self._case(row) for row in rows], served, settings, now, estimates)
        await self.db.executemany("UPDATE cases SET state='auditing' WHERE id=?", [(c["id"],) for c in cases])
        return [await self.case(c["id"]) for c in cases], wait

    async def queue_stage(self, cid, batch, now):
        current = await self.case(cid)
        if current is None or current["state"] != "auditing" or current["human"]:
            return
        await self.patch(cid, {"batch": batch, "work_queued_at": now})
        await self.db.execute("UPDATE cases SET state='queued',updated=? WHERE id=?", (now, cid))

    async def begin_screening(self, cases, stage, request, now):
        cursor = await self.db.execute(
            "INSERT INTO screenings(scope,created,updated,state,stage,case_ids,data)"
            " VALUES(?,?,?,'running',?,?,?)",
            (
                cases[0]["scope"],
                now,
                now,
                stage,
                json.dumps([c["id"] for c in cases]),
                json.dumps({"request": request}, ensure_ascii=False),
            ),
        )
        sid = cursor.lastrowid
        for index, case in enumerate(cases):
            batch = dict(case["data"].get("batch", {}))
            batch["screening_ids"] = [*batch.get("screening_ids", []), sid]
            if stage == "screen":
                batch["label"] = request["state"]["待初筛消息"][index][0]
            await self.patch(case["id"], {"batch": batch})
        return sid

    async def finish_screening(self, sid, now, result=None, error="", flagged=False):
        row = await self._row("SELECT data FROM screenings WHERE id=?", (sid,))
        data = json.loads(row[0])
        data.update(response=result.raw_response if result else None, error=error)
        await self.db.execute(
            "UPDATE screenings SET state=?,updated=?,flagged=?,data=? WHERE id=?",
            (
                "completed" if result else "incomplete",
                now,
                int(flagged),
                json.dumps(data, ensure_ascii=False),
                sid,
            ),
        )

    async def screening(self, sid):
        row = await self._row("SELECT * FROM screenings WHERE id=?", (sid,))
        return (
            {**dict(row), "data": json.loads(row["data"]), "case_ids": json.loads(row["case_ids"])}
            if row
            else None
        )

    async def flag_screenings(self, ids):
        await self.db.executemany("UPDATE screenings SET flagged=1 WHERE id=?", [(sid,) for sid in ids])

    @staticmethod
    def _clean_media_refs(data):
        data = dict(data)
        message = dict(data["message"])
        message["images"] = []  # Remove expiring, signed media URLs after processing.
        data["message"] = message
        return data

    async def patch(self, cid, changes):
        case = await self.case(cid)
        if not case:
            raise GuardError("审核记录已过期。")
        data = {**case["data"], **changes}
        await self.db.execute(
            "UPDATE cases SET data=?,action_ready=? WHERE id=?",
            (json.dumps(data, ensure_ascii=False), int(data.get("auto_ready", False)), cid),
        )

    async def finish(self, cid, state, changes, now, flagged=False):
        case = await self.case(cid)
        if not case:
            raise GuardError("审核记录已过期。")
        # A recall notice can arrive while a model call is still running.
        if case["state"] == "recalled" and state != "recalled":
            state = "recalled"
            changes = {**changes, "auto_ready": False, "reason": case["data"].get("reason", "收到撤回通知")}
            flagged = flagged or bool(case["flagged"])
        data = self._clean_media_refs({**case["data"], **changes})
        if state == "incomplete":
            flagged = (
                flagged
                or bool(case["flagged"])
                or bool(data.get("keyword_review") and data.get("keyword_matches"))
            )
        await self.db.execute(
            "UPDATE cases SET state=?,flagged=?,updated=?,action_ready=?,data=? WHERE id=?",
            (
                state,
                int(flagged),
                now,
                int(data.get("auto_ready", False)),
                json.dumps(data, ensure_ascii=False),
                cid,
            ),
        )
        return await self.case(cid)

    async def recalled(self, scope, mid, now):
        await self.db.execute("DELETE FROM history WHERE scope=? AND mid=?", (scope, mid))
        for row in await self._rows(
            "SELECT * FROM cases WHERE scope=? AND state!='recalled'"
            " AND json_extract(data,'$.message.mid')=?",
            (scope, mid),
        ):
            case = self._case(row)
            if case["data"]["message"]["mid"] == mid:
                changes = {"reason": "收到撤回通知"}
                if case["data"].get("auto_ready"):
                    # A queued message recalled by somebody else must not leave
                    # its administrator alert waiting for a worker that won't pick it.
                    changes.update(auto_ready=False, actions_pending=False)
                await self.finish(case["id"], "recalled", changes, now, flagged=bool(case["flagged"]))

    async def next_recall(self):
        row = await self._row(
            "SELECT * FROM cases WHERE action_ready=1 AND state='review' AND human=''"
            " ORDER BY created,id LIMIT 1"
        )
        if not row:
            return None
        case = self._case(row)
        data = {**case["data"], "auto_ready": False}
        await self.db.execute(
            "UPDATE cases SET data=?,action_ready=0 WHERE id=?",
            (json.dumps(data, ensure_ascii=False), case["id"]),
        )
        return await self.case(case["id"])

    async def recent(self, group="", pending=False, limit=10):
        filters, args = [], []
        if group:
            filters.append("json_extract(data,'$.message.group')=?")
            args.append(group)
        if pending:
            filters.append("human='' AND state IN ('review','incomplete','unknown','failed')")
        where = " WHERE " + " AND ".join(filters) if filters else ""
        rows = await self._rows("SELECT * FROM cases" + where + " ORDER BY id DESC LIMIT ?", (*args, limit))
        result = []
        for row in rows:
            case = self._case(row)
            if group and case["data"]["message"]["group"] != group:
                continue
            if pending and (
                case["human"] or case["state"] not in ("review", "incomplete", "unknown", "failed")
            ):
                continue
            result.append(case)
            if len(result) >= limit:
                break
        return result

    async def mark(self, cid, human, actor, now):
        case = await self.case(cid)
        if not case:
            raise GuardError("审核记录不存在或已过期。")
        data = {**case["data"], "human_actor": actor, "human_at": now}
        await self.db.execute("DELETE FROM image_cache WHERE source_case=?", (cid,))
        await self.db.execute(
            "UPDATE cases SET action_ready=0,data=json_set(data,'$.cache_invalidated',json('true'),"
            "'$.auto_ready',json('false'),'$.actions_pending',json('false'),"
            "'$.notification_ready',json('false')) WHERE json_extract(data,'$.cache_source')=?",
            (cid,),
        )
        if human == "违规":
            await self.db.executemany(
                "UPDATE screenings SET flagged=1,updated=? WHERE id=?",
                [(now, sid) for sid in data.get("batch", {}).get("screening_ids", [])],
            )
        if human == "误判":
            message = data["message"]
            key = member_key(case["scope"], message["user"])
            history = await self.get(key)
            if history:
                sources = history.get("source_ids", [])
                linked = await self._rows(
                    "SELECT id FROM cases WHERE json_extract(data,'$.cache_source')=?", (cid,)
                )
                if set(sources) & {cid, *(r["id"] for r in linked)}:
                    # Do not release a QQ mute automatically; just remove its
                    # invalid basis from future escalation.
                    await self.put(key, {**history, "duration": 0, "source_ids": [], "invalidated_by": cid})
        await self.db.execute(
            "UPDATE cases SET human=?,updated=?,data=?,action_ready=0 WHERE id=?",
            (human, now, json.dumps(data, ensure_ascii=False), cid),
        )

    async def record_call(
        self, cid, now, result=None, elapsed=0, error="", stage="detail", screening_id=None
    ):
        await self.db.execute(
            "INSERT INTO calls(case_id,at,ok,model,input_tokens,output_tokens,elapsed_ms,error,stage,screening_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                cid,
                now,
                int(result is not None),
                result.model if result else "",
                result.input_tokens if result else 0,
                result.output_tokens if result else 0,
                result.elapsed_ms if result else elapsed,
                error[:500],
                stage,
                screening_id,
            ),
        )

    async def read(self, account, now, limit):
        await self.db.execute("DELETE FROM reads WHERE at<?", (now - 3600,))
        count = await self._row("SELECT count(*) FROM reads WHERE account=?", (account,))
        if count[0] >= limit:
            raise Deferred("本插件QQ查询预算已用完，本次不操作。")
        await self.db.execute("INSERT INTO reads VALUES(?,?)", (account, now))

    async def can_write(self, cid, kind, account, now, limit, group_warnings=5):
        if cid is not None and await self._row(
            "SELECT 1 FROM operations WHERE case_id=? AND kind=? AND state!='not_sent'", (cid, kind)
        ):
            raise Deferred("该记录已有操作提交，不重复发送；请查看操作结果。")
        if await self.get("pause:" + account):
            raise Deferred("这个账号的审核操作已暂停，请检查状态。")
        category = (
            "kind='recall'"
            if kind == "recall"
            else "kind='mute'"
            if kind == "mute"
            else "kind NOT IN ('recall','mute')"
        )
        count = await self._row(
            "SELECT count(*) FROM operations WHERE account=? AND at>? AND state!='not_sent' AND " + category,
            (account, now - 3600),
        )
        if count[0] >= limit:
            raise Deferred("本插件每小时操作额度已用完。")
        if now < await self.get("gap:" + account, 0):
            raise Deferred("操作间隔尚未结束。")
        if kind == "warn":
            case = await self.case(cid)
            message = case["data"]["message"]
            warn_key = f"warn:{account}:{message['group']}:{message['user']}"
            if now < await self.get(warn_key, 0):
                raise Deferred("这个成员的群内警告仍在冷却期，不重复@。")
            count = await self._row(
                "SELECT count(*) FROM operations o JOIN cases c ON c.id=o.case_id"
                " WHERE o.account=? AND c.scope=? AND o.kind='warn' AND o.at>?"
                " AND o.state!='not_sent'",
                (account, case["scope"], now - 3600),
            )
            if count[0] >= group_warnings:
                raise Deferred("这个群的每小时警告次数已达上限。")

    async def reserve(
        self, cid, kind, account, now, limit, gap, warn_minutes=60, group_warnings=5, cooldowns=()
    ):
        await self.can_write(cid, kind, account, now, limit, group_warnings)
        keys = list(cooldowns)
        if kind == "warn":
            message = (await self.case(cid))["data"]["message"]
            keys.append((f"warn:{account}:{message['group']}:{message['user']}", now + warn_minutes * 60))
        for key, _ in keys:
            if now < await self.get(key, 0):
                raise Deferred("这位成员的提醒仍在冷却期，不重复发送。")
        previous = await self._row(
            "SELECT id FROM operations WHERE case_id=? AND kind=? AND state='not_sent'", (cid, kind)
        )
        if previous:
            # Reuse only an intention known not to have reached the transport.
            # The serialized transaction keeps the quota and unique key intact.
            oid = previous["id"]
            await self.db.execute(
                "UPDATE operations SET account=?,at=?,state='reserved',detail='' WHERE id=?",
                (account, now, oid),
            )
        else:
            cursor = await self.db.execute(
                "INSERT INTO operations(case_id,kind,account,at,state) VALUES(?,?,?,?,'reserved')",
                (cid, kind, account, now),
            )
            oid = cursor.lastrowid
        await self.put("gap:" + account, now + gap)
        for key, until in keys:
            await self.put(key, until)
        if keys:
            await self.put(f"op-cooldowns:{oid}", keys)
        return oid

    async def operation(self, oid, state, detail="", confirmed_at=None):
        previous = await self._row("SELECT * FROM operations WHERE id=?", (oid,))
        if previous and previous["state"] == "confirmed":
            return  # A later cancellation or bookkeeping failure cannot undo QQ's acknowledgement.
        await self.db.execute("UPDATE operations SET state=?,detail=? WHERE id=?", (state, detail[:500], oid))
        if previous:
            if state == "confirmed":
                await self.confirmed_member_operation(
                    previous, confirmed_at if confirmed_at is not None else previous["at"]
                )
            elif previous["kind"] == "mute" and previous["case_id"] is not None:
                await self.patch(previous["case_id"], {"mute_status": state, "mute_reason": detail[:500]})
            elif previous["kind"] == "recall" and state in ("unknown", "failed"):
                if previous["case_id"] is not None:
                    await self.finish(
                        previous["case_id"],
                        state,
                        {"reason": detail},
                        confirmed_at if confirmed_at is not None else time.time(),
                        flagged=True,
                    )
            if state == "unknown":
                # Persist the pause with the uncertain outcome. A power loss
                # between separate commits must not silently re-enable writes.
                await self.put("pause:" + previous["account"], "操作结果不明，请核对后恢复")
        if state in ("not_sent", "failed", "confirmed", "unknown"):
            key = f"op-cooldowns:{oid}"
            reservations = await self.get(key, [])
            if state in ("not_sent", "failed"):
                for name, until in reservations:
                    if await self.get(name) == until:
                        await self.db.execute("DELETE FROM state WHERE key=?", (name,))
            await self.db.execute("DELETE FROM state WHERE key=?", (key,))

    async def confirm(self, oid, now, gap):
        operation = await self._row("SELECT * FROM operations WHERE id=?", (oid,))
        if operation is None:
            raise GuardError("QQ操作记录不存在，无法保存回执。")
        await self.operation(oid, "confirmed", "", now)
        # Pace from the response, in the same transaction as the acknowledgement.
        await self.put("gap:" + operation["account"], now + gap)

    async def confirmed_member_operation(self, operation, now):
        if operation["kind"] not in ("recall", "mute", "warn") or operation["case_id"] is None:
            return
        case = await self.case(operation["case_id"])
        if case is None:
            return
        if operation["kind"] == "warn":
            await self.patch(case["id"], {"warned": True})
            return
        data, message = case["data"], case["data"]["message"]
        if operation["kind"] == "recall":
            await self.finish(case["id"], "recalled", {"reason": "撤回接口确认成功"}, now, flagged=True)
            if (
                data.get("recall_automatic")
                and data.get("mute_policy", {}).get("enabled")
                and case["flagged"]
                and case["human"] != "误判"
                and not data.get("cache_invalidated")
            ):
                await self.db.execute(
                    "INSERT OR IGNORE INTO mute_strikes VALUES(?,?,?,?)",
                    (case["id"], case["scope"], message["user"], now),
                )
            return
        plan = data.get("mute_plan", {})
        duration = plan.get("duration")
        if type(duration) is not int or not 1 <= duration <= 2592000:
            raise GuardError("禁言已提交但计划无效，请人工核对，不自动重试。")
        sources = plan.get("source_ids", [])
        valid, invalid = [], False
        for cid in sources:
            source = await self.case(cid)
            if source:
                valid.append(cid)
                invalid |= source["human"] == "误判" or bool(source["data"].get("cache_invalidated"))
        observed = await self.get(observation_key(case["scope"], message["user"]), {})
        until = now + duration
        event_at = 0
        if observed.get("received_at", 0) >= operation["at"] and observed.get("event_at", 0) >= int(
            operation["at"]
        ):
            until, event_at = observed["until"], observed["event_at"]
        history = {
            "duration": 0 if invalid else duration,
            "until": until,
            "muted_at": operation["at"],
            "case_id": case["id"],
            "source_ids": valid,
            "observed_event": event_at,
        }
        await self.put(member_key(case["scope"], message["user"]), history)
        await self.patch(
            case["id"],
            {
                "mute_status": "confirmed",
                "mute_seconds": duration,
                "mute_until": until,
                "mute_reason": plan["reason"],
            },
        )

    async def mute_strikes(self, scope, user, since, now, message_after=0):
        return [
            dict(row)
            for row in await self._rows(
                "SELECT s.case_id,s.at FROM mute_strikes s JOIN cases c ON c.id=s.case_id"
                " WHERE s.scope=? AND s.user=? AND s.at>=? AND s.at<=? AND c.human!='误判'"
                " AND json_extract(c.data,'$.message.at')>=?"
                " AND COALESCE(json_extract(c.data,'$.cache_invalidated'),0)!=1 ORDER BY s.at,s.case_id",
                (scope, user, since, now, message_after),
            )
        ]

    async def mute_notice(self, scope, user, stamp, duration, now):
        key = member_key(scope, user)
        history = await self.get(key)
        observed = await self.get(observation_key(scope, user), {})
        if stamp < max(int((history or {}).get("muted_at", 0)), observed.get("event_at", 0)):
            return
        until = stamp + duration
        await self.put(observation_key(scope, user), {"event_at": stamp, "until": until, "received_at": now})
        if history:
            await self.put(key, {**history, "until": until, "observed_event": stamp})

    async def mute_observe_member(self, scope, user, until, now, message_at):
        key = member_key(scope, user)
        history = await self.get(key)
        if history is None:
            return None
        actual = history["until"]
        if until > now:
            actual = until
        elif history["until"] > now and message_at >= int(history["muted_at"]):
            actual = max(message_at, history["muted_at"])
        elif 0 < until <= now and until >= history["muted_at"]:
            actual = until
        history = {**history, "until": actual}
        await self.put(key, history)
        return history

    async def operations(self, cid):
        return [dict(row) for row in await self._rows("SELECT * FROM operations WHERE case_id=?", (cid,))]

    async def alerts(self, revision):
        # Fetch each source's oldest alert before taking its next one. A busy
        # source with an unavailable destination must not hide other groups.
        rows = await self._rows(
            "WITH pending AS (SELECT id,created,"
            " ROW_NUMBER() OVER (PARTITION BY scope ORDER BY created,id) AS position"
            " FROM cases WHERE settings_revision=? AND notify_enabled=1 AND notified=0 AND human=''"
            " AND flagged=1 AND json_type(data,'$.scores')='object'"
            " AND json_extract(data,'$.notification_ready')=1"
            " AND COALESCE(json_extract(data,'$.actions_pending'),0)!=1)"
            " SELECT c.* FROM pending p JOIN cases c ON c.id=p.id"
            " ORDER BY p.position,p.created,p.id LIMIT 1000",
            (revision,),
        )
        return [self._case(row) for row in rows]

    async def notified(self, ids):
        await self.db.executemany("UPDATE cases SET notified=1 WHERE id=?", [(cid,) for cid in ids])

    async def batch_cases(self, ids):
        return [
            self._case(row)
            for row in await self._rows(
                "SELECT * FROM cases WHERE id IN (SELECT value FROM json_each(?)) ORDER BY id",
                (json.dumps(ids),),
            )
        ]

    async def stats(self, now):
        states = {
            r["state"]: r["n"]
            for r in await self._rows(
                "SELECT state,count(*) n FROM cases WHERE created>? GROUP BY state", (now - 86400,)
            )
        }
        counts = await self._row(
            "SELECT count(*) n,COALESCE(sum(ok),0) ok,COALESCE(sum(input_tokens),0) input,"
            "COALESCE(sum(output_tokens),0) output,COALESCE(avg(elapsed_ms),0) elapsed"
            " FROM calls WHERE at>?",
            (now - 86400,),
        )
        pauses = await self._rows(
            "SELECT key,value FROM state WHERE key LIKE 'pause:%' OR key LIKE 'group-pause:%'"
        )
        queue = await self._row(
            "SELECT count(*) n,min(COALESCE(json_extract(data,'$.work_queued_at'),created)) oldest FROM cases WHERE state='queued'"
        )
        return {
            "states": states,
            "calls": dict(counts),
            "queue": {"n": queue["n"], "oldest_seconds": max(0, now - (queue["oldest"] or now))},
            "pauses": {r["key"]: json.loads(r["value"]) for r in pauses if json.loads(r["value"])},
            "stages": {
                r["stage"]: r["n"]
                for r in await self._rows(
                    "SELECT stage,count(*) n FROM calls WHERE at>? GROUP BY stage", (now - 86400,)
                )
            },
            "screened_clear": (
                await self._row(
                    "SELECT count(*) n FROM cases WHERE created>? AND json_extract(data,'$.audit_path')='batch_clear'",
                    (now - 86400,),
                )
            )["n"],
            "image_cache_hits": (
                await self._row(
                    "SELECT count(*) n FROM cases WHERE created>? AND json_extract(data,'$.audit_path')='image_cache'",
                    (now - 86400,),
                )
            )["n"],
            "ocr_cache_hits": (
                await self._row(
                    "SELECT COALESCE(sum(json_extract(data,'$.cache_reuse.ocr_hits')),0) n FROM cases WHERE created>?",
                    (now - 86400,),
                )
            )["n"],
            "spot_checks": (
                await self._row(
                    "SELECT count(*) n FROM cases WHERE created>? AND json_extract(data,'$.batch.spot_check')=1",
                    (now - 86400,),
                )
            )["n"],
        }

    async def maintain(self, now, days, harmful_days):
        # Storage retention is distinct from prompt selection: never a five-minute context cutoff.
        await self.db.execute("DELETE FROM history WHERE at<?", (now - days * 86400,))
        rows = await self._rows(
            "SELECT * FROM cases WHERE state NOT IN ('queued','auditing') AND updated<?"
            " AND (human='误判' OR (flagged=0 AND human!='违规' AND state!='unknown') OR updated<?) LIMIT 1000",
            (now - days * 86400, now - harmful_days * 86400),
        )
        for row in rows:
            harmful = row["human"] != "误判" and (
                row["flagged"] or row["human"] == "违规" or row["state"] == "unknown"
            )
            if harmful and row["updated"] >= now - harmful_days * 86400:
                continue
            data = json.loads(row["data"])
            await self.db.executemany(
                "INSERT OR IGNORE INTO garbage VALUES(?)",
                [
                    (m["file"],)
                    for m in [*data.get("media_records", []), *data.get("evidence_records", [])]
                    if m.get("file")
                ]
                + [(m["file"] + ".tmp",) for m in data.get("media_records", []) if m.get("file")],
            )
            await self.db.execute("DELETE FROM calls WHERE case_id=? AND screening_id IS NULL", (row["id"],))
            await self.db.execute("DELETE FROM cases WHERE id=?", (row["id"],))
        await self.db.execute(
            "DELETE FROM image_cache WHERE expires<=? OR source_case NOT IN (SELECT id FROM cases)", (now,)
        )
        expired = await self._rows(
            "SELECT id FROM screenings WHERE state!='running' AND updated<? AND (flagged=0 OR updated<?)",
            (now - days * 86400, now - harmful_days * 86400),
        )
        await self.db.executemany("DELETE FROM calls WHERE screening_id=?", [(r["id"],) for r in expired])
        await self.db.executemany("DELETE FROM screenings WHERE id=?", [(r["id"],) for r in expired])
        await self.db.execute(
            "DELETE FROM operations WHERE at<? AND state NOT IN ('submitted','unknown')",
            (now - harmful_days * 86400,),
        )
        await self.db.execute("DELETE FROM reads WHERE at<?", (now - 3600,))
        await self.db.execute(
            "DELETE FROM mute_strikes WHERE at<? OR case_id NOT IN (SELECT id FROM cases)", (now - 86400,)
        )
        for row in await self._rows(
            "SELECT key,value FROM state WHERE key LIKE 'mute-member:%' OR key LIKE 'mute-observed:%'"
        ):
            value = json.loads(row["value"])
            if value.get("until", 0) < now - 720 * 3600:
                await self.db.execute("DELETE FROM state WHERE key=?", (row["key"],))
        for row in await self._rows(
            "SELECT key,value FROM state WHERE key LIKE 'warn:%' OR key LIKE 'notice-member:%'"
        ):
            if json.loads(row["value"]) < now:
                await self.db.execute("DELETE FROM state WHERE key=?", (row["key"],))
        await self.db.execute("PRAGMA incremental_vacuum(1000)")
        return [r[0] for r in await self._rows("SELECT file FROM garbage LIMIT 1000")]

    async def purge_files(self, files):
        await self.db.executemany("DELETE FROM garbage WHERE file=?", [(file,) for file in files])

    async def evidence_files(self, cid, files):
        case = await self.case(cid)
        if case is None:
            raise GuardError("审核记录不存在或已过期。")
        registered = {m["file"] for m in case["data"].get("evidence_records", [])}
        data = {
            **case["data"],
            "evidence_records": [{"file": file} for file in sorted(registered | set(files))],
        }
        # Viewing/archive bookkeeping must not change queued action state or retention age.
        await self.db.execute(
            "UPDATE cases SET data=? WHERE id=?", (json.dumps(data, ensure_ascii=False), cid)
        )

    async def image_cache_get(self, key, now):
        row = await self._row(
            "SELECT i.*,c.human FROM image_cache i JOIN cases c ON c.id=i.source_case WHERE i.key=? AND i.expires>?",
            (key, now),
        )
        if row is None or row["human"]:
            return None
        await self.db.execute("UPDATE image_cache SET accessed=? WHERE key=?", (now, key))
        return {"source_case": row["source_case"], "value": json.loads(row["data"])}

    async def image_cache_put(self, key, cid, value, now, hours, limit):
        row = await self._row("SELECT human FROM cases WHERE id=?", (cid,))
        if row is None or row["human"]:
            return
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode()) > 1024 * 1024:
            return
        await self.db.execute(
            "INSERT OR REPLACE INTO image_cache VALUES(?,?,?,?,?)",
            (key, cid, now + hours * 3600, now, encoded),
        )
        await self.db.execute("DELETE FROM image_cache WHERE expires<=?", (now,))
        await self.db.execute(
            "DELETE FROM image_cache WHERE key NOT IN (SELECT key FROM image_cache ORDER BY accessed DESC,key LIMIT ?)",
            (limit,),
        )
