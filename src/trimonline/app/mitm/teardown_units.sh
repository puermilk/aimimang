#!/bin/bash
# trimonline 载荷：停止并移除 systemd 单元，并把官方 socket 归还原路径
# 卸载时调用（需要 root）。幂等。
set -u
SOCK=/var/run/trim_media.sock
REAL=/var/run/trim_media.real.sock
U=/etc/systemd/system

# 1) 先停掉接管进程，释放 socket
systemctl stop trim-media-mitm.service           >/dev/null 2>&1
systemctl disable trim-media-mitm.service        >/dev/null 2>&1
systemctl disable --now trim-media-mitm-watchdog.timer    >/dev/null 2>&1
systemctl disable --now trim-media-mitm-maintenance.timer >/dev/null 2>&1
# 下面这个单元本版起已不再创建，保留是为了清掉老版本（1.0.0 / 1.1.0）留下的
systemctl disable --now trim-media-fty-update.timer       >/dev/null 2>&1
systemctl stop trim-media-mitm-watchdog.timer    >/dev/null 2>&1
systemctl stop trim-media-mitm-maintenance.timer >/dev/null 2>&1
systemctl stop trim-media-fty-update.timer       >/dev/null 2>&1

# 2) 删单元文件
rm -f "$U/trim-media-mitm.service" \
      "$U/trim-media-mitm-watchdog.service" "$U/trim-media-mitm-watchdog.timer" \
      "$U/trim-media-mitm-maintenance.service" "$U/trim-media-mitm-maintenance.timer" \
      "$U/trim-media-fty-update.service" "$U/trim-media-fty-update.timer"
systemctl daemon-reload
systemctl reset-failed 2>/dev/null

# 3) 把官方 socket 还原到原路径
#    官方进程仍监听同一个 inode，mv 回去即可，无需重启官方后端。
if [ -S "$REAL" ]; then
    rm -f "$SOCK"
    mv "$REAL" "$SOCK"
fi

# 4) 确认官方后端可用（curl 退出码 0 = 有活的监听者）
for i in $(seq 1 20); do
    if curl -s --max-time 2 --unix-socket "$SOCK" http://x/ >/dev/null 2>&1; then
        exit 0
    fi
    sleep 0.5
done
exit 0
