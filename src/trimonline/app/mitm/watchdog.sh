#!/bin/bash
# 看门狗（trim-media-mitm-watchdog.timer 每分钟调用）：
#  1) /v/__mitm__/health 探测：socket 是否仍被 MITM 接管（被官方抢回/进程挂掉 → 重启服务）
#  2) fd 守护：MITM 进程 fd 数暴涨（连接泄漏）→ 重启服务
#  3) 官方后端是否存活（TCP 8005 探测，挂了就记录；由 app-center 负责拉起）
DIR=/usr/local/trim_media_mitm
SOCK=/var/run/trim_media.sock
LOG=$DIR/watchdog.log

r=$(curl -s --max-time 3 --unix-socket "$SOCK" http://x/v/__mitm__/health 2>/dev/null)
rc=$?

restart_mitm() {
    echo "$(date '+%F %T') $1 → systemctl restart trim-media-mitm" >> "$LOG"
    systemctl restart trim-media-mitm.service >> "$LOG" 2>&1
}

if [ $rc -ne 0 ] || ! echo "$r" | grep -q '"mitm":1'; then
    restart_mitm "探测失败(rc=$rc r=${r:0:60})"
    exit 0
fi

pid=$(echo "$r" | sed -n 's/.*"pid":\([0-9]*\).*/\1/p')
if [ -n "$pid" ] && [ -d "/proc/$pid" ]; then
    nfd=$(ls "/proc/$pid/fd" 2>/dev/null | wc -l)
    if [ "$nfd" -gt 3000 ]; then
        restart_mitm "fd 暴涨 nfd=$nfd"
    fi
fi
exit 0
