#!/bin/bash
# 从已部署环境的 venv 里抽出运行所需的第三方包，做成纯目录 app/mitm/lib。
#
# 为什么不用 venv：
#   venv 必然带一个指向 /usr/bin/python3 的【绝对符号链接】，
#   部分 fnOS 版本的应用中心解包器会因这种条目直接报「解压 app.tgz 失败」。
#   纯目录 + PYTHONPATH 不含任何符号链接，也不依赖 venv 里写死的路径。
#
# 依赖清单来自实测：mitm.py 的第三方 import 是 aiohttp + quickjs，
# 其余全是标准库（asyncio / json / urllib / hashlib / ssl / shutil …）。
#
# 用法：bash make_lib.sh [site-packages路径]
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="${1:-/usr/local/trim_media_mitm/venv/lib/python3.11/site-packages}"
DST="$HERE/app/mitm/lib"

[ -d "$SRC" ] || { echo "找不到源 site-packages：$SRC" >&2; exit 1; }

rm -rf "$DST"
mkdir -p "$DST"

# aiohttp 及其依赖（含 C 扩展）
PKGS="aiohappyeyeballs aiohttp aiosignal attr attrs frozenlist idna multidict propcache yarl"
for p in $PKGS; do
    cp -a "$SRC/$p" "$DST/" 2>/dev/null || true
done
cp -a "$SRC/typing_extensions.py" "$DST/" 2>/dev/null || true

# 保留 dist-info：部分库会用 importlib.metadata 读自己的版本
for p in $PKGS; do
    for d in "$SRC/$p"-*.dist-info; do
        [ -d "$d" ] && cp -a "$d" "$DST/"
    done
done

# QuickJS：JS 蜘蛛（drpy2）引擎本体。只带 cp311 / x86_64 的 manylinux 扩展，
# 与 fnOS 自带的 /usr/bin/python3（3.11）匹配；不匹配时 jsspider 会优雅降级。
# 素材位置可覆盖：bash make_lib.sh [site-packages] [quickjs素材目录]
VENDOR_QJS="${2:-}"
if [ -n "$VENDOR_QJS" ] && [ ! -d "$VENDOR_QJS/quickjs" ]; then
    echo "警告：指定的 quickjs 素材目录里没有 quickjs 包：$VENDOR_QJS" >&2
    VENDOR_QJS=""
fi
if [ -z "$VENDOR_QJS" ]; then
    for cand in "$HERE/../../vendor/quickjs" \
                "$HERE/../../../fpk/vendor/quickjs" "/root/fpk/vendor/quickjs"; do
        if [ -d "$cand/quickjs" ]; then VENDOR_QJS="$cand"; break; fi
    done
fi
if [ -n "$VENDOR_QJS" ] && [ -d "$VENDOR_QJS/quickjs" ]; then
    cp -a "$VENDOR_QJS/quickjs" "$DST/"
    cp -a "$VENDOR_QJS"/_quickjs*.so "$DST/" 2>/dev/null || true
    echo "已并入 quickjs（JS 蜘蛛引擎）← $VENDOR_QJS"
elif compgen -G "$SRC/_quickjs*.so" >/dev/null; then
    cp -a "$SRC"/_quickjs*.so "$DST/" 2>/dev/null || true
    [ -d "$SRC/quickjs" ] && cp -a "$SRC/quickjs" "$DST/"
    echo "已从 site-packages 并入 quickjs"
else
    echo "警告：没找到 quickjs —— JS 源将不可用（其余功能不受影响）" >&2
fi

find "$DST" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true

echo "lib 体积：$(du -sh "$DST" | awk '{print $1}')  条目数：$(find "$DST" | wc -l)"

# 自检：用【系统 python3 + PYTHONPATH】真起一个 aiohttp 服务端 + 客户端
PYTHONPATH="$DST" /usr/bin/python3 - <<'PY'
import asyncio, aiohttp
from aiohttp import web
print("aiohttp", aiohttp.__version__)

async def h(_r):
    return web.Response(text="ok")

async def main():
    app = web.Application()
    app.router.add_get("/", h)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 18099).start()
    async with aiohttp.ClientSession() as s:
        async with s.get("http://127.0.0.1:18099/",
                         timeout=aiohttp.ClientTimeout(total=5)) as r:
            assert await r.text() == "ok"
    await runner.cleanup()

asyncio.run(main())
print("自检通过：系统 python3 + PYTHONPATH 可正常使用 aiohttp")
PY

# 硬性断言：lib 内不允许有任何符号链接（否则又会被解包器拒绝）
if find "$DST" -type l | grep -q .; then
    echo "错误：lib 内仍有符号链接——" >&2
    find "$DST" -type l >&2
    exit 1
fi
echo "确认：lib 内无任何符号链接"

# 自检：JS 蜘蛛引擎（quickjs + drpy2 运行时）能否真起一个上下文
RUNTIME="$HERE/app/mitm/runtime"
if compgen -G "$DST/_quickjs*.so" >/dev/null; then
    PYTHONPATH="$DST" /usr/bin/python3 - "$RUNTIME" <<'PY'
import sys, os
import quickjs
ctx = quickjs.Context()
ctx.eval("var f=(a)=>a?.b ?? 'ES2020-OK';")
print("quickjs:", ctx.eval("f({b:'ES2020-OK'})"))
rt = sys.argv[1]
bundle = os.path.join(rt, "runtime_bundle.js")
assert os.path.exists(bundle), bundle
print("runtime_bundle.js %.1f KB" % (os.path.getsize(bundle) / 1024.0))
print("自检通过：quickjs 可用、运行时齐备")
PY

    # 自检：配置页内嵌 JS 的【语法】。
    # 踩过的坑：webui.py 的页面字符串里少写一个花括号 → 整段 <script> 解析失败 →
    # 「引擎一直显示检测中、所有按钮都没反应」，而后端接口一切正常，从日志里看不出来。
    # 所以这里把它当作硬门禁：解析不过就中断打包。
    PYTHONPATH="$DST" /usr/bin/python3 - "$HERE/app/mitm" <<'PY'
import re, sys
import quickjs
d = sys.argv[1]
sys.path.insert(0, d)
import webui
blocks = re.findall(r"<script>(.*?)</script>", webui.PAGE, re.S)
assert len(blocks) == 1, "页面里的 <script> 块数不是 1（%d 个）" % len(blocks)
js = blocks[0]
# 包成函数体只做解析、不执行（执行会因缺 document 而报错）
quickjs.Context().eval("var __syntax_only = function(){\n" + js + "\n};")
print("页面 JS 语法自检通过（%d 字符）" % len(js))
PY
else
    echo "注意：未带 quickjs，跳过 JS 引擎自检（JS 源不可用，其余不受影响）"
fi

# 自检会重新生成 __pycache__，打完包前清掉（只留源码，减小体积）
find "$DST" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
