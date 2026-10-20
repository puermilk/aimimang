#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 drpy2 运行时所需的所有 ESM/UMD 模块拼成一个可被 QuickJS 执行的脚本。

产物： src/trimonline/app/mitm/runtime/runtime_bundle.js
素材： vendor/drpy/*.js        （来自 fantaiying7/EXT 与 Hululu007/drpy-node）

为什么需要这个拼装：
  · QuickJS（Python 绑定）不支持 ES module，只能跑普通脚本，所以要把
    import/export 抹掉、改成挂到 globalThis 上；
  · htmlParser.js 与 drpy2.js 都声明了顶层 const SPECIAL_URL / NOADD_INDEX /
    URLJOIN_ATTR，直接拼会 "invalid redefinition of lexical identifier"，
    所以每个模块单独包一层 IIFE。
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "drpy")
OUT = os.path.join(HERE, "..", "src", "trimonline", "app", "mitm", "runtime",
                   "runtime_bundle.js")


def read(p):
    return open(os.path.join(SRC, p), encoding="utf-8", errors="replace").read()


def strip_imports(src):
    src = re.sub(r'(?m)^\s*import\s+[^;\n]*?from\s*["\'][^"\']+["\']\s*;?\s*$', '', src)
    src = re.sub(r'(?m)^\s*import\s*["\'][^"\']+["\']\s*;?\s*$', '', src)
    # drpy2.js 是压成一行的，行内 import 也要处理（注意 模板 是 CJK 标识符）
    src = re.sub(r'import\s+[^\s;{}]+(?:\s*,\s*\{[^}]*\})?\s+from\s*["\'][^"\']+["\']\s*;?', '', src)
    src = re.sub(r'import\s*\{[^}]*\}\s*from\s*["\'][^"\']+["\']\s*;?', '', src)
    src = re.sub(r'import\s*["\'][^"\']+["\']\s*;?', '', src)
    return src


def esm_to_global(src, name):
    """ESM 的 export → globalThis.<name> 赋值"""
    def _named(m):
        pairs = []
        for it in m.group(1).split(','):
            it = it.strip()
            if not it:
                continue
            mm = re.match(r'^(\S+)\s+as\s+(\S+)$', it)
            pairs.append(("%s:%s" % (mm.group(2), mm.group(1))) if mm
                         else ("%s:%s" % (it, it)))
        return "globalThis.%s={%s};" % (name, ",".join(pairs))
    out = re.sub(r'export\s*\{([^}]*)\}\s*;?', _named, src)
    out = re.sub(r'\bexport\s+default\s*', 'globalThis.%s=' % name, out)
    out = re.sub(r'\bexport\s+(function|const|let|var|class)\b', r'\1', out)
    return out


def cheerio_as_global():
    """cheerio.min.js 是 ESM（尾部 export{...}），转成 globalThis.cheerio 普通对象。
    必须是普通对象而不是 ESM 命名空间：drpy2 会改写 cheerio.jinja2。"""
    src = read("cheerio.min.js")
    m = re.search(r'export\s*\{([^}]*)\}\s*;?\s*$', src)
    body = src[:m.start()] if m else src
    pairs = []
    for it in (m.group(1) if m else "").split(','):
        it = it.strip()
        if not it:
            continue
        mm = re.match(r'^(\S+)\s+as\s+(\S+)$', it)
        pairs.append((mm.group(2), mm.group(1)) if mm else (it, it))
    obj = ",".join("%s:%s" % (k, v) for k, v in pairs)
    return body + "\n;\nglobalThis.cheerio={%s};\n" % obj


def jsoup_as_global():
    """htmlParser.js（海阔视界 Jsoup 语法）→ globalThis.__Jsoup"""
    jsoup = strip_imports(read("jsoup.js"))
    jsoup = jsoup.replace("export const jsoup = Jsoup;", "")
    jsoup = jsoup.replace("export const jsonpath", "const jsonpath")
    jsoup = re.sub(r'^// export default Jsoup;', '', jsoup, flags=re.M)
    # 补回被剥掉的 urljoin / JSONPath
    return ("const urljoin=function(b,u){return globalThis.joinUrl(b,u)};\n"
            "const JSONPath=globalThis.JSONPath||undefined;\n" + jsoup +
            "\n;\nglobalThis.__Jsoup=Jsoup;\n")


def build():
    parts, notes = [], []

    def add(code, tag):
        parts.append(";(function(){\n%s\n})();\n" % code)
        notes.append(tag)

    add(cheerio_as_global(), "cheerio -> globalThis.cheerio")
    add(read("crypto-js.js"), "crypto-js -> CryptoJS")
    add(read("jinja.js"), "jinja -> jinja")
    add(read("uri.min.js"), "uri -> Uri")
    add(read("json5.js"), "json5 -> JSON5")
    add(read("pako.min.js"), "pako -> pako")
    add(read("node-rsa.js"), "node-rsa -> NODERSA")
    add(esm_to_global(strip_imports(read("模板.js")), "模板"), "模板 -> globalThis.模板")
    add(esm_to_global(strip_imports(read("gbk.js")), "gbkTool") +
        "\n;\nglobalThis.gbkTool=gbkTool;", "gbk -> gbkTool")
    add(jsoup_as_global(), "htmlParser(Jsoup) -> globalThis.__Jsoup")

    drpy = strip_imports(read("drpy2.js"))
    n = len(re.findall(r'let d=\[\]', drpy))
    # 站点 js: 片段常写 var d=[]；drpy 自己若用 let d 会与之在 direct-eval 同作用域冲突
    drpy = drpy.replace('let d=[]', 'var d=[]')
    notes.append("drpy2 补丁: let d=[] -> var d=[]（%d 处）" % n)
    add(esm_to_global(drpy, "__drpy"), "drpy2 -> globalThis.__drpy")

    code = "\n;\n".join(parts)
    out = os.path.normpath(OUT)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(code)
    print("bundle -> %s  (%.1f KB)" % (out, len(code.encode()) / 1024))
    for n2 in notes:
        print("   ", n2)
    return out


if __name__ == "__main__":
    build()
