# mc-mod-translate

把 Minecraft 模组的语言文件翻成简体中文，交付一个**可直接启用的资源包**的 DSH 插件。

原名 `@local/dsh-mc-translate`。装上之后，技能 `mc-mod-translate` 会出现在你的技能列表里，跟 DSH 说话就能用。

## 它做什么

- **只读 jar，绝不动原包**。产物是 `out/<jar名>-zh_cn-resourcepack.zip`，丢进 `resourcepacks/` 就能用。
- **内置术语库**：CFPA 官方术语表 + 原版语言记忆库 + i18n-Dict 词典，翻译时自动给出该用的译名，避免「红石」译成「红石粉」这类问题。
- **格式校验器**盯死细节：`%s`/`%d`/`%1$s` 占位符、`§6` 格式码、`\n` 转义、`{0}` 花括号、键序、行首行尾空格、中英混排空格、GUI 文本长度。有错误不给打包。
- **旧译审计**：jar 里已经带了 `zh_cn.json` 时，自动判断这份旧译能不能信（模组更新后旧译文错位是常见病），并列出与术语库冲突的条目。
- **适配真实模组的各种怪状**：MCreator 生成的乱序语言文件自动排序；JarJar 拆成多个 jar 的合并成一份；只读子集 `--namespace`。
- **联动模组对齐**：翻译附属模组时，用本体模组的官方 `zh_cn.json` 当术语基准（`Soulwood` 是「灵魂木」而不是自造的译名）。

## 安装

DSH 的 **Plugins** 面板选「添加插件」指向本目录，或者让 agent 用 `plugin_manager` 的 `install_bundle` 安装。

改过 `index.js` 需要重装并重启 DSH；改技能内容（`SKILL.md`、`references/`、`scripts/`）下次加载即生效，不用重启。

## 用法

装好后直接说：

> 帮我汉化 `E:\mods\somemod-1.0.0.jar`

技能会走完 probe → extract → 术语盘点 → 分批翻译 → 校验 → 通顺度校对 → 打包的完整流程。也可以自己调工具链：

```bash
python skills/mc-mod-translate/scripts/mct.py probe  mods/somemod.jar
python skills/mc-mod-translate/scripts/mct.py extract mods/somemod.jar
python skills/mc-mod-translate/scripts/mct.py nominal  .mct/somemod
python skills/mc-mod-translate/scripts/mct.py keys     .mct/somemod --missing --simple --cluster
python skills/mc-mod-translate/scripts/mct.py apply    .mct/somemod batch.json
python skills/mc-mod-translate/scripts/mct.py check    .mct/somemod
python skills/mc-mod-translate/scripts/mct.py pack     .mct/somemod
```

| 命令 | 作用 |
|---|---|
| `probe` | 读模组身份、语言文件清单、是否 MCreator 生成、有无嵌套 jar |
| `extract` / `merge` | 建工作目录；多模块命名空间合并 |
| `nominal` | 译前名词盘点：高频专名、大写短语、重复条目 |
| `keys` | 列出待译条目（`--cluster` 按概念簇、`--simple`/`--complex` 按难度） |
| `apply` | 合并译文；`--by-en` 按英文原文一次铺到所有同英文的键 |
| `fmt` | 按源文件风格重排 |
| `audit` | 复查旧译是否整体错位、是否与术语库冲突 |
| `check` | 全套校验，有错误退出码非零 |
| `pack` | 产出资源包 |
| `glossary` | 术语库 build / stats / lookup / search / qa |

## 目录结构

```
mc-translate-plugin/
├─ index.js                  技能 provider：每次 list/get 现读 SKILL.md
├─ cordis.patch.yml          往 profile 里插入一行
├─ skills/mc-mod-translate/  ← 技能本体，同时是技能的 <Base directory>
│  ├─ SKILL.md               工作流与铁律
│  ├─ references/            translation-rules.md、glossary-sources.md
│  ├─ scripts/               mct.py、selftest.py
│  └─ glossary/              raw/ 原始数据 + index/ 索引（索引不随仓库发布）
├─ locale/en.json, zh.json   Plugin Manager 里显示的名称与简介
├─ docs/DETAIL.md            插件详情页
└─ icon.svg
```

## 术语库数据

`glossary/index/glossary.db`（约 48 MB，已编译索引）**不随仓库发布**，`raw/i18n-dict-extender/Dict-Mini.json`（约 20 MB）也不包含。需要时：

1. 从 [VM-Chinese-translate-group/i18n-Dict-Extender](https://github.com/VM-Chinese-translate-group/i18n-Dict-Extender) 的 release 下载 `Dict-Mini.json`，放到 `skills/mc-mod-translate/glossary/raw/i18n-dict-extender/`。
2. 跑 `python skills/mc-mod-translate/scripts/mct.py glossary build` 重建索引。

不重建也能用，只是术语提示会少一些（CFPA 术语表已随仓库附带）。

## 数据来源与署名

- 术语表：[CFPAOrg/Glossary](https://github.com/CFPAOrg/Glossary)（简体中文翻译指南与术语表）
- 词典：[VM-Chinese-translate-group/i18n-Dict-Extender](https://github.com/VM-Chinese-translate-group/i18n-Dict-Extender)
- 原版语言记忆库来自 Minecraft 官方语言文件

## 开发

见 [DEV.md](DEV.md)。回归自测：

```bash
python skills/mc-mod-translate/scripts/selftest.py
```

## 许可

[CC BY-NC-SA 4.0](LICENSE) — 署名、非商业性使用、相同方式共享。
