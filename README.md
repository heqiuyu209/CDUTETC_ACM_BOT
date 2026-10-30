# CDUTETC ACM 事务助手

面向成都理工大学工程技术学院 ACM 集训队的 QQ 群事务助手：老师发布比赛资料，超管修正信息，同学查询安排、登记征集，机器人提醒比赛、课程和登记截止。

接入方式是 **NapCatQQ + OneBot 11 正向 WebSocket**。本项目作为客户端连接 NapCat，由 NapCat 登录普通 QQ 账号并执行群消息/私聊动作。运行环境为 Python 3.10+，数据保存在 SQLite；文本提取和问答使用 OpenAI 兼容模型接口。

**部署状态：完整部署、真实 NapCat/QQ 消息收发和真实模型请求尚未完成本次验收。** 离线测试和本地 WebSocket 测试用于验证业务及回执处理；安装完成或打印“已连接”不代表真实群链路已验收。

## 部署与启动

先在 NapCat 中登录机器人 QQ 账号，开启 OneBot 11 **正向 WebSocket 服务**，复制其监听地址和 token。本项目会主动连接这个地址；同机示例为 `ws://127.0.0.1:3001`，端口以 NapCat 实际配置为准。

在仓库根目录执行以下启动步骤。完整部署步骤在本次文档更新中**未执行**；配置检查和示例解析已离线验证。

### Windows / PowerShell

```powershell
py -3 -m venv .venv
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
}
notepad .env
& '.\.venv\Scripts\python.exe' -c "import config; config.check_config(); print('配置检查通过')"
& '.\.venv\Scripts\python.exe' main.py
```

### Linux / macOS / POSIX shell

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
test -f .env || cp .env.example .env
${EDITOR:-vi} .env
.venv/bin/python -c "import config; config.check_config(); print('配置检查通过')"
.venv/bin/python main.py
```

`.env` 从 `config.py` 所在目录加载，已有进程环境变量优先。修改配置后重启进程。启动检查只验证 `ACM_ONEBOT_WS_URL` 和 `ACM_LLM_API_KEY` 是否为空；缺失时仅列变量名，不打印密钥。它不会验证 NapCat 是否在线、模型接口是否可达或白名单是否正确。

至少填写 `.env` 中的 WebSocket 地址、模型 key、允许的 **QQ群号** 和老师 **QQ号**；需要 `/修正`、来源待审操作时还要填写超管 QQ 号。群白名单为空时，即使已连接 NapCat，也会忽略所有群消息。

## 配置

下列变量可放在 `.env` 或由进程环境注入。布尔开关使用 `0` / `1`；多值白名单用逗号分隔。

| 变量 | 默认值 | 作用 |
|---|---|---|
| `ACM_ONEBOT_WS_URL` | 空，必填 | NapCat 正向 WebSocket 地址，支持 `ws://` / `wss://` |
| `ACM_ONEBOT_TOKEN` | 空 | 与 NapCat 的访问 token 一致；NapCat 未配置鉴权时留空 |
| `ACM_BOT_QQ` | 空 | 机器人自身 QQ 号；留空时使用 OneBot 消息事件的 `self_id` 识别 @ 目标 |
| `ACM_ALLOWED_GROUPS` | 空 | 允许服务的 QQ 群号；空表示不服务任何群 |
| `ACM_TEACHER_WHITELIST` | 空 | 可发布比赛和管理群事务的老师 QQ 号 |
| `ACM_SUPER_ADMIN` | 空 | 超管 QQ 号，支持多人；可修正比赛、处理来源待审及管理群事务 |
| `ACM_GROUP_ACTIVE_MESSAGES_ENABLED` | `1` | 发布/修正群通知和定时提醒默认开启；设为 `0` 暂停主动通知，群内命令回复继续工作 |
| `ACM_PUBLISH_CMD` | `发布` | 比赛发布命令前缀 |
| `ACM_LLM_API_KEY` | 空，必填 | OpenAI 兼容模型接口的 key |
| `ACM_LLM_BASE_URL` | `https://api.deepseek.com/v1` | 模型 API 基础地址 |
| `ACM_LLM_MODEL` | `deepseek-chat` | 模型名 |
| `ACM_LLM_MAX_INPUT_CHARS` | `6000` | 单段提取/单次问答的输入字符上限 |
| `ACM_LLM_MAX_OUTPUT_TOKENS` | `2000` | 单次模型请求的输出 token 上限 |
| `ACM_LLM_MAX_RESPONSE_BYTES` | `262144` | 模型响应及解压后响应的实际字节上限 |
| `ACM_LLM_CONCURRENCY` | `2` | 模型请求并发上限 |
| `ACM_FETCH_ALLOWED_DOMAINS` | 空 | 可抓取域名；配置后重定向目标同样受限，未配置时仍校验公网地址 |
| `ACM_FETCH_MAX_BYTES` | `1000000` | 网页响应及解压后正文的字节上限 |
| `ACM_FETCH_MAX_REDIRECTS` | `3` | 最大重定向跳数 |
| `ACM_FETCH_TIMEOUT` | `10` | 每跳 DNS、连接、读取及解析的总超时，单位秒 |
| `ACM_FETCH_CONCURRENCY` | `2` | 抓取并发上限 |
| `ACM_FETCH_TEXT_LIMIT` | `3000` | 旧配置兼容保留；完整提取不再据此截断正文 |
| `ACM_AUTO_FETCH_ENABLED` | `0` | 公共来源自动抓取默认关闭；设为 `1` 开启 |
| `ACM_FETCH_MIN_INTERVAL_MINUTES` | `60` | 来源抓取调度周期和同一来源最小检查间隔 |
| `ACM_REMIND_CHECK_INTERVAL_MINUTES` | `30` | 提醒检查间隔，单位分钟 |
| `ACM_REMIND_BEFORE_HOURS` | `24` | 距目标时间进入提醒窗口的小时数 |
| `ACM_WEEKLY_CONTEST_ENABLED` | `1` | 自动周赛开关 |
| `ACM_WEEKLY_CONTEST_WEEKDAY` | `6` | 周赛星期，`0` 为周一、`6` 为周日 |
| `ACM_WEEKLY_CONTEST_START` | `19:00` | 周赛开始时间 |
| `ACM_WEEKLY_CONTEST_END` | `21:00` | 周赛结束时间，须晚于开始时间 |
| `ACM_WEEKLY_CONTEST_TEMPLATE` | `ACM 周赛（本周）` | 周赛名称模板 |
| `ACM_DB_PATH` | 项目目录中的 `contests.db` | 持久 SQLite 文件；自定义相对路径按启动工作目录解析 |

`.env.example` 使用 `ACM_DB_PATH=./contests.db`，对应上方从仓库根目录启动的命令。部署到其他目录时可改为稳定的绝对路径；父目录须先建立，重启和升级继续使用同一数据库。

长正文分段提取：每次完整提取最多 **8 段**，段间最多重叠 **256 字符**，总等待最多 **120 秒**。段数和总等待是 `llm.py` 内部常量，当前没有对应环境变量。默认单段 6000 字符时，完整输入预算为 **46208 字符**，包含发布时附带的老师补充文本；超预算、任一段失败或字段冲突会返回提取失败，保留原资料及原抓取基线。调整 `ACM_LLM_MAX_INPUT_CHARS` 会改变完整预算。

## 群消息与权限

斜杠命令和 `发布 <链接>` 可直接发送；自然语言提问需要 **@ 本机器人**。闲聊、普通链接和 @ 其他人的消息不触发模型或登记。机器人 QQ 号未配置且事件没有 `self_id` 时，仍可使用不带 @ 的显式命令。

公共正式比赛向允许群共享；课程、FAQ、征集和未答问题按群隔离。群成员只能修改自己的报名。老师由 `ACM_TEACHER_WHITELIST` 识别，超管由 `ACM_SUPER_ADMIN` 识别；权限取自 OneBot 事件的 QQ 号。首次比赛发布需要老师白名单，超管执行发布也需列入该白名单。

| 命令 | 权限 | 作用 |
|---|---|---|
| `发布 <官网链接>` | 老师 | 抓取并提取比赛，成功后免审存档并发送群通知 |
| `/修正 <比赛id> <字段=值 ...>` | 超管 | 修正比赛资料，保留修改版本 |
| `/修正 <比赛id> 重新提取 <链接>` | 超管 | 重新抓取提取，成功后更新资料和有效来源 |
| `/待审列表` | 超管 | 查看后台来源更新编号和改动字段 |
| `/待审批准 <更新id>` | 超管 | 合并有效字段，未提取字段保留原值 |
| `/待审拒绝 <更新id> [原因]` | 超管 | 拒绝来源更新，正式资料保持原样 |
| `/校安排` | 群成员 | 查看共享的学校安排 |
| `/课程列表` | 群成员 | 查看本群课程 |
| `/课程添加 <标题> [星期几] [开始-结束] [地点]` | 老师/超管 | 创建固定周期课程 |
| `/课程取消 <id>` | 老师/超管 | 取消本群课程 |
| `/征集 <标题> [截止时间] [名额] [字段1,字段2]` | 老师/超管 | 发起报名意向、设备借用或出勤征集 |
| `/报名 <征集id> [字段=值 ...]` | 群成员 | 报名或修改本人登记，重复操作不重复计数 |
| `/报名 取消 <征集id>` | 群成员 | 取消本人登记 |
| `/征集汇总 <征集id>` | 群成员 | 查看本群人数与剩余名额 |
| `/征集关闭 <征集id>` | 老师/超管 | 关闭本群征集 |
| `/FAQ列表 [关键词]` | 群成员 | 查看本群 FAQ |
| `/FAQ添加 <问题> = <答案>` | 老师/超管 | 添加 FAQ 并关闭同群同题未答问题 |
| `/FAQ删除 <id>` | 老师/超管 | 删除本群 FAQ |
| `/提问 <问题>` | 群成员 | 记录未答问题 |
| `/问题列表` | 老师/超管 | 查看本群未答问题 |
| `/提醒状态` | 老师/超管 | 查看本群投递队列、尝试次数和终结状态 |
| `/抓取状态` | 老师/超管 | 查看公共来源抓取状态 |
| `/抓取一次` | 老师/超管 | 触发一轮抓取，同样遵守抓取开关和最小间隔 |
| `/帮助` | 群成员 | 获取当前命令帮助 |

示例：

```text
@机器人 发布 https://example.com/contest
/修正 1 official_reg_deadline=2026-12-01 23:59 name="ICPC 2026 区域赛"
/课程添加 算法训练 周日 19:00-21:00 B201
/征集 集训报名 2026-11-10 23:59 50 姓名,学号
/报名 1 姓名=张三 学号=20260001
/征集汇总 1
/报名 取消 1
```

修正和报名的值包含空格或 `字段=值` 片段时使用引号。同一字段及别名不能重复指定；`字段=""` 明确清空可选字段。重复 `/报名 <id>` 保留原资料，只更新本次提供的字段。报名状态、截止和容量检查与写入使用同一事务。

## 提醒与来源更新

业务时间统一为北京时间 `Asia/Shanghai`。比赛报名截止/开赛、征集截止及课程开始使用模板生成提醒，不调用模型。课程按“课程 + 具体开课时刻 + 群”去重，下一周可再次提醒；取消、改期和生效期变化会使旧未发送计划失效。

周赛在提醒窗口打开前一个扫描周期生成：默认周六 **18:30** 生成周日 **19:00–21:00** 的比赛。启动时补最近一场未来周赛并立即安排提醒检查，已经开赛的场次不会补成过期记录。记录按周持久化去重，重启不覆盖老师修正。

投递使用 SQLite 原子领取和 120 秒发送租约，每次发送最多等待 60 秒，默认最多尝试 3 次。成功回执先独立持久化；状态或旧版成功标记写入失败不会撤销成功事实。一个对象失败不阻断后续健康提醒。关闭主动消息期间不消耗投递次数。只在 OneBot 返回成功状态和有效 `message_id` 时确认发送成功；普通文本消息不会自动 @ 全体。

首次发布免审。后台来源内容变化只生成待审更新，通过 `/待审列表`、`/待审批准`、`/待审拒绝` 处理；待审操作只回复发起群。官网地址修正后旧来源停用并保留历史，新来源须真实抓取成功才能建立正文基线。完整提取失败时不更新基线，下轮仍可重试。

## 数据与旧库

启动会自动补齐旧库所需的表和字段，保留业务记录。旧版无群归属的数据保持隔离，不参与提醒。群级历史数据若存有原接入方式的群标识，需要管理员显式映射到 QQ 群号后才可从新群号访问；迁移不会猜测归属。

群回复只显示征集人数和剩余名额，老师/超管查询也不发送报名明细。需要名单时，由本机管理员只读导出到新的本地 CSV，再单独交付老师：

```powershell
New-Item -ItemType Directory -Force private-exports
& '.\.venv\Scripts\python.exe' scripts/export_collection.py --group-id '123456789' --collection-id 1 --output private-exports/collection-1.csv
```

POSIX 下使用 `mkdir -p private-exports` 和 `.venv/bin/python scripts/export_collection.py`，参数相同。该命令读取 `ACM_DB_PATH`，可用 `--db` 指定文件；只导出该群仍报名的成员，不发送 QQ 消息，不覆盖已有 CSV。上述业务示例和导出步骤需要替换真实对象编号；真实部署数据导出**未执行**。

`.env`、数据库、日志和 `private-exports/` 均被 Git 忽略；`.env.example` 只保留空密钥和配置说明。

## 离线测试与真实联调

Windows：

```powershell
& '.\.venv\Scripts\python.exe' -m pip install -r requirements-dev.txt
& '.\.venv\Scripts\python.exe' -m pytest -q
```

POSIX：

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

离线测试使用合成身份、临时 SQLite 和受控模型/消息回执，本地 WebSocket 验证连接与事件/动作回执处理。真实验收还需在目标 NapCat/QQ 环境运行 `/帮助`、老师发布和修正、成员登记/取消，以及一次定时提醒；核对实际群接收、掉线重连、通知回执和数据库重启后的数据。真实链路验收在本次工作中**未执行**。

## 主要文件

| 文件 | 作用 |
|---|---|
| `main.py` | 业务路由、启动与定时任务 |
| `onebot_client.py` | NapCat 正向 WebSocket、OneBot 事件与动作回执 |
| `config.py` / `.env.example` | 配置加载和环境样例 |
| `commands.py` | 命令帮助、权限与业务分发 |
| `database.py` | 群隔离、事务、版本、登记与提醒状态 |
| `fetcher.py` / `llm.py` / `crawler.py` | 网页抓取、全文提取与来源变化处理 |
| `reminder.py` | 比赛、征集、周期课程的投递与去重 |
| `scripts/export_collection.py` | 本机只读导出征集明细 |
| `tests/` | 离线回归测试 |
