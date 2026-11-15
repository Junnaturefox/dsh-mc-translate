# Minecraft mod translation

把一个 Minecraft 模组的 `en_us.json` 翻成简体中文，产出**可直接启用的资源包**。只翻译字符串值，键名、键序、缩进、换行、转义、颜色码、占位符全部原样保留。

装上这个插件后，会话技能目录里会出现 `mc-mod-translate`，工具链和术语库都随包提供，不需要额外下载。

## 它做什么

给一个模组 jar，走完整条流水线：

1. **probe** —— 读 `fabric.mod.json` / `mods.toml`，拿到模组 id、加载器、MC 版本、全部语言文件与可用 locale。
2. **extract / merge** —— 提取 `assets/<ns>/lang/en_us.json`。
   - 普通模组用 `extract`，同时生成译文骨架，jar 内已有 `zh_cn.json` 会自动沿用。
   - JarJar 拆包的多模块模组用 `merge`：穿进嵌套 jar，把所有模块的 `en_us.json` 合并成一份并**按键名排序**，否则资源包里同名的 `zh_cn.json` 会互相覆盖。
3. **翻译** —— 先查内置术语库，无解再查 MC 百科 / 官方 Wiki / 作者仓库；定不下来的术语给选项让用户拍板。
4. **check** —— 逐条比对占位符（`%s` `%1$s` `%%`）、`§` 颜色码、`\n` 转义、`{0}` 占位符的数量与顺序，以及键集、键序、行首尾空格、空译文、术语一致性。有错误不算通过。
5. **通顺度校对** —— check 通过后按中文语感重写拗口的长句，不增删信息。
6. **pack** —— 产出 `out/<jar名>-zh_cn-resourcepack.zip`，含 `pack.mcmeta` 与 `assets/<ns>/lang/zh_cn.json`。`pack_format` 按模组 MC 版本推断。

## 用法

在会话里说「汉化这个模组」并给出 jar 路径即可，模式会接管后续流程。

手动调工具：

```powershell
. "<Base directory>\scripts\_run.ps1"
Run-Py "<Base directory>\scripts\mct.py" @('probe','mods/foo.jar')
```

## 术语库

随包附带，约 67 MB，只读，不联网：

- **CFPAOrg/Glossary** —— 原版术语表、分域术语（字幕 / 陶片 / 旗帜 / 锻造 / 热带鱼）、官方 QA 规则、原版语言 en→zh 对照 8560 条、模组译名 1705 条
- **VM-Chinese-translate-group/i18n-Dict-Extender** —— 约 39.6 万条模组词条

合计 2755 条术语 + 405033 条整串记忆，查询走 SQLite 索引。

## 已知限制

- **不自动更新术语库**：只有用户明确要求时才刷新，且先给变更清单。上游地址与流程见技能的 `references/glossary-sources.md`。
- **不重打包 jar**：交付物只有资源包，原 jar 一个字节都不动。
- **老 `.lang` 格式不处理**：1.6 时代的 `lang/en_US.lang` 会明确报错，不做猜测转换。
- **硬编码文本覆盖不到**：少数模组把文字写在代码里，资源包覆盖不了，这类条目会在汇报里单独列出。
- **术语库是跨领域合库**：泛用词（`Press`、`Track`、`Scroll`）可能匹配到别的模组的专业义，`check` 的术语警告属建议性质，需逐条甄别。
