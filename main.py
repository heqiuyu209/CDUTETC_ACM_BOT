"""程序入口。

    python main.py

真正的业务实现在 cdutetc 包里：
    cdutetc/config.py    配置
    cdutetc/database.py  数据访问
    cdutetc/commands.py  命令处理
    cdutetc/main.py      消息路由与启动
    cdutetc/clients/     QQ 开放平台官方客户端
"""

from cdutetc.main import run_bot

if __name__ == "__main__":
    run_bot()
