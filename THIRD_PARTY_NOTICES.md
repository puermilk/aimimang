# 致谢与第三方组件说明

「爱迷茫」本身只做播放器侧的中转与界面，真正让它跑起来的是下面这些开源项目和作者。
**感谢每一位愿意把代码、规则和踩坑经验公开出来的作者。**

如果你认为这里的署名、链接或许可证标注有误，欢迎提 Issue 更正。

---

## 一、特别感谢

| 项目 / 作者 | 在本项目中的作用 | 许可证 | 来源 |
|---|---|---|---|
| **drpy / drpy-node**（Hululu007 及贡献者） | JS 蜘蛛（drpy2 运行时），TVBox `type:3` 规则引擎 | **GPL-3.0** | <https://github.com/Hululu007/drpy-node> |
| **fantaiying7/EXT** | drpy 运行时素材、模板与规则写法参考 | 仓库未声明许可证 | <https://github.com/fantaiying7/EXT> |
| **QuickJS**（Fabrice Bellard、Charlie Gordon） | 嵌入式 JavaScript 引擎 | MIT | <https://bellard.org/quickjs/> |
| **quickjs（Python 绑定）**（Petter Strandmark） | 让 Python 侧可以执行 drpy 规则 | MIT | <https://github.com/PetterS/quickjs> |
| **cheerio** | 服务端 jQuery 风格 HTML 解析 | MIT | <https://github.com/cheeriojs/cheerio> |
| **crypto-js** | 规则里常见的加密 / 摘要函数 | MIT | <https://github.com/brix/crypto-js> |
| **jinja-js**（sstur） | 规则模板渲染 | MIT | <https://github.com/sstur/jinja-js> |
| **URI.js**（Rodney Rehm） | URL 解析与拼接 | MIT | <https://github.com/medialize/URI.js> |
| **JSON5** | 宽松 JSON 解析 | MIT | <https://github.com/json5/json5> |
| **pako** | zlib / gzip 解压 | MIT AND Zlib | <https://github.com/nodeca/pako> |
| **node-rsa** | RSA 加解密 | MIT | <https://github.com/rzcoder/node-rsa> |
| **aiohttp** 及其依赖（aio-libs） | MITM 转发服务的 HTTP 客户端 / 服务端 | Apache-2.0 AND MIT | <https://github.com/aio-libs/aiohttp> |
| **海阔视界 Jsoup 语法** | `htmlParser` 的选择器写法参考 | — | 社区文档与 drpy 生态 |
| **TVBox** | 采集接口与 `type:3` 配置格式的事实标准 | — | TVBox 社区 |
| **飞牛 fnOS / 飞牛影视** | 插件运行的平台与被接管的官方服务 | 官方条款 | <https://www.fnnas.com/> |

> **关于 drpy2 的许可证**：本项目打包的 `drpy2.js` 来自 **GPL-3.0** 的 drpy-node，
> 因此本仓库整体按 **GPL-3.0** 发布（见 [`LICENSE`](LICENSE)）。
> 如果你只想使用「采集接口」部分、不想要 GPL 义务，可以自行移除
> `vendor/drpy/`、`src/trimonline/app/mitm/runtime/` 与 `jsspider.py` 中的 JS 引擎，
> 采集源搜索与播放链路仍然可用。
>
> **关于 fantaiying7/EXT**：该仓库目前没有声明开源许可证，默认保留所有权利。
> 本仓库保留它仅用于说明运行时素材的来源；如果你要再分发或商用，
> 建议先联系原作者取得授权，或替换为自己有权使用的素材。

---

## 二、随包分发的组件

| 组件 | 版本 / 说明 | 许可证 |
|---|---|---|
| drpy2 | `3.9.49beta2 20231122`，仅打了一处补丁：`let d=[]` → `var d=[]` | GPL-3.0 |
| cheerio | 打包进 `runtime_bundle.js` | MIT |
| crypto-js | 打包进 `runtime_bundle.js` | MIT |
| jinja-js | v0.1.8 | MIT |
| URI.js | 打包进 `runtime_bundle.js` | MIT |
| JSON5 | 打包进 `runtime_bundle.js` | MIT |
| pako | 2.1.0 | MIT AND Zlib |
| node-rsa | 打包进 `runtime_bundle.js` | MIT |
| gbk.js / `模板.js` / `jsoup.js` | 来自 drpy 生态，打包进 `runtime_bundle.js` | 随上游 |
| QuickJS | C 引擎，经 Python 绑定加载 | MIT |
| quickjs (Python) | `1.19.x` | MIT |
| aiohttp | 3.14.5 | Apache-2.0 AND MIT |
| aiohappyeyeballs | 2.7.1 | PSF-2.0 |
| aiosignal | 1.4.0 | Apache-2.0 |
| attrs | 26.1.0 | MIT |
| frozenlist | 1.8.0 | Apache-2.0 |
| idna | 3.20 | BSD-3-Clause |
| multidict | 7.1.0 | Apache-2.0 |
| propcache | 0.5.4 | Apache-2.0 |
| yarl | 1.25.1 | Apache-2.0 |
| typing_extensions | 随 Python 生态 | PSF-2.0 |

> 运行库由 `src/trimonline/make_lib.sh` 从部署环境抽取，构建时并入 FPK；
> 源码仓库不重复存放这些第三方二进制（`app/mitm/lib/` 已被 `.gitignore` 排除）。

---

## 三、MIT 许可证文本

下列组件以 MIT 许可证分发。版权归各项目作者所有。

```text
MIT License

Copyright (c) the respective authors and contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

Apache-2.0 组件的完整文本见各项目仓库；GPL-3.0 的完整文本见本仓库 [`LICENSE`](LICENSE)。

---

## 四、再次感谢

- 感谢 **Hululu007** 把 drpy 做成能在 Node/浏览器里跑的完整实现，并保留了规则的写法与调试经验。
- 感谢 **fantaiying7** 整理的 EXT 素材与模板，让规则适配少走了很多弯路。
- 感谢 **Fabrice Bellard / Charlie Gordon** 的 QuickJS，以及 **Petter Strandmark** 的 Python 绑定。
- 感谢 **cheerio / crypto-js / jinja-js / URI.js / JSON5 / pako / node-rsa / aiohttp** 的维护者。
- 感谢 **TVBox / 海阔视界** 社区沉淀下来的接口与规则生态。
- 感谢 **飞牛 fnOS** 提供的平台，以及所有在 Issue 里帮忙试错、反馈日志的用户。

没有这些公开的工作，就没有「爱迷茫」。
