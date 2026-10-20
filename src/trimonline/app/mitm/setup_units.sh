#!/bin/bash
# trimonline 载荷：写出并重载 systemd 单元（需要 root）
# 由 cmd/main(start) 与 pkg_install.sh 调用；幂等。
set -u
DIR=/usr/local/trim_media_mitm
U=/etc/systemd/system

cat > "$U/trim-media-mitm.service" <<EOF
[Unit]
Description=trim.media online-source MITM takeover
After=network.target trim_app_center.service
Wants=trim_app_center.service

[Service]
Type=simple
Environment=PYTHONPATH=$DIR/lib
ExecStartPre=$DIR/ensure_takeover.sh
ExecStart=/usr/bin/python3 $DIR/mitm.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

cat > "$U/trim-media-mitm-watchdog.service" <<EOF
[Unit]
Description=trim-media MITM watchdog

[Service]
Type=oneshot
ExecStart=$DIR/watchdog.sh
EOF

cat > "$U/trim-media-mitm-watchdog.timer" <<EOF
[Unit]
Description=Run trim-media MITM watchdog every 15s

[Timer]
OnBootSec=60
OnUnitActiveSec=15
AccuracySec=5

[Install]
WantedBy=timers.target
EOF

cat > "$U/trim-media-mitm-maintenance.service" <<EOF
[Unit]
Description=trim.media online-source daily maintenance (refresh douban lists, resolve new items)
After=trim-media-mitm.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
# 走 unix socket 直接打 MITM，registry 只由 mitm 进程写入，避免抢文件
ExecStart=/usr/bin/curl -s --max-time 20 --unix-socket /var/run/trim_media.sock http://x/v/__mitm__/refresh
StandardOutput=append:$DIR/maintenance.log
StandardError=append:$DIR/maintenance.log
EOF

cat > "$U/trim-media-mitm-maintenance.timer" <<EOF
[Unit]
Description=Daily trim.media maintenance after 00:00

[Timer]
OnCalendar=*-*-* 00:20:00
Persistent=true
AccuracySec=1min
Unit=trim-media-mitm-maintenance.service

[Install]
WantedBy=timers.target
EOF

chmod +x "$DIR"/*.sh 2>/dev/null
systemctl daemon-reload
exit 0
