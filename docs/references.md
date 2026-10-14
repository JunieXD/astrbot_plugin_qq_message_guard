# 实现依据

调研日期：2026-10-13。本插件独立实现 Python 代码，参考下列项目的公开接口与设计；未移植 sub2api 的 Go 服务、数据库模型或接口网关。

## sub2api

参考仓库：[Wei-Shaw/sub2api](https://github.com/Wei-Shaw/sub2api)，调研固定提交 `4d9ac3e482106d2da3a39fe3e546d8d6801f1cc1`。

- [`content_moderation_typesafe.go`](https://github.com/Wei-Shaw/sub2api/blob/4d9ac3e482106d2da3a39fe3e546d8d6801f1cc1/backend/internal/service/content_moderation_typesafe.go)：原生 System One `noul` 问题、13 个独立判断项、模型与规则版本元信息、未覆盖图片的计数。
- [`content_moderation.go`](https://github.com/Wei-Shaw/sub2api/blob/4d9ac3e482106d2da3a39fe3e546d8d6801f1cc1/backend/internal/service/content_moderation.go)：独立类别阈值、有界后台队列、耗时和排队统计、审核错误记录。
- [`prompt_worker.go`](https://github.com/Wei-Shaw/sub2api/blob/4d9ac3e482106d2da3a39fe3e546d8d6801f1cc1/backend/internal/securityaudit/prompt_worker.go)：任务生命周期与停止、运行指标。

区别：接口网关可以在请求发出前阻断，而 QQ bot 只能在消息发出后处理。v0.3.0支持13项分类并为QQ聊天独立重写行为与例外，补充隐私、推广、敏感话题及粗俗玩笑；默认10个相关类别与意图共用一次请求。不复制网关封禁策略。上述 TypeSafe 审核实现当时跳过图片，本插件使用 OneJev 本地的真实视觉输入，明确记录覆盖缺失。

## OneJev

参考 [OmniJev/OneJev](https://github.com/OmniJev/OneJev)，调研时上游提交 `6122519058af23504a33d944f0e63b2d868c8a4b`；本机部署来源记录为 `81d5d26`，已读取实际部署代码并检查健康响应。

- [`qev/schema.py`](https://github.com/OmniJev/OneJev/blob/6122519058af23504a33d944f0e63b2d868c8a4b/qev/schema.py)：`state`、`questions`、`criteria.true/false`、`answers.*.noul` 和 `usage`。
- 同一 schema 的 `ChoiceQuestion` / `ChoiceAnswer` 和 `qev/answers.py` 的 `make_choice()`：原生类别选项、概率分布和 concentration confidence。意图分类使用三选一，不能与二元审核分值混用。
- [`qev/prompt.py`](https://github.com/OmniJev/OneJev/blob/6122519058af23504a33d944f0e63b2d868c8a4b/qev/prompt.py)：状态与问题的渲染，原生选项判断，不是自由生成理由。
- [`qev/server.py`](https://github.com/OmniJev/OneJev/blob/6122519058af23504a33d944f0e63b2d868c8a4b/qev/server.py)：`POST /v1/systemone`、模型别名、`debias`、响应诊断信息。
- [`qev/llamacpp.py`](https://github.com/OmniJev/OneJev/blob/6122519058af23504a33d944f0e63b2d868c8a4b/qev/llamacpp.py)：本机后端串行执行和锁，多问题仍有额外成本。
- [`qev/media.py`](https://github.com/OmniJev/OneJev/blob/6122519058af23504a33d944f0e63b2d868c8a4b/qev/media.py)：`<image:N>` 必须按顺序引用，使用 `media` 中的 data URI。

采用服务原生返回的实际 token 统计，不自行估算。固定 `debias=1`，避免实时审核无依据地多倍增加推理成本。对于不可信内容中的同名媒体占位符先转义，只给应用插入的占位符绑定图片。

## 上下文、词库与OCR

- [group_guardian](https://github.com/zcj-ui/astrbot_plugin_group_guardian) 的群背景与同作者上下文组织，以及 [引用归责问题 #33](https://github.com/zcj-ui/astrbot_plugin_group_guardian/issues/33)：引用原作者内容应与当前作者的新发言分开。
- [sensitive-word](https://github.com/houbb/sensitive-word) 的文字归一化思路；本插件采用少量词义而非大范围词库封禁。
- [Windows.Media.Ocr](https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrengine)：本机中文文字提取，经 WinRT 读取压缩图片；实际使用 zh-Hans-CN。
- [Tesseract](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html)：本机命令行输出文字，使用 chi_sim+eng。程序路径和图片路径通过参数数组传入，不执行群消息内容或拼接 shell 命令。

上下文无时间过滤，缓存以条数约束并受数据保留期管理。词库命中只生成复核候选；举报制止、正常与低置信度意图阻止自动操作。以上设计通过本机选定历史样本和构造对照验证，不代表已证明可以避免腾讯封群。

## AstrBot 与 NapCat

- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin.html)。已对照本机 `astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py`：文字和消息段数组转换、原始 OneBot 事件、消息 ID、引用转换及群通知事件。
- 对照 `astrbot/core/platform/astr_message_event.py` 的 `is_admin()`、`stop_event()`，以及群白名单与唤醒阶段。监听所有 aiocqhttp 事件，管理命令只响应 AstrBot 机器人管理员的私聊。
- [`DeleteMsg.ts`](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/action/msg/DeleteMsg.ts)：用 OneBot 消息 ID 映射到原始消息并撤回，缓存映射可能不存在，不能把请求发出视作成功。
- [`OB11GroupRecallNoticeEvent.ts`](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/event/notice/OB11GroupRecallNoticeEvent.ts)：原生 `group_recall` 通知和消息 ID，用于取消待处理操作。
- [NapCat OneBot API 文档](https://napneko.github.io/onebot/api)。使用 `get_login_info`、`get_status`、`get_group_member_info`、`get_msg`、`delete_msg`、`send_group_msg`、`send_private_msg`；警告由真实 `at` 段与 `text` 段组成，不拼 CQ 代码。

角色资料在撤回或公开警告前重新核验。不能假设 bot 管理员能处理所有角色的消息，因此首版不自动处理群主/管理员。消息查找与内容指纹核对失败时不尝试直接撤回猜测的 ID。

账号写操作可接入用户已有插件的 `_qq_automation_guard_v1` 协调器；接口不兼容时停止写入，协调器不在时只启用本插件的持久化限额。所有 QQ 请求显式绑定收到原始事件的平台和账号。
