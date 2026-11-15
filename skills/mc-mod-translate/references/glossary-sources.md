# 上游术语库与词典：来源清单与更新流程

本文件记录 `mc-mod-translate` 内置术语库的全部上游来源、本地落位、以及**用户主动要求更新时**才执行的流程。

> **不要自动更新**。用户要求了才动，并且动手前先把「变更清单」交给他确认（见 SKILL.md「术语库改动：先确认再落盘」）。

---

## 1. 上游来源

### CFPAOrg/Glossary

- 仓库：<https://github.com/CFPAOrg/Glossary>
- 分支：`main`
- Raw 前缀：`https://raw.githubusercontent.com/CFPAOrg/Glossary/main/`
- 许可：见仓库 LICENSE；术语数据由 CFPA 社区维护

需要同步的文件（12 个，缺一不可）：

| 上游路径 | 本地落位 | 内容 | 规模（2026-11-15） |
|---|---|---|---|
| `Vanilla/terms/terms-v1.tsv` | `raw/cfpa/Vanilla/terms/terms-v1.tsv` | 原版术语表；字段 `en, zh, reason, comment`，`zh` 用 `\|` 分同所指变体 | 736 行 / 13,729 B |
| `Vanilla/terms/qa-rules.tsv` | 同结构 | 官方 QA 规则；字段 `id, key, except_key, en, require, forbid, mode, except_en, comment` | 11 行 / 948 B |
| `Vanilla/terms/terms-version.tsv` | 同结构 | 分版本术语差异 | 30 行 / 3,348 B |
| `Vanilla/terms/special/terms-banner.tsv` | 同结构 | 旗帜纹章术语（key 含 `banner` 才生效） | 45 行 / 2,877 B |
| `Vanilla/terms/special/terms-pottery.tsv` | 同结构 | 陶片术语（key 含 `pottery`） | 72 行 / 4,824 B |
| `Vanilla/terms/special/terms-subtitles.tsv` | 同结构 | 字幕术语（key 含 `subtitles`） | 129 行 / 5,593 B |
| `Vanilla/terms/special/terms-trim.tsv` | 同结构 | 锻造纹饰术语（key 含 `trim`） | 38 行 / 2,041 B |
| `Vanilla/terms/special/terms-tropical_fish.tsv` | 同结构 | 热带鱼变种（key 含 `tropical_fish`） | 36 行 / 2,455 B |
| `Vanilla/latest.tsv` | 同结构 | 原版语言文件 en/zh 对照；字段 `key, en_us, zh_cn, zh_tw, zh_hk, ja_jp` | 8560 行 / 831,793 B |
| `Vanilla/diffs/term-key-filters.json` | 同结构 | 名称域 key 的白/黑名单正则 | 29 行 / 708 B |
| `Mod/模组简体中文翻译指南.tsv` | `raw/cfpa/Mod/...` | 模组通用译名；字段 `Term, Translation, Remark, Source` | 964 行 / 24,570 B |
| `Mod/模组材料简体中文翻译指南.tsv` | 同结构 | 模组材料译名 | 745 行 / 26,505 B |

上游还有 4 个**未导入**的文件（版本间术语变更差异，索引构建器不读，不影响翻译）：

```
Vanilla/diffs/26.1.2-26.2.json
Vanilla/diffs/26.1.2-26.2.terms.json
Vanilla/diffs/26.2-26.3.json
Vanilla/diffs/26.2-26.3.terms.json
```

用户要求对照新旧版本术语变动时再导入。

### VM-Chinese-translate-group/i18n-Dict-Extender

- 仓库：<https://github.com/VM-Chinese-translate-group/i18n-Dict-Extender>
- 数据不在仓库里，在 **GitHub Releases**，每周五自动发布一次
- 最新 release 接口：`https://api.github.com/repos/VM-Chinese-translate-group/i18n-Dict-Extender/releases/latest`
- 取资产 `Dict-Mini.json`：`{"英文": ["译名", ...]}`，列表按该译名在完整词典中的出现次数降序
- 本地落位：`raw/i18n-dict-extender/Dict-Mini.json`（20,170,676 B）
- 另有 `Dict.json`（447 MB）/ `Dict-Sqlite.db`（362 MB）/ `diff.json`，**不要下**，`Dict-Mini.json` 已够用

---

## 2. 本地布局

```
<Base directory>/glossary/
├─ raw/
│  ├─ cfpa/...                        CFPA 原始 TSV，保持上游目录结构
│  └─ i18n-dict-extender/Dict-Mini.json
└─ index/glossary.db                  只读 SQLite 索引（term / memory / qa 三张表）
```

索引由 `mct.py glossary build` 从 `raw/` 生成，**不联网**。表结构：

- `term(en, zh, reason, comment, scope, src)` —— 术语；`scope` 非空表示只在 key 含该 token 时生效
- `memory(en PRIMARY KEY, zh, src)` —— 整串对照；CFPA 原版优先，i18n-dict 补缺
- `qa(id, key, except_key, en, require, forbid, mode, except_en, comment)` —— QA 规则

---

## 3. 更新流程（仅当用户主动要求）

### 3.1 先查有没有更新（不写任何文件）

沙箱内的 python / PowerShell **没有出网能力**，抓取必须走 ego 浏览器的运行器：

```js
// ego_script
const fs = await import('node:fs');
const local = fs.readFileSync('<Base directory>/glossary/raw/cfpa/Vanilla/terms/terms-v1.tsv', 'utf8');
const remote = String(await fetch.server('https://raw.githubusercontent.com/CFPAOrg/Glossary/main/Vanilla/terms/terms-v1.tsv'));
// 比对长度 + 哈希，逐文件输出
```

- 逐个文件比对**长度 + 内容哈希**，不要只看 `Last-Modified`
- Dict-Mini 比对 `releases/latest` 的 `tag_name`（如 `dict-update-2026-11-13-1732`）与本地那份是否同源
- 再用 `https://api.github.com/repos/CFPAOrg/Glossary/git/trees/main?recursive=1` 对比**文件清单**，确认上游没有新增 `terms-<新token>.tsv`

### 3.2 有变化 → 先给变更清单

按 SKILL.md 的格式列出：哪些文件变了、字节数增减、行数增减、新增/删除的术语条数、对 `term`/`memory`/`qa` 表的影响，**等用户确认后再落盘**。

### 3.3 落盘

沙箱对 skill 目录是只读的，`glossary build` 写不进 `index/`，所以分两步：

1. 把新的 raw 文件下载到工作区的临时目录；
2. 在工作区的工具副本上 `mct.py --glossary-home <临时目录> glossary build`，重建索引；
3. 用 `ego_script` 的 `node:fs` 把 `raw/` 和 `index/glossary.db` 拷回 skill 目录；
4. 用 `mct.py glossary stats` 复核三张表的条数，并抽查一条 `glossary lookup`。

### 3.4 不要做的事

- 不要动上次同步记录（见下节），更新完要改
- 不要顺手改 `term` 表里的单条内容——那是另一件事，同样需要先确认
- 不要 `--force` 覆盖用户项目里的 `glossary.json`

---

## 4. 同步记录

| 日期 | CFPA HEAD | Dict-Mini release | term | memory | qa |
|---|---|---|---|---|---|
| 2026-11-15 | `e27df31f1d` | `dict-update-2026-11-13-1732` | 2755 | 405033 | 10 |
| 2026-11-15（复查，无变化） | `e27df31f1d` | `dict-update-2026-11-13-1732` | 2755 | 405033 | 10 |

下次更新时在表尾追加一行。

---

## 5. 授权与署名

- 社区译文数据：**CC BY-NC-SA 4.0**
- 原版游戏文本：Mojang Studios，受 Minecraft EULA 约束
- 汉化产物应保留原作者与 CFPA 社区署名，**不得商用**
