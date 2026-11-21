"""已知赛事平台的稳定身份：平台比赛编号独立于名称、轮次与开赛时间。"""

import re
from urllib.parse import urlsplit


def platform_contest_key(url: str) -> str:
    """仅解析官方平台的比赛地址，未知页面继续沿用原精确去重规则。"""
    if not isinstance(url, str):
        return ""
    try:
        parts = urlsplit(url.strip())
        if (parts.scheme.lower() not in {"http", "https"} or parts.username is not None
                or parts.password is not None
                or parts.port not in {None, {"http": 80, "https": 443}[parts.scheme.lower()]}):
            return ""
        host = (parts.hostname or "").lower()
    except ValueError:
        return ""
    if host in {"codeforces.com", "www.codeforces.com"}:
        match = re.fullmatch(r"/(contest|gym)/([1-9]\d*)(?:/.*)?", parts.path)
        if match:
            return f"codeforces:{match[1]}:{match[2]}"
    if host == "ac.nowcoder.com":
        match = re.fullmatch(r"/acm/contest/([1-9]\d*)(?:/.*)?", parts.path)
        if match:
            return f"nowcoder:contest:{match[1]}"
    return ""


def prefer_title(candidate: str, current: str) -> bool:
    """同一平台编号的展示优先含轮次的完整名称，其余按非空名称长度。"""
    def rank(title):
        text = " ".join(str(title or "").split())
        return bool(re.search(r"\bRound\s+\d+\b", text, re.I)), len(text)

    return rank(candidate) > rank(current)
