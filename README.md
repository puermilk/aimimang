# 爱迷茫 · 飞牛影视在线源插件（FPK）

> 内部包名仍是 `trimonline`（这样升级安装能认出旧版本，不会装成两个应用）。
> 在应用中心里显示的名字是 **爱迷茫**。当前版本 **1.3.0**。
>
> **下载安装包**：[`dist/爱迷茫-1.3.0.fpk`](dist/爱迷茫-1.3.0.fpk)（[校验和](dist/爱迷茫-1.3.0.fpk.sha256)）
> **使用前必读**：[免责声明](免责声明.md) · [致谢与第三方组件](THIRD_PARTY_NOTICES.md)

为飞牛影视（`trim.media`）增加 TVBox 式在线片源：**搜索 → 点开 → 选集 → 播放**，
不下载到 NAS，Web / TV / 手机端通用；原有的本地媒体库、播放记录、继续播放全部照旧。

**本版起自带 JS 蜘蛛引擎**（QuickJS + drpy2，已打进安装包）：
TVBox 配置里 `type: 3` 的 drpy 规则站点也能真跑起来，不用装 Java / Node。
**采集接口 JSON / XML 都认**：苹果CMS 那套 `?ac=videolist&wd=` 的 XML 形式
（`…/at/xml/`）一样能搜、能出片源。

原理：接管 `/var/run/trim_media.sock`（把官方后端原本的 socket 改名为
`trim_media.real.sock` 保管），MITM 绑回原路径，按需把请求转发给官方后端，
并把在线采集站的搜索结果合并进去。

> 不修改官方后端的任何数据与配置；卸载时官方 socket 原样归位。

---

## 免责声明（请先读这段）

- 本插件是一个**播放器侧的中转工具**：它**不含任何影视内容**，也**不内置任何片源**。
- 你在「源设置」里填写的源地址、以及由这些源播放出来的内容，**均由你自己提供**。
  请只接入**你有权访问**的接口，并遵守当地法律法规与源站的服务条款。
- 作者**不对任何第三方源的合法性、可用性、稳定性与内容负责**，也不提供任何源。
- 插件不会上传你的媒体库信息；所有源配置都只存在你 NAS 本地的
  `/usr/local/trim_media_mitm/conf/sources.json`。
- 配置页（写操作）默认走**「信任这台浏览器」**：第一次改配置时自动信任一次并给这台浏览器
  发一张写凭证（cookie），之后换网络、宽带重播号、走飞牛远程访问（fnconnect）都还能改；
  也可改成白名单 / 不限制来源，见下面的「配置页存不进去怎么办」。
- 如不同意以上任何一条，请不要安装或使用本插件。

---

## 文件

| 路径 | 说明 |
|---|---|
| `dist/爱迷茫-1.3.0.fpk` | 最新安装包（自带 JS 蜘蛛引擎） |
| `dist/爱迷茫-1.3.0.fpk.sha256` | 校验和 |
| `dist/SHA256SUMS.txt` | 全部版本的校验清单 |
| `1.3.0-更新说明.md` | 这一版：查询方式可选 + 用户自选启用源数量 |
| `1.2.9-更新说明.md` | 上一版：本地播放记录恢复 + 在线源按用户可见 |
| `1.2.8-更新说明.md` | 更早：移除非凡硬编码偏好与相关示例 |
| `1.2.7-更新说明.md` | 再上一版：manifest 依赖声明 + 安装向导免责声明确认 |
| `1.2.6-更新说明.md` | 更早：总时长 / 硬解码提示 / 实测流参数 |
| `1.2.5-更新说明.md` | 更早：XML 接口 / 跳过明细 / 新图标 |
| `1.2.4-更新说明.md` | 更早：JS 蜘蛛引擎 |
| `1.1.0-更新说明.md` | 最早的公开版本说明 |
| `源的要求与例子.md` | 换源前先看这个（采集源 + JS 源两种写法） |
| `免责声明.md` | 单独一份，方便随包一起发 |
| `THIRD_PARTY_NOTICES.md` | 第三方组件、作者与许可证（**致谢**） |
| `LICENSE` | GPL-3.0（因为打包了 GPL-3.0 的 drpy2 运行时） |
| `src/trimonline/` | 可重建的源码树（不含运行库） |
| `vendor/` | 构建期素材：drpy2 运行时 + QuickJS（不进 FPK 源码树） |
| `tools/build_fpk.py` | 打包脚本（产出 `dist/爱迷茫-<版本>.fpk`） |

---

## 安装

**方式一 · 命令行**

```bash
appcenter-cli install-fpk /vol1/1000/MUSCI/爱迷茫-1.3.0.fpk    # 换成你自己的实际路径
```

**方式二 · 应用中心界面**

应用中心 → 侧边栏底部「手动安装」→ 选择 `爱迷茫-1.3.0.fpk`

> 安装向导会先要求阅读并勾选《免责声明》；未勾选不会继续安装。
> 插件依赖官方「飞牛影视（trim.media）」；未安装或未启动时，安装会明确提示先启动它。
> 安装会重启一次接管服务，飞牛影视中断几秒属正常。
> 如果机器上已经手工部署过同一套接管脚本，安装是**幂等收编**，不会冲突。
> 安装过程会要求选择安装位置（存储空间），与其它第三方应用一致。

---

## 装完第一件事：填源（本插件不自带任何源）

打开配置页 —— 桌面上的「爱迷茫」图标就是这个页面，也可以直接开网址：

```
http://<NAS 的 IP>:5666/v/__mitm__/config
```

页面里能干这些事：

| 功能 | 说明 |
|---|---|
| **导入 TVBox 配置地址** | 粘贴一份配置地址，自动挑出其中**能直接调用**的明文采集接口，**以及 type:3 的 drpy JS 蜘蛛**；网盘类、加密 JAR 会跳过并写明原因 |
| **主用源 / 兜底源** | 两组分开摆；可在「查询方式」里选择主用优先或全部同时 |
| **查询方式与启用数量** | `主用优先`：先查主用、命中即停（旧行为）；`全部同时`：主用 + 兜底一起查，所有启用的源都参与搜索和换源。采集源 / JS 源都能设「最多启用几个」（0=不限） |
| **JS 源（drpy）** | TVBox 配置里 `type:3` 的那批站点。只填**规则文件地址**（配置里的 `ext`），运行时已内置 |
| **单线路 / 多线路** | 每个源可以标「单线路 / 多线路 / 自动」。只影响分组显示与推荐排序，不影响播放逻辑 |
| **保存并测速** | 保存后**立刻**逐个测：响应时间、命中数、能出几条可放线路；JS 源额外报「分类数 / 搜索命中」 |
| **推荐方案** | 按「能放线路数优先、响应时间其次」给出建议启用哪些源，数量跟随你设置的采集源上限，可**一键应用** |

也可以在命令行里等价操作：

```bash
# 看当前生效的源
curl -s --unix-socket /var/run/trim_media.sock http://x/v/__mitm__/sources
# 热加载（配置页保存时已自动做）
curl -s --unix-socket /var/run/trim_media.sock 'http://x/v/__mitm__/sources?reload=1'
# 命令行测速
python3 /usr/local/trim_media_mitm/source_check.py --keyword 凡人
```

配置写错时**不会**顶掉正在用的源，会返回「读取失败，保持现有源不变（原因）」。
**升级应用不会覆盖你改过的 `sources.json`**，每日维护（00:20）会顺带重载一次。

### 在线源给谁看（按飞牛影视账号）

配置页里有一张「在线源可见范围」卡片：

| 模式 | 效果 |
|---|---|
| **仅管理员（默认）** | 只有飞牛影视管理员能看到在线搜索结果、豆瓣伪库和在线继续播放 |
| **所有用户** | 所有飞牛影视账号都能看到（旧版行为） |
| **指定用户** | 管理员 + 勾选的账号能看到 |

- 身份来自飞牛影视自己的登录 token（`Trim-MC-token`）→ `trimactivity.db.user_token`
  → `trimmedia.db.user`，插件只读，不改官方账号数据。
- 只管**在线源**。本地媒体库、本地播放记录、本地继续播放不受影响。
- 在线继续播放也按用户隔离：每个账号只看到自己的在线进度。
- 升级后默认「仅管理员」。如果原来全家都在用在线源，去配置页改成「所有用户」，
  或在「指定用户」里勾选家人即可。
- 旧版 `watch_history.json` 没有用户字段，升级后只给管理员看，不会暴露给其他账号。

### 配置页存不进去怎么办（提示「不允许从当前来源改配置」）

为了避免把 5666 映射到公网后被人乱改源，**配置页的写操作（保存 / 导入）默认只放行**：
内网 IPv4 私网段、回环、链路本地、`100.64/10`（Tailscale / ZeroTier 这类叠加网）、
IPv6 的 ULA（`fc00::/7`）与链路本地（`fe80::/10`）、以及你写在白名单里的地址。

所以如果你是这样打开配置页的，**可能会被挡**：公网域名 / DDNS、飞牛远程访问（fnconnect）、
手机 App、IPv6 公网地址。被挡时页面顶部会有红色横幅，**直接告诉你检测到的来源 IP 和原因**。

有四条路，从省事到彻底：

1. **第一次改配置会自动信任一次，并给这台浏览器发一张写凭证**（`auto_trust_first`，默认开）。
   装完直接点「保存并测速」就行，不用 SSH。凭证存在 `write_token` 里、浏览器那边是个 cookie，
   **跟来源 IP 无关** —— 之后你换网络、宽带重播号、切 4G、走飞牛远程访问，都还是能改。
2. **在页面里自己放行**：点「把当前来源 IP 加进白名单」，或者点「不限制来源：开 / 关」。
   这两个按钮点下去需要「你已经被放行」—— 所以用**之前改过配置的那台浏览器**点最有效。
3. **走本机 socket 解锁**（等于你已经在 NAS 上了，永远放行）——被挡时横幅里给的就是这条：
   ```bash
   curl -s --unix-socket /var/run/trim_media.sock -X POST \
     -H 'Content-Type: application/json' \
     -d '{"write_allow_any":true}' http://x/v/__mitm__/api/config
   ```
4. **直接改文件** `/usr/local/trim_media_mitm/conf/config.json`（改完立即生效，不用重启）：

   ```json
   {
     "write_allow_any": false,
     "write_allow_ip": ["198.51.100.7", "203.0.113.0/24", "2001:db8::/32"],
     "auto_trust_first": true,
     "first_trusted": true,
     "write_token": "（浏览器写凭证，别外传；置空 = 作废并允许重新信任一次）"
   }
   ```

| 字段 | 含义 |
|---|---|
| `write_allow_any` | `true` = 不限制来源，任何地址都能改配置。**5666 别对公网裸奔时再开** |
| `write_allow_ip` | 额外放行的 IP / 网段，支持单个 IP 和 CIDR（IPv4、IPv6 都行） |
| `auto_trust_first` | 本机第一次写操作时信任一次并下发浏览器凭证（默认 `true`）；不想用就设 `false` |
| `write_token` | 发给浏览器的写凭证。删掉/置空 = 作废它，下一次写操作会重新信任一次并换新凭证 |
| `first_trusted` | 上面那次自动信任用掉没有（留档，可改回 `false` 再触发一次） |

> 被挡时想确认现在是哪台浏览器有凭证：配置页「写操作来源」卡片里，被放行的那次会显示
> **「这台浏览器带写凭证」**。别人的浏览器（哪怕同一个 IP）没有这张凭证也改不了。

也可以走环境变量 `TRIM_MITM_WRITE_ANY=1` 强制不限制来源。
**升级应用不会覆盖你改过的 `config.json`。**

### 导入 TVBox 配置地址失败？先说清楚三种情况

| 症状 | 原因 | 本版怎么处理 |
|---|---|---|
| `UnicodeEncodeError('latin-1', 'www.<你的中文域名>.cc', ...)` | `urllib` 只会用 latin-1 编码 Host，中文域名不转 punycode | 先转 punycode 再请求，照常能导入 |
| `JSONDecodeError('Expecting value: line 1 column 1 (char 0)')` | 站点**按 UA 发牌**：用浏览器 UA 去抓，它只回你一句「你好！」（9 字节） | 按 TVBox 客户端的 `okhttp` UA 轮流试，谁给配置用谁 |
| 也是 `JSONDecodeError`，但地址是 `.jpg` / 重定向到图片 | 配置**伪装成图片**：一张真 JPEG，后面贴「随便几位 + `**` + base64(配置)」（可能还套了 gzip） | 按**字节**找最后一个 `**`，解 base64，再认 JSON |
| 提示「没筛出可用源」 | 整份配置的站点全是 `csp_xxxGuard` 这种**加密 JAR 蜘蛛**（或网盘类），一个明文采集接口都没有 | 不是故障：这份配置是给带 Java 运行时的客户端用的。页面会给出**「被跳过的站点明细」**：名称 / 接口 / 凭什么跳过，每条都带「**仍要加入**」按钮，还有「网盘类的也全部加入（N 个）」一键全选；加进来测速 ❌ 也会写明理由 |

顺带：粘贴时带的**全角冒号/斜杠**、零宽字符、首尾空白会自动规整。

### 源体检（先筛出可用源，再决定留谁）

```bash
# 测当前配置里的全部源（默认关键词「凡人」）
python3 /usr/local/trim_media_mitm/source_check.py

# 换关键词 / 换超时
python3 /usr/local/trim_media_mitm/source_check.py --keyword 庆余年 --timeout 25

# 直接测候选接口（不读配置文件）
python3 /usr/local/trim_media_mitm/source_check.py https://x/api.php/provide/vod/
```

输出「搜索耗时 / 命中数 / 可放线路数 / **格式** / 判定」：`✅ 可出片源`、`⚠️ 该关键词无结果`、
`❌ 不可用`、`❌ 搜到但无可放线路`。格式列显示这个接口是 `JSON` 还是 `XML`。
它按 `mitm.py` 的**真实调用**探测，纯标准库，**不需要 PYTHONPATH**。

### 源的查询方式与启用数量

配置页「查询方式与启用数量」卡片对应 `config.json` 里的三个字段：

| 字段 | 值 | 说明 |
|---|---|---|
| `source_mode` | `priority` / `parallel` | `priority` 主用优先（旧行为）：先查主用，命中即停；`parallel` 全部同时：主用 + 兜底一起查 |
| `source_limit` | 0–100 | 最多启用几个采集源；0=不限。按「主用 → 兜底」顺序取前 N 个 |
| `js_source_limit` | 0–50 | 最多同时查几个 JS 源；0=不限。默认 4，避免一次启动太多引擎 |

- 新装模板默认 `parallel`；旧配置没有这三个字段时按 `priority` 走，升级不会突然改变原来的查询节奏。
  想让所有源都生效，去配置页把「查询方式」切到「全部同时」即可。
- `全部同时` 下，搜索和标题解析都会查询所有启用的源；同一部片子的多条线路会合并，
  换源时能看到更多线路。搜索结果是按来源轮流取的，不会让第一个源把前 8 条占满。
- 源越多覆盖越广，但搜索也会更慢；建议先用「保存并测速」筛掉不可用的，再设一个合适的数量。
- `primary` / `fallback` 仍然保留：它们是分组和排序依据，`priority` 下还决定“先查谁、何时查兜底”。

采集站必须支持苹果CMS V10 标准接口：

| 调用 | 用途 |
|---|---|
| `<api>?ac=videolist&wd=关键词` | 搜索 |
| `<api>?ac=detail&ids=<vod_id>` | 详情（搜索结果没带播放地址时补拉） |

响应可以是 **JSON**（顶层 `list` 数组），也可以是 **XML**
（苹果CMS 的 `/at/xml/` 形式：`<rss version="5.1"><list><video>…`）—— 两种由同一份解析器处理
（`source_check.py` 的 `load_payload()`，只用标准库 `xml.etree.ElementTree`；
XML 里带 `<!DOCTYPE` / `<!ENTITY` 的一律不解析，防 XXE）。

条目至少要有 `vod_id` / `vod_name` / `vod_play_url`。
`vod_play_url` 格式：`集名$地址#集名$地址`，多线路以 `$$$` 分隔（XML 里就是
`<dl><dd flag="线路名">…</dd></dl>` 多个 `<dd>`）；地址必须是
**直接可放**的（`.m3u8` / 直链）——`/share/xxx` 这类 HTML 分享页放不了，
所以每个源至少要有一条 m3u8 线路。

配置字段：`name` 名称（同时作为同源去重键）、`api` 采集接口、
`referer` 可选（缺省按 api 域名推导）、`lines` 可选（`single` / `multi` / `auto`）。

> 完整要求与一个可照抄的例子见同目录的 `源的要求与例子.md`。

### JS 源（drpy 蜘蛛）—— 这一版新加的

TVBox 配置里 `type: 3` 的那批 drpy 规则站点，现在**能真跑起来**了：

| 项 | 说明 |
|---|---|
| 引擎 | **QuickJS**（内嵌，约 6.6MB，已打进 FPK）+ drpy2 运行时 + drpy 改造版 cheerio + 海阔视界那套 `pdfh/pdfa/pd` 选择器 |
| 要不要装东西 | **不用**。不用 Java、不用 Node、不用联网 apt；装完即用 |
| 填什么 | 只填**规则文件地址**——就是配置里 `ext` 那一条。运行时（配置里的 `api` → `drpy2.min.js`）是**内置**的，别填 |
| 还能填什么 | `**` 开头的 base64 规则、或者直接以 `var rule={...}` 开头的规则文本，都认 |
| 配置字段 | `{"name":"兔小贝","js":"https://…/兔小贝.js","referer":"","lines":"auto"}` |

它在搜索链路里的位置：与采集源**并行**查询，结果按来源轮流合并去重，避免单个源把结果名额占满。
JS 源的规则是「搜索 → 详情 → 必要时用站点自己的 `play` 规则免嗅」，只有**最终能拿到直链**
（`.m3u8 / .mp4 / .flv / rtmp…`）的条目才会出现在结果里 —— 拿不到直链的（纯直播间那种
需要客户端嗅探的）会**被丢掉**，免得点开黑屏。

命令行走一遍引擎自检（不启动服务、不影响现网）：

```bash
# 需要引擎就绪；会打印 home/分类/搜索 的条数
PYTHONPATH=/usr/local/trim_media_mitm/lib \
/usr/bin/python3 /usr/local/trim_media_mitm/jsspider.py "https://…/兔小贝.js" 贝
```

注意事项（写清楚了免得踩）：

- 引擎扩展是 **cp311 / x86_64 manylinux** wheel，正好对上 fnOS 的 `/usr/bin/python3`（3.11.2）。
  万一哪天系统 Python 变了版本，引擎会**优雅降级**（配置页会标「引擎不可用」），
  **不影响采集源与点播主链路**。
- 站点规则有磁盘缓存：`conf/js_cache/`（1 天）。站点挂了也还能用上次的规则。
- 每个站点一个独立 QuickJS 上下文（内存有界、上限 6 个，先进先出淘汰），所以多源是**真并行**的。
- JS 源天然比采集源慢（一次搜索要 `1 次搜索 + N 次详情 + 免嗅`），一般在 2~7 秒。
  采集源够用时插件**只等它 2.5 秒**；采集源没结果时才等满 10 秒。

---

## 关于「饭太硬」与 Java（一个必须说清楚的结论）

插件**支持直接粘一份 TVBox 配置地址**来筛源（饭太硬、小苹果那种订阅格式都认），
**网盘类默认不勾选**——但会**逐条列出来**（名称 / 接口 / 凭什么判成网盘），
不认同就点「仍要加入」自己实测。实测结论分两半：

> 饭太硬那份订阅的 49 个站点里，**46 个是加密 JAR**（`csp_*Guard`）、
> **3 个是 drpy2 JS 蜘蛛**，另有 13 个左右网盘类（四盘 / 夸克 / UC / 盘搜 / 小雅 …），
> 已按要求全部剔除。

**加密 JAR 这条路走不通**，原因很硬：

| 现象 | 实测 |
|---|---|
| `spider` 字段那份「jar」 | 下载后 md5 与配置里写的一致（是真货），但**它不是 JVM 字节码**：里面只有 `classes.dex`（Android 的 dex）+ `assets/*.so` |
| `java -jar` | 直接报错——dex 不是 class 文件 |
| 里面的 .so | `ftyguard_v7.so` = **ARM 32 位**、`ftyguard_v8.so` = **AArch64**，而 NAS 是 **x86-64** |
| 结论 | 需要「Android 运行时（ART）+ ARM 指令翻译」才能跑，等于在 NAS 上开个安卓模拟器；不现实，也没必要 |

**JS 蜘蛛（`type:3`）这条路走通了**：不用 Java，直接上 QuickJS（见上一节）。

### 那为什么电视上的「影视仓」能跑这些加密源？

因为**影视仓是安卓 App**：它跑在 Android 的 ART 上、CPU 是 ARM —— 那份 jar 本来就是给它编的。

| 那份 `csp_*Guard` jar 需要什么 | 影视仓（安卓 TV / 手机） | 飞牛上的本插件 |
|---|---|---|
| ART / Dalvik 运行时（它是 `classes.dex`） | 系统自带 | ❌ 只有 Python + QuickJS；**OpenJDK 也不认 dex** |
| ARM 的 CPU（`*guard_v7.so` ARM32 / `v8.so` arm64） | 电视就是 arm64 / armv7 | ❌ 飞牛是 x86_64，ARM 原生库加载不了 |
| Android Context（`Init.init(Context)` 解那个加密 `.guard`） | 有 | ❌ 没有安卓框架 |

装上 JRE 也解决不了，当场验过：`java -jar` 报 `no main manifest attribute`；
`java -cp … com.github.catvod.spider.DouDouGuard` 直接 `ClassNotFoundException`
（包内 `.class` 文件数为 0，全文件搜不到 `0xCAFEBABE`）。
TVBox / 影视仓自己的加载器就是 `new DexClassLoader(...)`，官方文档把 JAR 蜘蛛的运行时写作
「Android ART」；做 PC 版 TVBox 的社区项目结论也一样（jar 源只支持安卓端）。
另外：**影视仓并没有真正的电脑版** —— 网上那些「电脑版」下下来就是安卓模拟器 + APK。

> 想在这台 NAS 上看到同一批站点，现实做法是**换成 JS 规则版或普通采集接口**
> （插件里的 JS 引擎与影视仓的 JS 引擎是同一个物种：有 JS 规则，两边都能跑）。

### 饭太硬相关的东西，插件里一个都不留

早期版本（1.0.0）会**每天两次自动抓饭太硬订阅并留档**（`update_fty.py` +
`conf/fty_tv.json` + 一个定时器）。**本版把这些全部删掉了**：

| 之前 | 现在 |
|---|---|
| `update_fty.py` + `trim-media-fty-update.timer`（00:10 / 12:10 各抓一次） | **已删**。插件不再自动联网抓任何订阅 |
| 随包发出的 `conf/fty_tv.json`（饭太硬 49 站快照）、`conf/fty_meta.json` | **不再随包发出**。装完 `conf/` 里只有 `sources.json` 和 `config.json`（都是你自己的配置） |
| 生成 `conf/fty_usable.json` | **已删** |

> 升级安装时会**主动清理**老版本留下的这些文件与定时器（`pkg_install.sh` 里有专门一段），
> 但**不会碰你自己的 `sources.json`**。
> 至于那份 49 站订阅「哪几个能用」的实测结论，仍然在上面这张表里 ——
> **结论保留，数据不留**：想知道某个源行不行，把配置地址粘进配置页现测即可。
>
> 代码里还留着一张「若干片源方标注的固定广告时长」表和一张「图片域名镜像」表，
> 那是**被动功能数据**（只在遇到对应时长/域名时才生效，不会引入任何源），
> 广告剔除功能靠它撑着。

---

## 安装后验证

```bash
# 1) 接管是否生效（期望 {"mitm":1,...}）
curl -s --unix-socket /var/run/trim_media.sock http://x/v/__mitm__/health

# 2) 配置页是否可达（期望 200，且能看到「爱迷茫」标题）
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5666/v/__mitm__/config

# 3) 三个单元是否都在跑，输出应全为 active
systemctl is-active trim-media-mitm.service \
                    trim-media-mitm-watchdog.timer \
                    trim-media-mitm-maintenance.timer

# 3b) 确认饭太硬残留已清干净（期望：无 fty 定时器、conf 里只剩你自己的 sources.json + config.json）
systemctl list-timers --all | grep -i fty || echo "无 fty 定时器 ✓"
ls /usr/local/trim_media_mitm/conf/

# 4) 应用是否登记
appcenter-cli list | grep -i trimonline

# 5) JS 蜘蛛引擎是否就绪（期望 available:true / "QuickJS + drpy2"）
curl -s --unix-socket /var/run/trim_media.sock http://x/v/__mitm__/api/sources \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["engine"])'
```

然后在飞牛影视里搜一部片子，走一遍「搜索 → 点开 → 选集 → 播放」。

> 想单独验证引擎本身（会真去拉一次站点规则，不写任何配置）：
>
> ```bash
> PYTHONPATH=/usr/local/trim_media_mitm/lib \
> /usr/bin/python3 /usr/local/trim_media_mitm/jsspider.py "https://…/兔小贝.js" 贝
> ```

### 装完没自动接管怎么办

官方**不保证**调用 `install_callback`，所以 `cmd/main(start)` 里做了同一套兜底：

```bash
cat /var/apps/trimonline/config/privilege   # 应看到 "run-as": "root"
appcenter-cli start trimonline              # 手动触发一次
```

若 privilege 被强制写成 `package`，改回来：

```bash
sed -i 's/"run-as": "[^"]*"/"run-as": "root"/' /var/apps/trimonline/config/privilege
appcenter-cli stop trimonline && appcenter-cli start trimonline
```

---

## 卸载

```bash
appcenter-cli uninstall trimonline
```

卸载会：停服务 → 删 systemd 单元 → 把 `trim_media.real.sock` **挪回**
`trim_media.sock`（官方进程监听的是同一个 inode，**无需重启官方后端**，影视立刻恢复正常）
→ **保留用户数据**（在线条目 / 继续播放 / 片源配置 `sources.json`）。

### 卸载出问题时的手工兜底

```bash
# 前三个是本版的单元；最后一个 fty-update 是 1.0.0/1.1.0 遗留的，顺手一起清掉
systemctl disable --now trim-media-mitm.service \
        trim-media-mitm-watchdog.timer \
        trim-media-mitm-maintenance.timer \
        trim-media-fty-update.timer 2>/dev/null
rm -f /etc/systemd/system/trim-media-mitm.service \
      /etc/systemd/system/trim-media-mitm-watchdog.service \
      /etc/systemd/system/trim-media-mitm-watchdog.timer \
      /etc/systemd/system/trim-media-mitm-maintenance.service \
      /etc/systemd/system/trim-media-mitm-maintenance.timer \
      /etc/systemd/system/trim-media-fty-update.service \
      /etc/systemd/system/trim-media-fty-update.timer
# 老版本留下的饭太硬产物（不是用户数据）
rm -f /usr/local/trim_media_mitm/update_fty.py \
      /usr/local/trim_media_mitm/update_fty.log \
      /usr/local/trim_media_mitm/conf/fty_tv.json \
      /usr/local/trim_media_mitm/conf/fty_tv.json.bak \
      /usr/local/trim_media_mitm/conf/fty_meta.json \
      /usr/local/trim_media_mitm/conf/fty_usable.json
systemctl daemon-reload
# 还原官方 socket
if [ -S /var/run/trim_media.real.sock ]; then
    rm -f /var/run/trim_media.sock
    mv /var/run/trim_media.real.sock /var/run/trim_media.sock
fi
curl -s --unix-socket /var/run/trim_media.sock http://x/ -o /dev/null && echo "官方后端 OK"
```

---

## 重新构建

### 环境

- Linux x86_64，Python **3.11**（与飞牛当前系统一致）
- 一份包含 `aiohttp` 及其依赖的 site-packages（可从已部署的飞牛环境抽取，
  也可以先 `pip download/install` 到临时目录）
- `vendor/quickjs/` 已随仓库提供
- FPK 本质是 **tar.gz**，打包脚本只用 Python 标准库，**不需要 `mksquashfs` / `fnpack`**

### 步骤

```bash
# 1) 生成运行库（依赖有变化时才需要；已有 app/mitm/lib 可跳过）
cd src/trimonline
bash make_lib.sh /path/to/site-packages ../vendor/quickjs
cd ../..

# 2) 语法自检
python3 -m py_compile src/trimonline/app/mitm/*.py
find src -name __pycache__ -type d -prune -exec rm -rf {} +

# 3) 打包（产出 dist/爱迷茫-<版本>.fpk 与 .sha256）
python3 tools/build_fpk.py 1.3.0

# 4) 校验
cd dist && sha256sum -c 爱迷茫-1.3.0.fpk.sha256
```

只改 Python / 前端、不重打运行库时：

```bash
python3 tools/build_fpk.py 1.3.0 --no-lib
```

运行库需要 **aiohttp** 及其依赖 + **quickjs**（其余全是标准库）。
`make_lib.sh` 做四件事：抽取 aiohttp 依赖、并入 QuickJS 引擎（来自 `vendor/quickjs/`）、
用**系统 python3 + PYTHONPATH** 真起一个 aiohttp 服务端+客户端自检并跑一次
QuickJS ES2020 自检、最后断言 `lib` 内**没有任何符号链接**。

> QuickJS 素材不在 FPK 源码树里（是构建期素材，放在 `vendor/quickjs/`）。
> 素材目录可覆盖：`bash make_lib.sh [site-packages路径] [quickjs素材目录]`。
> 找不到时会**只警告不失败**——打出来的包 JS 源不可用，其余功能照常。

> **为什么不用 venv（踩过的坑）**：venv 必然带一个指向 `/usr/bin/python3` 的
> **绝对符号链接**，部分 fnOS 版本的应用中心解包器会因此直接报「解压 app.tgz 失败」。
> 纯目录 + `PYTHONPATH` 不含任何符号链接，也不依赖 venv 里写死的路径。

> 运行时的 JS（`drpy2.js`、`cheerio.min.js` 等 11 个模块）由
> `vendor/build_runtime.py` 拼成 `src/trimonline/app/mitm/runtime/runtime_bundle.js`
> （每个模块各自 IIFE，避免顶层 `const` 重定义）。改运行时素材后要重新生成再打包：

```bash
python3 vendor/build_runtime.py
```

> `cmd/main` 必须保持可执行（`-rwxr-xr-x`），否则应用中心无法启停应用。
> `config/privilege` 只声明 `run-as: root`，不写死任何账号，便于在任何机器上安装。

---

## 已知点

- 图标是自绘的（圆角 + 落日粉紫渐变 + 心形里嵌播放键 + 一点星光和淡月牙），
  生成脚本在 `tools/make_icon.py`（纯 Pillow），旧图标备份在 `tools/icons-old/`。
  要换只需覆盖这 4 个文件：`ICON.PNG`(64)、`ICON_256.PNG`(256)、
  `app/ui/images/icon_64.png`、`icon_256.png`（PNG 带 alpha，圆角外透明）。
- 声明 `run-as: root` 是因为接管 socket、写 systemd 单元、重启官方服务都需要特权。
  官方「root 仅限合作企业」约束的是**上架应用商店**，本地手动安装不受限。
- 运行方式：`/usr/bin/python3` + `PYTHONPATH=/usr/local/trim_media_mitm/lib`，
  不装 venv、不写死账号、不改动官方后端的数据与配置。依赖含 cp311 的 C 扩展
  （aiohttp 与 quickjs），适用于飞牛当前基于 Debian 12 / Python 3.11 的系统。
- 卸载**保留用户数据**；要彻底清干净，另删 `/usr/local/trim_media_mitm`。
- **不声明 `install_type`**：安装到用户选择的存储空间（所有第三方应用都是这个形态）。
- **载荷目录由 `cmd/_common` 统一解析**，所有 `cmd/*` 脚本都经它找载荷，不写死目录层级。
- **JS 引擎是可选增强**：`quickjs` 导入失败或 `runtime/` 文件缺失时，
  `jsspider.available()` 返回 False，配置页会标「引擎不可用」，
  **采集源搜索与点播链路完全不受影响**。
- **关键回归（务必记住）**：官方后端是 HTTP/1.0 服务端，用**关闭连接**表示响应结束，
  且 `POST /v/api/v1/stream` 的成功响应既无 `Content-Length` 也无 `Transfer-Encoding`。
  MITM 必须把「关闭」透传给 nginx，否则前端永远停在「视频加载中」、nginx 日志记 `499 0`。
- drpy2 运行时本体只打了**一处**源码补丁：`let d=[]` → `var d=[]`（3 处）。
  原因：站点规则的 `js:` 片段里常写 `var d=[]`，direct eval 同作用域会与 `let` 冲突
  （Rhino 宽松、标准引擎报 `redefinition`）。
- **每个站点一个独立 QuickJS 上下文**（上限 6 个，FIFO 淘汰）：drpy 的 `rule` 是模块级
  状态，共用上下文就必须全局串行，一次搜索会拖到十几秒。
- 官方登录按**客户端 IP** 识别身份，凭据来自 nginx 注入的 `X-Real-IP` / `Trim-Host`；
  若在别处直连官方 socket 而不带这两个头，会报 `Password Incorrect`。
- **配置页的写操作走「信任这台浏览器」**：凭证是 cookie + `config.json` 里的 `write_token`，
  **与来源 IP 无关**，所以局域网 IP、公网域名 / DDNS、飞牛远程访问（fnconnect）三条路都能改。
  白名单与「不限制来源」是可选加严/放宽项，见上面的「配置页存不进去怎么办」。
- **在线源可见范围依赖官方登录 token**：认不出当前账号时按「不可见」处理（`all` 模式除外），
  所以非管理员需要先在飞牛影视里正常登录；本地媒体库与本地播放不受此影响。

---

## 致谢

本插件大量站在开源社区的肩膀上。**完整清单、版本与许可证见
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。**

特别感谢：

- **Hululu007 / drpy-node**：drpy2 运行时与 TVBox `type:3` 规则引擎，本项目 JS 蜘蛛的基础。
- **fantaiying7 / EXT**：drpy 运行时素材、模板与规则写法参考。
- **Fabrice Bellard、Charlie Gordon / QuickJS**，以及 **Petter Strandmark / quickjs-python**：
  让插件不用装 Java / Node 也能执行站点规则。
- **cheerio、crypto-js、jinja-js、URI.js、JSON5、pako、node-rsa** 的维护者：
  HTML 解析、加密、模板、压缩这些脏活都由它们兜底。
- **aiohttp / aio-libs**：MITM 转发服务的 HTTP 客户端与服务端。
- **TVBox、海阔视界** 社区：采集接口、`type:3` 配置与 Jsoup 选择器的事实标准。
- **飞牛 fnOS** 平台，以及所有在 Issue 里帮忙试错、反馈日志的用户。

## 许可证

本仓库以 **GPL-3.0** 发布，完整文本见 [`LICENSE`](LICENSE)。

原因：FPK 内置的 `drpy2.js` 来自 **GPL-3.0** 的
[drpy-node](https://github.com/Hululu007/drpy-node)，打包分发时需要遵守 GPL-3.0。

如果你只想使用「采集接口」部分、不希望承担 GPL 义务，可以自行移除
`vendor/drpy/`、`src/trimonline/app/mitm/runtime/` 以及 `jsspider.py` 中的 JS 引擎；
采集源搜索与播放链路仍然可用。

> 第三方素材（尤其是 `fantaiying7/EXT`）的版权归各自作者所有，
> 本仓库的 GPL-3.0 不会改变这一点。再分发或商用前请先确认上游授权。
