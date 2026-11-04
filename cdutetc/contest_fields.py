"""模型提取字段到公共比赛字段的统一映射。缺失字段不表示清空。"""

_ALIASES = {
    "name": ("name",),
    "edition": ("edition",),
    "official_reg_start": ("official_reg_start", "reg_start"),
    "official_reg_deadline": ("official_reg_deadline", "reg_deadline"),
    "official_time": ("official_time", "contest_time"),
    "official_rules": ("official_rules", "raw_note"),
    "official_site": ("official_site", "source_link"),
    "tracks": ("tracks",),
    "device_req": ("device_req",),
    "summary": ("summary",),
}


def map_competition_info(info: dict) -> dict:
    """优先使用非空官方字段，其次兼容旧提取字段；保留所有支持资料。"""
    if not isinstance(info, dict):
        raise ValueError("比赛信息必须是对象")
    name = info.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("比赛名称不能为空")
    mapped = {}
    for field, aliases in _ALIASES.items():
        for alias in aliases:
            value = info.get(alias)
            if value is None:
                continue
            if not isinstance(value, str):
                raise ValueError(f"字段 {alias} 类型错误（应为字符串）")
            if value.strip():
                mapped[field] = value.strip()
                break
    return mapped
