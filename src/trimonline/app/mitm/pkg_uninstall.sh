#!/bin/bash
# trimonline 载荷：卸载时拆除接管，并保留用户数据
# 需要 root。幂等。
set -u
DIR=/usr/local/trim_media_mitm

# 1) 停服务 + 删单元 + 把官方 socket 还原
[ -x "$DIR/teardown_units.sh" ] && bash "$DIR/teardown_units.sh"

# 2) 移除可再生产物（代码/venv/引擎），保留数据：
#    online_registry.json（在线条目）、watch_history.json（继续播放）、
#    conf/（源配置 + js_cache/ 站点规则缓存）
for f in mitm.py webui.py jsspider.py source_check.py ensure_takeover.sh watchdog.sh \
         setup_units.sh teardown_units.sh pkg_install.sh pkg_uninstall.sh \
         update_fty.py update_fty.log watchdog.log maintenance.log pkg.log; do
    rm -f "$DIR/$f"
done
rm -rf "$DIR/lib" "$DIR/venv" "$DIR/runtime" "$DIR/__pycache__"
# conf/ 里的这些东西不是用户数据，而是老版本内置订阅的留档 —— 一并清掉
rm -f "$DIR/conf/fty_tv.json" "$DIR/conf/fty_meta.json" "$DIR/conf/fty_usable.json"
exit 0
