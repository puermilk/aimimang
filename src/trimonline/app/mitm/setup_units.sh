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

# 2.0 VIP 采集桥：爱奇艺 / 咪咕 / 芒果。只监听 127.0.0.1，由主程序按暗号开关决定
# 是否把它们注入搜索；不打开时这些进程只占几十 MB 内存，不产生外部请求。
for pair in "iqiyi:8770" "migu:8771" "mgtv:8772"; do
    name=${pair%%:*}
    cat > "$U/trim-media-vip-$name.service" <<EOF
[Unit]
Description=aimimang VIP bridge ($name)
After=network.target trim-media-mitm.service
Wants=trim-media-mitm.service

[Service]
Type=simple
Environment=AIMIMANG_CONF=$DIR/conf
Environment=AIMIMANG_DATA=$DIR/vip_data
Environment=PYTHONPATH=$DIR/lib
Environment=PLAYWRIGHT_BROWSERS_PATH=/usr/local/trim_media_mitm/vip_data/ms-playwright
Environment=AIMIMANG_CHROMIUM=/usr/bin/chromium
WorkingDirectory=$DIR/vip/$name
ExecStart=/usr/bin/python3 $DIR/vip/$name/bridge.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
done

chmod +x "$DIR"/*.sh 2>/dev/null
systemctl daemon-reload
exit 0
