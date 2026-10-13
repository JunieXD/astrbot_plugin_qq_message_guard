from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .config import QUESTIONS, GuardError, integer, qq

STATES = {
    "queued": "排队",
    "auditing": "审核中",
    "allowed": "未命中",
    "review": "待复核",
    "incomplete": "未完整审核",
    "recalled": "已撤回",
    "unknown": "操作结果不明",
    "failed": "操作失败",
}
HELP = (
    "群消息审核（仅AstrBot机器人管理员可用）\n"
    "/消息审核 状态\n"
    "/消息审核 待复核 [群号]\n"
    "/消息审核 最近 [群号]\n"
    "/消息审核 查看 编号\n"
    "/消息审核 标记 编号 误判或违规\n"
    "/消息审核 撤回 编号\n"
    "/消息审核 恢复 群号\n"
    "恢复表示已人工核对操作结果；旧的已提交请求不会重发。\n"
    "群、模式、群规和阈值在插件配置中调整。"
)


def lines(case):
    data, message = case["data"], case["data"]["message"]
    scores = data.get("scores", {})
    names = {v[0]: k for k, v in QUESTIONS.items()}
    score_text = "、".join(f"{names.get(k, k)} {v:.3f}" for k, v in scores.items())
    return (
        f"#{case['id']} 群{message['group']} QQ{message['user']}：{STATES.get(case['state'], case['state'])}"
        + (f"；{score_text}" if score_text else "")
        + (f"；人工：{case['human']}" if case["human"] else "")
    )


class Commands:
    def __init__(self, service):
        self.s = service

    async def run(self, text, actor, pid, account):
        parts = text.strip().lstrip("/").split()
        args = parts[1:]
        if not args or args == ["帮助"]:
            return HELP
        command = args[0]
        if command == "状态" and len(args) == 1:
            settings = self.s.settings()
            stats = await self.s.store.call("stats", self.s.clock())
            calls, states = stats["calls"], stats["states"]
            groups = (
                "；".join(
                    f"{p.group_id} 撤回{'开' if p.auto_recall else '关'}、"
                    f"警告{'开' if p.warn_member else '关'}、"
                    f"私聊通知{'开' if p.notify_private else '关'}、"
                    f"管理员群通知{'开' if p.notify_group else '关'}"
                    for p in settings.groups
                    if p.enabled
                )
                or "未配置"
            )
            return (
                f"群消息审核 v0.1.0\n总开关：{'开启' if settings.enabled else '关闭'}\n群：{groups}\n"
                f"后台：{self.s.failure or '运行中'}\n"
                f"最近24小时：{sum(states.values())}条，排队{states.get('queued', 0)}，"
                f"审核中{states.get('auditing', 0)}，未完整审核{states.get('incomplete', 0)}。\n"
                f"模型调用{calls['n']}次，成功{calls['ok']}次，失败{calls['n'] - calls['ok']}次；"
                f"输入{calls['input']} tokens，输出{calls['output']} tokens；"
                f"平均请求{calls['elapsed'] / 1000:.2f}秒。\n"
                f"本插件QQ读取额度：{settings.advanced.reads_per_hour}/小时；"
                f"写入额度：{settings.advanced.writes_per_hour}/小时。\n"
                f"暂停：{'；'.join(stats['pauses'].values()) or '无'}\n"
                f"模型故障冷却：{max(0, int(self.s.model_cooldown - self.s.clock()))}秒。\n"
                f"操作节奏：{'接入共享队列' if self.s.platform.shared_guard() else '仅本插件'}。"
            )
        if command in ("待复核", "最近") and len(args) in (1, 2):
            group = qq(args[1], "群号") if len(args) == 2 else ""
            cases = await self.s.store.call("recent", group, command == "待复核")
            return "\n".join(lines(c) for c in cases) if cases else "暂无对应审核记录。"
        if command in ("查看", "撤回") and len(args) == 2:
            cid = integer(args[1].lstrip("#"), "记录编号", 1, 10**12)
            case = await self.s.store.call("case", cid)
            if not case:
                raise GuardError("审核记录不存在或已过期。")
            if command == "撤回":
                message = case["data"]["message"]
                if message["platform"] != pid or message["account"] != account:
                    raise GuardError("请私聊负责这条消息的机器人账号执行撤回。")
                self.s.journal.record("人工请求撤回", case=cid, actor=actor)
                return await self.s.recall(case, manual=True)
            data, message = case["data"], case["data"]["message"]
            stamp = datetime.fromtimestamp(message["at"], timezone(timedelta(hours=8))).strftime(
                "%m月%d日 %H:%M:%S"
            )
            ops = await self.s.store.call("operations", cid)
            return (
                lines(case) + f"\n时间：{stamp}\n模型：{data.get('actual_model', '未完成调用')}\n"
                f"说明：{data.get('reason') or '请结合群规人工判断，分数不代表现实违规概率。'}\n"
                f"覆盖缺失：{'；'.join(data.get('missing', [])) or '无已知缺失'}\n"
                f"操作：{'；'.join(op['kind'] + ':' + op['state'] for op in ops) or '无'}\n"
                f"文字：\n{message['text'] or '没有文字，请查看本地审核详情与图片证据。'}"
            )
        if command == "标记" and len(args) == 3 and args[2] in ("误判", "违规"):
            cid = integer(args[1].lstrip("#"), "记录编号", 1, 10**12)
            await self.s.mark(cid, args[2], actor)
            return f"#{cid} 已标记为{args[2]}。标记不会自动触发撤回。"
        if command == "恢复" and len(args) == 2:
            return await self.s.resume(qq(args[1], "群号"), pid, account)
        return HELP
