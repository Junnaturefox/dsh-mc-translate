# 开发笔记

本目录就是**唯一事实来源**。模组翻译在别的会话里跑，那些会话通过已安装的插件加载 `mc-mod-translate` 技能，读到的是这里的文件。

## 生效链路

```
mc-translate-plugin/
├─ index.js ──激活时──> 注册技能 provider
│                        每次 list() / get() 现读 SKILL.md
└─ skills/mc-mod-translate/   ← 技能 <Base directory>
```

- **技能正文（SKILL.md / references / scripts）每次加载都重新读盘**，改完下一次技能调用就生效，不用重装、不用重启。
  （`index.js` 用的是 `ctx.skills.registerProvider()`，`list()` / `get()` 里现读文件；早先用 `register()` 会把正文快照在激活时固定住，改文档等于没改。）
- **`index.js` 是模块代码**，改动要重装包并重启 DSH 才会换新的模块代际（DSH 文档原话：replacing an installed package requires restart to load a fresh JavaScript module generation）。
- 术语库 `glossary/` 是纯数据，改完直接生效；重建索引见 `skills/mc-mod-translate/references/glossary-sources.md`。

## 改完怎么验

```powershell
cd mc-translate-plugin

# 1. 回归自测（29 项：提取 / MCreator 排序 / 合并 / 旧译审计 / 校验 / 打包 / 译前盘点）
python skills/mc-mod-translate/scripts/selftest.py

# 2. 工具链能不能从技能根解析到术语库
python skills/mc-mod-translate/scripts/mct.py glossary stats

# 3. 语法检查
node --check index.js
```

装完用 `plugin_manager` 的 `list_plugins` 看 `include:mc-translate-plugin` 的 `fiberPhase` 是不是 `active`；再调一次 `skill` 工具读 `mc-mod-translate`，确认 `Base directory` 指向本包的 `skills/mc-mod-translate`。

## 已知状态与坑

| 事项 | 情况 |
|---|---|
| 技能自身的路径解析 | `mct.py` 的默认术语库按 `脚本同级/glossary` → `技能根/glossary` 顺序找，所以不带 `--glossary-home` 也能跑。改布局时注意别打破这条。 |
| 同名技能副本 | `~/.dsh/skills/mc-mod-translate` 若存在会盖住插件的注册。别把副本搬回去。 |
| 术语库索引 | `glossary/index/glossary.db` 不入库；重建用 `mct.py glossary build`。缺 `Dict-Mini.json` 时 `build` 仍能跑，只是记忆库条目变少。 |
| 旧译审计的误报 | `audit` 的「强错位」在同名词撞车时会误报（`Acid`→酸液 与 `Blazing Acid`→酸液）。判定逻辑是「这个值本文件里别的键也在用」，所以先看明细再决定清盘还是只修那几条。 |
| Windows 沙箱 | DSH 沙箱里子进程管道会失败，`selftest.py` 一律用文件重定向拿输出，不要在测试里改回 `capture_output`。 |

## 结构约束

- 技能目录必须保持 `SKILL.md` + `references/` + `scripts/` + `glossary/` 的平铺形状，`SKILL.md` 里所有 `<Base directory>/…` 路径都依赖它。
- `SKILL.md` 的 frontmatter 只读 `name` / `description` / `whenToUse`，由 `index.js` 里的迷你 YAML 解析器处理；加别的字段要同时改解析器。
- `package.json` 的 `files` 对本地链接安装不过滤，但要保持准确，将来打包发布靠它。
- 图标：36×36 viewBox，禁 `<style>` / 动画 / `<text>`（字形用路径），256 KiB 以内。
