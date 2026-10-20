#!/bin/bash
# 爱迷茫 1.2.0（清饭太硬后重打）沙箱端到端复测
#   0) 生产侧快照
#   1) 解包 + 伪造「老版本装过」的现场（update_fty.py / conf/fty_* / fty 定时器）
#   2) 跑 pkg_install.sh（沙箱路径）→ 验证清理逻辑 + 文件就位
#   3) 从部署目录跑引擎自检
#   4) 起临时实例，真打配置页 HTTP 接口
set -u
FPK=/root/fpk/爱迷茫-1.2.4.fpk
SB=/tmp/opencode/sb124
LIVE=/usr/local/trim_media_mitm
MOCK=http://127.0.0.1:18082/api.php/provide/vod/
JSSRV=http://127.0.0.1:18081

echo "### 0) 生产侧快照"
systemctl is-active trim-media-mitm.service
ls -la "$LIVE/conf/" 2>/dev/null

rm -rf "$SB"; mkdir -p "$SB"/{run,bin,etc-systemd,usrlocal,payload}

echo
echo "### 1) 解包 + 伪造老版本现场"
tar xzf "$FPK" -C "$SB/payload"
tar xzf "$SB/payload/app.tgz" -C "$SB/payload"
P="$SB/payload/mitm"

cat > "$SB/bin/systemctl" <<EOF
#!/bin/bash
echo "[stub] systemctl \$*" >> $SB/systemctl.log
exit 0
EOF
chmod +x "$SB/bin/systemctl"
export PATH="$SB/bin:$PATH"

for f in pkg_install.sh pkg_uninstall.sh setup_units.sh teardown_units.sh; do
    [ -f "$P/$f" ] || continue
    sed -i \
      -e "s#/usr/local/trim_media_mitm#$SB/usrlocal/trim_media_mitm#g" \
      -e "s#/var/run/trim_media.real.sock#$SB/run/trim_media.real.sock#g" \
      -e "s#/var/run/trim_media.sock#$SB/run/trim_media.sock#g" \
      -e "s#/etc/systemd/system#$SB/etc-systemd#g" \
      -e "s#seq 1 40#seq 1 2#" "$P/$f"
done

# 伪造：老版本已经装过 —— 运行目录与 conf 里有饭太硬产物，系统里有那个定时器
D="$SB/usrlocal/trim_media_mitm"
mkdir -p "$D/conf"
echo "old update script"          > "$D/update_fty.py"
echo "1 2 3"                      > "$D/update_fty.log"
echo '{"sites":[1,2,3]}'          > "$D/conf/fty_tv.json"
echo '{"ts":1}'                   > "$D/conf/fty_meta.json"
echo '{"sources":[]}'             > "$D/conf/fty_usable.json"
echo '{"primary":[{"name":"我的源","api":"http://me/api.php/provide/vod/"}]}' > "$D/conf/sources.json"
cat > "$D/conf/config.json" <<'JSON'
{
 "_note": ["用户自己改过的"],
 "write_allow_any": false,
 "write_allow_ip": ["203.0.113.7"],
 "auto_trust_first": false,
 "first_trusted": true
}
JSON
echo "old unit"                   > "$SB/etc-systemd/trim-media-fty-update.timer"
echo "old unit"                   > "$SB/etc-systemd/trim-media-fty-update.service"
echo "--- 安装前 ---"
ls "$D" | tr '\n' ' '; echo
ls "$D/conf" | tr '\n' ' '; echo

echo
echo "### 2) 跑 pkg_install.sh（沙箱）"
bash "$P/pkg_install.sh"

echo "--- 安装后：饭太硬产物应全部消失 ---"
for x in "$D/update_fty.py" "$D/update_fty.log" "$D/conf/fty_tv.json" \
         "$D/conf/fty_meta.json" "$D/conf/fty_usable.json" \
         "$SB/etc-systemd/trim-media-fty-update.timer" \
         "$SB/etc-systemd/trim-media-fty-update.service"; do
    if [ -e "$x" ]; then echo "  ✗ 仍在: $x"; else echo "  ✓ 已清: $(basename "$x")"; fi
done
echo "--- 用户自己的源配置必须原样保留 ---"
cat "$D/conf/sources.json"
echo "--- 用户改过的 config.json 也必须原样保留（不能覆盖成模板）---"
cat "$D/conf/config.json"
/usr/bin/python3 - <<'CK'
import json
d = json.load(open("/tmp/opencode/sb124/usrlocal/trim_media_mitm/conf/config.json"))
ok = (d.get("write_allow_any") is False and d.get("write_allow_ip") == ["203.0.113.7"]
      and d.get("auto_trust_first") is False and d.get("first_trusted") is True)
print("  ✓ config.json 未被覆盖，用户设置还在" if ok else "  ✗ config.json 被覆盖了: %s" % d)
CK
echo "--- 就位的文件 ---"
ls "$D" | tr '\n' ' '; echo
ls "$D/runtime" | tr '\n' ' '; echo
echo "--- 生成的单元（应无 fty）---"
ls "$SB/etc-systemd" | tr '\n' ' '; echo
echo "--- 启用了哪些单元 ---"
grep -o "enable trim-media-[a-z.-]*" "$SB/systemctl.log" | sort -u

echo
echo "### 3) 部署目录里跑引擎自检"
TRIM_CONF_DIR="$D/conf" PYTHONPATH="$D/lib" timeout 60 /usr/bin/python3 "$D/jsspider.py" "$JSSRV/兔小贝.js" 贝

echo
echo "### 3b) 配置页前端自检（语法 + 用迷你 DOM 真跑一遍关键流程）"
MITM_DIR="$D" PYTHONPATH="$D/lib" /usr/bin/python3 /tmp/opencode/check_page.py
echo "    （退出码 $?，应为 0）"

echo
echo "### 4) 临时实例 + 配置页 HTTP 接口"
INST="$SB/instance"
cp -a "$D" "$INST"
sed -i \
  -e "s#^CONF_DIR = \"/usr/local/trim_media_mitm/conf\"#CONF_DIR = \"$INST/conf\"#" \
  -e "s#^REG_FILE = \"/usr/local/trim_media_mitm/online_registry.json\"#REG_FILE = \"$INST/online_registry.json\"#" \
  -e "s#^WATCH_FILE = \"/usr/local/trim_media_mitm/watch_history.json\"#WATCH_FILE = \"$INST/watch_history.json\"#" \
  -e "s#^SOCK = \"/var/run/trim_media.sock\"#SOCK = \"$SB/run/trim_media.sock\"#" \
  -e "s#^REAL = \"/var/run/trim_media.real.sock\"#REAL = \"$SB/run/trim_media.real.sock\"#" \
  "$INST/mitm.py"
cat > "$SB/instance_boot.py" <<'PY'
import asyncio, os, sys
sys.path.insert(0, os.environ["INST"])
import mitm
async def main():
    if os.path.exists(mitm.SOCK):
        os.unlink(mitm.SOCK)
    server = await asyncio.start_unix_server(mitm.handle_client, path=mitm.SOCK)
    os.chmod(mitm.SOCK, 0o777)
    print("listening", mitm.SOCK, flush=True)
    async with server:
        await server.serve_forever()
asyncio.run(main())
PY
rm -f "$SB/run/trim_media.sock"
INST="$INST" nohup env INST="$INST" PYTHONPATH="$INST/lib" \
  /usr/bin/python3 "$SB/instance_boot.py" > "$SB/instance.log" 2>&1 &
sleep 3
U="$SB/run/trim_media.sock"
C() { curl -s --max-time 60 --unix-socket "$U" "$@"; }

echo "--- health ---";        C http://x/v/__mitm__/health; echo
echo "--- config 页应出现的新标题 ---"
C http://x/v/__mitm__/config | grep -o '导入 TVBox 配置地址\|导入 TVBox / 饭太硬 配置地址' | sort -u
echo "--- 写操作来源卡片 / 自救命令 ---"
C http://x/v/__mitm__/config | grep -o '写操作来源（决定你保存得下去没有）\|btnAllowIp\|btnAllowAny\|UNLOCK_CMD' | sort -u
echo "--- 旧文案必须为 0 次 ---"
C http://x/v/__mitm__/config | grep -c '只允许从内网访问配置页' | xargs echo "    旧文案出现次数:"
echo "--- /api/sources 的 write 状态 ---"
C http://x/v/__mitm__/api/sources | /usr/bin/python3 -c 'import json,sys; d=json.load(sys.stdin); print("   ", json.dumps(d["write"], ensure_ascii=False))'
echo "--- 公网来源打保存（应被 403，因为用户已设 auto_trust_first=false）---"
C -X POST -H 'Content-Type: application/json' -H 'X-Real-IP: 198.51.100.8' \
  --data '{"primary":[],"fallback":[],"js":[]}' http://x/v/__mitm__/api/sources | /usr/bin/python3 -c 'import json,sys; d=json.load(sys.stdin); print("    ok=%s ip=%s" % (d.get("ok"), d.get("ip")))'
echo "--- 内网来源打保存（应 200）---"
C -X POST -H 'Content-Type: application/json' -H 'X-Real-IP: 10.99.99.20' \
  --data '{"primary":[],"fallback":[],"js":[]}' http://x/v/__mitm__/api/sources | /usr/bin/python3 -c 'import json,sys; d=json.load(sys.stdin); print("    ok=%s ip=%s" % (d.get("ok"), d.get("ip")))' 
echo "--- /api/sources 的 engine ---"
C http://x/v/__mitm__/api/sources | /usr/bin/python3 -c 'import json,sys; d=json.load(sys.stdin); print("engine:", d["engine"]); print("saved:", [s["name"] for s in d["primary"]], d["js"])'
echo "--- /api/import（含 type:3 + 加密JAR + 网盘）---"
C -X POST -H 'Content-Type: application/json' \
  --data '{"url":"http://127.0.0.1:18085/tvbox.json"}' \
  http://x/v/__mitm__/api/import | /usr/bin/python3 -m json.tool --no-ensure-ascii | head -30
echo "--- /api/test（采集 + JS 并行）---"
C -X POST -H 'Content-Type: application/json' \
  --data "{\"keyword\":\"贝\",\"timeout\":15,\"sites\":[{\"name\":\"本地mock\",\"api\":\"$MOCK\",\"lines\":\"multi\",\"level\":\"primary\"},{\"name\":\"兔小贝JS\",\"js\":\"$JSSRV/兔小贝.js\",\"referer\":\"$JSSRV/\",\"lines\":\"auto\",\"level\":\"js\",\"kind\":\"js\"}]}" \
  http://x/v/__mitm__/api/test \
  | /usr/bin/python3 -c 'import json,sys; d=json.load(sys.stdin); [print("  %-10s ms=%-6.0f hits=%-3s playable=%s kind=%s" % (r["name"], r["ms"] or 0, r["hits"], r["playable"], r.get("kind","cms"))) for r in d["results"]]; print("推荐:", d["recommend"]["text"].split(chr(10))[0])'

echo
echo "### 4b) 用你给的两个真实地址打配置页导入接口"
for u in "http://www.饭太硬.cc/tv" "http://tvbox.xn--4kq62z5rby2qupq9ub.top/"; do
  echo "--- $u"
  C -X POST -H 'Content-Type: application/json' --data "{\"url\":\"$u\"}" \
    http://x/v/__mitm__/api/import \
    | /usr/bin/python3 -c '
import json,sys
r=json.load(sys.stdin)
if not r.get("ok"):
    print("    ✗ 失败:", r.get("msg")); raise SystemExit
m=r["meta"]
print("    ✓ 采集 %d / JS %d / 跳过 %d（sites %d）" % (m["kept"], m["js"], m["skipped"], m["sites"]))
if r.get("note"): print("    note:", r["note"])
for s in (r.get("kept") or [])[:2]: print("      采集:", s["name"], s["api"][:60])
for s in (r.get("js") or [])[:4]:   print("      JS  :", s["name"], s["js"][:70])
print("      跳过原因:", ", ".join("%s×%d" % (w,n) for w,n in m.get("why",[])[:3]))
'
done
echo "--- 中文域名的语法错误地址（应给清楚提示，不是 raw repr）---"
C -X POST -H 'Content-Type: application/json' --data '{"url":"http：//不存在的站.top/tv"}' \
  http://x/v/__mitm__/api/import | /usr/bin/python3 -c 'import json,sys; print("    ", json.load(sys.stdin)["msg"][:160])'

echo
echo "### 5) 收尾"
PAT=$(echo aW5zdGFuY2VfYm9vdA== | base64 -d); pkill -f "$PAT" 2>/dev/null
PAT2=$(echo aHR0cC5zZXJ2ZXIK | base64 -d | head -c 11); pkill -f "$PAT2" 2>/dev/null
PAT3=$(echo bW9ja2Ntcw== | base64 -d); pkill -f "$PAT3" 2>/dev/null
sleep 1
echo "残留进程数: $(ps aux | grep -cE '[i]nstance_boot|[m]ockcms')"
echo "--- 生产侧对照（应仍是 active / 原样）---"
systemctl is-active trim-media-mitm.service
ls "$LIVE/conf/" 2>/dev/null | tr '\n' ' '; echo
