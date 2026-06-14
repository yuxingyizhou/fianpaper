#!/usr/bin/env python3

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
