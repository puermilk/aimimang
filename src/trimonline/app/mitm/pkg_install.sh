#!/bin/bash
# trimonline 载荷：安装/升级时把运行文件就位并启用服务（需要 root）
# 幂等，可重复执行。安装回调不保证被调用，因此 cmd/main(start) 也会走一遍。
set -u
SRC="$(cd "$(dirname "$0")" && pwd)"     # 载荷目录（app/mitm）
DIR=/usr/local/trim_media_mitm
SOCK=/var/run/trim_media.sock
LOG=$DIR/pkg.log

mkdir -p "$DIR"
echo "$(date '+%F %T') pkg_install: src=$SRC" >> "$LOG"

# 1) 复制运行文件
for f in mitm.py webui.py jsspider.py source_check.py ensure_takeover.sh watchdog.sh \
         setup_units.sh teardown_units.sh pkg_install.sh pkg_uninstall.sh; do
    [ -f "$SRC/$f" ] && cp -f "$SRC/$f" "$DIR/$f"
done
# 1b) 清掉老版本（1.0.0 / 1.1.0）遗留在本机的「内置订阅抓取」相关产物：
#     抓取脚本 + 它的留档 + 每天两次的定时器。本插件不自带任何源，这些也不参与播放。
rm -f "$DIR/update_fty.py" "$DIR/update_fty.log"
rm -f "$DIR/conf/fty_tv.json" "$DIR/conf/fty_meta.json" "$DIR/conf/fty_usable.json"
if [ -f /etc/systemd/system/trim-media-fty-update.timer ]; then
    systemctl disable --now trim-media-fty-update.timer >/dev/null 2>&1
    rm -f /etc/systemd/system/trim-media-fty-update.service \
          /etc/systemd/system/trim-media-fty-update.timer
    systemctl daemon-reload >/dev/null 2>&1
fi
# JS 蜘蛛运行时（drpy2 本体 + cheerio + pdfh/pdfa）；引擎本体在 lib/quickjs
if [ -d "$SRC/runtime" ]; then
    mkdir -p "$DIR/runtime"
    cp -f "$SRC"/runtime/*.js "$DIR/runtime/" 2>/dev/null || true
fi
# conf/：默认文件随包更新，但用户改过的 sources.json / config.json 不覆盖
#       （那是他自己的源配置和写操作来源策略，覆盖了就白改了）
if [ -d "$SRC/conf" ]; then
    mkdir -p "$DIR/conf"
    for f in "$SRC"/conf/*; do
        [ -e "$f" ] || continue
        b=$(basename "$f")
        if [ "$b" = "sources.json" ] || [ "$b" = "config.json" ]; then
            [ -f "$DIR/conf/$b" ] && continue
        fi
        cp -a "$f" "$DIR/conf/$b"
    done
fi
# 运行库：纯目录 + PYTHONPATH。不用 venv —— venv 必带一个指向 /usr/bin/python3 的
# 绝对符号链接，会被部分版本的应用中心解包器拒绝（报「解压 app.tgz 失败」）。
if [ -d "$SRC/lib" ]; then
    rm -rf "$DIR/lib"
    cp -a "$SRC/lib" "$DIR/lib"
fi
chmod +x "$DIR"/*.sh 2>/dev/null
# 注意：online_registry.json / watch_history.json 属于用户数据，永不覆盖

# 2) 写单元并启用
bash "$DIR/setup_units.sh"
systemctl enable trim-media-mitm.service >/dev/null 2>&1
systemctl enable trim-media-mitm-watchdog.timer >/dev/null 2>&1
systemctl enable trim-media-mitm-maintenance.timer >/dev/null 2>&1

# 3) 拉起接管
systemctl restart trim-media-mitm.service

# 4) 等健康探测通过
for i in $(seq 1 40); do
    r=$(curl -s --max-time 2 --unix-socket "$SOCK" http://x/v/__mitm__/health 2>/dev/null || true)
    if echo "$r" | grep -q '"mitm":1'; then
        echo "$(date '+%F %T') pkg_install: 接管成功 $r" >> "$LOG"
        exit 0
    fi
    sleep 0.5
done
echo "$(date '+%F %T') pkg_install: 接管未就绪（服务已启用，看门狗会继续尝试）" >> "$LOG"
exit 0
