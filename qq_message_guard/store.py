from __future__ import annotations

import asyncio
import json
import time

import aiosqlite

from .config import Deferred, GuardError


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
            PRAGMA user_version=2;
        """)
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
        await self.db.execute(
            "INSERT OR IGNORE INTO history(scope,mid,user,at,text) VALUES(?,?,?,?,?)",
            (message.scope, message.mid, message.user, message.at, text),
        )
        # Fixed count cache persists across reloads; no wall-clock cutoff.
        await self.db.execute(
            "DELETE FROM history WHERE scope=? AND id NOT IN"
            " (SELECT id FROM history WHERE scope=? ORDER BY id DESC LIMIT ?)",
            (message.scope, message.scope, capacity),
        )

    async def recover(self, now):
        for row in await self._rows("SELECT id FROM cases WHERE state IN ('queued','auditing')"):
            await self.finish(row["id"], "incomplete", {"reason": "插件重载中断，未完成审核"}, now)
        for row in await self._rows("SELECT * FROM operations WHERE state='submitted'"):
            await self.db.execute("UPDATE operations SET state='unknown' WHERE id=?", (row["id"],))
            await self.put("pause:" + row["account"], "上次提交结果不明，请核对后使用恢复命令")
        await self.db.execute("UPDATE operations SET state='not_sent' WHERE state='reserved'")
        for row in await self._rows("SELECT * FROM cases WHERE state='review' AND action_ready=1"):
            case = self._case(row)
            if case["data"].get("auto_ready"):
                await self.finish(
                    case["id"],
                    "review",
                    {"auto_ready": False, "reason": "重载取消了尚未提交的自动撤回，请人工复核"},
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
                await self.finish(
                    case["id"], "recalled", {"reason": "收到撤回通知"}, now, flagged=bool(case["flagged"])
                )

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
        await self.db.execute(
            "UPDATE cases SET human=?,updated=?,data=?,action_ready=0 WHERE id=?",
            (human, now, json.dumps(data, ensure_ascii=False), cid),
        )

    async def record_call(self, cid, now, result=None, elapsed=0, error=""):
        await self.db.execute(
            "INSERT INTO calls(case_id,at,ok,model,input_tokens,output_tokens,elapsed_ms,error)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (
                cid,
                now,
                int(result is not None),
                result.model if result else "",
                result.input_tokens if result else 0,
                result.output_tokens if result else 0,
                result.elapsed_ms if result else elapsed,
                error[:500],
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
        count = await self._row(
            "SELECT count(*) FROM operations WHERE account=? AND at>? AND state!='not_sent'",
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

    async def reserve(self, cid, kind, account, now, limit, gap, warn_minutes=60, group_warnings=5):
        await self.can_write(cid, kind, account, now, limit, group_warnings)
        if kind == "warn":
            message = (await self.case(cid))["data"]["message"]
            await self.put(f"warn:{account}:{message['group']}:{message['user']}", now + warn_minutes * 60)
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
        return oid

    async def operation(self, oid, state, detail=""):
        await self.db.execute("UPDATE operations SET state=?,detail=? WHERE id=?", (state, detail[:500], oid))

    async def operations(self, cid):
        return [dict(row) for row in await self._rows("SELECT * FROM operations WHERE case_id=?", (cid,))]

    async def alerts(self, revision):
        # Fetch each source's oldest alert before taking its next one. A busy
        # source with an unavailable destination must not hide other groups.
        rows = await self._rows(
            "WITH pending AS (SELECT id,created,"
            " ROW_NUMBER() OVER (PARTITION BY scope ORDER BY created,id) AS position"
            " FROM cases WHERE settings_revision=? AND notify_enabled=1 AND notified=0 AND human=''"
            " AND flagged=1 AND json_type(data,'$.scores')='object')"
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
        queue = await self._row("SELECT count(*) n,min(created) oldest FROM cases WHERE state='queued'")
        return {
            "states": states,
            "calls": dict(counts),
            "queue": {"n": queue["n"], "oldest_seconds": max(0, now - (queue["oldest"] or now))},
            "pauses": {r["key"]: json.loads(r["value"]) for r in pauses if json.loads(r["value"])},
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
                [(m["file"],) for m in data.get("media_records", []) if m.get("file")],
            )
            await self.db.execute("DELETE FROM calls WHERE case_id=?", (row["id"],))
            await self.db.execute("DELETE FROM cases WHERE id=?", (row["id"],))
        await self.db.execute(
            "DELETE FROM operations WHERE at<? AND state NOT IN ('submitted','unknown')",
            (now - harmful_days * 86400,),
        )
        await self.db.execute("DELETE FROM reads WHERE at<?", (now - 3600,))
        for row in await self._rows("SELECT key,value FROM state WHERE key LIKE 'warn:%'"):
            if json.loads(row["value"]) < now:
                await self.db.execute("DELETE FROM state WHERE key=?", (row["key"],))
        await self.db.execute("PRAGMA incremental_vacuum(1000)")
        return [r[0] for r in await self._rows("SELECT file FROM garbage LIMIT 1000")]

    async def purge_files(self, files):
        await self.db.executemany("DELETE FROM garbage WHERE file=?", [(file,) for file in files])
