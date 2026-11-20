"""私聊寒暄的本地分流：不从工作区推导群资料或管理权限。"""

import re

from cdutetc import content_filter

_NAMES = r"(?:雄姐|学姐)"
_CALL = re.compile(
    rf"^(?:{_NAMES}[\s，,~～]*)(?:在吗|在不在|你好|您好|嗨|哈喽|早安|早上好|"
    r"晚上好|晚安)?[呀啊哦呢啦~～!！?？,，。.、\s]*$"
)
_BUSINESS = re.compile(
    r"比赛|周赛|校赛|课程|集训|老师|群|提醒|配置|报名|征集|权限|白名单|黑名单|"
    r"审批|待审|待确认|发布|绑定|认领|选群|切换|清理|FAQ|工单|数据库|"
    r"开启|关闭|添加|删除|设置|改成|修改|清空",
    re.I,
)
_DECISION = re.compile(r"^(?:同意|通过|拒绝|驳回|批准|确认|FAQ驳回)(?:\s*\d|\s*全部|\s*$)", re.I)


def local_call_reply(text: str) -> str:
    """简单喊名字确定性回应，不需要模型或群事实。"""
    return "在呢，雄姐听着～" if _CALL.fullmatch((text or "").strip()) else ""


def is_chat_message(text: str) -> bool:
    """管理和队务留给原入口，纯私聊只带用户自己的聊天内容。"""
    body = (text or "").strip()
    if not body or body.startswith("/") or _DECISION.search(body):
        return False
    if local_call_reply(body) or content_filter.is_social_chat(body):
        return True
    return not (_BUSINESS.search(body) or content_filter.is_topic_relevant(body))
