#!/bin/bash
# systemd ExecStartPre：把官方后端 socket 挪到 $REAL，原路径 $SOCK 留给 MITM。
#
# 关键点（针对开机场景）：
#  - 官方后端由 app-center 以用户会话方式拉起，启动时刻不确定，可能
#    早于或晚于本服务，因此这里要「先等、再判定、必要时反抢回」。
#  - 判定用 curl 退出码（0=有活的监听者，7=死文件），不看响应体：
#      SOCK 是 MITM 自己的残留   → 删掉重绑
#      SOCK 是官方活监听          → 挪去 REAL 保管
#      SOCK 是死文件              → 删掉
SOCK=/var/run/trim_media.sock
REAL=/var/run/trim_media.real.sock

# 等官方后端绑出 socket，最多 30s（开机时它可能启动较慢）
for i in $(seq 1 60); do
    if [ -S "$SOCK" ] || [ -S "$REAL" ]; then break; fi
    sleep 0.5
done

if [ -S "$SOCK" ]; then
    body=$(curl -s --max-time 2 --unix-socket "$SOCK" \
                http://x/v/__mitm__/health 2>/dev/null)
    rc=$?
    if [ $rc -ne 0 ]; then
        # 连不上 = 死文件，删掉让 MITM 绑定
        rm -f "$SOCK"
    elif echo "$body" | grep -q '"mitm":1'; then
        # 已经是 MITM（重启残留），删掉文件后重绑，避免沿用旧 inode
        rm -f "$SOCK"
    else
        # 官方活监听：把它挪去 REAL 保管
        rm -f "$REAL"
        mv "$SOCK" "$REAL"
    fi
fi
# SOCK 不存在时 MITM 直接绑定即可
exit 0
