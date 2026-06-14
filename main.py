#!/usr/bin/env python3
"""Paper Pusher 兼容入口 —— 按参数路由到 subscribe.py / query.py。

历史上一切都从 main.py 走；现在拆成两个程序：

* ``subscribe.py`` —— 定时订阅推送（``--once`` / 守护 / ``--stats``）
* ``query.py``     —— 临时查询（``--search`` / ``--search-more`` /
                       ``--search-clear`` / ``--push-item``）

本文件保留为薄包装：当 argv 含 ``--search`` / ``--search-more`` /
``--search-clear`` / ``--push-item`` 时路由到 ``query.main()``，
否则到 ``subscribe.main()``。所有老命令继续可用。

新代码建议直接调 ``python subscribe.py`` / ``python query.py``。
"""

from __future__ import annotations

import sys


# 出现这些参数时走 query.py；其余（--once / --stats / 守护）走 subscribe.py。
_QUERY_TRIGGERS = {'--search', '--search-more', '--search-clear', '--push-item'}


def main() -> int:
    argv_keys = {arg.split('=', 1)[0] for arg in sys.argv[1:]}
    if argv_keys & _QUERY_TRIGGERS:
        from query import main as query_main
        return query_main()
    from subscribe import main as subscribe_main
    return subscribe_main()


if __name__ == '__main__':
    sys.exit(main())
