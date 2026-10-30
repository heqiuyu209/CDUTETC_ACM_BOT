"""集中配置：所有需要改动的参数都放这里，通过环境变量覆盖。

安全约定：
- 本文件不含任何真实密钥；密钥一律通过环境变量注入。
- 日志与报错严禁打印 AppSecret / API Key 明文。
- 权限默认全拒绝：未配置允许群/老师/超管白名单时，机器人不服务任何群、不允许任何人管理。
"""

import os

# ================= QQ 机器人（必填） =================
APPID = os.getenv("ACM_APPID", "")
APP_SECRET = os.getenv("ACM_APP_SECRET", "")
# 确认 QQ 平台和接收群允许主动消息后设为 1；关闭时暂停提醒投递。
GROUP_ACTIVE_MESSAGES_ENABLED = os.getenv("ACM_GROUP_ACTIVE_MESSAGES_ENABLED", "0") == "1"

# ================= 大模型（OpenAI 兼容接口，必填） =================
LLM_API_KEY = os.getenv("ACM_LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("ACM_LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_MODEL = os.getenv("ACM_LLM_MODEL", "deepseek-chat")

# ================= 群与角色权限（默认全拒绝） =================
# 允许机器人服务的群 openid 列表（逗号分隔）。未配置 = 不服务任何群。
ALLOWED_GROUPS = {
    g.strip() for g in os.getenv("ACM_ALLOWED_GROUPS", "").split(",") if g.strip()
}
# 超级管理员 openid 白名单（逗号分隔，可多人）：/修正 纠正比赛、维护全局信息、
# 管理征集与查看全部明细；未配置 = 超管命令全部拒绝。
SUPER_ADMIN = {
    m.strip() for m in os.getenv("ACM_SUPER_ADMIN", "").split(",") if m.strip()
}
# 老师 openid 白名单（逗号分隔，发布权并入此白名单，原发布者白名单已移除）：
# 可免审发布比赛、维护校安排/课程/FAQ、发起征集、查看征集汇总。
# 未配置 = 老师管理命令全部拒绝。
TEACHER_WHITELIST = {
    m.strip() for m in os.getenv("ACM_TEACHER_WHITELIST", "").split(",") if m.strip()
}
# 发布命令前缀：授权成员发送“发布 <链接/信息>”才会入库，避免含链接的普通提问误触发
PUBLISH_CMD = os.getenv("ACM_PUBLISH_CMD", "发布")

# ================= 链接抓取（SSRF 与资源限制） =================
# 域名白名单（逗号分隔，小写）。配置后仅白名单域名可抓取（重定向目标同样校验）
FETCH_ALLOWED_DOMAINS = {
    d.strip().lower()
    for d in os.getenv("ACM_FETCH_ALLOWED_DOMAINS", "").split(",")
    if d.strip()
}
FETCH_MAX_BYTES = int(os.getenv("ACM_FETCH_MAX_BYTES", "1000000"))        # 解压后响应体上限（字节）
FETCH_MAX_REDIRECTS = int(os.getenv("ACM_FETCH_MAX_REDIRECTS", "3"))      # 重定向跳数上限
FETCH_TIMEOUT = float(os.getenv("ACM_FETCH_TIMEOUT", "10"))               # 连接/读取超时（秒）
FETCH_CONCURRENCY = int(os.getenv("ACM_FETCH_CONCURRENCY", "2"))          # 同时抓取上限
FETCH_TEXT_LIMIT = int(os.getenv("ACM_FETCH_TEXT_LIMIT", "3000"))         # 送入模型的正文长度上限（字符）

# ================= 大模型资源限制 =================
LLM_MAX_INPUT_CHARS = int(os.getenv("ACM_LLM_MAX_INPUT_CHARS", "6000"))   # 单次请求输入长度上限
LLM_MAX_OUTPUT_TOKENS = int(os.getenv("ACM_LLM_MAX_OUTPUT_TOKENS", "2000"))  # 单次请求输出长度上限
LLM_MAX_RESPONSE_BYTES = int(os.getenv("ACM_LLM_MAX_RESPONSE_BYTES", "262144"))  # 实际响应/解压后字节上限
LLM_CONCURRENCY = int(os.getenv("ACM_LLM_CONCURRENCY", "2"))              # 模型并发请求上限

# ================= 定时提醒 =================
TZ_NAME = "Asia/Shanghai"
REMIND_CHECK_INTERVAL_MINUTES = int(os.getenv("ACM_REMIND_CHECK_INTERVAL_MINUTES", "30"))
REMIND_BEFORE_HOURS = int(os.getenv("ACM_REMIND_BEFORE_HOURS", "24"))
REMIND_TYPES = ("reg_deadline", "contest_time")

# ================= 自动周赛（v2） =================
# 每周日按模板自动生成周赛记录并提醒；生成记录默认不通知全员，
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
    "ACM_APPID": APPID,
    "ACM_APP_SECRET": APP_SECRET,
    "ACM_LLM_API_KEY": LLM_API_KEY,
}


def check_config() -> None:
    """启动前检查必要配置；缺失时抛错，只列缺失项名，不打印密钥值。"""
    missing = [name for name, value in REQUIRED_CONFIG.items() if not value]
    if missing:
        raise RuntimeError(
            "缺少必要配置（请通过环境变量设置）: " + ", ".join(missing)
        )
