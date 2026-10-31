"""集中配置：所有需要改动的参数都放这里，通过环境变量覆盖。

安全约定：
- 本文件不含任何真实密钥；密钥一律通过环境变量注入。
- 日志与报错严禁打印 AppSecret / API Key 明文。
- 权限默认全拒绝：未配置允许群/老师/超管白名单时，机器人不服务任何群、不允许任何人管理。
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# 加载项目根目录下的 .env（若存在）。系统环境变量优先，.env 不覆盖已注入的值。
load_dotenv(Path(__file__).resolve().parent / ".env")

# ================= QQ 机器人接入（OneBot/NapCat，必填） =================
# NapCatQQ 正向 WebSocket 地址，例如 ws://127.0.0.1:3001
ONEBOT_WS_URL = os.getenv("ACM_ONEBOT_WS_URL", "")
# 可选鉴权 token（与 NapCat 配置一致；未开启留空）
ONEBOT_TOKEN = os.getenv("ACM_ONEBOT_TOKEN", "")
# 机器人自身 QQ 号（用于识别群内 @ 目标；缺省使用 OneBot 事件的 self_id）
BOT_QQ = os.getenv("ACM_BOT_QQ", "")
# 普通 QQ 无平台主动消息限制；保留开关以便在需要时暂停提醒投递。
GROUP_ACTIVE_MESSAGES_ENABLED = os.getenv("ACM_GROUP_ACTIVE_MESSAGES_ENABLED", "1") == "1"

# ================= 大模型（OpenAI 兼容接口，必填） =================
LLM_API_KEY = os.getenv("ACM_LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("ACM_LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_MODEL = os.getenv("ACM_LLM_MODEL", "deepseek-chat")

# ================= 群与角色权限（默认全拒绝） =================
# 允许机器人服务的 QQ 群号列表（逗号分隔）。未配置 = 不服务任何群。
ALLOWED_GROUPS = {
    g.strip() for g in os.getenv("ACM_ALLOWED_GROUPS", "").split(",") if g.strip()
}
# 超级管理员 QQ 号白名单（逗号分隔，可多人）：/修正 纠正比赛、维护全局信息、
# 管理征集与查看全部明细；未配置 = 超管命令全部拒绝。
SUPER_ADMIN = {
    m.strip() for m in os.getenv("ACM_SUPER_ADMIN", "").split(",") if m.strip()
}
# 老师 QQ 号白名单（逗号分隔，发布权并入此白名单，原发布者白名单已移除）：
# 可免审发布比赛、维护校安排/课程/FAQ、发起征集、查看征集汇总。
# 未配置 = 老师管理命令全部拒绝。
TEACHER_WHITELIST = {
    m.strip() for m in os.getenv("ACM_TEACHER_WHITELIST", "").split(",") if m.strip()
}
# 发布命令前缀：授权成员发送“发布 <链接/信息>”才会入库，避免含链接的普通提问误触发
PUBLISH_CMD = os.getenv("ACM_PUBLISH_CMD", "发布")
# 私聊管理命令作用的默认群（超管私聊发 /FAQ添加、/课程添加 等命令时使用）；
# 留空自动取 ALLOWED_GROUPS 中排序首个群号。
ADMIN_GROUP = os.getenv("ACM_ADMIN_GROUP", "").strip()

# ================= 回答限制（五层防护） =================
# 在保留 llm.py 现有 system prompt 软约束的基础上，回答前增加五层硬限制：
#   1) 话题范围硬限制：与 ACM 明显无关的问题不调模型，直接回复拒绝话术；
#   2) 敏感内容拦截：用户输入 / 模型输出命中敏感词（政治、色情、暴力、广告、
#      诈骗、涉密等）即拒绝；
#   3) 用户权限：问答默认对全体群成员开放（学生本来就要提问）。
#      显式配置 ACM_QA_WHITELIST 后仅白名单成员可问；留空则自动复用
#      「老师白名单 ∪ 超管白名单」，若两者也为空则退化为群成员皆可提问。
#      命中黑名单直接忽略不回复；
#   4) 频率防刷：单用户对问答接口滑动窗口限流，超限返回提示话术；
#   5) 输出过滤：回复文本二次敏感检查 + 长度上限截断 + 禁止超长链接。
# 以上每层均可独立开关；全部默认开启。

# 调试开关：是否把群号/QQ号/消息前 50 字写入日志（默认关闭，避免留存个人信息）。
# 仅在初次部署采集白名单 openid 时临时设为 1。
DEBUG_LOG_IDENTITY = os.getenv("ACM_DEBUG_LOG_IDENTITY", "0") == "1"

# --- 1) 话题范围硬限制 ---
QA_TOPIC_GATE_ENABLED = os.getenv("ACM_QA_TOPIC_GATE_ENABLED", "1") != "0"
QA_OFFTOPIC_REPLY = os.getenv(
    "ACM_QA_OFFTOPIC_REPLY", "我只回答与 ACM 集训队、比赛、训练相关的问题哦~"
)

# --- 2) 敏感内容拦截 ---
QA_SENSITIVE_GATE_ENABLED = os.getenv("ACM_QA_SENSITIVE_GATE_ENABLED", "1") != "0"
QA_SENSITIVE_REPLY = os.getenv(
    "ACM_QA_SENSITIVE_REPLY", "这个问题我不方便回答，请换个话题~"
)
# 追加自定义敏感词（逗号分隔），与 content_filter.py 内置词表合并使用
QA_SENSITIVE_EXTRA_WORDS = {
    w.strip() for w in os.getenv("ACM_QA_SENSITIVE_EXTRA_WORDS", "").split(",") if w.strip()
}

# --- 3) 用户权限 ---
# 开关默认开启。白名单动态合并：显式配置 ACM_QA_WHITELIST 时优先使用；
# 留空则自动复用「老师白名单 ∪ 超管白名单」；若两者也为空，则退化为
# “群成员皆可提问”（学生答疑的预期行为，如需限制请显式配置白名单）。
QA_WHITELIST_ENABLED = os.getenv("ACM_QA_WHITELIST_ENABLED", "1") != "0"
QA_BLACKLIST = {
    m.strip() for m in os.getenv("ACM_QA_BLACKLIST", "").split(",") if m.strip()
}
QA_WHITELIST = {
    m.strip() for m in os.getenv("ACM_QA_WHITELIST", "").split(",") if m.strip()
}

# --- 4) 频率防刷 ---
QA_RATE_LIMIT_ENABLED = os.getenv("ACM_QA_RATE_LIMIT_ENABLED", "1") != "0"
QA_RATE_LIMIT_SECONDS = int(os.getenv("ACM_QA_RATE_LIMIT_SECONDS", "10"))   # 窗口时长（秒）
QA_RATE_LIMIT_MAX_CALLS = int(os.getenv("ACM_QA_RATE_LIMIT_MAX_CALLS", "1"))  # 窗口内最大调用次数
QA_RATE_LIMIT_REPLY = os.getenv(
    "ACM_QA_RATE_LIMIT_REPLY", "你问得太快啦，稍等几秒再问吧~"
)

# --- 5) 输出过滤 ---
QA_OUTPUT_MAX_LENGTH = int(os.getenv("ACM_QA_OUTPUT_MAX_LENGTH", "800"))       # 回复长度上限（字符）
QA_OUTPUT_MAX_LINK_LENGTH = int(os.getenv("ACM_QA_OUTPUT_MAX_LINK_LENGTH", "60"))  # 链接最长长度（字符）
QA_OUTPUT_FILTERED_REPLY = os.getenv(
    "ACM_QA_OUTPUT_FILTERED_REPLY", "这条回复内容不合规，已被过滤，换个问法试试~"
)

# --- 6) 算法话题限制（题解/思路/概念科普一律不答，省 token） ---
# 命中算法相关提问（求解、要代码、调错、科普讲解等）直接拒绝并返回引导话术；
# 队务类问题（课程/比赛/通知等）不受影响，FAQ 命中优先于本限制。
QA_PROBLEM_GATE_ENABLED = os.getenv("ACM_QA_PROBLEM_GATE_ENABLED", "1") != "0"
QA_PROBLEM_REPLY = os.getenv(
    "ACM_QA_PROBLEM_REPLY",
    "算法相关的题解、思路和概念科普，机器人这边就不展开啦（省点 token 留给队务和比赛消息~）。想啃题就自己多想想，或把代码贴给队友讨论，等集训课问老师收获更大！",
)
QA_PROBLEM_EXTRA_WORDS = {
    w.strip() for w in os.getenv("ACM_QA_PROBLEM_EXTRA_WORDS", "").split(",") if w.strip()
}

# ================= 链接抓取（SSRF 与资源限制） =================
# 域名白名单（逗号分隔，小写）。配置后仅白名单域名可抓取（重定向目标同样校验）
FETCH_ALLOWED_DOMAINS = {
    d.strip().lower()
    for d in os.getenv("ACM_FETCH_ALLOWED_DOMAINS", "").split(",")
    if d.strip()
}
FETCH_MAX_BYTES = int(os.getenv("ACM_FETCH_MAX_BYTES", "1000000"))        # 解压后响应体上限（字节）
FETCH_MAX_REDIRECTS = int(os.getenv("ACM_FETCH_MAX_REDIRECTS", "3"))      # 重定向跳数上限
FETCH_TIMEOUT = float(os.getenv("ACM_FETCH_TIMEOUT", "10"))               # 每跳 DNS/连接/读取/解析总超时（秒）
FETCH_CONCURRENCY = int(os.getenv("ACM_FETCH_CONCURRENCY", "2"))          # 同时抓取上限
FETCH_TEXT_LIMIT = int(os.getenv("ACM_FETCH_TEXT_LIMIT", "3000"))         # 旧配置兼容保留；模型输入使用 LLM_MAX_INPUT_CHARS

# ================= 大模型资源限制 =================
LLM_MAX_INPUT_CHARS = int(os.getenv("ACM_LLM_MAX_INPUT_CHARS", "6000"))   # 单次请求输入长度上限
LLM_MAX_OUTPUT_TOKENS = int(os.getenv("ACM_LLM_MAX_OUTPUT_TOKENS", "2000"))  # 单次请求输出长度上限
LLM_CONCURRENCY = int(os.getenv("ACM_LLM_CONCURRENCY", "2"))              # 模型并发请求上限

# ================= 定时提醒 =================
TZ_NAME = "Asia/Shanghai"
REMIND_CHECK_INTERVAL_MINUTES = int(os.getenv("ACM_REMIND_CHECK_INTERVAL_MINUTES", "30"))
REMIND_BEFORE_HOURS = int(os.getenv("ACM_REMIND_BEFORE_HOURS", "24"))
REMIND_TYPES = ("reg_deadline", "contest_time")

# ================= 自动周赛（v2） =================
# 在提前提醒窗口前按模板生成周赛，启动时补最近未来场次；默认不通知全员，
# 超管确认后才通知全员或 /修正 调整/删除。地点不纳入管理。
WEEKLY_CONTEST_ENABLED = os.getenv("ACM_WEEKLY_CONTEST_ENABLED", "1") != "0"
WEEKLY_CONTEST_WEEKDAY = int(os.getenv("ACM_WEEKLY_CONTEST_WEEKDAY", "6"))   # 0=周一 ... 6=周日
WEEKLY_CONTEST_START = os.getenv("ACM_WEEKLY_CONTEST_START", "19:00")
WEEKLY_CONTEST_END = os.getenv("ACM_WEEKLY_CONTEST_END", "21:00")
WEEKLY_CONTEST_TEMPLATE = os.getenv(
    "ACM_WEEKLY_CONTEST_TEMPLATE",
    "ACM 周赛（本周）",
)

# ================= 公共来源低频集中抓取（v2，默认关闭） =================
# 自动抓取默认关闭：开启后同一来源两次抓取的最小间隔（分钟）由下值控制。
AUTO_FETCH_ENABLED = os.getenv("ACM_AUTO_FETCH_ENABLED", "0") != "0"
FETCH_MIN_INTERVAL_MINUTES = int(os.getenv("ACM_FETCH_MIN_INTERVAL_MINUTES", "60"))
# 自动抓取生成的记录默认不通知全员，由超管确认后处理（/修正 或 /比赛详情 人工核对）。

# ================= 本地数据 =================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("ACM_DB_PATH", os.path.join(BASE_DIR, "contests.db"))

# ================= 启动前配置检查 =================

REQUIRED_CONFIG = {
    "ACM_ONEBOT_WS_URL": ONEBOT_WS_URL,
    "ACM_LLM_API_KEY": LLM_API_KEY,
}


def check_config() -> None:
    """启动前检查必要配置；缺失时抛错，只列缺失项名，不打印密钥值。"""
    missing = [name for name, value in REQUIRED_CONFIG.items() if not value]
    if missing:
        raise RuntimeError(
            "缺少必要配置（请通过环境变量设置）: " + ", ".join(missing)
        )
