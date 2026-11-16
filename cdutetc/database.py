"""数据库模块（Facade）：对外保持 cdutetc.database 的全部原有符号与语义。

实现已按职责拆分到 cdutetc.db 子包（connection / contests / sources / training /
faq_collections / persona_memes / admin）。本模块通过模块级 __getattr__ 与
__setattr__ 把属性读写（含测试里的 monkeypatch）统一转发到真实实现所在子模块，
因此 `import cdutetc.database as db; db.get_faq(...)`、
`monkeypatch.setattr(db, "X", ...)` 等既有用法语义不变。
"""

import sys
import types

from cdutetc.db import connection as _conn
from cdutetc.db import contests as _contests
from cdutetc.db import sources as _sources
from cdutetc.db import training as _training
from cdutetc.db import faq_collections as _faq
from cdutetc.db import persona_memes as _persona
from cdutetc.db import admin as _admin

_CHILDREN = (_conn, _contests, _sources, _training, _faq, _persona, _admin)
_LOCAL = {
    "__name__", "__doc__", "__spec__", "__loader__", "__package__", "__path__",
    "__builtins__", "_CHILDREN", "_LOCAL", "_DatabaseFacade",
}


class _DatabaseFacade(types.ModuleType):
    """数据库门面：属性读写转发到拆分后的真实子模块。"""

    def __getattr__(self, name):
        for mod in _CHILDREN:
            if name in mod.__dict__:
                return mod.__dict__[name]
        raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")

    def __setattr__(self, name, value):
        if name in _LOCAL:
            self.__dict__[name] = value
            return
        for mod in _CHILDREN:
            if name in mod.__dict__:
                setattr(mod, name, value)
                return
        self.__dict__[name] = value

    def __dir__(self):
        names = set(self.__dict__)
        for mod in _CHILDREN:
            names |= set(mod.__dict__)
        return sorted(n for n in names if not n.startswith("__"))


sys.modules[__name__].__class__ = _DatabaseFacade
