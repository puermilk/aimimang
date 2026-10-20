#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置页前端自检：语法 + 关键流程（引擎检测 / 表格渲染 / 加源 / 导入 / 加入）。

为什么要它：配置页的 JS 是嵌在 webui.py 里的，Python 语法检查管不到它。
一旦有个花括号写漏，整段脚本解析失败 —— 表现是「引擎一直显示检测中、
所有按钮都没反应」，从后端一点都看不出来（接口全是好的）。这里用 QuickJS
配一个迷你 DOM 把页面脚本真跑一遍，跑不通就出声。

用法：PYTHONPATH=<含 quickjs 的 lib> python3 check_page.py
退出码非 0 = 有问题。
"""
import os
import re
import sys

MITM_DIR = os.environ.get("MITM_DIR",
                          "/root/fpk/src/trimonline/app/mitm")
sys.path.insert(0, MITM_DIR)

import quickjs                              # noqa: E402
import webui                                # noqa: E402

blocks = re.findall(r"<script>(.*?)</script>", webui.PAGE, re.S)
if len(blocks) != 1:
    print("✗ 页面里的 <script> 块不是 1 个（%d 个）—— 内嵌字符串里可能有 "
          "</script> 把页面截断了" % len(blocks))
    sys.exit(1)
js = blocks[0]

# ---------------------------------------------------------------- 迷你 DOM ---
HARNESS = r"""
var __els = {};
function __El(id){
  return {id:id, innerHTML:"", textContent:"", value:"", disabled:false,
          style:{}, className:"", onclick:null, oninput:null, onchange:null};
}
var document = {getElementById: function(id){
  if (!__els[id]) __els[id] = __El(id);
  return __els[id];
}};
var window = {};

// 同步 thenable：让 fetch 的 .then 链在同一个 tick 里跑完，不用等微任务
var __lastErr = "";
function T(v){ this.v = v; }
T.prototype.then = function(cb){
  var r;
  try { r = cb ? cb(this.v) : this.v; }
  catch (e) { __lastErr = String(e) + " @@ " + (e && e.stack ? e.stack : ""); throw e; }
  while (r instanceof T) r = r.v;
  return new T(r);
};
T.prototype.catch = function(cb){
  if (__lastErr && cb) { try { cb(new Error(__lastErr)); } catch (e) {} }
  return this;
};

var __routes = {};
function fetch(path, opt){
  if (!(path in __routes)) throw new Error("自检里没有准备这个接口：" + path);
  var d = __routes[path];
  return new T({ok:true, status:200, headers:{get:function(){return null;}},
                json:function(){ return new T(d); }});
}
__routes["/v/__mitm__/api/sources"] = {
  ok:true, msg:"", primary:[], fallback:[], js:[],
  write:{allowed:true, ip:"10.99.99.5", why:"内网 / 叠加网地址", allow_any:false,
         auto_trust_first:true, first_trusted:false, browser_bound:false,
         token_set:false, config_file:"/tmp/conf/config.json"},
  engine:{available:true, why:"drpy2 + cheerio + QuickJS 就绪",
          engine:"QuickJS + drpy2", stats:{calls:0, errors:0, contexts:0}}
};
__routes["/v/__mitm__/api/import"] = {
  ok:true,
  kept:[{name:"测试采集", label:"普通采集", api:"http://a/api.php/provide/vod/",
         referer:"http://a/"}],
  skipped:[{name:"豆豆片单", label:"豆豆", api:"csp_DouDouGuard", referer:"",
            why:"加密 JAR 蜘蛛（需要原厂 Java 运行时，已剔除）"},
           {name:"夸克", label:"网盘", api:"https://pan.quark.cn/x", referer:"",
            why:"网盘类站点"}],
  js:[{name:"兔小贝JS", label:"兔小贝", js:"http://b/兔小贝.js", referer:"http://b/",
       lines:"auto"}],
  meta:{spider:"http://x/spider.jpg;md5;x", sites:3, kept:1, skipped:2, js:1,
        why:[["加密 JAR 蜘蛛（需要原厂 Java 运行时，已剔除）", 1], ["网盘类站点", 1]]},
  note:"域名里的中文已转成 punycode 再请求：www.饭太硬.cc → www.xn--sss604efuw.cc"
};
"""

TAIL = r"""
// -------------------------------------------------------------- 自检本体 ---
var __out = [];
function E(id){ return document.getElementById(id); }   // 取不到就现造一个
function ck(cond, msg){ __out.push((cond ? "  ✓ " : "  ✗ ") + msg); }

// 1) load() 有没有跑完：引擎标签应该已经被更新（初始文案是「引擎检测中…」）
var eg = E("jsEngine").textContent || "";
ck(eg.indexOf("检测中") < 0 && eg.length > 0,
   "load() 跑完了，引擎标签 = " + eg.slice(0, 70) + "   [load 里的异常: " + __lastErr.slice(0, 200) + "]");

// 2) 主用源卡片渲染出来了（「+ 加主用源」按钮是 draw() 渲染的）
ck((E("tblPrimary").innerHTML || "").length > 0, "主用源表格已渲染（draw() 跑到过）");
ck(!!E("btnAddP").onclick, "「+ 加主用源」按钮已绑定处理函数");
ck(!!E("btnAddF").onclick, "「+ 加兜底源」按钮已绑定处理函数");
ck((E("cli").textContent || "").indexOf("write_token") >= 0, "命令行等价操作区渲染了");
ck((E("wgtInfo").innerHTML || "").length > 0, "「写操作来源」卡片渲染出来了");

// 3) 点「+ 加主用源」/「+ 加 JS 源」要有反应
var n0 = S.primary.length;
E("btnAddP").onclick.call(E("btnAddP"));
ck(S.primary.length === n0 + 1, "点「+ 加主用源」加进去了一行");
var j0 = S.js.length;
E("btnAddJ").onclick.call(E("btnAddJ"));
ck(S.js.length === j0 + 1, "点「+ 加 JS 源」加进去了一行");

// 4) 导入流程
E("cfgurl").value = "http://example/tv";
E("btnImport").onclick.call(E("btnImport"));
var oh = E("importOut").innerHTML || "";
ck(oh.indexOf("导入失败") < 0, "导入没报错");
ck(oh.indexOf("采集接口 1 个") >= 0, "输出区显示采集接口计数");
ck(oh.indexOf("JS 源 1 个") >= 0, "输出区显示 JS 源计数");
ck(oh.indexOf("addCms(IMP.kept[0])") >= 0, "每条采集源后面有「加入」按钮");
ck(oh.indexOf("addJs(IMP.js[0])") >= 0, "每条 JS 源后面有「加入」按钮");
ck(oh.indexOf("punycode") >= 0, "note（中文域名转码提示）显示了");
ck(oh.indexOf("加密 JAR 蜘蛛」是什么") >= 0, "加密 JAR 的解释段显示了");

// 5) 单条「加入」
var a0 = S.primary.length;
addCms(window.IMP.kept[0]);
ck(S.primary.length === a0 + 1, "单条「加入」能加采集源");
addCms(window.IMP.kept[0]);
ck(S.primary.length === a0 + 1, "重复「加入」不会重复加");
var b0 = S.js.length;
addJs(window.IMP.js[0]);
ck(S.js.length === b0 + 1, "单条「加入」能加 JS 源");

// 6) 「全部加进去」
var l0 = S.primary.length + S.js.length;
E("btnAddAll").onclick.call(E("btnAddAll"));
ck(S.primary.length + S.js.length >= l0, "「全部加进去」没炸");
ck((E("log").textContent || "").indexOf("点「保存并测速」生效") >= 0,
   "底部日志有反馈：" + (E("log").textContent || "").slice(0, 60));

ck(!!E("btnSave").onclick, "「保存并测速」按钮有绑定处理函数");
ck(!!E("btnTest").onclick, "「只测速不保存」按钮有绑定处理函数");

__out.join("\n");
"""

ctx = quickjs.Context()
try:
    res = ctx.eval(HARNESS + "\n" + js + "\n" + TAIL)
except Exception as e:
    msg = str(e)
    print("✗ 页面脚本跑不起来：%s" % msg[:600])
    sys.exit(1)

text = res if isinstance(res, str) else str(res)
print(text)
fails = text.count("✗")
print("—— 失败 %d 项" % fails)
sys.exit(1 if fails else 0)
