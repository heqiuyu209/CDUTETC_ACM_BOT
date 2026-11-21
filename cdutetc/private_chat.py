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
_SELF_IDENTITY = re.compile(rf"^(?:{_NAMES}(?:是(?:谁|什么)|是谁啊)|你(?:是(?:谁|什么)|叫什么))[呀啊哦呢~～!！?？,，。.、\s]*$")
_SELF_LOCATION = re.compile(rf"^(?:{_NAMES}[\s，,]*你?|你)(?:在哪(?:里|儿)?|到哪(?:里|儿)?了?|在什么地方|在哪里)[呀啊哦呢~～!！?？,，。.、\s]*$")
_ATTENDANCE = re.compile(r"^(?:我|我们)?(?:也|已经|刚刚|刚)?(?:到了|到场了|到啦|来了|到了机房|到机房了|到现场了|到教室了|签到了)[呀啊哦呢啦~～!！?？,，。.、\s]*$")


def local_call_reply(text: str) -> str:
    """简单喊名字确定性回应，不需要模型或群事实。"""
    return "在呢，雄姐听着～" if _CALL.fullmatch((text or "").strip()) else ""


def local_persona_reply(text: str) -> str:
    """称呼和询问机器人身份/位置本地回答，不误检索真实业务地点。"""
    body = (text or "").strip()
    if _SELF_IDENTITY.fullmatch(body):
        return "我是雄姐，集训队的机器人助教，管队务也接梗～"
    if _SELF_LOCATION.fullmatch(body):
        return "一直在聊天窗口里呀，现实里的到场可得看你们～"
    return local_call_reply(body)


def can_interject(text: str) -> bool:
    body = (text or "").strip()
    return bool(body and not body.startswith("/") and not _BUSINESS.search(body)
                and not _DECISION.search(body) and not _ATTENDANCE.fullmatch(body)
                and not local_persona_reply(body) and not content_filter.is_information_request(body)
                and not re.search(r"^请问|[?？]|谁|哪(?:里|儿|间)|什么|怎么|是否", body))


def valid_persona_reply(text: str) -> bool:
    """主动接梗不复读业务拒答，也不冒认真实到场。"""
    body = "".join((text or "").split())
    if re.search(r"我(?:只|仅)(?:会|能)?(?:回答|答).*?(?:ACM|集训队|比赛|训练)", body, re.I):
        return False
    return not re.search(r"我(?:已经|刚刚|刚|也)?(?:到了|到场了|到机房了)|"
                         r"(?:我|雄姐|学姐)?(?:已经|刚刚|刚)?到(?:达|了)?(?:机房|现场|教室)|"
                         r"我(?:已经|现在)?在(?:机房|现场|教室)(?:里|了)", body)


def is_chat_message(text: str) -> bool:
    """管理和队务留给原入口，纯私聊只带用户自己的聊天内容。"""
    body = (text or "").strip()
    if not body or body.startswith("/") or _DECISION.search(body):
        return False
    if local_persona_reply(body) or content_filter.is_social_chat(body):
        return True
    return not (_BUSINESS.search(body) or content_filter.is_topic_relevant(body))
