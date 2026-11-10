# 从零开始：QQ 频道机器人开发教程（基于 botpy）

> **本文来源**：根据 CSDN 文章《手把手 QQ 机器人制作教程，根据官方接口进行开发，基于 Python 语言制作的详细教程》（作者：唤醒手腕，2022-07-31）整理重写。
>
> **写作目的**：原文是"操作记录"式的教程，步骤给得很全，但对零基础读者缺少"为什么这样做"的解释。本文保留原文的完整操作脉络，并在每一步补充原理说明、参数含义和常见坑。
>
> **重要提醒**：原文写于 2022 年 7 月，QQ 开放平台和 SDK 都有变化。本文以当前 `qq-botpy 1.2.x` 的接口为准，凡是与原文写法不同的地方都会用「版本差异」标出。实际开发时，请始终以 [QQ 机器人官方文档](https://bot.q.qq.com/wiki/) 为最终依据。

---

## 目录

- [第 0 章 开始之前：环境与概念准备](#第-0-章-开始之前环境与概念准备)
- [第 1 章 注册开放平台并创建机器人](#第-1-章-注册开放平台并创建机器人)
- [第 2 章 先理解接口：REST 与 WebSocket](#第-2-章-先理解接口rest-与-websocket)
- [第 3 章 安装 SDK](#第-3-章-安装-sdk)
- [第 4 章 最小可运行机器人（botpy 快速入门）](#第-4-章-最小可运行机器人botpy-快速入门)
- [第 5 章 用旧版 SDK 走一遍完整流程](#第-5-章-用旧版-sdk-走一遍完整流程)
- [第 6 章 天气案例：调用第三方 API](#第-6-章-天气案例调用第三方-api)
- [第 7 章 主动推送：定时发消息](#第-7-章-主动推送定时发消息)
- [第 8 章 指令回复：Ark 卡片与 Embed 私信](#第-8-章-指令回复ark-卡片与-embed-私信)
- [第 9 章 小程序（简述）](#第-9-章-小程序简述)
- [第 10 章 用 PyCharm 通过 SSH 在服务器上开发](#第-10-章-用-pycharm-通过-ssh-在服务器上开发)
- [第 11 章 用 YAML 管理配置](#第-11-章-用-yaml-管理配置)
- [第 12 章 User 对象：机器人自己是谁](#第-12-章-user-对象机器人自己是谁)
- [第 13 章 发消息与回复的全部姿势](#第-13-章-发消息与回复的全部姿势)
- [第 14 章 语音房上麦](#第-14-章-语音房上麦)
- [第 15 章 用装饰器注册指令](#第-15-章-用装饰器注册指令)
- [第 16 章 频道对象 guild](#第-16-章-频道对象-guild)
- [第 17 章 发送按钮消息](#第-17-章-发送按钮消息)
- [第 18 章 禁言](#第-18-章-禁言)
- [第 19 章 错误码与排查方法](#第-19-章-错误码与排查方法)
- [第 20 章 事件监听全景](#第-20-章-事件监听全景)
- [第 21 章 从 Demo 到可维护的项目](#第-21-章-从-demo-到可维护的项目)
- [附录 A 术语表](#附录-a-术语表)
- [附录 B 官方链接](#附录-b-官方链接)
- [附录 C 原文课程与本文章节对照](#附录-c-原文课程与本文章节对照)
- [附录 D 原文中已过时或有误的写法](#附录-d-原文中已过时或有误的写法)

---

## 第 0 章 开始之前：环境与概念准备

### 0.1 读完这份教程，你能做出什么

跟着做完，你会得到一个能跑的 QQ 频道机器人，具备这些能力：

1. 在频道里被 @ 时自动回复；
2. 接收 `/天气 成都` 这样的指令，调用第三方天气接口并回复；
3. 定时主动推送消息；
4. 发送 Ark 卡片消息、Embed 内嵌消息、私信；
5. 用装饰器管理多个指令；
6. 把机器人部署到服务器上长期运行。

### 0.2 先分清：频道机器人 ≠ 群机器人

这是初学者最容易混淆的地方，必须先说清楚。

QQ 机器人目前有几条不同的产品线，接口和事件名都不一样：

| 对比项 | QQ 频道机器人（本文） | QQ 群机器人 |
|--------|----------------------|-------------|
| 聊天场所 | QQ 频道（Guild）下的子频道（Channel） | QQ 群 |
| 消息事件 | `on_at_message_create` | `on_group_at_message_create` |
| 订阅开关 | `public_guild_messages=True` | `public_messages=True` |
| 发送接口 | `post_message(channel_id=...)` | `post_group_message(group_openid=...)` |
| ID 形式 | `guild_id` / `channel_id` | `group_openid` / `user_openid` |

原文讲的是**频道机器人**：一个"频道"（类似服务器）下面有很多"子频道"（文字频道、语音频道），机器人被拉进频道后，在某个子频道里收发消息。

> **为什么要分清？**
> 如果你拿着频道机器人的代码去接群机器人，事件永远不会触发——因为平台推送的是另一种事件，而你的回调函数名对不上。反过来也一样。先确定自己做的是哪一种，再去抄对应的示例。

本仓库 `CDUTETC_ACM_BOT` 用的是**群机器人**接口（`Intents(public_messages=True)` + `on_group_at_message_create`），但底层原理（WebSocket 收事件、REST 发消息、Intents 订阅）和本文完全一致。学完本文再看本仓库代码会轻松很多。

### 0.3 安装 Python

机器人是 Python 程序，所以第一步是装 Python。

1. 打开 <https://www.python.org/downloads/>，下载 Python 3.10 或 3.11（不要用太新的版本，部分依赖还没适配）。
2. Windows 安装时**务必勾选 "Add Python to PATH"**。这一步的作用是把 `python` 命令写进系统环境变量，否则命令行会提示"找不到 python"。
3. 安装完成后打开终端验证：

```bash
python --version
```

macOS / Linux 一般用：

```bash
python3 --version
```

看到 `Python 3.10.x` 这样的输出就成功了。

> **为什么推荐 3.10/3.11 而不是最新版？**
> botpy 依赖的一些库（比如 `aiohttp`）在新 Python 上可能还没有预编译包，`pip install` 会尝试现场编译，容易失败。3.10/3.11 是目前兼容性最稳的区间。原文也特别提到 botpy 要求 Python 3.8 以上。

### 0.4 创建虚拟环境

虚拟环境是给每个项目单独准备的一套 Python 依赖，避免不同项目互相污染。

Windows：

```bash
cd C:\Users\你的用户名\Desktop\bot
mkdir my_qq_bot
cd my_qq_bot
python -m venv .venv
.venv\Scripts\activate
```

macOS / Linux：

```bash
mkdir -p ~/bot/my_qq_bot && cd ~/bot/my_qq_bot
python3 -m venv .venv
source .venv/bin/activate
```

激活成功后，命令行前面会出现 `(.venv)`。

> **为什么必须用虚拟环境？**
> 假设你以后要做两个机器人，一个依赖 botpy 1.2，另一个依赖 botpy 2.0。如果都装在系统 Python 里，后装的会覆盖先装的，前一个项目就跑不起来了。虚拟环境让每个项目有独立的 `site-packages`，互不干扰。
>
> 另外，虚拟环境目录（`.venv/`）不要提交到 Git，它体积大且和操作系统绑定。

Windows PowerShell 如果提示"无法加载文件，因为在此系统上禁止运行脚本"，执行一次：

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

### 0.5 最终目录结构

本文的示例建议这样组织：

```
my_qq_bot/
├── .venv/              # 虚拟环境（不要提交）
├── .gitignore
├── config.yaml         # 配置：AppID、密钥、API key
├── requirements.txt    # 依赖清单
└── main.py             # 机器人主程序
```

---

## 第 1 章 注册开放平台并创建机器人

### 1.1 打开平台

浏览器访问：

```
https://q.qq.com/#/app/bot
```

用你的 QQ 登录，选择「QQ 机器人」→「创建机器人」。

### 1.2 选择机器人类型：私域还是公域

创建时会让你选类型，原文建议选**私域**。

| 类型 | 含义 | 适合谁 |
|------|------|--------|
| 私域 | 机器人只能被拉进你指定的频道，用户量可控 | 个人开发、学习、内部工具 |
| 公域 | 机器人可以被任意用户添加到任意频道，需要审核 | 面向公众的产品 |

> **为什么初学者选私域？**
> 公域机器人要经过平台审核，对服务器稳定性、内容安全、用户量都有要求；私域机器人不需要这些，创建完就能在测试频道里跑。原文说"公域对服务器要求高"，指的就是这个。
>
> 私域的代价是：机器人只能在你有管理权限的频道里使用，不能对外推广。学习阶段完全够用。

### 1.3 创建沙箱测试频道

原文特别强调：**沙箱频道人数必须少于 20 人**。

操作顺序：

1. 在 QQ 里创建一个频道（或者用已有的频道）；
2. 进入频道设置 → 机器人 → 添加你刚创建的机器人；
3. 确保这个频道的人数少于 20 人。

> 后台和客户端的菜单名称可能随版本调整，找不到入口时，优先在官方文档搜"沙箱""添加机器人"。步骤的**顺序**不会变：先有频道，再把机器人加进去，最后在沙箱里测试。

> **什么是沙箱？**
> 沙箱是平台给开发者准备的"测试环境"。机器人还没正式发布时，只能在沙箱频道里收发消息，普通用户看不到。这样即使代码有 bug、发了奇怪的内容，也不会影响线上用户。
>
> **为什么限制 20 人？**
> 沙箱的定位是"小范围自测"，人数少才能避免未审核的机器人被大量用户接触。这个数字是平台规则，不是技术限制。

### 1.4 拿到三把"钥匙"

进入机器人后台 →「开发设置」，你会看到三个值：

| 名称 | 英文 | 作用 | 保密级别 |
|------|------|------|----------|
| 机器人 AppID | BotAppID | 机器人的身份证号，公开也无妨 | 低 |
| 机器人令牌 | Token | 旧版 SDK 用它换取访问令牌 | **高** |
| 机器人密钥 | AppSecret | 新版 SDK 用它换取 access_token | **最高** |

> **这三个值分别代表什么？**
> - **AppID** 是"你是谁"，每次请求都要带上，用来告诉平台"这个请求来自哪个机器人"。
> - **Token / AppSecret** 是"证明你是你"的密码。平台用它们签发一个短期的 `access_token`，后续所有 API 请求都带这个 `access_token`。
> - 所以 AppID 泄露问题不大，但 Token/AppSecret 泄露等于别人可以冒充你的机器人发消息。**绝对不能写进代码提交到 GitHub。**
>
> 本文后面的代码会把它们放在 `config.yaml` 里，并且把 `config.yaml` 加进 `.gitignore`。

> **版本差异**：原文第 4 课用 `client.run(appid="12345", token="xxxx")`，那是旧版 botpy 的参数名。当前 `qq-botpy 1.2.x` 的签名是 `run(appid, secret)`，第二个参数填的是**机器人密钥 AppSecret**，SDK 内部会拿它去 `https://bots.qq.com/app/getAppAccessToken` 换 `access_token`。如果你照抄原文报 `TypeError: start() got an unexpected keyword argument 'token'`，就是这个原因。

### 1.5 找到 API 文档和发布流程

后台左侧有「发布流程」→「代码开发」，点进去可以跳到官方 API 文档。建议把下面这个地址收藏：

```
https://bot.q.qq.com/wiki/
```

> **为什么先看文档再写代码？**
> 这类平台的能力、权限、字段会变。教程只能给你一条走得通的路，遇到新需求（比如发按钮、开语音房）时，最终还是要查官方文档。学会看文档比背代码重要。

### 1.6 创建检查清单

- [ ] 已在 q.qq.com 创建机器人
- [ ] 类型选择「私域」
- [ ] 已创建一个测试频道，人数 < 20
- [ ] 机器人已添加进该频道
- [ ] 已记录 BotAppID、Token、AppSecret
- [ ] 已把密钥保存在本地文件，准备加入 `.gitignore`

---

## 第 2 章 先理解接口：REST 与 WebSocket

这一章不写代码，但非常重要。不理解"消息是怎么进来的、怎么出去的"，后面所有代码都只能死记硬背。

### 2.1 机器人有两个方向

一个机器人本质上做两件事：

```
方向一（收）：用户在频道发消息  →  平台  →  推送给你的程序
方向二（发）：你的程序处理完  →  调用平台接口  →  消息出现在频道
```

这两个方向用的是**两套不同的接口**：

- 收消息：WebSocket 长连接（事件中心）；
- 发消息：REST 风格的 HTTP 接口（OpenAPI）。

### 2.2 REST 是什么

REST 是一种"用 URL 表示资源、用 HTTP 方法表示动作"的接口设计风格。

原文给了一个很好的对比：

```
传统风格：http://localhost/user/getById?id=1
REST 风格：http://localhost/user/1
```

> **为什么 REST 更好？**
> - 传统写法把"动作"（getById）塞进了 URL 里。如果还要"删除用户"，就得再来一个 `/user/deleteById`，URL 越写越多。
> - REST 把 URL 只用来表示"资源"（`/user/1` 就是 1 号用户），动作交给 HTTP 方法：
>
> | 方法 | 含义 | 例子 |
> |------|------|------|
> | GET | 查询 | `GET /guilds/{guild_id}` 获取频道信息 |
> | POST | 创建 | `POST /channels/{channel_id}/messages` 发消息 |
> | PATCH | 修改 | `PATCH /guilds/{guild_id}/members/{user_id}/mute` 禁言 |
> | DELETE | 删除 | `DELETE /channels/{channel_id}` 删除子频道 |
>
> 这样同一批 URL 能表达所有操作，接口更统一，也方便平台做权限控制和缓存。

你不需要手写 HTTP 请求，botpy 的 `self.api.xxx()` 已经把这些 REST 接口封装成了 Python 方法。但知道底层是 HTTP，排错时很有用——比如报 401，你就知道是"请求没通过身份验证"。

### 2.3 WebSocket 事件中心是什么

如果不用长连接，你的程序只能不断问平台："有新消息吗？有新消息吗？"这叫**轮询**，既费流量又有延迟。

WebSocket 换了个思路：你的程序和平台建立一条**一直不断开的 TCP 连接**，平台一有新事件就顺着这条连接推给你。

```
轮询：     客户端 → 问 → 服务器
           客户端 → 问 → 服务器
           客户端 → 问 → 服务器

WebSocket：客户端 ←——— 事件推送 ——— 服务器
           客户端 ←——— 事件推送 ——— 服务器
```

> **"事件"是什么？**
> 平台上发生的任何事都叫事件：有人 @ 了机器人、有人删了消息、有人加入了频道、有人上麦了……平台把这些事打包成 JSON，通过 WebSocket 推给你的程序。
>
> 如果你用过 nonebot，这个概念很类似：协议端和平台保持长连接，事件通过连接推送过来，而不是你的程序主动去轮询。区别只是 botpy 把"协议端"也封装进 SDK 了。
>
> botpy 收到 JSON 后，会看事件的类型，然后去找你类里对应名字的方法。比如事件 `at_message_create` 对应方法 `on_at_message_create`。
>
> **这就是为什么回调函数名不能随便起**——名字是 SDK 和你的代码之间的约定。

### 2.4 一次完整的收发流程

把上面两部分拼起来，机器人启动后的流程是：

```
① client.run(appid, secret)
        │
        ▼
② 用 AppID + Secret 换 access_token（REST）
        │
        ▼
③ 建立 WebSocket 连接，完成鉴权
        │
        ▼
④ 告诉平台"我订阅了哪些事件"（Intents）
        │
        ▼
⑤ 用户 @ 机器人 → 平台通过 WebSocket 推事件
        │
        ▼
⑥ botpy 解析事件，调用 on_at_message_create(message)
        │
        ▼
⑦ 你在回调里调用 await message.reply(...)
        │
        ▼
⑧ botpy 用 access_token 发 REST 请求 → 消息出现在频道
```

第 ③ 到 ⑧ 步的细节 SDK 都替你处理了，你只需要写第 ⑥ 步的回调。

### 2.5 被动消息与主动消息（贯穿全文的关键概念）

平台把机器人发的消息分成两类：

| 类型 | 触发方式 | 限制 |
|------|----------|------|
| 被动消息 | 回复用户刚发的消息，带 `msg_id` 或 `event_id` | 有效期 5 分钟；不占用主动推送配额 |
| 主动消息 | 机器人自己发起，不带 `msg_id` | 每个子频道每天限 2 条（botpy 文档注明） |

> **为什么要这样设计？**
> 如果没有限制，机器人可以无限刷屏，用户体验会很差。平台用"你必须回复一条真实存在的用户消息"来约束机器人，同时给主动推送留了很小的额度（每天 2 条），迫使我们把重要的通知合并发送。
>
> **这解释了两件事**：
> 1. 为什么 `MessageSendRequest` 里要带 `msg_id`——证明"我是在回复，不是在骚扰"；
> 2. 为什么第 7 课的定时天气推送要特别小心——它是主动消息，额度非常有限，真实项目里通常一天只推一次，而且要和其它通知合并。

---

## 第 3 章 安装 SDK

### 3.1 先认清 SDK 的名字

原文提到了多个名字，初学者很容易装错：

| 包名 | 导入名 | 状态 | 说明 |
|------|--------|------|------|
| `qq-botpy` | `import botpy` | **推荐** | 当前官方 Python SDK，本文主线 |
| `qq-bot` | `import qqbot` | 旧版，逐步停用 | 原文第 5–8 课用的就是它 |
| `botgo` | — | Go 版 | 非 Python |
| `bot-node-sdk` | — | Node.js 版 | 非 Python |

> **为什么会有两个 Python SDK？**
> `qq-bot` 是早期的 SDK，接口偏"对象式"（返回的对象用 `.id` 取值）；`qq-botpy` 是后来的重构版，接口更贴近平台原始 JSON（返回字典，用 `["id"]` 取值），并且支持群机器人、私信等新能力。原文前半部分用旧版、后半部分用新版，所以代码风格会来回跳。
>
> **学习建议**：新项目一律用 `qq-botpy`。旧版代码只需要能看懂，不要求能跑。

### 3.2 安装

确保虚拟环境已激活，然后：

```bash
pip install qq-botpy
```

国内网络慢可以换镜像：

```bash
pip install qq-botpy -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 3.3 验证安装

```bash
python -c "import botpy; print(botpy.__file__)"
```

能打印出 `.../site-packages/botpy/__init__.py` 就说明装好了。

再确认版本：

```bash
pip show qq-botpy
```

> **为什么要验证？**
> 常见的"装好了但跑不起来"是因为：终端里激活的是 A 虚拟环境，而 IDE 里选的是 B 解释器。用 `python -c "import botpy"` 验证的是**当前终端**的解释器；PyCharm 里还要在 Settings → Project → Python Interpreter 里选对同一个 `.venv`。

### 3.4 生成依赖清单

```bash
pip freeze > requirements.txt
```

> **这个文件有什么用？**
> 它记录了你项目用到的所有包和精确版本。换一台服务器部署时，只需 `pip install -r requirements.txt`，就能装出一模一样的环境，避免"我电脑上能跑，服务器上不行"。

---

## 第 4 章 最小可运行机器人（botpy 快速入门）

这一章对应原文第 4 课。目标是：**用最少的代码，让机器人在频道里回复一句 @ 消息**。先跑通，再理解。

### 4.1 完整代码

新建 `main.py`：

```python
import botpy
from botpy.message import Message


class MyClient(botpy.Client):
    async def on_at_message_create(self, message: Message):
        await message.reply(
            content=f"机器人 {self.robot.name} 收到你的@消息了：{message.content}"
        )


intents = botpy.Intents(public_guild_messages=True)
client = MyClient(intents=intents)
client.run(appid="你的BotAppID", secret="你的AppSecret")
```

把 `appid` 和 `secret` 换成第 1 章拿到的值，运行：

```bash
python main.py
```

然后在沙箱频道里 @ 机器人说一句话。如果一切正常，机器人会回复你。

### 4.2 逐行拆解

#### 第 1–2 行：导入

```python
import botpy
from botpy.message import Message
```

- `import botpy` 导入整个 SDK，后面用 `botpy.Client`、`botpy.Intents`。
- `from botpy.message import Message` 只导入 `Message` 这个类，用来做类型标注。

> **`Message` 是什么？**
> 它是 SDK 对"一条频道消息"的封装。当用户 @ 机器人时，平台推过来的 JSON 里有消息 ID、内容、作者、频道 ID 等字段，botpy 把它包装成 `Message` 对象再传给你的回调。
>
> 你后面会用到的属性：
>
> | 属性 | 含义 |
> |------|------|
> | `message.id` | 消息 ID（被动回复时作为 `msg_id`） |
> | `message.content` | 文本内容 |
> | `message.channel_id` | 消息所在子频道 ID |
> | `message.guild_id` | 消息所在频道（服务器）ID |
> | `message.author.id` | 发送者 ID |
> | `message.author.username` | 发送者昵称 |
> | `message.author.avatar` | 发送者头像 URL |
> | `message.event_id` | 这次事件的唯一 ID |

#### 第 5 行：继承 Client

```python
class MyClient(botpy.Client):
```

这一行做了两件事：

1. **继承**：`MyClient` 继承了 `botpy.Client` 的所有能力——建立 WebSocket、维持心跳、自动重连、解析事件、调用 REST API。
2. **留出扩展点**：你只需要在子类里写"收到某事件后做什么"，不用管连接细节。

> **为什么不用自己写 WebSocket？**
> 因为连接管理很繁琐：鉴权、心跳（每隔几十秒发一个包证明还活着）、断线重连、事件分片、错误码处理……这些和你的业务无关。SDK 把它们封装在 `Client` 里，你只写业务回调，这叫"框架负责骨架，你负责血肉"。

#### 第 6 行：事件回调

```python
    async def on_at_message_create(self, message: Message):
```

这是整个机器人最核心的一行。

- `on_at_message_create` 不是随便起的名字，它是 SDK 约定的**事件处理器名称**。平台推来 `at_message_create` 事件，SDK 就把事件名前面加上 `on_`，用 `getattr` 找到这个方法。
- 如果找不到同名方法，SDK 会直接忽略这个事件（日志里显示"事件未注册"）。
- `async def` 表示这是一个协程函数，必须用 `await` 调用。原因见 4.3。

> **SDK 内部大概是这样找你的方法的**（简化）：
>
> ```python
> method = "on_" + event_name          # at_message_create → on_at_message_create
> coro = getattr(self, method)         # 在 MyClient 实例上找这个方法
> self.loop.create_task(coro(...))     # 作为一个异步任务调度
> ```
>
> 所以：**事件名决定方法名，方法名写错就收不到事件**。这是新手最常见的"机器人没反应"原因之一。

#### 第 7–9 行：回复消息

```python
        await message.reply(
            content=f"机器人 {self.robot.name} 收到你的@消息了：{message.content}"
        )
```

- `message.reply(...)` 是 `Message` 对象提供的快捷方法，等价于：

  ```python
  await self.api.post_message(channel_id=message.channel_id, msg_id=message.id, content=...)
  ```

  它会自动把"回复到哪个子频道""回复哪条消息"填好，所以最省事。
- `self.robot.name` 是机器人自己的昵称。`self.robot` 在登录成功后由 SDK 赋值。
- `f"..."` 是 f-string，花括号里的表达式会被替换成实际值。比如 `message.content` 是 `"你好"`，最终字符串就是 `"机器人 XXX 收到你的@消息了：你好"`。

> **为什么回复要带 `msg_id`？**
> 回到 2.5：带 `msg_id` 表示"我在回复这条具体的消息"，属于被动消息，5 分钟内有效，不占主动推送配额。`message.reply` 自动帮你带上了 `message.id`，所以你不用手动传。

#### 第 12 行：声明订阅哪些事件

```python
intents = botpy.Intents(public_guild_messages=True)
```

`Intents` 是"事件订阅开关"。平台不会把所有事件都推给你，只推你声明过的。

- `public_guild_messages=True` 表示订阅"公域频道消息"事件，包括 @ 机器人。
- 不写这一行，`on_at_message_create` 永远不会被调用——即使方法名完全正确。

> **为什么平台要让你显式订阅？**
> 三个原因：
> 1. **省流量**：一个大型频道每秒可能有上百条消息，全推给你既浪费带宽也浪费你的 CPU。
> 2. **隐私与权限**：成员列表、私信、论坛等事件涉及用户隐私，平台需要确认你确实需要。
> 3. **按需计费/限流**：平台可以按订阅类型做配额管理。
>
> `Intents` 底层是一个**位掩码**：每个开关占一个二进制位，打开多个就是把对应的位设为 1。所以你可以同时开多个：
>
> ```python
> intents = botpy.Intents(
>     public_guild_messages=True,
>     guild_members=True,
>     direct_message=True,
> )
> ```

#### 第 13–14 行：创建并启动

```python
client = MyClient(intents=intents)
client.run(appid="你的BotAppID", secret="你的AppSecret")
```

- `MyClient(intents=intents)` 把订阅开关传给父类构造函数。父类还会初始化 HTTP 客户端、事件循环等。
- `client.run(...)` 是**阻塞**的：它会一直运行，直到你按 `Ctrl+C`。所以它必须是程序最后一行，后面写的代码永远不会执行。

> **`run` 内部发生了什么？**
> 简化版：
>
> ```python
> def run(self, appid, secret):
>     async def runner():
>         token = Token(appid, secret)       # 换取 access_token
>         await self._bot_login(token)
>         await self._bot_init(token)        # 建立 WebSocket、鉴权、订阅事件
>     self.loop.run_until_complete(runner())
> ```
>
> 它启动了 asyncio 事件循环，把所有异步任务跑起来。这也是为什么你的回调必须是 `async def`——它们要被塞进同一个事件循环里调度。

### 4.3 补充：async/await 十分钟入门

如果你没写过异步代码，这一段能帮你避开 80% 的坑。

#### 同步 vs 异步

同步代码是一条路走到黑：

```python
def handler():
    result = requests.get(url)   # 等 3 秒，期间什么都做不了
    print(result)
```

异步代码在等待时会把 CPU 让给别人：

```python
async def handler():
    result = await aiohttp_get(url)   # 发起请求后先让出控制权
    print(result)
```

> **打个比方**：
> - 同步 = 你点外卖，站在店门口干等 30 分钟，期间不能做任何事。
> - 异步 = 你点完外卖拿到取餐号，回去继续工作，外卖到了再去取。
>
> 事件循环就是那个"帮你记住取餐号、到点通知你"的调度员。

#### 三个关键词

| 关键词 | 含义 |
|--------|------|
| `async def` | 定义一个协程函数。调用它不会立刻执行，而是返回一个协程对象 |
| `await` | 等待一个协程完成，等待期间把控制权交还事件循环 |
| 事件循环 | 一个无限循环，负责调度所有协程 |

看个例子：

```python
import asyncio

async def say_after(delay, word):
    await asyncio.sleep(delay)
    print(word)

async def main():
    await asyncio.gather(
        say_after(2, "世界"),   # 2 秒后打印
        say_after(1, "你好"),   # 1 秒后打印
    )

asyncio.run(main())
```

输出顺序是"你好"再"世界"，总耗时约 2 秒而不是 3 秒——因为两个任务在并行等待。

#### 最重要的禁忌

在回调里**不要写阻塞代码**：

```python
import time
import requests

async def on_at_message_create(self, message):
    time.sleep(5)                  # ❌ 整个机器人卡死 5 秒
    requests.get("https://...")    # ❌ 同样是阻塞的
```

> **为什么阻塞代码这么危险？**
> 事件循环是单线程的。你阻塞了它，所有其他事件（别人的消息、心跳包）都处理不了。心跳超时后，平台会认为机器人掉线，直接断开连接。
>
> 正确做法：
>
> | 场景 | 错误写法 | 正确写法 |
> |------|----------|----------|
> | 等待 | `time.sleep(5)` | `await asyncio.sleep(5)` |
> | HTTP 请求 | `requests.get(...)` | `await session.get(...)`（aiohttp/httpx） |
> | 读大文件/重计算 | 直接在回调里做 | `await asyncio.to_thread(func)` |
> | 定时任务 | `threading.Timer` 里碰事件循环 | `asyncio.create_task` 或 APScheduler |

### 4.4 Intents 的两种写法

原文给了两种，效果一样。

写法一，构造函数里直接开：

```python
intents = botpy.Intents(public_guild_messages=True)
```

写法二，先全关，再逐项开：

```python
intents = botpy.Intents.none()
intents.public_guild_messages = True
```

> **什么时候用第二种？**
> 需要开很多项时，第二种更清晰，也方便根据配置文件动态开关。`Intents.none()` 表示"一个都不订阅"，然后你按需打开。
>
> 另外还有 `Intents.all()`（全开）和 `Intents.default()`（开公域事件，关私域事件）。生产环境不建议 `all()`，容易收到大量用不到的事件。

### 4.5 运行与测试

```bash
python main.py
```

日志里看到 `[botpy] 机器人 ... 启动成功` 之类的输出，就说明连上了。

测试步骤：

1. 打开沙箱频道；
2. 在文字子频道里输入 `@你的机器人 你好`；
3. 看机器人是否回复。

> **如果没反应，按这个顺序检查**：
> 1. 机器人是否已加入这个频道？
> 2. 频道人数是否 < 20（沙箱限制）？
> 3. `intents` 是否开了 `public_guild_messages`？
> 4. 方法名是否精确拼写为 `on_at_message_create`？
> 5. 终端有没有报错（尤其是 401/403）？
> 6. 是不是同时开了多个机器人进程，消息被另一个进程抢走了？

### 4.6 常见报错

| 报错 | 原因 | 解决 |
|------|------|------|
| `TypeError: start() got an unexpected keyword argument 'token'` | 新版 botpy 参数叫 `secret` | 改成 `client.run(appid=..., secret=...)` |
| `ImportError: cannot import name 'TypedDict'` | Python < 3.8 | 升级 Python 到 3.8+（推荐 3.10） |
| `ModuleNotFoundError: No module named 'botpy'` | 装到了别的解释器/环境 | 确认虚拟环境激活，`pip show qq-botpy` |
| `401 Unauthorized` | AppID/Secret 错误或过期 | 重新复制，注意不要带空格 |
| 连上但收不到消息 | Intents 没开 / 方法名错 / 机器人不在频道 | 按 4.5 的顺序排查 |

---

## 第 5 章 用旧版 SDK 走一遍完整流程

这一章对应原文第 5 课。原文用的是旧版 `qq-bot`，通过腾讯云的在线实验环境操作。

> **为什么还要学旧版？**
> 1. 很多网上教程和公司老项目仍在使用 `qq-bot`，看懂它能帮你维护旧代码；
> 2. 它把"读配置 → 注册 handler → 监听事件 → 调 API 回复"的流程暴露得更明显，有助于理解 botpy 帮你封装了什么；
> 3. 原文的天气、推送、Ark 案例都是基于它写的，不看这一章，后面几章会断档。
>
> **但请注意**：新项目不要再用 `qq-bot`。如果 `pip install qq-bot` 失败或运行报 404，不要卡住，直接看 5.6 的 botpy 等价写法。

### 5.1 环境命令逐条解释

原文在腾讯云实验环境里执行了这些命令：

```bash
python3 --version
```

查看 Python 版本。`python3` 是 Linux/macOS 的叫法，Windows 上通常是 `python`。

```bash
pip install qq-bot
pip install pyyaml
```

安装旧版 SDK 和 YAML 解析库。

> **`pyyaml` 是干什么的？**
> 它负责读写 YAML 格式的配置文件。后面 `config.yaml` 里放 AppID 和 token，程序启动时用 `YamlUtil`（内部就是 pyyaml）读出来。这样改配置不用改代码。

```bash
mkdir /home/demo && cd /home/demo
```

- `mkdir` 创建目录；
- `&&` 表示"前一条命令成功后才执行后一条"；
- `cd` 切换进去。

如果目录可能已存在，用 `mkdir -p /home/demo`，`-p` 的作用是"父目录不存在就一起创建，已存在也不报错"。

```bash
touch config.yaml
touch robot.py
```

`touch` 创建空文件。它不会覆盖已有文件的内容，只是把"最后修改时间"更新一下。

### 5.2 写配置文件

`config.yaml`：

```yaml
token:
  appid: "你的AppID"
  token: "你的Token"
```

YAML 的三个基本规则：

1. **用缩进表示层级**，不能用 Tab，只能用空格（通常 2 个）；
2. **冒号后面必须有一个空格**，`appid: "xxx"` 对，`appid:"xxx"` 错；
3. 字符串建议加引号，尤其是纯数字或含特殊字符时。

> **为什么不把 AppID 直接写在代码里？**
> - 改配置不用改代码，降低改错风险；
> - 可以把 `config.yaml` 排除在 Git 之外，避免密钥泄露；
> - 同一份代码可以用不同配置跑测试环境和生产环境。
>
> 这是"配置与代码分离"原则，几乎所有正式项目都会这么做。

### 5.3 robot.py 逐段解释

先看导入和配置读取：

```python
import asyncio
import json
import os
import threading
from typing import Dict, List

import aiohttp
import qqbot

from qqbot.core.util.yaml_util import YamlUtil

test_config = YamlUtil.read(
    os.path.join(os.path.dirname(__file__), "config.yaml")
)
t_token = qqbot.Token(test_config["token"]["appid"], test_config["token"]["token"])
```

- `os.path.dirname(__file__)` 取当前脚本所在目录。用绝对路径拼配置文件，避免"从别的目录启动就找不到文件"。
- `YamlUtil.read(...)` 读取 YAML，返回一个字典。
- `test_config["token"]["appid"]` 就是 YAML 里两层缩进对应的值。
- `qqbot.Token(appid, token)` 创建一个令牌对象，后续所有 API 都用它鉴权。

> **原文把 `t_token` 写在 `if __name__ == "__main__":` 里面**，而 `_message_handler` 里直接用这个名字。Python 会去全局作用域找，运行时恰好能找到，但这是很脆弱的写法——一旦你在别的地方导入这个模块，`t_token` 就不存在了。本文把它提前到模块顶层，逻辑更清楚。

然后是消息处理函数：

```python
async def _message_handler(event, message: qqbot.Message):
    """
    定义事件回调的处理
    :param event: 事件类型
    :param message: 事件对象（如监听消息是Message对象）
    """
    msg_api = qqbot.AsyncMessageAPI(t_token, False)
    qqbot.logger.info("event %s, receive message %s" % (event, message.content))

    # 构造一条回复消息
    message_to_send = qqbot.MessageSendRequest(content="你好", msg_id=message.id)
    await msg_api.post_message(message.channel_id, message_to_send)
```

几个关键点：

- `event` 是事件类型字符串，`message` 是解析后的消息对象。
- `AsyncMessageAPI(t_token, False)` 的第二个参数 `False` 表示**不使用沙箱环境**。如果你在沙箱频道测试，有些旧版接口需要传 `True`，具体看当时的文档。
- `MessageSendRequest(content=..., msg_id=...)` 是"消息请求体"，描述你要发什么。
- `msg_id=message.id` 让它成为被动回复（见 2.5）。
- `post_message(channel_id, request)` 才是真正发出去的动作。

最后是入口：

```python
if __name__ == "__main__":
    qqbot_handler = qqbot.Handler(
        qqbot.HandlerType.AT_MESSAGE_EVENT_HANDLER, _message_handler
    )
    qqbot.async_listen_events(t_token, False, qqbot_handler)
```

- `if __name__ == "__main__":` 保证只有直接运行这个文件时才启动；被别的模块 import 时不会意外启动。
- `qqbot.Handler(...)` 把"事件类型"和"处理函数"绑定起来。
- `qqbot.async_listen_events(...)` 建立 WebSocket 并开始监听，**阻塞**运行。

> **旧版 SDK 和 botpy 的设计差异**
>
> | 对比 | 旧版 qq-bot | 新版 botpy |
> |------|-------------|------------|
> | 注册方式 | 手动创建 `Handler` 并绑定事件类型 | 在 `Client` 子类里写 `on_xxx` 方法，SDK 自动发现 |
> | 事件类型 | 用 `HandlerType` 枚举显式指定 | 方法名隐含事件类型 |
> | 返回对象 | 对象，用 `.id` 取值 | 字典（TypedDict），用 `["id"]` 取值 |
> | 启动 | `async_listen_events(...)` | `client.run(appid, secret)` |
>
> botpy 的写法更"约定优于配置"，少写绑定代码，但也要求方法名必须精确。

### 5.4 运行

```bash
python3 /home/demo/robot.py
```

然后在频道里 @ 机器人，它应该回复"你好"。

### 5.5 新旧 SDK 对照表

| 功能 | 旧版 qq-bot | 新版 botpy |
|------|-------------|------------|
| 令牌 | `qqbot.Token(appid, token)` | `client.run(appid, secret)` 内部处理 |
| 发消息 API | `qqbot.AsyncMessageAPI(token, is_sandbox)` | `self.api`（Client 自带） |
| 消息请求体 | `qqbot.MessageSendRequest(...)` | 直接给 `message.reply(content=...)` 或 `post_message(...)` 传关键字参数 |
| 创建私信 | `AsyncDmsAPI.create_direct_message(...)` | `self.api.create_dms(guild_id, user_id)` |
| 发私信 | `AsyncDmsAPI.post_direct_message(...)` | `self.api.post_dms(...)` |
| 用户频道列表 | `AsyncUserAPI.me_guilds()` | `self.api.me_guilds()` |
| 子频道列表 | `AsyncChannelAPI.get_channels(guild_id)` | `self.api.get_channels(guild_id)` |

### 5.6 用 botpy 重写同一个示例（推荐照着跑）

如果你不想碰旧 SDK，这一段是 5.3 的现代等价版本，同时把第 6 章的天气功能也合进来了。需要先安装：

```bash
pip install qq-botpy pyyaml
```

`config.yaml`：

```yaml
appid: "你的BotAppID"
secret: "你的AppSecret"

weather:
  appkey: "你的天气API appkey"
  sign: "你的天气API sign"
```

`main.py`：

```python
import os

import aiohttp
import botpy
import yaml
from botpy.message import Message

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(BASE_DIR, "config.yaml"), "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)


async def get_weather(city_name: str) -> dict:
    url = "http://api.k780.com/"
    params = {
        "app": "weather.today",
        "appkey": config["weather"]["appkey"],
        "sign": config["weather"]["sign"],
        "format": "json",
        "cityNm": city_name,
    }
    timeout = aiohttp.ClientTimeout(total=5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, params=params) as resp:
            return await resp.json()


class MyClient(botpy.Client):
    async def on_at_message_create(self, message: Message):
        content = (message.content or "").strip()

        if content.startswith("/天气"):
            city = content[len("/天气"):].strip() or "深圳"
            weather = await get_weather(city)
            result = weather.get("result")

            if not result:
                await message.reply(content="没查到这座城市的天气，请检查城市名。")
                return

            text = (
                f"{result['citynm']} {result['weather']} "
                f"{result['days']} {result['week']}\n"
                f"温度：{result['temperature']}，当前 {result['temperature_curr']}"
            )
            await message.reply(content=text, image=result["weather_icon"])
            return

        await message.reply(
            content=f"机器人 {self.robot.name} 收到你的@消息了：{message.content}"
        )


intents = botpy.Intents(public_guild_messages=True)
client = MyClient(intents=intents)
client.run(appid=config["appid"], secret=config["secret"])
```

运行：

```bash
python main.py
```

> **这段代码相比原文多了什么？**
> - 用 `yaml.safe_load` 读配置，不再写死密钥；
> - 用 `str.startswith` 判断指令，避免 `split` 越界；
> - 处理了"城市查不到"的分支，机器人不会因为 `IndexError` 崩掉；
> - 用 `message.reply` 同时发文字和图标，代码更短。
>
> 这些改动看起来小，但都是实际项目里必须考虑的健壮性问题。

---

## 第 6 章 天气案例：调用第三方 API

这一章对应原文第 6 课。核心知识点不是"天气"，而是**机器人如何调用外部 HTTP 接口**。

### 6.1 整体思路

```
用户 @机器人 发 "/天气 深圳"
        │
        ▼
on_at_message_create 收到消息
        │
        ▼
解析出城市名 "深圳"
        │
        ▼
get_weather("深圳") ──HTTP──▶ 天气 API
        │                         │
        │◀──────── JSON ─────────┘
        ▼
从 JSON 里取字段，拼成一句话
        │
        ▼
message.reply(...) 回复用户
```

### 6.2 原文代码

```python
async def get_weather(city_name: str) -> Dict:
    weather_api_url = (
        "http://api.k780.com/?app=weather.today"
        "&appkey=10003"
        "&sign=b59bc3ef6191eb9f747dd4e83c99f2a4"
        "&format=json"
        "&cityNm=" + city_name
    )
    async with aiohttp.ClientSession() as session:
        async with session.get(url=weather_api_url, timeout=5) as resp:
            content = await resp.text()
            content_json_obj = json.loads(content)
            return content_json_obj
```

调用处：

```python
weather_dict = await get_weather("深圳")
weather_desc = (
    weather_dict["result"]["citynm"] + " "
    + weather_dict["result"]["weather"] + " "
    + weather_dict["result"]["days"] + " "
    + weather_dict["result"]["week"]
)
message_to_send = qqbot.MessageSendRequest(
    msg_id=message.id,
    content=weather_desc,
    image=weather_dict["result"]["weather_icon"],
)
await msg_api.post_message(message.channel_id, message_to_send)
```

### 6.3 逐行解释

#### `async with aiohttp.ClientSession() as session:`

- `aiohttp` 是异步 HTTP 客户端，用来替代阻塞的 `requests`。
- `ClientSession` 内部维护连接池，多个请求可以复用同一条 TCP 连接，比每次新建连接快。
- `async with` 是异步上下文管理器，退出时会自动关闭会话，不需要手动 `close()`。

> **为什么不能直接用 `requests`？**
> `requests.get()` 是同步阻塞的。在异步回调里调用它，整个事件循环会停下来等网络返回（见 4.3）。aiohttp 则是"发起请求后让出控制权，数据到了再继续"，不会拖累其他事件。

#### `timeout=5`

设置 5 秒超时。

> **为什么必须设超时？**
> 第三方接口可能挂掉、可能很慢。如果不设超时，请求会一直挂着，你的回调永远不返回，用户等不到回复，连接也占着不放。5 秒是一个合理的上限——超过就认为失败，给用户回一句"天气服务暂时不可用"。

> **另一个容易忽略的问题：URL 里的中文**
> 原文直接把 `city_name` 拼进 URL（`...&cityNm=` + city_name）。中文没有经过 URL 编码，在部分服务器或客户端上会请求失败。更稳的写法是把所有参数放进字典，交给 aiohttp 自动编码：
>
> ```python
> params = {"app": "weather.today", "cityNm": city_name, ...}
> async with session.get(url, params=params) as resp:
>     ...
> ```
>
> 见 5.6 的完整写法。这属于"能跑但不够稳"的细节，初学时容易忽略。

#### `await resp.text()` + `json.loads(...)`

先把响应体读成字符串，再用标准库 `json.loads` 解析成 Python 字典。

更简洁的写法是：

```python
return await resp.json()
```

> **两者有什么区别？**
> 几乎一样。`resp.json()` 内部也是读文本再解析，只是省了一行。如果接口返回的不是合法 JSON，它会抛 `ContentTypeError` 或 `JSONDecodeError`，所以生产代码要包 `try/except`。

#### 返回的 JSON 长什么样

k780 天气接口的响应大致是：

```json
{
  "success": "1",
  "result": {
    "citynm": "深圳",
    "weather": "晴",
    "days": "2022-07-31",
    "week": "星期日",
    "temperature": "28℃~35℃",
    "temperature_curr": "32℃",
    "temp_high": "35℃",
    "temp_low": "28℃",
    "humidity": "60%",
    "weather_icon": "http://api.k780.com/upload/weather/xxx.png"
  }
}
```

> **为什么取值要写 `["result"]["citynm"]` 两层？**
> 因为接口把"状态码"和"业务数据"分开了：外层 `success` 表示请求是否成功，真正的内容在 `result` 里。这种结构很常见，好处是接口可以统一返回错误信息（比如 `{"success":"0","msg":"城市不存在"}`），而不用改变 `result` 的结构。
>
> 写代码时先判断 `result` 是否存在，再取值，否则接口报错时你的程序会抛 `KeyError`。

### 6.4 关于 sign 过期

原文特别提醒：文章里那个 `sign` 可能会过期。

> **`sign` 是什么？**
> 它是 API 提供方用来验证"调用者身份"的签名。免费接口通常给一个公共的测试 key，但这个 key 有有效期或调用次数限制，过期后接口会返回错误。
>
> 解决办法：去 [nowapi](https://www.nowapi.com/) 注册一个免费账号，拿到自己的 `appkey` 和 `sign`，替换掉代码里的公共值。
>
> **不要把别人的公共 key 当成长期方案**——它随时可能失效，而且所有教程读者共用一个 key，很容易被限流。

### 6.5 安全提醒

```python
# ❌ 不要这样
appkey = "10003"
sign = "b59bc3ef6191eb9f747dd4e83c99f2a4"
```

> **为什么不能把 key 写死在代码里？**
> 代码一旦推到 GitHub，key 就公开了。爬虫会扫描公开仓库里的 key，几分钟内就能盗用你的额度。正确做法是放在 `config.yaml` 或环境变量里，并把配置文件加入 `.gitignore`。
>
> 如果已经泄露了，去 API 提供方后台重置 key，不要只是删掉 commit——Git 历史里仍然能找到。

---

## 第 7 章 主动推送：定时发消息

这一章对应原文第 7 课。

### 7.1 先分清主动与被动

| | 被动回复 | 主动推送 |
|--|----------|----------|
| 触发 | 用户先发消息，机器人回复 | 机器人自己定时发 |
| 必带字段 | `msg_id`（或 `event_id`） | 不带 |
| 有效期 | 5 分钟内 | 无（随时可发） |
| 配额 | 不占主动配额 | 每子频道每天 2 条 |
| 适用场景 | 问答、指令 | 每日天气、活动提醒 |

> **为什么主动推送要单独设计配额？**
> 因为它是"机器人主动打扰用户"。平台必须限制频率，否则一个机器人就能把频道刷爆。每天 2 条意味着：真实项目里你应该把所有定时通知**合并成一条**，而不是每个功能各发一条。

### 7.2 推送需要先找到"往哪发"

机器人可能被拉进多个频道，每个频道下有多个子频道。主动推送时没有"用户消息"可以回复，所以必须自己指定 `channel_id`。

流程：

```
me_guilds()           → 我加入了哪些频道（Guild）
        │
        ▼
guilds[0].id          → 取第一个频道的 ID
        │
        ▼
get_channels(guild_id) → 这个频道下有哪些子频道
        │
        ▼
channels[0].id        → 取第一个子频道的 ID
        │
        ▼
post_message(channel_id, ...) → 发出去
```

> **为什么原文用 `[0]`？**
> 演示代码图省事，直接取第一个。真实项目不能这样——你无法保证第一个就是"公告频道"。更稳的做法是按名字或 ID 查找：
>
> ```python
> target = next(c for c in channels if c["name"] == "公告")
> channel_id = target["id"]
> ```
>
> 或者把目标 `channel_id` 直接写在配置里，启动时校验一次。

### 7.3 原文代码

```python
async def send_weather_message_by_time():
    """
    任务描述：每天推送一次普通天气消息（演示方便改为100s定时运行）
    """
    weather_dict = await get_weather("深圳")

    # 获取频道列表，都取首个频道的首个子频道推送
    user_api = qqbot.AsyncUserAPI(t_token, False)
    guilds = await user_api.me_guilds()
    guilds_id = guilds[0].id

    channel_api = qqbot.AsyncChannelAPI(t_token, False)
    channels = await channel_api.get_channels(guilds_id)
    channels_id = channels[0].id
    qqbot.logger.info("channelid %s" % channel_id)   # 原文这里有 bug

    weather = "当前天气是：" + weather_dict["result"]["weather"]
    send = qqbot.MessageSendRequest(content=weather)
    msg_api = qqbot.AsyncMessageAPI(t_token, False)
    await msg_api.post_message(channels_id, send)

    # 如果需要每天都执行，加上下面两句
    t = threading.Timer(100, await send_weather_message_by_time)
    t.start()
```

### 7.4 原文的两个 bug

这段代码直接运行会出问题，必须指出来。

#### bug 1：`channel_id` 未定义

```python
qqbot.logger.info("channelid %s" % channel_id)
```

上面定义的变量叫 `channels_id`，这里写成了 `channel_id`。运行到这一行会抛 `NameError`。

修正：

```python
qqbot.logger.info("channel_id %s" % channels_id)
```

#### bug 2：`threading.Timer` 里不能用 `await`

```python
t = threading.Timer(100, await send_weather_message_by_time)
```

这里有两层错误：

1. `await send_weather_message_by_time()` 会**立刻执行**这个协程，而不是"100 秒后再执行"。`await` 是"现在等待它完成"，不是"延迟"。
2. `threading.Timer` 的第二个参数要求是一个**普通可调用对象**（函数）。协程函数的返回值是协程对象，不能直接被线程调用。

更糟的是，这段代码写在 `send_weather_message_by_time` 自己内部，`await` 自己会造成无限递归。

> **正确的定时思路**
> "延迟执行"和"异步"是两件事：
> - 延迟：用 `threading.Timer`、`asyncio.sleep`、APScheduler；
> - 异步：用 `async def` + `await`。
>
> 两者要正确组合，不能把 `await` 塞进 `Timer` 的参数里。

### 7.5 三种正确的定时方案

> 无论用哪种方案，都要先删掉 `send_weather_message_by_time` 函数末尾那两行 `threading.Timer`。调度的职责应该交给外层的定时器，不要让函数自己安排自己——否则会出现"一次执行安排出两次"的重复推送。

#### 方案一：演示用，线程 + 新事件循环

```python
import asyncio
import threading


def run_push_job(interval_seconds: int = 100):
    """在线程里跑一次异步任务，然后再安排下一次。"""
    try:
        asyncio.run(send_weather_message_by_time())
    except Exception:
        qqbot.logger.exception("天气推送失败")
    finally:
        threading.Timer(interval_seconds, run_push_job, args=(interval_seconds,)).start()


if __name__ == "__main__":
    run_push_job()   # 启动第一次
    qqbot_handler = qqbot.Handler(
        qqbot.HandlerType.AT_MESSAGE_EVENT_HANDLER, _message_handler
    )
    qqbot.async_listen_events(t_token, False, qqbot_handler)
```

> 为什么这样能工作？
> `threading.Timer` 在**另一个线程**里调用 `run_push_job`，这个线程用 `asyncio.run` 新建一个独立的事件循环来跑协程，不会干扰主线程里的 WebSocket 循环。`finally` 保证即使这次推送失败，下一次仍会被安排。

#### 方案二：botpy 里用 `asyncio.create_task`

如果你用的是 botpy，并且能拿到事件循环：

```python
import asyncio


async def weather_push_loop(interval_seconds: int = 86400):
    while True:
        try:
            await send_weather_message_by_time()
        except Exception:
            botpy.logger.exception("天气推送失败")
        await asyncio.sleep(interval_seconds)


# 在 client.run 之前启动后台任务
client.loop.create_task(weather_push_loop())
client.run(appid=..., secret=...)
```

> `asyncio.sleep` 是非阻塞的，等待期间事件循环可以继续处理消息。`while True` 让任务一直活着。

#### 方案三：生产环境用 APScheduler（推荐）

```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
scheduler.add_job(
    send_weather_message_by_time,
    CronTrigger(hour=7, minute=30),
    id="daily_weather",
    misfire_grace_time=300,
)
scheduler.start()
```

> **为什么生产环境推荐 APScheduler？**
> - 支持 cron 表达式（每天 7:30），不用自己算秒数；
> - 支持持久化、错过任务补偿（`misfire_grace_time`）；
> - 支持任务去重、并发控制；
> - 进程重启后任务配置不会丢。
>
> 本仓库 `CDUTETC_ACM_BOT` 用的就是 APScheduler 做提醒投递，思路完全一样。

### 7.6 主动推送的注意事项

1. **配额**：每个子频道每天 2 条，超了会被平台拒绝。多个通知要合并。
2. **权限**：机器人必须在目标子频道有发言权限。
3. **在线**：发消息接口要求机器人保持 WebSocket 在线。程序挂了就发不出去。
4. **幂等**：定时任务可能因重启重复执行，发之前最好记录"今天是否已发"。
5. **异常**：一次推送失败不能让整个循环退出，必须 `try/except` 包住。

---

## 第 8 章 指令回复：Ark 卡片与 Embed 私信

这一章对应原文第 8 课。它讲三件事：怎么解析指令、怎么发漂亮的卡片、怎么发私信。

### 8.1 解析指令的基本思路

用户发的消息是纯文本，比如：

```
@机器人 /天气 成都
```

机器人收到的 `message.content` 大概是 `/天气 成都`（@ 部分由平台处理）。解析步骤：

1. 判断是否以某个指令开头；
2. 把指令后面的部分当作参数；
3. 参数为空时给默认值或提示。

原文的写法：

```python
content = message.content
if "/天气" in content:
    split = content.split("/天气 ")
    weather = await get_weather(split[1])
    await send_weather_ark_message(weather, message.channel_id, message.id)
```

> **这段写法有三个隐患**：
> 1. `in` 是子串匹配，`"今天/天气怎么样"` 也会命中；
> 2. 用户只发 `/天气`（没有空格和城市）时，`split("/天气 ")` 只有一段，`split[1]` 会抛 `IndexError`；
> 3. 多个空格时，`split` 结果里会出现空字符串。
>
> 更稳的写法：
>
> ```python
> content = (message.content or "").strip()
> if content.startswith("/天气"):
>     city = content[len("/天气"):].strip()
>     if not city:
>         await message.reply(content="用法：/天气 城市名，例如 /天气 成都")
>         return
>     weather = await get_weather(city)
> ```
>
> `startswith` 保证是"开头匹配"，切片取参数，`strip` 去掉多余空格，空参数单独处理。这三点是所有指令解析都应该做的。

### 8.2 Ark 是什么

Ark 是平台预置的**结构化卡片消息**。你不需要自己画布局，只需选一个模板 ID，然后往模板的占位符里填数据。

原文用的是 **23 号模板**（一个列表样式的卡片），结构如下：

```
┌─────────────────────────────┐
│  #DESC#    （描述/标题）      │
│  #PROMPT#  （提示语）         │
│  #LIST#    （若干行数据）     │
│    · 深圳 晴 2022-07-31 星期日│
│    · 当日温度区间：28℃~35℃    │
│    · 当前温度：32℃            │
│    · 当前湿度：60%            │
└─────────────────────────────┘
```

> **为什么用 Ark 而不是纯文本？**
> 纯文本只能一行行堆，信息密度低、不好看。Ark 由平台渲染，样式统一，还能带图标、按钮、跳转链接。适合"天气卡片""比赛通知""榜单"这类结构化信息。
>
> **代价**：Ark 的布局由模板决定，你不能随意改。想自定义样式只能用 Markdown 消息（见第 17 章）。

### 8.3 构造 Ark 消息

原文先构造列表项：

```python
async def _create_ark_obj_list(weather_dict) -> List[MessageArkObj]:
    obj_list = [
        MessageArkObj(obj_kv=[
            MessageArkObjKv(
                key="desc",
                value=weather_dict["result"]["citynm"] + " "
                + weather_dict["result"]["weather"] + " "
                + weather_dict["result"]["days"] + " "
                + weather_dict["result"]["week"],
            )
        ]),
        MessageArkObj(obj_kv=[
            MessageArkObjKv(key="desc", value="当日温度区间：" + weather_dict["result"]["temperature"])
        ]),
        MessageArkObj(obj_kv=[
            MessageArkObjKv(key="desc", value="当前温度：" + weather_dict["result"]["temperature_curr"])
        ]),
        MessageArkObj(obj_kv=[
            MessageArkObjKv(key="desc", value="当前湿度：" + weather_dict["result"]["humidity"])
        ]),
    ]
    return obj_list
```

再构造完整的 Ark：

```python
async def send_weather_ark_message(weather_dict, channel_id, message_id):
    ark = MessageArk()
    ark.template_id = 23
    ark.kv = [
        MessageArkKv(key="#DESC#", value="描述"),
        MessageArkKv(key="#PROMPT#", value="提示消息"),
        MessageArkKv(key="#LIST#", obj=await _create_ark_obj_list(weather_dict)),
    ]
    send = qqbot.MessageSendRequest(content="", ark=ark, msg_id=message_id)
    msg_api = qqbot.AsyncMessageAPI(t_token, False)
    await msg_api.post_message(channel_id, send)
```

拆解字段：

| 字段 | 含义 |
|------|------|
| `template_id` | 模板编号，23 是平台预置的列表模板 |
| `kv` | 模板占位符的填充值列表 |
| `key="#DESC#"` | 模板里"描述"位置的占位符 |
| `key="#PROMPT#"` | 模板里"提示"位置的占位符 |
| `key="#LIST#"` | 模板里"列表"位置的占位符 |
| `obj` | 列表项的数组，每项是一行 |
| `obj_kv` | 一行里的键值对，`key="desc"` 表示这行显示描述文本 |
| `content=""` | Ark 消息的文本内容留空，显示以 Ark 为准 |

> **为什么 `content` 要传空字符串？**
> Ark 消息的展示完全由模板决定，`content` 只是兼容字段。如果填了文本，有些客户端会在卡片下方额外显示一行纯文本，看起来重复。留空最干净。

> **`#DESC#` 这种带井号的 key 是什么？**
> 它是模板作者定义的"变量名"。平台渲染时，会把 `kv` 里同名的 `value` 填进去。你可以把它理解成函数参数：模板是函数体，`kv` 是实参。
>
> 不同模板的变量名不同，用之前必须查该模板的文档。原文的 23 号模板只接受 `#DESC#`、`#PROMPT#`、`#LIST#` 三个。

### 8.4 用 botpy 写 Ark（更简洁）

新版 botpy 的接口直接收字典，不需要构造一堆类：

```python
async def build_weather_ark(weather: dict) -> dict:
    r = weather["result"]
    return {
        "template_id": 23,
        "kv": [
            {"key": "#DESC#", "value": "天气播报"},
            {"key": "#PROMPT#", "value": "数据来自公开天气接口"},
            {
                "key": "#LIST#",
                "obj": [
                    {"obj_kv": [{"key": "desc", "value": f"{r['citynm']} {r['weather']} {r['days']} {r['week']}"}]},
                    {"obj_kv": [{"key": "desc", "value": f"当日温度区间：{r['temperature']}"}]},
                    {"obj_kv": [{"key": "desc", "value": f"当前温度：{r['temperature_curr']}"}]},
                    {"obj_kv": [{"key": "desc", "value": f"当前湿度：{r['humidity']}"}]},
                ],
            },
        ],
    }


# 在回调里
ark = await build_weather_ark(weather)
await message.reply(ark=ark)
```

> **为什么可以直接用字典？**
> 因为 botpy 的类型定义（`Ark`、`ArkKv`、`ArkObj`）本质是 `TypedDict`——运行时就是普通字典，类型标注只给 IDE 和静态检查看。所以传字典和传 `MessageArk()` 对象效果一样，字典更轻。

### 8.5 Embed 是什么

Embed 是另一种结构化消息，比 Ark 简单，适合"标题 + 缩略图 + 几行字段"的卡片。

原文的私信天气用的就是 Embed：

```python
async def send_weather_embed_direct_message(weather_dict, guild_id, user_id):
    embed = MessageEmbed()
    embed.title = weather_dict["result"]["citynm"] + " " + weather_dict["result"]["weather"]
    embed.prompt = "天气消息推送"

    thumbnail = MessageEmbedThumbnail()
    thumbnail.url = weather_dict["result"]["weather_icon"]
    embed.thumbnail = thumbnail

    embed.fields = [
        MessageEmbedField(name="当日温度区间：" + weather_dict["result"]["temperature"]),
        MessageEmbedField(name="当前温度：" + weather_dict["result"]["temperature_curr"]),
        MessageEmbedField(name="最高温度：" + weather_dict["result"]["temp_high"]),
        MessageEmbedField(name="最低温度：" + weather_dict["result"]["temp_low"]),
        MessageEmbedField(name="当前湿度：" + weather_dict["result"]["humidity"]),
    ]

    send = qqbot.MessageSendRequest(embed=embed, content="")
    dms_api = qqbot.AsyncDmsAPI(t_token, False)
    direct_message_guild = await dms_api.create_direct_message(
        CreateDirectMessageRequest(guild_id, user_id)
    )
    await dms_api.post_direct_message(direct_message_guild.guild_id, send)
```

字段含义：

| 字段 | 含义 |
|------|------|
| `title` | 卡片标题 |
| `prompt` | 卡片上方的提示语 |
| `thumbnail.url` | 右侧缩略图 |
| `fields` | 若干行文本，每行一个 `name` |

> **Ark 和 Embed 怎么选？**
>
> | | Ark | Embed |
> |--|-----|-------|
> | 布局 | 平台模板，固定 | 简单卡片，自由组合字段 |
> | 复杂度 | 高（要记模板变量） | 低 |
> | 适用 | 列表、榜单、带按钮的卡片 | 标题 + 图 + 几行信息 |
> | 私信支持 | 部分模板支持 | 支持 |
>
> 初学建议先用 Embed，够用且不容易出错；需要更丰富的展示再研究 Ark 模板。

### 8.6 私信：为什么要先"创建会话"

原文的私信流程分两步：

```python
# 第一步：创建（或获取）与用户的私信会话
direct_message_guild = await dms_api.create_direct_message(
    CreateDirectMessageRequest(guild_id, user_id)
)

# 第二步：往这个会话里发消息
await dms_api.post_direct_message(direct_message_guild.guild_id, send)
```

> **为什么不能直接发？**
> 私信会话在平台上有独立的 ID（`direct_message_guild.guild_id`），它不是普通的子频道。第一次给某个用户发私信时，这个会话可能还不存在，必须先调 `create_dms` 创建；平台返回会话 ID 后，才能往里面发消息。
>
> 可以把它理解成"先加好友，再聊天"：`create_dms` 是建立会话，`post_dms` 是发消息。

新版 botpy 的写法：

```python
# 1. 创建私信会话
dms = await self.api.create_dms(guild_id=message.guild_id, user_id=message.author.id)

# 2. 发送
await self.api.post_dms(
    guild_id=dms["guild_id"],
    embed=embed,
    msg_id=message.id,   # 作为被动回复；不传则是主动私信
)
```

> **`guild_id` 传哪个？**
> 传"来源频道"的 ID，也就是用户发消息的那个频道（`message.guild_id`）。平台需要知道你是从哪个频道发起私信的，用于权限校验和风控。

> **为什么原文说"私信可能需等待几分钟"？**
> 私信不是即时消息通道，平台可能对主动发起的私信做异步处理或安全审核，尤其是包含链接、图片时。具体延迟以平台实际行为为准。
>
> 如果平台支持带 `msg_id` 的被动私信，优先用被动方式；否则就要接受主动私信可能延迟，并注意频控。

### 8.7 把指令串起来

原文在 `_message_handler` 里用 `if/elif` 分流：

```python
content = message.content

if "/天气" in content:
    split = content.split("/天气 ")
    weather = await get_weather(split[1])
    await send_weather_ark_message(weather, message.channel_id, message.id)

elif "/私信天气" in content:
    split = content.split("/私信天气 ")
    weather = await get_weather(split[1])
    await send_weather_embed_direct_message(weather, message.guild_id, message.author.id)
```

> **注意顺序**：`"/天气" in "/私信天气 成都"` 也是 `True`！所以如果先判断 `/天气`，`/私信天气` 会被错误地当成 `/天气` 处理。
>
> 修正方法：把更长的指令放前面，或者用 `startswith` 精确匹配：
>
> ```python
> if content.startswith("/私信天气"):
>     ...
> elif content.startswith("/天气"):
>     ...
> ```
>
> 这也是第 15 章要用"指令装饰器"的原因——集中管理指令，避免这种顺序陷阱。

---

## 第 9 章 小程序（简述）

原文第 9 课讲的是：用户在频道里反复 @ 机器人查天气很麻烦，可以做一个 QQ 小程序，把查询界面做成按钮和表单。

原文的结论是：**机器人打开小程序只需要在平台配置，不需要额外写代码**。但原文没有给出具体的配置字段和截图细节，所以这里只说明思路：

1. 在 QQ 小程序平台创建一个小程序；
2. 在小程序里调用你的后端接口（可以复用机器人的天气接口）；
3. 在机器人后台配置小程序入口；
4. 用户在频道里点击入口打开小程序。

> **什么时候需要小程序？**
> 当交互从"一问一答"变成"多步表单/列表浏览"时。比如查比赛、报名、选课，用聊天指令会很啰嗦，图形界面更合适。
>
> 学习阶段可以先跳过这一章，把精力放在消息收发和指令处理上。

---

## 第 10 章 用 PyCharm 通过 SSH 在服务器上开发

这一章对应原文第 10 课。

### 10.1 为什么要远程开发

机器人需要 7×24 小时在线。如果你在自己电脑上跑：

- 电脑休眠/关机，机器人就掉线；
- 网络切换（Wi-Fi → 热点），WebSocket 断开；
- 家里断电，服务停止。

所以正式一点的做法是：把代码放到云服务器上，让服务器一直跑。PyCharm 的 SSH 远程开发可以让你"在本地编辑，在服务器运行"。

### 10.2 准备服务器信息

你需要：

| 信息 | 说明 |
|------|------|
| 服务器 IP | 公网地址 |
| SSH 端口 | 默认 22 |
| 用户名 | 通常是 `root` 或 `ubuntu` |
| 密码或密钥 | 密钥更安全 |

原文提到从宝塔面板下载 `id_rsa` 私钥文件。

> **`id_rsa` 是什么？**
> SSH 密钥对里的私钥。公钥放在服务器上，私钥留在你电脑上。连接时服务器用公钥验证"你手里的私钥"是否匹配，匹配就放行。
>
> **私钥绝对不能泄露**，拿到它的人可以登录你的服务器。不要提交到 Git，不要发到群里。文件权限建议设为 `600`（只有自己能读）：
>
> ```bash
> chmod 600 ~/.ssh/id_rsa
> ```

### 10.3 配置 PyCharm

大致步骤（不同版本菜单名略有差异）：

1. `File` → `Remote Development` → `SSH`，或 `Settings` → `Project` → `Python Interpreter` → `Add Interpreter` → `On SSH`；
2. 填 Host、Port、Username，选择密钥或密码；
3. 选择服务器上的项目目录；
4. 选择远程 Python 解释器（服务器上的 `.venv/bin/python`）；
5. 等待 PyCharm 上传辅助程序、建立索引（首次可能较慢）。

> **为什么首次连接要等很久？**
> PyCharm 需要在服务器上安装一个"远程开发后端"，还要扫描项目文件建索引。网络慢的话十几分钟都正常。

### 10.4 测试代码

原文的测试代码：

```python
import botpy
from botpy.types.message import Message


class MyClient(botpy.Client):
    async def on_at_message_create(self, message: Message):
        await self.api.post_message(
            channel_id=message.channel_id,
            content="content",
        )


intents = botpy.Intents(public_guild_messages=True)
client = MyClient(intents=intents)
client.run(appid="你的AppID", secret="你的AppSecret")
```

> **版本差异**：原文写的是 `from botpy.types.message import Message`，而当前 botpy 里事件回调用的 `Message` 类在 `botpy.message`。`botpy.types.message.Message` 是 TypedDict 类型，用来标注数据结构，不是回调参数的实际类型。用错了通常不影响运行（Python 不强制类型），但 IDE 会提示类型不匹配。建议统一用：
>
> ```python
> from botpy.message import Message
> ```

### 10.5 `TypedDict` 报错

原文提到一个典型报错：

```
ImportError: cannot import name 'TypedDict' from 'typing'
```

> **原因**：`TypedDict` 是 Python 3.8 才加入标准库的。服务器上的 Python 是 3.7 或更早，而 botpy 用到了它，所以导入失败。
>
> **解决**：
> 1. 升级服务器 Python 到 3.8+（推荐 3.10）；
> 2. 或者升级 botpy 到兼容你当前 Python 的版本：
>
> ```bash
> pip install --upgrade qq-botpy
> ```
>
> 3. 如果服务器是多 Python 环境，确认 `python3 --version` 和 PyCharm 选的解释器是同一个。

### 10.6 让程序在服务器上常驻

PyCharm 里点运行，关掉 IDE 程序就停了。正式部署要用别的方式。

#### 方式一：`nohup`（最简单）

```bash
nohup python main.py > bot.log 2>&1 &
```

- `nohup`：忽略挂断信号，退出 SSH 后程序继续跑；
- `> bot.log 2>&1`：标准输出和错误都写入日志；
- `&`：放到后台。

查看日志：

```bash
tail -f bot.log
```

停止：

```bash
ps aux | grep main.py
kill <PID>
```

#### 方式二：`systemd`（推荐，开机自启）

创建 `/etc/systemd/system/myqqbot.service`：

```ini
[Unit]
Description=My QQ Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/my_qq_bot
ExecStart=/home/ubuntu/my_qq_bot/.venv/bin/python main.py
Restart=always
RestartSec=5
StandardOutput=append:/home/ubuntu/my_qq_bot/bot.log
StandardError=append:/home/ubuntu/my_qq_bot/bot.log

[Install]
WantedBy=multi-user.target
```

启用：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now myqqbot
sudo systemctl status myqqbot
```

> **`Restart=always` 的意义**：程序崩溃或服务器重启后，systemd 会自动把它拉起来。这是"无人值守"的关键。

#### 方式三：`screen` / `tmux`

```bash
screen -S bot
python main.py
# 按 Ctrl+A 再按 D 退出会话
screen -r bot   # 重新进入
```

适合临时调试，不如 systemd 正规。

### 10.7 安全清单

- [ ] 服务器防火墙只开放必要端口（22、80、443）
- [ ] 不用 root 跑机器人，建一个普通用户
- [ ] 密钥文件权限 600
- [ ] 配置文件权限 600
- [ ] 定期 `pip install --upgrade` 更新依赖（先在测试环境验证）
- [ ] 日志里不要打印 token/secret

---

## 第 11 章 用 YAML 管理配置

这一章对应原文第 11 课。

### 11.1 为什么用 YAML

| 格式 | 优点 | 缺点 |
|------|------|------|
| 硬编码 | 简单 | 改配置要改代码，密钥易泄露 |
| JSON | 通用 | 不能写注释，括号多，手写易错 |
| YAML | 可读性好、支持注释、层级清晰 | 缩进敏感，格式要求严 |
| 环境变量 | 适合部署、天然不进代码库 | 层级多时不好管理 |

机器人项目通常用 **YAML 放配置 + 环境变量放密钥** 的组合。

### 11.2 安装

```bash
pip install pyyaml
```

导入：

```python
import yaml
```

### 11.3 读取：三种写法与两个坑

#### 写法一：原文的旧写法（不推荐）

```python
file = open("test.yaml", "r", encoding="utf-8")
data = yaml.load(file)
print(data)
```

> **坑 1：PyYAML 6 会直接报错**
>
> ```
> TypeError: load() missing 1 required positional argument: 'Loader'
> ```
>
> 因为从 PyYAML 5.1 开始，`yaml.load` 不再提供默认的 Loader，必须显式指定。这是为了防止开发者无意中加载不安全的 YAML。

#### 写法二：指定 `FullLoader`（原文的修正版）

```python
file = open("test.yaml", "r", encoding="utf-8")
file_data = file.read()
file.close()
data = yaml.load(file_data, Loader=yaml.FullLoader)
```

> **`Loader` 是什么？**
> YAML 文件里的内容可以被解析成 Python 对象。不同的 Loader 支持的对象范围不同：
>
> | Loader | 能力 | 安全性 |
> |--------|------|--------|
> | `SafeLoader` | 只支持基础类型（dict/list/str/int/float/bool/null） | 安全 |
> | `FullLoader` | 额外支持一些 Python 标签 | 对不可信输入不安全 |
> | `UnsafeLoader` | 几乎任意 Python 对象 | 危险 |
>
> 配置文件里只有字符串和数字，用 `SafeLoader` 就够了。`FullLoader` 能做的事更多，但也意味着如果 YAML 来自不可信来源，可能被构造出恶意对象。

#### 写法三：推荐写法

```python
import yaml

with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

print(config["appid"])
```

优点：

- `with` 自动关闭文件，即使中途异常也不会泄漏文件句柄；
- `safe_load` 只解析基础类型，安全；
- 一行搞定，不用手动 `read()` + `close()`。

### 11.4 `load_all`：一个文件多段配置

YAML 用 `---` 分隔多个文档：

```yaml
appid: "123"
secret: "abc"
---
appid: "456"
secret: "def"
```

读取：

```python
with open("config.yaml", encoding="utf-8") as f:
    docs = yaml.safe_load_all(f)   # 返回生成器
    for doc in docs:
        print(doc)
```

> **为什么返回生成器而不是列表？**
> 因为文件可能很大，生成器一次只解析一段，省内存。需要列表就 `list(docs)`。
>
> **`load_all` 的典型用途**：一个文件里放多个环境的配置（开发/测试/生产），启动时按环境变量选其中一段。

### 11.5 写入：`dump` 参数逐个解释

```python
data = {"name": "唤醒手腕", "age": 20, "sex": "男"}

with open("test.yaml", "w", encoding="utf-8") as f:
    yaml.dump(
        data,
        f,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )
```

| 参数 | 作用 | 不写会怎样 |
|------|------|-----------|
| `default_flow_style=False` | 用块状格式（每行一个键） | 可能输出 `{age: 20, name: ...}` 这种一行流式格式，难读 |
| `allow_unicode=True` | 中文原样输出 | 中文被转成 `"\u5524\u9192..."` 转义序列 |
| `sort_keys=False` | 保持字典插入顺序 | 键按字母排序，和源码顺序不一致 |
| `encoding="utf-8"` | 指定编码 | 只对二进制流有意义；文本模式用 `open(encoding=...)` 即可 |

> **`encoding` 参数要不要写？**
> 原文的 `yaml.dump(..., encoding='utf-8')` 在 PyYAML 6 + 文本模式下能跑，但容易让人误解。更清晰的做法是：**打开文件时指定编码，`dump` 里不再传 encoding**。
>
> ```python
> with open("test.yaml", "w", encoding="utf-8") as f:
>     yaml.dump(data, f, allow_unicode=True, sort_keys=False)
> ```

### 11.6 在本项目中的建议

```python
import os
import yaml

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(BASE_DIR, "config.yaml"), encoding="utf-8") as f:
    config = yaml.safe_load(f)

APPID = os.getenv("BOT_APPID", config["appid"])
SECRET = os.getenv("BOT_SECRET", config["secret"])
```

> **为什么优先读环境变量？**
> 部署到服务器/容器时，环境变量可以在不修改文件的情况下注入密钥，避免配置文件被误提交。`os.getenv("BOT_APPID", 默认值)` 的第二个参数是"没设置环境变量时的回退值"，本地开发用配置文件，线上用环境变量，一套代码两种环境。

---

## 第 12 章 User 对象：机器人自己是谁

这一章对应原文第 12 课。

### 12.1 获取机器人自己的信息

```python
class MyClient(botpy.Client):
    async def on_at_message_create(self, message: Message):
        user = await self.api.me()
        botpy.logger.info(user)
```

`me()` 调用的是 `GET /users/@me`，返回当前机器人账号的信息。

日志里能看到：

| 字段 | 含义 |
|------|------|
| `id` | 机器人在当前应用下的用户 ID |
| `username` | 机器人昵称 |
| `avatar` | 头像 URL |
| `union_openid` | 关联应用的统一 openid（不一定有） |
| `union_user_account` | 关联账号（不一定有） |

### 12.2 这些 ID 不是 QQ 号

原文特别提醒：**ID 类数据仅在机器人场景流通，不是真实 QQ 号**。

> **为什么平台不给真实 QQ 号？**
> 隐私保护。如果机器人能拿到用户的真实 QQ 号，就可以跨应用追踪用户、做用户画像，甚至骚扰。平台给每个机器人分配一套"匿名 ID"，同一个用户在不同机器人下看到的 ID 不同，机器人之间无法直接关联。
>
> **实际影响**：
> - 你不能用这个 ID 去搜索用户、加好友；
> - 不能用它拼接 QQ 空间链接；
> - 只能用它在本机器人内做标识（比如记录"谁登记了活动"）。
>
> 这是设计上的隐私边界，不是 bug。

### 12.3 `union_openid` 是什么

> `union_openid` 是"联合 openid"：当你的多个应用（小程序、公众号、机器人）属于同一个开放平台主体时，平台可以给同一个用户分配相同的 `union_openid`，让你的应用之间识别出"这是同一个人"。
>
> 但它**不是所有场景都会返回**。原文说"仅在单独拉取 member 信息时提供"——也就是说，消息事件里的用户对象可能没有这个字段，你需要额外调用成员详情接口才能拿到。写代码时要做"字段可能不存在"的判断。

### 12.4 什么时候用 `me()`

- 启动时打印机器人信息，确认登录的是正确的机器人（避免测试号/正式号搞混）；
- 需要机器人头像、昵称做展示时；
- 排查"为什么消息发不出去"时，确认 token 对应的账号。

> **不要在每条消息里都调用 `me()`**：这是一个额外的 HTTP 请求，浪费配额和延迟。启动时调一次，缓存起来即可。

---

## 第 13 章 发消息与回复的全部姿势

这一章对应原文第 13 课，是日常开发用得最多的一章。

### 13.1 `reply` 与 `post_message`

| 方式 | 写法 | 适用 |
|------|------|------|
| 快捷回复 | `await message.reply(content="...")` | 回复当前消息，最常用 |
| 通用发送 | `await self.api.post_message(channel_id=..., content=..., msg_id=...)` | 需要指定频道、引用、发到别处 |

`message.reply` 的源码只有一行：

```python
async def reply(self, **kwargs):
    return await self._api.post_message(
        channel_id=self.channel_id,
        msg_id=self.id,
        **kwargs,
    )
```

> **看懂这一行，就理解了被动回复**：它自动填入 `channel_id`（发到哪）和 `msg_id`（回复哪条），剩下的参数原样透传。所以 `reply(content=...)`、`reply(image=...)`、`reply(embed=...)`、`reply(ark=...)` 都可用。

### 13.2 发图片

```python
await message.reply(image=message.author.avatar)
```

`image` 是一个 **URL 字符串**，平台会去下载并展示。

> **为什么传 URL 而不是文件？**
> 平台需要先把图片上传到自己的 CDN 才能展示。传 URL 时，平台替你下载；传本地文件则要用 `file_image` 参数：
>
> ```python
> await message.reply(file_image="local.png")
> ```
>
> `file_image` 接受本地路径、`bytes` 或文件对象。适合发送生成的图片（比如统计图表）。
>
> **注意**：图片 URL 必须是公网可访问的。`http://localhost/...` 或内网地址平台访问不到，会发送失败。

### 13.3 发 emoji

```python
await message.reply(content="你在干嘛呀？<emoji:4>")
```

`<emoji:4>` 是平台的表情语法，数字是表情 ID。

> **为什么不用 Unicode emoji（比如 😀）？**
> 频道消息支持平台内置表情，显示效果统一，而且可以和文本混排。Unicode emoji 也能显示，但不同客户端字体不同，效果不可控。
>
> **重要限制**：这种内嵌格式**只在 `content` 里生效**。放在 `embed` 或 `ark` 的字段里会被当成普通文本，原样显示 `<emoji:4>`。

### 13.4 引用消息

```python
from botpy.types.message import Reference

message_reference = Reference(message_id=message.id)
await self.api.post_message(
    channel_id=message.channel_id,
    content="<emoji:4>这是一条引用消息",
    msg_id=message.id,
    message_reference=message_reference,
)
```

效果是消息上方显示"回复了 XXX 的消息"。

> **`msg_id` 和 `message_reference` 有什么区别？**
> - `msg_id` 告诉平台"这是一条被动回复"，用于通过风控和配额检查；
> - `message_reference` 告诉客户端"在界面上显示引用关系"。
>
> 两者可以同时传，但作用不同。只传 `msg_id` 时，消息是回复，但不一定显示引用条；只传 `message_reference` 时，界面有引用，但可能被当成主动消息。

### 13.5 `event_id` 与 `id`

| 字段 | 来源 | 用途 |
|------|------|------|
| `message.id` | 消息本身 | 被动回复的 `msg_id` |
| `message.event_id` | 这次事件 | 被动回复的 `event_id`（部分场景） |

原文第 16 课用了 `msg_id=message.event_id`，这在某些版本/场景下可能可用，但更稳妥的是用 `message.id`。

> **为什么会有两个 ID？**
> 一条消息可能触发多个事件（创建、更新、删除），每个事件有独立 `event_id`；而消息只有一个 `id`。回复消息时，平台要的是"消息 ID"，所以优先用 `message.id`。
>
> 如果文档明确说某个接口要 `event_id`（比如音频事件），再用 `event_id`。

### 13.6 消息有效期

botpy 文档写明：**被动回复消息有效期为 5 分钟**。

> **这意味着什么？**
> 用户 10:00 发消息，你 10:06 才回复，平台会拒绝。所以：
> - 回调里不要做长时间任务（比如调用一个要 30 秒的模型）；
> - 如果任务确实很慢，先回一句"正在处理"，处理完再用主动消息或私信告知（注意主动配额）；
> - 或者把结果存起来，等用户下次查询时再展示。

### 13.7 URL 需要配置

原文提到"消息中的 URL 需配置"。

> **为什么？**
> 平台出于安全考虑，通常会对机器人可发送的链接/图片域名做限制（具体是白名单、安全检查还是别的机制，以平台当前配置项为准）。原文只写了"需要配置"，没有展开。
>
> 如果你发的图片/链接显示不出来，先检查：
> 1. URL 是否公网可访问；
> 2. 域名是否需要在平台后台登记；
> 3. URL 是否是 HTTPS（部分客户端要求）。

### 13.8 常用发送参数速查

```python
await message.reply(content="文本")
await message.reply(image="https://example.com/a.png")
await message.reply(file_image="local.png")
await message.reply(embed=embed_dict)
await message.reply(ark=ark_dict)
await message.reply(markdown=markdown_dict)
await message.reply(content="文本", image="https://...")
```

> **能同时传 content 和 embed 吗？**
> 可以，但展示效果取决于客户端：通常文本在上、卡片在下。建议不要堆太多类型，一条消息只表达一个重点。

---

## 第 14 章 语音房上麦

这一章对应原文第 14 课。

### 14.1 音频状态

原文给出的状态：

| 状态 | 数值 | 含义 |
|------|------|------|
| `START` | 0 | 开始播放 |
| `PAUSE` | 1 | 暂停 |
| `RESUME` | 2 | 继续 |
| `STOP` | 3 | 停止 |

在 botpy 的 `audio_control` 里，`status` 用的是**整数**：`0` 开始、`1` 暂停、`2` 继续、`3` 停止。不要写成字符串 `"START"`——当前 SDK 的类型定义是 `Literal[0, 1, 2, 3]`，传字符串可能被平台拒绝。

### 14.2 代码

```python
import botpy
from botpy.audio import Audio
from botpy.message import Message


class MyClient(botpy.Client):
    async def on_audio_start(self, audio: Audio):
        audio_control = {
            "audio_url": "https://example.com/song.mp3",
            "text": "简单爱-周杰伦",
            "status": 0,   # 0 = START，开始播放
        }
        await self.api.update_audio(audio.channel_id, audio_control=audio_control)


intents = botpy.Intents(audio_action=True)
client = MyClient(intents=intents)
client.run(appid="你的AppID", secret="你的AppSecret")
```

### 14.3 触发条件与权限

- 必须先订阅 `audio_action=True`，否则收不到 `on_audio_start`。
- `audio_url` 必须是公网可访问的音频地址。
- 机器人需要是"音频类机器人"，并在平台申请了音频接口权限。

原文提到报错：

```
401 / 11253 check app privilege not pass
```

> **这个错误什么意思？**
> `check app privilege not pass` 直译是"应用权限检查未通过"。说明你的机器人类型不支持音频接口，或者没有向平台申请开通。
>
> 解决：去机器人后台查看接口权限，提交申请。这不是代码问题，改代码没用。
>
> 类似地，很多高级接口（私信、按钮、论坛）都需要单独申请权限。遇到 403/11253 先查权限，不要盲目改代码。

---

## 第 15 章 用装饰器注册指令

这一章对应原文第 15 课。

### 15.1 先看没有装饰器时的痛点

```python
async def on_at_message_create(self, message):
    content = message.content
    if content.startswith("/天气"):
        ...
    elif content.startswith("/私信天气"):
        ...
    elif content.startswith("/比赛"):
        ...
    elif content.startswith("/提醒"):
        ...
```

指令一多，这个函数会变成几百行的 `if/elif` 迷宫，而且顺序敏感（`/天气` 会误匹配 `/私信天气`）。

### 15.2 用 `Commands` 装饰器

```python
from botpy import BotAPI
from botpy.ext.command_util import Commands
from botpy.message import Message


@Commands("你好", "hello")
async def hello(api: BotAPI, message: Message, params=None):
    await message.reply(content=params or "你好呀")
    return True


@Commands("晚安")
async def good_night(api: BotAPI, message: Message, params=None):
    await message.reply(content="晚安，做个好梦")
    return True
```

然后在客户端里遍历：

```python
class MyClient(botpy.Client):
    async def on_at_message_create(self, message: Message):
        handlers = [hello, good_night]
        for handler in handlers:
            if await handler(api=self.api, message=message):
                return   # 已处理，不再往下传
```

### 15.3 装饰器到底做了什么

`@Commands("你好", "hello")` 是语法糖，等价于：

```python
hello = Commands("你好", "hello")(hello)
```

`Commands` 类的 `__call__` 返回一个包装函数，逻辑是：

```python
async def decorated(*args, **kwargs):
    message = kwargs["message"]
    for command in self.commands:
        if command in message.content:
            params = message.content.split(command)[1].strip()
            kwargs["params"] = params
            return await func(*args, **kwargs)
    return False
```

拆开看：

1. **匹配**：遍历你注册的所有别名（`"你好"`、`"hello"`），看消息内容里有没有；
2. **取参**：用 `split(command)[1]` 取指令后面的部分，`strip()` 去空格；
3. **注入**：把参数塞进 `kwargs["params"]`，所以你的函数签名要有 `params=None`；
4. **调用**：命中就执行原函数，并返回它的返回值；
5. **未命中**：返回 `False`，让下一个 handler 尝试。

> **这就是为什么函数必须接收 `api`、`message`、`params` 三个参数**：包装函数会把它们传进来。`params` 给默认值 `None`，是为了兼容"只发指令不带参数"的情况。

> **版本差异**：原文写的是 `@Commands(name=("你好", "hello"))`。当前 `qq-botpy 1.2.1` 的 `Commands.__init__` 签名是 `def __init__(self, *args)`，只接受位置参数，写成 `name=(...)` 会报 `TypeError`。正确写法是 `@Commands("你好", "hello")`。

### 15.4 返回 `True` 的意义

```python
return True
```

遍历 handler 时：

```python
for handler in handlers:
    if await handler(...):
        return
```

> 返回 `True` 表示"这条消息我已经处理了"，调用方就不再尝试后面的 handler。这样可以避免"你好"和"你好呀"两个指令同时回复。
>
> 如果某个 handler 只是"观察"消息、不打算独占，就返回 `False`（或不返回），让后面的继续处理。

### 15.5 子串匹配的坑

`Commands` 内部用的是 `if command in message.content`，不是 `startswith`。

> **这会导致**：
> - 用户说"今天天气不错"，如果注册了 `"天气"`，也会命中；
> - 用户说"我说你好了吗"，注册 `"你好"` 也会命中；
> - `params` 取的是**第一次出现**指令之后的内容，可能不是你想要的。
>
> **缓解办法**：
> 1. 指令加前缀，比如 `/天气`，降低误命中概率；
> 2. 在 handler 里再校验一次 `message.content.startswith("/天气")`；
> 3. 自己写一个更严格的装饰器（用 `startswith` + 正则）。

### 15.6 更完整的注册方式

```python
COMMANDS = [hello, good_night]


class MyClient(botpy.Client):
    async def on_at_message_create(self, message: Message):
        for handler in COMMANDS:
            handled = await handler(api=self.api, message=message)
            if handled:
                return
        await message.reply(content="没听懂，试试 /你好 或 /晚安")
```

> **为什么把 handler 放在模块级列表里？**
> 这样新增指令只需写一个函数并加进列表，不用改客户端类。这叫"开闭原则"——对扩展开放，对修改关闭。

---

## 第 16 章 频道对象 guild

这一章对应原文第 16 课。

### 16.1 三个层级

```
Guild（频道/服务器）
  └── Channel（子频道：文字、语音、公告...）
        └── Message（消息）
```

对应到字段：

| 字段 | 层级 | 来源 |
|------|------|------|
| `guild_id` | 频道 | `message.guild_id` |
| `channel_id` | 子频道 | `message.channel_id` |
| `message.id` | 消息 | `message.id` |

> **为什么消息里两个 ID 都有？**
> 因为一条消息既属于某个子频道，也属于该子频道所在的频道。很多接口需要 `guild_id`（比如禁言、获取成员），很多需要 `channel_id`（比如发消息）。

### 16.2 获取频道信息

```python
class MyClient(botpy.Client):
    async def on_at_message_create(self, message: Message):
        guild = await self.api.get_guild(guild_id=message.guild_id)
        await self.api.post_message(
            channel_id=message.channel_id,
            content=str(guild),
            msg_id=message.id,
        )
```

> 原文这里写的是 `msg_id=message.event_id`。两者在部分场景都能通过，但被动回复的本质是"回复某条消息"，用 `message.id` 语义更准确，也更不容易因版本差异失效。

`get_guild` 返回一个字典，常用字段：

| 字段 | 含义 |
|------|------|
| `id` | 频道 ID |
| `name` | 频道名称 |
| `icon` | 频道头像 |
| `owner_id` | 频道主 ID |
| `member_count` | 成员数 |

### 16.3 获取子频道列表

```python
channels = await self.api.get_channels(guild_id=message.guild_id)
await self.api.post_message(
    channel_id=message.channel_id,
    content=str(channels),
    msg_id=message.id,
)
```

每个子频道包含：

| 字段 | 含义 |
|------|------|
| `id` | 子频道 ID |
| `name` | 名称 |
| `type` | 类型（文字/语音/公告等） |
| `parent_id` | 所属分组 ID |

> **`str(guild)` 能直接发吗？**
> 能，但这是调试写法。字典的字符串形式很长，而且包含大量你不需要的字段。正式代码应该只取需要的字段：
>
> ```python
> text = f"频道：{guild['name']}，成员数：{guild['member_count']}"
> ```
>
> 另外注意：botpy 返回的是字典，用 `guild["name"]`；旧版 qq-bot 返回对象，用 `guild.name`。混用会报 `TypeError: 'dict' object has no attribute 'name'`。

### 16.4 Intents 可以同时开多个

原文提到：

```python
intents = botpy.Intents(audio_action=True, public_guild_messages=True)
```

> **为什么可以同时开？**
> 因为 Intents 是位掩码，多个开关就是多个二进制位同时为 1。平台会把你订阅的所有事件都推过来。
>
> 但不要无脑全开：每多订阅一类事件，就多一份网络流量和回调调度开销。只开你真正处理的。

---

## 第 17 章 发送按钮消息

这一章对应原文第 17 课。

### 17.1 三个前提

原文强调：

1. 操作人（机器人）在该子频道要有**发送消息**和**消息按钮组件**权限；
2. "keyboard 模板"和"自定义 keyboard"**只能二选一**；
3. **只有 markdown 消息支持消息按钮**。

> **为什么按钮只能配 markdown？**
> 按钮是 markdown 消息的一种交互扩展。普通文本消息的渲染模型里没有"按钮"这个概念，平台不会为它解析 `keyboard` 字段。
>
> 所以发按钮的正确组合是：`markdown=...` + `keyboard=...`，缺一不可。

### 17.2 方式一：markdown 模板

先在平台申请"消息按钮组件模板"，拿到模板 ID。原文示例：

```python
from botpy.types.message import MarkdownPayload, MessageMarkdownParams

params = [
    MessageMarkdownParams(key="title", values=["标题"]),
    MessageMarkdownParams(
        key="content",
        values=["为了成为一名合格的巫师，请务必阅读频道公告", "藏馆黑色魔法书"],
    ),
]
markdown = MarkdownPayload(template_id=65, params=params)
await self.api.post_message(channel_id, markdown=markdown, msg_id=msg_id)
```

> **`params` 是什么？**
> 模板里定义的变量。`key="title"` 对应模板里的标题占位符，`values` 是一个列表（因为同一个变量可能在多处使用，或者模板支持多值）。
>
> **版本差异**：当前 `qq-botpy 1.2.1` 的 `MarkdownPayload` 字段是 `custom_template_id`（字符串），不是 `template_id`（整数）。如果你用的是新版，写成：
>
> ```python
> markdown = {"custom_template_id": "65", "params": params}
> ```

### 17.3 方式二：直接写 markdown 内容

```python
markdown = MarkdownPayload(content="# 标题 \n## 简介很开心 \n内容")
await self.api.post_message(channel_id, markdown=markdown, msg_id=msg_id)
```

> **`content` 里支持哪些语法？**
> 频道 markdown 支持标题、加粗、列表、链接、代码块等常见语法，但不支持完整的 HTML。具体支持范围以官方文档为准。
>
> 这种方式不用申请模板，适合快速排版；但按钮必须用模板 ID，因为按钮的布局和回调需要在平台上定义。

### 17.4 加按钮

```python
await self.api.post_message(
    channel_id=message.channel_id,
    markdown={"content": "# 每日提醒\n今天的训练赛 19:00 开始"},
    keyboard={"id": "你的按钮模板ID"},
    msg_id=message.id,
)
```

> **`keyboard` 的两种形态**：
> - `{"id": "模板ID"}`：使用平台申请的按钮模板；
> - `{"content": {...}}`：自定义按钮布局（JSON 结构参考平台的 InlineKeyboard 文档）。
>
> 两者只能传一个。传了 `keyboard` 却没传 `markdown`，按钮不会显示——这是新手最常踩的坑。

### 17.5 按钮点击后会发生什么

用户点击按钮，平台会推送一个**互动事件**（interaction）。你需要：

1. 订阅 `interaction=True`；
2. 实现 `on_interaction_create(self, interaction)`；
3. 根据 `interaction.data` 里的按钮标识做相应处理。

> 这一部分原文没有展开。实际开发时，按钮的 `id`/`value` 要和你的业务逻辑对应起来，并且要注意"用户可能重复点击"，处理逻辑要幂等。

---

## 第 18 章 禁言

这一章对应原文第 18 课。

### 18.1 代码

```python
await self.api.mute_member(
    guild_id="频道ID",
    user_id="用户ID",
    mute_end_timestamp="结束时间戳（毫秒）",
    mute_seconds="禁言秒数",
)
```

### 18.2 参数解释

| 参数 | 类型 | 含义 |
|------|------|------|
| `guild_id` | str | 频道（服务器）ID |
| `user_id` | str | 被禁言的用户 ID |
| `mute_end_timestamp` | str | 禁言结束时间，Unix 毫秒时间戳 |
| `mute_seconds` | str | 禁言时长（秒） |

> **为什么是字符串不是数字？**
> 平台 API 的 ID 和时间戳都按字符串传输，避免 JavaScript 等语言的大整数精度问题。Python 里要用 `str()` 转换。
>
> **两个时间参数二选一**：文档写明"默认以 `mute_end_timestamp` 为准"。如果你两个都传，以结束时间戳为准。
>
> **时间戳是毫秒**：
>
> ```python
> import time
>
> # 禁言 10 分钟
> mute_end = str(int(time.time() * 1000) + 10 * 60 * 1000)
> await self.api.mute_member(
>     guild_id=message.guild_id,
>     user_id=message.author.id,
>     mute_end_timestamp=mute_end,
> )
> ```
>
> `time.time()` 返回秒（浮点数），乘 1000 转毫秒，再转 int 去掉小数。

### 18.3 权限要求

原文标题写的是"禁言（非管理员）"，意思是**不能禁言管理员**。文档补充：调用者（机器人）需要是频道管理员。

> **为什么机器人要管理员权限？**
> 禁言是管理操作，普通成员没有这个能力。机器人即使有 API，也需要在频道里被授予管理员身份，否则会返回 403。
>
> **注意**：不要给机器人过高的权限。只开它需要的（发消息、管理消息、禁言），遵循最小权限原则。

### 18.4 常见错误

| 错误 | 原因 |
|------|------|
| 403 / 无权限 | 机器人不是管理员，或目标用户是管理员 |
| 400 / 参数错误 | `guild_id` 和 `user_id` 不匹配，或时间戳格式不对 |
| 404 | 用户不在该频道 |

> **调试建议**：先用 `get_guild_member(guild_id, user_id)` 确认用户存在，再调禁言。禁言前最好回复一句"已禁言 XXX，理由：..."，让操作可追溯。

---

## 第 19 章 错误码与排查方法

这一章对应原文第 19 课。原文只给了一个链接，这里补充一套实用的排查方法。

### 19.1 官方错误码

```
https://bot.q.qq.com/wiki/develop/api/openapi/error/error.html
```

### 19.2 按 HTTP 状态码分类

| 状态码 | 含义 | 常见原因 |
|--------|------|----------|
| 400 | 请求参数错误 | 字段名拼错、类型不对、缺必填字段 |
| 401 | 未认证 | AppID/Secret 错误、access_token 过期 |
| 403 | 无权限 | 机器人不在频道、不是管理员、接口未申请 |
| 404 | 资源不存在 | ID 写错、消息已删除、频道已解散 |
| 429 | 请求过于频繁 | 触发限流，需要退避重试 |
| 5xx | 平台错误 | 服务端问题，稍后重试 |

### 19.3 业务错误码

平台还会返回业务错误码，比如原文提到的：

```
11253 check app privilege not pass
```

> **`check app privilege not pass` 怎么处理？**
> 这是"应用权限检查未通过"。依次检查：
> 1. 机器人类型是否支持该接口（音频、私信、按钮等有单独的类型要求）；
> 2. 是否在后台申请了对应权限；
> 3. 机器人是否在目标频道；
> 4. 是否缺少管理员身份。
>
> 这类错误改代码没用，必须去平台配置。

### 19.4 排查顺序

遇到问题按这个顺序，能解决 90% 的情况：

```
1. 看日志的完整报错（HTTP 状态码 + 业务码 + message）
        │
        ▼
2. 401/403？ → 检查 AppID/Secret、权限、机器人是否在频道
        │
        ▼
3. 400？ → 检查参数名、类型、必填项（对照官方文档）
        │
        ▼
4. 没报错但没反应？ → 检查 Intents、方法名、事件是否触发
        │
        ▼
5. 偶发失败？ → 检查限流、超时、网络
```

### 19.5 日志建议

```python
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
```

> **日志里要有什么？**
> - 时间戳（排查时序问题）；
> - 级别（INFO/WARNING/ERROR）；
> - 事件名和关键 ID（`guild_id`、`channel_id`、`message_id`）；
> - 异常堆栈（`logger.exception` 会自动带上）。
>
> **日志里不要有什么？** token、secret、完整用户隐私数据。日志可能被上传到日志平台，泄露风险很高。

---

## 第 20 章 事件监听全景

这一章对应原文第 20 课。它把前面零散的事件汇总成一张表。

### 20.1 机制回顾

1. 在 `Intents` 里订阅事件类别；
2. 在 `Client` 子类里实现 `on_事件名` 方法；
3. SDK 收到事件后，用 `getattr(self, "on_" + event)` 找方法并调度。

> **没有订阅 = 收不到**；**方法名错 = 收不到**；**没实现 = 静默忽略**。这三句话能解释大部分"事件不触发"的问题。

### 20.2 五类事件与 Intents 对照

| 类别 | Intents 开关 | 典型事件 | 说明 |
|------|--------------|----------|------|
| 公域 @ 消息 | `public_guild_messages` | `on_at_message_create`、`on_public_message_delete` | 最常用 |
| 全部消息 | `guild_messages` | `on_message_create`、`on_message_delete` | 仅私域机器人 |
| 成员事件 | `guild_members` | 成员加入/更新/离开 | 需要权限 |
| 互动事件 | `interaction` | `on_interaction_create` | 按钮点击等 |
| 论坛事件 | `forums` | 帖子/回复/审核 | 仅私域机器人 |

> **`public_guild_messages` 和 `guild_messages` 有什么区别？**
> - `public_guild_messages`：只推"@ 机器人"的消息和公开消息删除事件；
> - `guild_messages`：推频道里的**所有**消息（不限于 @ 机器人）。
>
> 后者权限更高（仅私域），流量也大得多。除非你要做"监听所有聊天内容"的功能，否则用前者。

### 20.3 公域消息

```python
intents = botpy.Intents(public_guild_messages=True)
client = MyClient(intents=intents)


class MyClient(botpy.Client):
    async def on_at_message_create(self, message: Message):
        """有人 @ 机器人"""

    async def on_public_message_delete(self, message: Message):
        """公开消息被删除"""
```

### 20.4 全部消息

```python
intents = botpy.Intents(guild_messages=True)


class MyClient(botpy.Client):
    async def on_message_create(self, message: Message):
        """频道里任何新消息"""

    async def on_message_delete(self, message: Message):
        """任何消息被删除"""
```

### 20.5 成员事件

```python
intents = botpy.Intents(guild_members=True)
```

> 成员事件的回调名取决于 SDK 版本，常见的有 `on_guild_member_add`、`on_guild_member_update`、`on_guild_member_remove`。使用前先在 SDK 源码或文档里确认当前版本支持哪些。

### 20.6 互动事件

```python
from botpy.interaction import Interaction

intents = botpy.Intents(interaction=True)


class MyClient(botpy.Client):
    async def on_interaction_create(self, interaction: Interaction):
        """按钮点击、菜单选择等"""
```

> **`Interaction` 里有什么？**
> 通常包含 `id`（互动 ID）、`data`（按钮的标识和值）、`channel_id`、`guild_id`、`user` 等。你需要根据 `data` 里的标识判断用户点了哪个按钮，然后执行对应逻辑。

### 20.7 论坛事件

```python
from botpy.forum import Thread
from botpy.types.forum import Post, Reply, AuditResult

intents = botpy.Intents(forums=True)


class MyClient(botpy.Client):
    async def on_forum_thread_create(self, thread: Thread): ...
    async def on_forum_thread_update(self, thread: Thread): ...
    async def on_forum_thread_delete(self, thread: Thread): ...
    async def on_forum_post_create(self, post: Post): ...
    async def on_forum_post_delete(self, post: Post): ...
    async def on_forum_reply_create(self, reply: Reply): ...
    async def on_forum_reply_delete(self, reply: Reply): ...
    async def on_forum_publish_audit_result(self, auditresult: AuditResult): ...
```

> **论坛事件仅私域机器人可用**。公域机器人订阅了也收不到，或者直接报错。
>
> `Thread` 是主题帖，`Post` 是帖子，`Reply` 是回复，`AuditResult` 是审核结果。四者的关系类似"板块 → 帖子 → 回复"。

### 20.8 事件处理的三个原则

1. **快返回**：回调里不要做耗时操作。平台对事件处理有时间要求，超时可能断开连接或丢事件。
2. **异常隔离**：botpy 会把回调里的异常交给 `on_error`，但你最好自己 `try/except`，至少记录上下文：

   ```python
   async def on_at_message_create(self, message):
       try:
           await self.handle(message)
       except Exception:
           botpy.logger.exception("处理消息失败: id=%s", message.id)
           await message.reply(content="出了点问题，请稍后再试")
   ```

3. **幂等**：同一条消息可能因重连被重复推送。发奖、登记、扣积分等操作要先去重（用 `message.id` 做唯一键）。

### 20.9 重写 `on_error`

```python
class MyClient(botpy.Client):
    async def on_error(self, event_method, *args, **kwargs):
        botpy.logger.exception("事件 %s 处理失败", event_method)
```

> **`on_error` 是什么？**
> SDK 捕获到回调异常后会调用它。默认实现只是记录日志。重写它可以做告警（发到管理员、写入错误表），但注意：**不要在 `on_error` 里再抛异常**，否则可能形成错误循环。

---

## 第 21 章 从 Demo 到可维护的项目

前面 20 章是"能跑"，这一章是"能长期跑"。原文没有这部分，但它是从教程走向实际项目的必经之路。

### 21.1 目录结构

```
my_qq_bot/
├── .venv/
├── .gitignore
├── requirements.txt
├── config.yaml            # 本地配置（不提交）
├── config.example.yaml    # 配置模板（提交，给人看）
├── main.py                # 入口：构造 Client、注册指令、启动
├── bot/
│   ├── __init__.py
│   ├── client.py          # Client 子类、事件分发
│   ├── commands.py        # 指令处理
│   ├── services/
│   │   ├── weather.py     # 天气 API
│   │   └── storage.py     # 数据存储
│   └── config.py          # 配置加载与校验
└── tests/
    └── test_commands.py
```

> **为什么要分层？**
> - `main.py` 只负责"启动"，不写业务；
> - `commands.py` 只负责"解析指令、调用服务"，不直接发 HTTP；
> - `services/` 只负责"外部依赖"（API、数据库），方便 mock 测试；
> - `config.py` 集中处理配置和默认值。
>
> 这样改一个功能时，你知道该去哪个文件，而不是在 2000 行的 `main.py` 里翻。

### 21.2 配置与密钥

`.gitignore`：

```
.venv/
__pycache__/
config.yaml
*.log
botpy.log
```

`config.example.yaml`：

```yaml
appid: "your-appid"
secret: "your-secret"
weather:
  appkey: "your-appkey"
  sign: "your-sign"
```

> **为什么要放一个 `example` 文件？**
> 别人（或未来的你）克隆仓库后，知道需要哪些配置项。直接提交真实 `config.yaml` 会泄露密钥；完全不提供模板，别人又不知道怎么配。

### 21.3 启动时校验配置

```python
def load_config():
    with open("config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    missing = [k for k in ("appid", "secret") if not cfg.get(k)]
    if missing:
        raise SystemExit(f"缺少配置项: {', '.join(missing)}")

    return cfg
```

> **为什么启动时就校验？**
> 配置错了，程序跑到一半才报错，排查成本高。启动时检查，错误信息直接告诉你缺什么，几秒钟就能修好。

### 21.4 异常处理模板

```python
class MyClient(botpy.Client):
    async def on_at_message_create(self, message: Message):
        try:
            await self.dispatch(message)
        except Exception:
            botpy.logger.exception(
                "处理失败 guild=%s channel=%s message=%s",
                message.guild_id,
                message.channel_id,
                message.id,
            )
            try:
                await message.reply(content="服务开小差了，请稍后再试")
            except Exception:
                botpy.logger.exception("发送错误提示也失败了")
```

> **为什么回复也要包一层？**
> 如果原异常是"网络断了"，回复大概率也会失败。不包的话，`on_error` 里再抛异常，日志会很乱。包一层能让日志聚焦在真正的原因上。

### 21.5 断线重连

botpy 的 WebSocket 内部有心跳和重连逻辑，一般不需要你手动处理。但要注意：

- 重连后可能收到重复事件 → 业务要幂等；
- 长时间断网后，`access_token` 可能过期 → SDK 会自动刷新；
- 进程崩溃 → 用 systemd 的 `Restart=always` 拉起。

> **要不要自己写重连？**
> 不要。自己写容易和 SDK 的内置重连打架，导致重复连接、事件重复。先确认 SDK 版本的重连行为，再决定是否需要外层守护（systemd 就是外层守护）。

### 21.6 测试

```python
import pytest
from bot.commands import parse_weather_command


def test_parse_weather_command():
    assert parse_weather_command("/天气 成都") == "成都"
    assert parse_weather_command("/天气") is None
    assert parse_weather_command("今天天气不错") is None
```

> **为什么要写测试？**
> 机器人很多 bug 是"指令解析"引起的：多一个空格、少一个参数、指令前缀冲突。这些逻辑是纯函数，不需要连 QQ 就能测。把解析逻辑从回调里抽出来，测试成本很低。
>
> 本仓库 `CDUTETC_ACM_BOT` 的 `tests/` 目录就是这种做法：业务逻辑与 QQ 接口分离，用 mock 测试。

### 21.7 上线检查清单

- [ ] 密钥不在代码里，`config.yaml` 已加入 `.gitignore`
- [ ] `requirements.txt` 有版本约束
- [ ] 启动时校验配置
- [ ] 回调有 `try/except` 和上下文日志
- [ ] 业务操作幂等（用消息 ID 去重）
- [ ] 主动消息合并，不超每日配额
- [ ] systemd 配置 `Restart=always`
- [ ] 日志轮转（避免磁盘写满）
- [ ] 有管理员告警渠道（出错时能知道）
- [ ] 在沙箱频道完整测过一遍

---

## 附录 A 术语表

| 术语 | 解释 |
|------|------|
| Guild | 频道（类似 Discord 的服务器），机器人被拉入的"大群" |
| Channel | 子频道，Guild 下的文字/语音/公告频道 |
| Intents | 事件订阅开关，决定平台推哪些事件给你 |
| WebSocket | 全双工长连接，平台用它推送事件 |
| REST | 一种 HTTP 接口风格，用 URL 表示资源、方法表示动作 |
| access_token | 用 AppID + Secret 换来的短期令牌，API 请求要带 |
| BotAppID | 机器人唯一标识 |
| Token / AppSecret | 机器人的密钥，用于换 access_token，必须保密 |
| 被动消息 | 回复用户消息，带 msg_id，5 分钟内有效 |
| 主动消息 | 机器人主动发，每子频道每天限 2 条 |
| Ark | 平台预置的结构化卡片消息 |
| Embed | 简单的结构化消息（标题 + 图 + 字段） |
| markdown 消息 | 支持标题、列表等排版的文本消息 |
| keyboard | 消息按钮组件，只能配 markdown 消息 |
| DMS | Direct Message Service，私信 |
| openid | 平台给用户的匿名 ID，不是真实 QQ 号 |
| union_openid | 跨应用统一 openid，用于关联同一用户 |
| 沙箱 | 开发测试环境，机器人未发布时只能在这里跑 |
| 私域 | 机器人只能在你指定的频道使用 |
| 公域 | 机器人可被任意用户添加，需要审核 |

---

## 附录 B 官方链接

| 资源 | 地址 |
|------|------|
| QQ 机器人官方文档 | <https://bot.q.qq.com/wiki/> |
| 开放平台后台 | <https://q.qq.com/> |
| 错误码说明 | <https://bot.q.qq.com/wiki/develop/api/openapi/error/error.html> |
| Python SDK（botpy） | <https://github.com/tencent-connect/botpy> |
| 天气 API（nowapi） | <https://www.nowapi.com/> |
| Python 官网 | <https://www.python.org/downloads/> |

> **文档会更新，教程会过时。** 遇到接口对不上，先看官方文档的"更新日志"和 SDK 的 GitHub Release。

---

## 附录 C 原文课程与本文章节对照

| 原文课程 | 本文章节 |
|----------|----------|
| 第 1 课 注册 QQ 开放平台账户 | 第 1 章 |
| 第 2 课 接口说明 | 第 2 章 |
| 第 3 课 SDK 安装 | 第 3 章 |
| 第 4 课 botpy 快速入门 | 第 4 章 |
| 第 5 课 腾讯云官方模拟实验 | 第 5 章 |
| 第 6 课 天气案例 | 第 6 章 |
| 第 7 课 主动推送 | 第 7 章 |
| 第 8 课 指令回复（Ark 与私信） | 第 8 章 |
| 第 9 课 小程序 | 第 9 章 |
| 第 10 课 PyCharm SSH 连接服务器 | 第 10 章 |
| 第 11 课 读取 YAML | 第 11 章 |
| 第 12 课 用户对象 User | 第 12 章 |
| 第 13 课 发消息/回复 | 第 13 章 |
| 第 14 课 语音房上麦 | 第 14 章 |
| 第 15 课 注册指令 handler | 第 15 章 |
| 第 16 课 频道对象 guild | 第 16 章 |
| 第 17 课 发送按钮消息 | 第 17 章 |
| 第 18 课 禁言 | 第 18 章 |
| 第 19 课 查看错误码 | 第 19 章 |
| 第 20 课 事件监听 | 第 20 章 |

---

## 附录 D 原文中已过时或有误的写法

整理这份教程时，对照当前 `qq-botpy 1.2.1` 逐项核对，以下地方需要特别注意。这不是挑原文的错，而是平台和 SDK 确实变了。

| 原文写法 | 问题 | 本文写法 |
|----------|------|----------|
| `client.run(appid="12345", token="xxxx")` | 1.2.x 参数名是 `secret` | `client.run(appid=..., secret=...)` |
| `@Commands(name=("你好", "hello"))` | 1.2.x 的 `Commands.__init__(*args)` 不接受 `name=` | `@Commands("你好", "hello")` |
| `threading.Timer(100, await send_weather_message_by_time)` | `await` 会立即执行；Timer 需要普通函数 | `threading.Timer(100, run_push_job)` 或 `asyncio.create_task` |
| `qqbot.logger.info("channelid %s" % channel_id)` | 变量名未定义（应为 `channels_id`） | 改为 `channels_id` |
| `yaml.load(file)` | PyYAML 6 要求显式 Loader | `yaml.safe_load(f)` |
| `MarkdownPayload(template_id=65, ...)` | 1.2.x 字段为 `custom_template_id` | `{"custom_template_id": "65", ...}` |
| `AsyncDmsAPI.create_direct_message(...)` | 旧版 API；新版是 `create_dms` | `self.api.create_dms(guild_id, user_id)` |
| `msg_id=message.event_id` | 被动回复优先用消息 ID | `msg_id=message.id` |
| 频道对象用 `guild.name` | botpy 返回字典 | `guild["name"]` |
| `from botpy.types.message import Message` 作为回调类型 | 回调实际类型在 `botpy.message` | `from botpy.message import Message` |

> **最后再强调一次**：如果你安装的 SDK 版本与本文不同，以 `pip show qq-botpy` 的版本和官方文档为准。代码可以照抄，但参数名和字段名要对着当前版本核一遍。

---

## 写在最后：给初学者的学习路径

如果你是完全零基础，建议按这个顺序练习，不要一上来就写复杂功能：

1. **跑通最小机器人**（第 4 章）：先看到"@ 它有回复"，建立信心。
2. **加一个指令**（第 8、15 章）：`/天气 成都`，理解"收消息 → 解析 → 调 API → 回复"的闭环。
3. **加配置管理**（第 11 章）：把密钥挪出代码，理解配置与代码分离。
4. **加定时任务**（第 7 章）：做每日提醒，理解主动消息和配额。
5. **加异常处理与日志**（第 19、21 章）：让机器人出错时你能定位。
6. **部署到服务器**（第 10 章）：让它 7×24 小时运行。
7. **最后再碰高级功能**（Ark、按钮、私信、语音房）：这些都需要额外权限，先把基础打牢。

> **一个心态建议**：机器人开发 80% 的时间不是在写业务，而是在处理"为什么没反应""为什么报 403""为什么消息发不出去"。这不是你笨，这是平台开发的常态。学会看日志、查文档、按 19.4 的顺序排查，比背 API 有用得多。
