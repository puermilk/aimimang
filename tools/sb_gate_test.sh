#!/bin/bash
# 写操作来源闸门（首次信任 + 浏览器凭证）专项测试 —— 直接拿源码树跑，不用重打 FPK
set -u
# 说明：脚本里的 IP 均为 RFC 5737 / RFC 1918 测试占位，与任何真实网络无关。
SRC=/root/fpk/src/trimonline/app/mitm
SB=/tmp/opencode/sbgate
LIVE=/usr/local/trim_media_mitm/venv/lib/python3.11/site-packages
U=$SB/run/trim_media.sock

rm -rf "$SB"; mkdir -p "$SB"/{run,conf}

cp -a "$SRC/mitm.py" "$SRC/webui.py" "$SRC/jsspider.py" "$SRC/source_check.py" "$SB/"
cp -a "$SRC/runtime" "$SB/runtime"
cp -a "$SRC/conf/." "$SB/conf/"

sed -i \
  -e "s#^CONF_DIR = \"/usr/local/trim_media_mitm/conf\"#CONF_DIR = \"$SB/conf\"#" \
  -e "s#^REG_FILE = \"/usr/local/trim_media_mitm/online_registry.json\"#REG_FILE = \"$SB/online_registry.json\"#" \
  -e "s#^WATCH_FILE = \"/usr/local/trim_media_mitm/watch_history.json\"#WATCH_FILE = \"$SB/watch_history.json\"#" \
  -e "s#^SOCK = \"/var/run/trim_media.sock\"#SOCK = \"$U\"#" \
  -e "s#^REAL = \"/var/run/trim_media.real.sock\"#REAL = \"$SB/run/trim_media.real.sock\"#" \
  "$SB/mitm.py"

cat > "$SB/boot.py" <<'PY'
import asyncio, os, sys
sys.path.insert(0, os.environ["SB"])
import mitm
async def main():
    if os.path.exists(mitm.SOCK): os.unlink(mitm.SOCK)
    srv = await asyncio.start_unix_server(mitm.handle_client, path=mitm.SOCK)
    os.chmod(mitm.SOCK, 0o777)
    print("listening", flush=True)
    async with srv:
        await srv.serve_forever()
asyncio.run(main())
PY

rm -f "$U"
SB="$SB" PYTHONPATH="$LIVE:$SB" nohup /usr/bin/python3 "$SB/boot.py" > "$SB/inst.log" 2>&1 &
sleep 3

CFG="$SB/conf/config.json"
API=http://x/v/__mitm__/api

# $1=方法 $2=路径 $3=JSON体(可空) $4=X-Real-IP(可空) $5=附加头(可空) → "HTTP码 body"
hit() {
  local args=(-s -o "$SB/out.txt" -w '%{http_code}' --max-time 30 --unix-socket "$U" -X "$1")
  [ -n "${4:-}" ] && args+=(-H "X-Real-IP: $4")
  [ -n "${5:-}" ] && args+=(-H "$5")
  if [ -n "${3:-}" ]; then
    args+=(-H 'Content-Type: application/json' --data "$3")
  fi
  local code; code=$(curl "${args[@]}" "$2")
  echo "$code $(cat "$SB/out.txt")" | head -c 4000
}
# 同上，但把响应头抓下来给下面看 Set-Cookie
hdr() {
  local args=(-s -o /dev/null -D "$SB/hdr.txt" --max-time 30 --unix-socket "$U" -X "$1")
  [ -n "${4:-}" ] && args+=(-H "X-Real-IP: $4")
  [ -n "${5:-}" ] && args+=(-H "$5")
  if [ -n "${3:-}" ]; then
    args+=(-H 'Content-Type: application/json' --data "$3")
  fi
  curl "${args[@]}" "$2" >/dev/null
  grep -i '^set-cookie' "$SB/hdr.txt" | tr -d '\r' | head -1
}
code() { echo "${1%% *}"; }
tok() { /usr/bin/python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("write_token",""))' "$CFG" 2>/dev/null; }

SRCJ='{"primary":[],"fallback":[],"js":[]}'
PASS=0; FAIL=0
chk() { # $1=期望码 $2=实际  $3=说明
  if [ "$(code "$2")" = "$1" ]; then echo "  ✓ $3  → $(code "$2")"; PASS=$((PASS+1));
  else echo "  ✗ $3  期望 $1 实到 $(code "$2")"; echo "      体: $(echo "$2" | cut -c1-220)"; FAIL=$((FAIL+1)); fi
}
ok()  { echo "  ✓ $1"; PASS=$((PASS+1)); }
bad() { echo "  ✗ $1"; FAIL=$((FAIL+1)); }

echo "### A) 全新安装（conf/config.json 不存在）"
rm -f "$CFG"
r=$(hit POST "$API/sources" "$SRCJ" "10.99.99.5");        chk 200 "$r" "局域网 10.99.99.5 保存"
[ -f "$CFG" ] && bad "内网放行不该写 config.json" || ok "内网放行没写 config.json"

echo
echo "### B) 公网来源 + 首次信任（自动信任一次并下发浏览器凭证）"
r=$(hit POST "$API/sources" "$SRCJ" "198.51.100.10");     chk 200 "$r" "公网 198.51.100.10 第一次保存（首次信任）"
echo "  config.json:"; sed -n '1,60p' "$CFG" | sed 's/^/    /'
TOKEN=$(tok)
[ -n "$TOKEN" ] && ok "已下发 write_token（${TOKEN:0:8}…）" || bad "config.json 里没有 write_token"
sc=$(hdr POST "$API/sources" "$SRCJ" "198.51.100.10")
echo "  Set-Cookie: ${sc:-（无）}"
echo "$sc" | grep -q "trim_write=$TOKEN" && ok "可信请求的响应里带 Set-Cookie" || bad "可信请求没下发 cookie"

echo
echo "### C) 凭证：换 IP / 换网络也不受影响（这次要修的就是它）"
r=$(hit POST "$API/sources" "$SRCJ" "198.51.100.8");            chk 403 "$r" "另一个公网 198.51.100.8（没凭证）被挡"
r=$(hit POST "$API/sources" "$SRCJ" "198.51.100.8" "Cookie: trim_write=$TOKEN");  chk 200 "$r" "198.51.100.8 带上凭证 → 放行（与 IP 无关）"
r=$(hit POST "$API/sources" "$SRCJ" "203.0.113.7" "Cookie: trim_write=$TOKEN"); chk 200 "$r" "你那个 203.0.113.7 + 凭证 → 放行"
r=$(hit POST "$API/sources" "$SRCJ" "198.51.100.8" "Cookie: trim_write=deadbeef"); chk 403 "$r" "凭证不对 → 挡"
r=$(hit POST "$API/config" '{"write_allow_any":true}' "198.51.100.8");            chk 403 "$r" "公网没凭证不能自己开 write_allow_any"
r=$(hit POST "$API/config" '{"write_allow_any":true}' "198.51.100.8" "Cookie: trim_write=$TOKEN"); chk 200 "$r" "持凭证的公网可以开（=你自己那台浏览器）"
hit POST "$API/config" '{"write_allow_any":false}' >/dev/null
r=$(hit POST "$API/sources" "$SRCJ" "198.51.100.10");     chk 200 "$r" "已进白名单的 198.51.100.10 再保存"

echo
echo "### C2) 陌生人打开配置页拿不到凭证"
sc=$(hdr GET "http://x/v/__mitm__/config" "" "198.51.100.8")
[ -z "$sc" ] && ok "公网无凭证 GET 页面 → 不下发 cookie" || bad "陌生人 GET 也拿到了 cookie：$sc"
sc=$(hdr GET "http://x/v/__mitm__/config" "" "10.99.99.5")
echo "$sc" | grep -q "trim_write=$TOKEN" && ok "内网 GET 页面 → 下发 cookie" || bad "内网 GET 没下发 cookie"

echo
echo "### D) 本机 unix socket 永远放行"
r=$(hit POST "$API/config" '{"write_allow_any":true}');    chk 200 "$r" "无 X-Real-IP → 放行并写入"
r=$(hit POST "$API/sources" "$SRCJ" "198.51.100.8");            chk 200 "$r" "开了不限制来源后，198.51.100.8 也能写"
r=$(hit POST "$API/config" '{"write_allow_any":false}');   chk 200 "$r" "再关掉"
r=$(hit POST "$API/sources" "$SRCJ" "198.51.100.8");            chk 403 "$r" "关掉后 198.51.100.8 又被挡（没凭证）"

echo
echo "### E) 网段判定：IPv6 / 叠加网 / IPv4-mapped"
chk 200 "$(hit POST "$API/sources" "$SRCJ" "fd00::1")"           "IPv6 ULA fd00::1"
chk 200 "$(hit POST "$API/sources" "$SRCJ" "fe80::1")"           "IPv6 链路本地 fe80::1"
chk 200 "$(hit POST "$API/sources" "$SRCJ" "::1")"               "IPv6 回环 ::1"
chk 200 "$(hit POST "$API/sources" "$SRCJ" "100.64.0.9")"   "叠加网 100.64.0.9"
chk 200 "$(hit POST "$API/sources" "$SRCJ" "::ffff:10.99.99.9")" "IPv4-mapped 内网"
chk 403 "$(hit POST "$API/sources" "$SRCJ" "2001:db8:1234::9")" "公网 IPv6 2001:db8:1234::9"
chk 403 "$(hit POST "$API/sources" "$SRCJ" "203.0.113.9")"       "公网 IPv4 203.0.113.9"

echo
echo "### F) 白名单支持 CIDR / 单个 IP"
r=$(hit POST "$API/config" '{"write_allow_ip":["203.0.113.0/24","198.51.100.7","2001:db8::/32"]}'); chk 200 "$r" "写白名单"
chk 200 "$(hit POST "$API/sources" "$SRCJ" "203.0.113.9")"       "203.0.113.9（命中 /24）"
chk 200 "$(hit POST "$API/sources" "$SRCJ" "198.51.100.7")"      "198.51.100.7（单个）"
chk 200 "$(hit POST "$API/sources" "$SRCJ" "2001:db8:dead:beef::1")" "2001:db8::/32（v6 网段）"
chk 403 "$(hit POST "$API/sources" "$SRCJ" "198.51.100.9")"       "198.51.100.9（不在网段、没凭证）"

echo
echo "### G) 配置页拿得到写状态"
hit GET "$API/sources" "" "198.51.100.8" >/dev/null
/usr/bin/python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("    write =", json.dumps(d["write"], ensure_ascii=False))' "$SB/out.txt"
hit GET "$API/config" "" "198.51.100.8" >/dev/null
/usr/bin/python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("    （无凭证）allowed=%s ip=%s why=%s allow_any=%s token_set=%s browser_bound=%s" % (d["allowed"],d["ip"],d["why"],d["allow_any"],d["token_set"],d["browser_bound"]))' "$SB/out.txt"
hit GET "$API/config" "" "198.51.100.8" "Cookie: trim_write=$TOKEN" >/dev/null
/usr/bin/python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("    （带凭证）allowed=%s why=%s browser_bound=%s" % (d["allowed"], d["why"], d["browser_bound"]))' "$SB/out.txt"

echo
echo "### H) 随包模板 = 代码默认值"
PYTHONPATH="$LIVE" /usr/bin/python3 - <<PY
import json, sys
sys.path.insert(0, "$SB")
import mitm
tpl = json.load(open("/root/fpk/src/trimonline/app/mitm/conf/config.json"))
dft = mitm.DEFAULT_CONFIG
print("  ✓ 模板与默认值一致" if tpl == dft else "  ✗ 不一致:\n    模板=%s\n    默认=%s" % (
      json.dumps(tpl, ensure_ascii=False), json.dumps(dft, ensure_ascii=False)))
PY

echo
echo "### I) 页面元素"
curl -s --max-time 20 --unix-socket "$U" http://x/v/__mitm__/config > "$SB/page.html"
for k in "写操作来源" "btnAllowIp" "btnAllowAny" "wgtInfo" "UNLOCK_CMD" "write_token" \
         "这台浏览器带写凭证" "addCms" "addJs" "把可用的全部加进去"; do
  grep -q "$k" "$SB/page.html" && ok "页面含 $k" || bad "页面缺 $k"
done
c=$(grep -c "只允许从内网访问配置页" "$SB/page.html")
[ "$c" = "0" ] && ok "旧文案（只允许从内网访问配置页）出现 0 次" || bad "旧文案还在（$c 次）"

echo
echo "### 汇总: 通过 $PASS / 失败 $FAIL"
PAT=$(echo Ym9vdC5weQ== | base64 -d); pkill -f "$PAT" 2>/dev/null
sleep 1
echo "残留: $(ps aux | grep -cE '[b]oot\.py')"
