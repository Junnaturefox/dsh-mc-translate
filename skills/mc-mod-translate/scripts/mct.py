#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mct.py - Minecraft 模组语言文件翻译工具链（提取 / 校验 / 打包 / 术语查询）

子命令
  probe   JAR                查看模组元数据与语言文件清单，不落盘
  extract JAR [--work DIR]   提取 en_us.json，生成 zh_cn.json 骨架
  keys    WORK               列出待翻译条目（TSV）
  apply   WORK FILE.json     把 {key: 译文} 合并进 zh_cn.json
  fmt     WORK               按源文件格式重排译文（缩进/换行/键序）
  check   WORK               校验译文（占位符、格式码、键集、术语一致性）
  pack    WORK               打包成资源包（默认；加 --jar 才额外产出覆盖用 jar）
  glossary ...               术语库构建与查询

设计原则：源文件的键序、缩进、换行风格全部保留；只改字符串值。
交付形态：资源包 zip（原 jar 不重打包）。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _default_glossary_home() -> Path:
    """术语库位置：环境变量 > 脚本同级的 glossary > 技能根下的 glossary > 旧安装位置。

    技能包的布局是 `<skill>/scripts/mct.py` 配 `<skill>/glossary/`；单独跑一份脚本时
    也可能把 glossary 放在脚本旁边。两种都认。
    """
    env = os.environ.get("MCT_GLOSSARY")
    if env:
        return Path(env)
    for candidate in (HERE / "glossary", HERE.parent / "glossary"):
        if (candidate / "index" / "glossary.db").exists():
            return candidate
    installed = Path.home() / ".dsh" / "skills" / "mc-mod-translate" / "glossary"
    if (installed / "index" / "glossary.db").exists():
        return installed
    return HERE.parent / "glossary"


DEFAULT_GLOSSARY_HOME = _default_glossary_home()
WORK_ROOT_NAME = ".mct"
SURFACE_VERSION = "1.0"

# ---------------------------------------------------------------- 基础工具


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def eprint(*a):
    print(*a, file=sys.stderr)


class Fail(Exception):
    pass


# ---------------------------------------------------------------- JSON 读写


def decode_bytes(raw: bytes) -> tuple[str, str]:
    """返回 (文本, 编码名)。BOM 单独记录，正文不带 BOM。"""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace"), "utf-8-BOM"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8(replace)"


class _DupDetecting(dict):
    dups: list

    def __init__(self, *a, **k):
        super().__init__()
        self.dups = []


def load_json_pairs(text: str) -> tuple[dict, list[str]]:
    """解析 JSON 并返回 (数据, 重复键列表)，保持键序。"""
    dups: list[str] = []

    def hook(pairs):
        d = {}
        for k, v in pairs:
            if k in d:
                dups.append(k)
            d[k] = v
        return d

    return json.loads(text, object_pairs_hook=hook), dups


def detect_style(text: str) -> dict:
    """探测 JSON 文件的排版风格，用于原样回写。"""
    style = {
        "indent": 2,
        "newline": "\n",
        "trailing_newline": True,
        "had_bom": False,
        "compact": False,
    }
    if text.startswith("\ufeff"):
        style["had_bom"] = True
        text = text[1:]
    style["trailing_newline"] = text.endswith("\n")
    style["newline"] = "\r\n" if "\r\n" in text else "\n"
    body = text.strip()
    if "\n" not in body:
        style["compact"] = True
        return style
    m = re.search(r"\n([ \t]+)\"", text)
    if m:
        ind = m.group(1)
        style["indent"] = "\t" if ind.startswith("\t") else len(ind)
    else:
        style["indent"] = 0
    return style


def dump_style(data: dict, style: dict) -> str:
    """按探测到的风格序列化。始终不写 BOM（Gson 读取带 BOM 文件会失败）。"""
    key_indent = style.get("indent", 2)
    if style.get("compact"):
        s = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    elif key_indent in (0, None):
        s = json.dumps(data, ensure_ascii=False)
    else:
        s = json.dumps(data, ensure_ascii=False, indent=key_indent)
    if style.get("newline") == "\r\n":
        s = s.replace("\n", "\r\n")
    if style.get("trailing_newline", True):
        s += "\r\n" if style.get("newline") == "\r\n" else "\n"
    return s


# ---------------------------------------------------------------- 占位符与格式码

# Java Formatter 说明符：%s %d %1$s %-10s %.2f %%
RE_JAVA_FMT = re.compile(r"%(?:\d+\$)?[-#+0,(]*\d*(?:\.\d+)?[tT]?[a-zA-Z%]")
RE_FMT_INDEX = re.compile(r"%(\d+)\$")
# 花括号占位符：{0} {player} 等（软占位符）
RE_CURLY = re.compile(r"\{[A-Za-z0-9_.\- ]{0,24}\}")
# 段落样式颜色/格式码
RE_SECTION = re.compile(r"§[0-9a-fk-orA-FK-OR]")
# 反斜杠转义（\n \t \r \" \\）
RE_ESCAPE = re.compile(r"\\[nrt\"'\\]")

# 专有名词候选：大写开头的英文词
CAP_TOKEN_RE = re.compile(r"[A-Z][A-Za-z0-9'\-]*")
CJK_CHAR = "[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"
# 只查「字母↔汉字」相邻：数字与量词连写（9 个 / 5 个）属正常中文，不该报
CJK_LATIN_HIT = re.compile(f"({CJK_CHAR}[A-Za-z])|([A-Za-z]{CJK_CHAR})")
# 宽度受限的 GUI 文本
TIGHT_KEY_RE = re.compile(r"(?:^|\.)(gui|button|buttons|menu|screen|label)(?:\.|$)")
# 判断"简单名词"：无标点、词数少
SIMPLE_REJECT_RE = re.compile(r"[.,;:!?()\[\]{}]")
# 检查中英混排前先抹掉占位符与格式码
SPACE_STRIP_PATTERNS = (RE_JAVA_FMT, RE_SECTION, RE_ESCAPE, RE_CURLY, re.compile(r"<[^<>]{1,24}>"))


def capitalized_tokens(text) -> list[str]:
    if not isinstance(text, str):
        return []
    return CAP_TOKEN_RE.findall(text)


def cluster_rank(en_map: dict) -> Counter:
    """统计每个大写词在整份文件里出现的次数，用于判断"哪个词才是这条的主语"。"""
    counter: Counter = Counter()
    for value in en_map.values():
        for token in capitalized_tokens(value):
            counter[token] += 1
    return counter


def key_cluster(key: str, value, rank: Counter) -> str:
    """把条目归到某个概念簇：优先用值里最高频的大写词，否则退回 key 的第二段。"""
    caps = capitalized_tokens(value)
    if caps:
        return max(caps, key=lambda tok: (rank[tok], len(tok))).lower()
    parts = key.split(".")
    return parts[1] if len(parts) > 2 else parts[0]


def looks_simple(text, max_words: int = 6) -> bool:
    if not isinstance(text, str):
        return True
    if SIMPLE_REJECT_RE.search(text):
        return False
    return len(text.split()) <= max_words


def cjk_space_hits(text) -> list[str]:
    """中文与拉丁字母/数字直接相邻的地方（按规范应加空格）。"""
    if not isinstance(text, str):
        return []
    cleaned = text
    for pattern in SPACE_STRIP_PATTERNS:
        cleaned = pattern.sub(" ", cleaned)
    return [m.group(0) for m in CJK_LATIN_HIT.finditer(cleaned)]


def extract_tokens(text: str) -> list[str]:
    """提取一处文本里所有必须原样保留的符号序列，排序后返回。

    位置参数（%1$s / %2$d）归一成 %s / %d 再比对：Java 的 format 允许位置参数与
    普通参数混用，中文语序需要调换两个参数的位置时只能写 %1$s、%2$s。参数个数
    （由归一后的多重集体现）才是真正约束，写法不是。
    """
    if not isinstance(text, str):
        return []
    out: list[str] = []
    for m in RE_JAVA_FMT.finditer(text):
        out.append(RE_FMT_INDEX.sub("%", m.group(0)))
    out.extend(m.group(0) for m in RE_SECTION.finditer(text))
    out.extend(m.group(0) for m in RE_ESCAPE.finditer(text))
    # 花括号：排除被 Java 格式串包住的情况已由上面的匹配覆盖
    for m in RE_CURLY.finditer(text):
        frag = m.group(0)
        if not frag.strip("{}").strip():
            continue
        out.append(frag)
    out.sort()
    return out


def positional_indices(text: str) -> list[int]:
    """取出文本里显式写了序号的占位符序号（%1$s → 1）。"""
    if not isinstance(text, str):
        return []
    return [int(m.group(1)) for m in re.finditer(r"%(\d+)\$", text)]


def fmt_token_diff(src: str, dst: str) -> list[str]:
    a, b = extract_tokens(src), extract_tokens(dst)
    if a == b:
        # 归一后一致，但译文可能用位置参数漏掉了某个序号
        # （%1$s 写两遍顶替 %2$s 是常见笔误，多重集看不出来）
        idx = positional_indices(dst)
        n = sum(1 for t in a if t.startswith("%") and t != "%%")
        if idx and n and sorted(set(idx)) != list(range(1, max(n, max(idx)) + 1)):
            return [f"位置参数序号不连续：{sorted(set(idx))}，原文共有 {n} 个占位符"]
        return []
    ca, cb = Counter(a), Counter(b)
    problems = []
    for t, n in ca.items():
        have = cb.get(t, 0)
        if have < n:
            problems.append(f"缺少 {t!r}×{n - have}")
    for t, n in cb.items():
        have = ca.get(t, 0)
        if have < n:
            problems.append(f"多出 {t!r}×{n - have}")
    if not problems:
        problems.append("格式码顺序不一致")
    return problems


# ---------------------------------------------------------------- 术语库

LANG_SKIP_NAMESPACES = {"minecraft", "realms"}


def find_term_matches(text: str, term_lookup: dict[int, dict[tuple, dict]]) -> list[dict]:
    """按 CFPA 规范做最长匹配：英文按词边界，从左到右，已用词不重复计。

    term_lookup: {词数: {小写词元组: 条目}}
    """
    words = re.findall(r"[A-Za-z][A-Za-z0-9'\-]*", text)
    if not words:
        return []
    low = [w.lower() for w in words]
    out: list[dict] = []
    i = 0
    maxlen = max(term_lookup) if term_lookup else 0
    while i < len(words):
        hit = None
        for n in range(min(maxlen, len(words) - i), 0, -1):
            rec = term_lookup.get(n, {}).get(tuple(low[i : i + n]))
            if rec:
                hit = (rec, n)
                break
        if hit:
            rec, n = hit
            out.append(rec)
            i += n
        else:
            i += 1
    return out


def open_glossary_db(glossary_home: Path):
    """以只读方式打开术语库，避免在只读目录（如 skill 安装目录）写入日志文件。"""
    import sqlite3

    db = Path(glossary_home).resolve() / "index" / "glossary.db"
    if not db.exists():
        return None
    for attempt in (db.as_uri() + "?mode=ro", db.as_uri() + "?mode=ro&immutable=1", str(db)):
        try:
            conn = sqlite3.connect(attempt, uri=attempt.startswith("file:"))
            conn.row_factory = sqlite3.Row
            conn.execute("SELECT COUNT(*) FROM term").fetchone()
            return conn
        except sqlite3.Error:
            continue
    return None


def load_en2zh(glossary_home: Path) -> dict[str, str]:
    """加载「英文 → 首个中文」对照，用于旧译可信度审计。"""
    conn = open_glossary_db(glossary_home)
    if conn is None:
        return {}
    table: dict[str, str] = {}
    for e, z in conn.execute("SELECT en, zh FROM memory"):
        ek, zk = (e or "").strip(), (z or "").strip()
        if ek and zk:
            table.setdefault(ek.lower(), zk)
    for e, z in conn.execute("SELECT en, zh FROM term"):
        ek = (e or "").strip()
        if not ek:
            continue
        for v in (z or "").split("|"):
            v = v.strip()
            if v:
                table.setdefault(ek.lower(), v)
    return table


def audit_legacy(en_map: dict, old_map: dict, en2zh: dict[str, str]):
    """检查旧译文是否可信。

    返回 (strong, weak)：
      strong —— 旧值其实是**本文件另一个键**的规范译名，几乎可以断定整段错位；
      weak   —— 旧值与术语库对该英文的译法不符，可能只是命名习惯不同，仅供参考。
    """
    expected = {
        k: en2zh.get(v.strip().lower())
        for k, v in en_map.items()
        if isinstance(v, str) and v.strip()
    }
    owners: dict[str, list[str]] = {}
    for k, exp in expected.items():
        if exp:
            owners.setdefault(exp, []).append(k)

    strong: list[dict] = []
    weak: list[dict] = []
    for k, ov in old_map.items():
        if k not in en_map or not isinstance(ov, str) or not ov.strip():
            continue
        value = ov.strip()
        here = expected.get(k)
        if here and here == value:
            continue
        others = [o for o in owners.get(value, []) if o != k]
        if others:
            strong.append(
                {
                    "key": k,
                    "en": en_map[k],
                    "old": ov,
                    "belongs_key": others[0],
                    "belongs_en": en_map[others[0]],
                }
            )
        elif here:
            weak.append({"key": k, "en": en_map[k], "old": ov, "expected": here})
    return strong, weak


def report_legacy_audit(strong: list[dict], weak: list[dict], label: str, max_show: int = 5) -> None:
    if not strong and not weak:
        return
    if strong:
        eprint(
            f"[warn] {label}：{len(strong)} 条旧译疑似整体错位"
            "（旧值其实是本文件里另一个键的规范译名）"
        )
        for row in strong[:max_show]:
            eprint(
                f"         {row['key']}：旧值「{row['old']}」应属于 "
                f"{row['belongs_key']}（{row['belongs_en']}），而非 {row['en']}"
            )
        eprint("         先逐条看：若只是同名词巧合（Acid=酸液、Resistance=电阻 这类，值本身不算错），")
        eprint("         只修这几条；若连成一片、整段文不对题，才是真的错位，清空重译。")
    if weak:
        eprint(f"[info] {label}：另有 {len(weak)} 条旧译与术语库不符，仅供参考（可能只是命名习惯不同）")


def load_term_lookup(conn) -> dict[int, dict[tuple, dict]]:
    lookup: dict[int, dict[tuple, dict]] = {}
    for row in conn.execute(
        "SELECT en, zh, reason, comment, scope, src FROM term ORDER BY length(en) DESC"
    ):
        en = row["en"]
        if not en:
            continue
        key = tuple(w.lower() for w in en.split())
        if not key:
            continue
        lookup.setdefault(len(key), {}).setdefault(
            key,
            {
                "en": en,
                "zh": [v for v in (row["zh"] or "").split("|") if v],
                "reason": row["reason"] or "",
                "scope": row["scope"] or "",
                "src": row["src"] or "",
            },
        )
    return lookup


def glossary_lookup_payload(conn, text: str, project_terms: dict | None = None) -> dict:
    """查一条完整的英文串：先整串记忆，再术语。"""
    res = {"text": text, "memory": [], "terms": []}
    if conn is not None:
        for row in conn.execute(
            "SELECT en, zh, src FROM memory WHERE en = ? ORDER BY src", (text,)
        ):
            res["memory"].append({"zh": row["zh"], "src": row["src"]})
        lookup = load_term_lookup(conn)
        res["terms"] = find_term_matches(text, lookup)
    if project_terms:
        pl: dict[int, dict[tuple, dict]] = {}
        for en, zh in project_terms.items():
            key = tuple(w.lower() for w in en.split())
            if not key:
                continue
            pl.setdefault(len(key), {})[key] = {
                "en": en,
                "zh": [zh],
                "reason": "项目术语",
                "scope": "",
                "src": "project",
            }
        res["terms"] = find_term_matches(text, pl) + res["terms"]
    return res


# ---------------------------------------------------------------- jar 遍历

JAR_SIGNATURE_PATHS = re.compile(r"^META-INF/[^/]+\.(SF|RSA|DSA|EC)$", re.I)
NESTED_JAR_RE = re.compile(r"\.jar$", re.I)
LANG_RE = re.compile(r"^assets/([^/]+)/lang/([A-Za-z0-9_\-]+)\.json$")


def iter_jar_lang_sources(jar_path: Path, max_depth: int = 3):
    """遍历 jar（含嵌套 jar）里的语言文件，产出 dict。"""
    seen: set[str] = set()

    def walk(zf: zipfile.ZipFile, outer: str | None, depth: int):
        for info in zf.infolist():
            name = info.filename
            if info.is_dir():
                continue
            m = LANG_RE.match(name)
            if m:
                yield {
                    "rel": f"{outer}!/{name}" if outer else name,
                    "inner_path": name,
                    "outer_jar": outer,
                    "namespace": m.group(1),
                    "locale": m.group(2),
                    "size": info.file_size,
                }
                continue
            if NESTED_JAR_RE.search(name) and depth < max_depth:
                key = f"{outer}!/{name}" if outer else name
                if key in seen:
                    continue
                seen.add(key)
                try:
                    data = zf.read(name)
                    with zipfile.ZipFile(io.BytesIO(data)) as inner:
                        yield from walk(inner, key, depth + 1)
                except Exception:
                    continue

    with zipfile.ZipFile(jar_path) as zf:
        yield from walk(zf, None, 0)


def read_jar_entry(jar_path: Path, outer: str | None, inner: str) -> bytes:
    if outer is None:
        with zipfile.ZipFile(jar_path) as zf:
            return zf.read(inner)
    parts = outer.split("!/")
    with zipfile.ZipFile(jar_path) as zf:
        data = zf.read(parts[0])
    for i, p in enumerate(parts[1:]):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            data = z.read(p)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return z.read(inner)


# ---------------------------------------------------------------- 模组元数据


# 这些是加载器/平台 id，不算「前置模组」
LOADER_DEP_IDS = {
    "minecraft", "neoforge", "forge", "fabric", "fabricloader", "quilt_loader",
    "quilted_fabric_api", "java", "cloth_config", "architectury",
}


def parse_mod_metadata(jar_path: Path) -> dict:
    meta = {
        "id": None,
        "name": None,
        "version": None,
        "mc": None,
        "loader": None,
        "authors": [],
        "description": "",
        "homepage": "",
        "source": "",
        "deps": [],
    }
    import tomllib

    with zipfile.ZipFile(jar_path) as zf:
        names = set(zf.namelist())

        def raw(n):
            try:
                return zf.read(n)
            except KeyError:
                return b""

        if "fabric.mod.json" in names:
            meta["loader"] = "fabric"
            try:
                d = json.loads(raw("fabric.mod.json").decode("utf-8-sig"))
                meta["id"] = d.get("id")
                meta["name"] = d.get("name")
                meta["version"] = d.get("version")
                meta["description"] = d.get("description") if isinstance(d.get("description"), str) else ""
                meta["homepage"] = d.get("contact", {}).get("homepage", "") if isinstance(d.get("contact"), dict) else ""
                meta["source"] = d.get("contact", {}).get("sources", "") if isinstance(d.get("contact"), dict) else ""
                authors = d.get("authors") or []
                meta["authors"] = [
                    a if isinstance(a, str) else (a.get("name") if isinstance(a, dict) else "")
                    for a in authors
                ]
                dep = d.get("depends", {})
                if isinstance(dep, dict):
                    meta["mc"] = str(dep.get("minecraft", "")) or None
                    for mod_id, rng in dep.items():
                        meta["deps"].append({"id": mod_id, "type": "required", "range": str(rng)})
            except Exception as e:
                meta["error"] = f"fabric.mod.json 解析失败: {e}"

        for toml_name in ("META-INF/neoforge.mods.toml", "META-INF/mods.toml"):
            if toml_name in names:
                meta["loader"] = meta["loader"] or ("neoforge" if "neoforge" in toml_name else "forge")
                try:
                    d = tomllib.loads(raw(toml_name).decode("utf-8-sig"))
                    mods = d.get("mods") or [{}]
                    first = mods[0] if isinstance(mods, list) and mods else {}
                    meta["id"] = meta["id"] or first.get("modId")
                    meta["name"] = meta["name"] or first.get("displayName")
                    meta["version"] = meta["version"] or first.get("version")
                    meta["authors"] = meta["authors"] or [first.get("authors")] if first.get("authors") else meta["authors"]
                    meta["description"] = meta["description"] or str(first.get("description", ""))
                    deps = d.get("dependencies") or {}
                    for _, lst in (deps.items() if isinstance(deps, dict) else []):
                        for dep in lst if isinstance(lst, list) else []:
                            dep_id = str(dep.get("modId", ""))
                            if dep_id.lower() == "minecraft":
                                meta["mc"] = meta["mc"] or dep.get("versionRange")
                            if dep_id:
                                meta["deps"].append(
                                    {
                                        "id": dep_id,
                                        "type": str(dep.get("type", "required")),
                                        "range": dep.get("versionRange"),
                                    }
                                )
                except Exception as e:
                    meta["error"] = f"{toml_name} 解析失败: {e}"
                break

        if "mcmod.info" in names and not meta["id"]:
            try:
                d = json.loads(raw("mcmod.info").decode("utf-8-sig"))
                if isinstance(d, dict):
                    d = d.get("modList") or [d]
                first = d[0] if isinstance(d, list) and d else {}
                meta["loader"] = meta["loader"] or "forge-legacy"
                meta["id"] = first.get("modid")
                meta["name"] = first.get("name")
                meta["version"] = first.get("version")
                meta["mc"] = first.get("mcversion")
                meta["description"] = first.get("description") or ""
            except Exception:
                pass

        if "META-INF/MANIFEST.MF" in names:
            txt = raw("META-INF/MANIFEST.MF").decode("utf-8", "replace")
            for line in txt.splitlines():
                if line.lower().startswith("implementation-title:") and not meta["name"]:
                    meta["name"] = line.split(":", 1)[1].strip()

        meta["has_signature"] = any(JAR_SIGNATURE_PATHS.match(n) for n in names)
        meta["nested_jars"] = sorted(n for n in names if NESTED_JAR_RE.search(n))
        meta["mcreator"] = any(n.startswith("net/mcreator/") for n in names)
        meta["entry_count"] = len(names)

    filename = jar_path.name
    m = re.search(r"(?:^|[-_])mc?(\d+\.\d+(?:\.\d+)?)", filename, re.I)
    if not meta["mc"] and m:
        meta["mc"] = f"~{m.group(1)}"
    return meta


# ---------------------------------------------------------------- 工作目录

MCT_MARK = "mct.json"


def work_manifest_path(work: Path) -> Path:
    return work / MCT_MARK


def load_manifest(work: Path) -> dict:
    p = work_manifest_path(work)
    if not p.exists():
        raise Fail(f"{work} 不是有效的 mct 工作目录（缺少 {MCT_MARK}），请先运行 extract")
    return json.loads(p.read_text(encoding="utf-8"))


def save_manifest(work: Path, man: dict) -> None:
    man["updated"] = now_iso()
    work_manifest_path(work).write_text(
        json.dumps(man, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def load_project_glossary(work: Path) -> dict:
    p = work / "glossary.json"
    if not p.exists():
        return {"mod_name_zh": "", "terms": {}, "do_not_translate": [], "notes": ""}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"mod_name_zh": "", "terms": {}, "do_not_translate": [], "notes": ""}
    d.setdefault("terms", {})
    d.setdefault("do_not_translate", [])
    return d


# ---------------------------------------------------------------- 子命令实现


def cmd_probe(args) -> int:
    jar = Path(args.jar)
    if not jar.exists():
        raise Fail(f"找不到文件: {jar}")
    if not zipfile.is_zipfile(jar):
        raise Fail(f"不是有效的 jar/zip: {jar}")
    meta = parse_mod_metadata(jar)
    langs = list(iter_jar_lang_sources(jar))
    by_locale: dict[str, list[str]] = {}
    for t in langs:
        by_locale.setdefault(t["locale"], []).append(t["rel"])
    out = {
        "jar": str(jar),
        "size": jar.stat().st_size,
        "mod": meta,
        "locales": {k: len(v) for k, v in sorted(by_locale.items())},
        "lang_files": langs,
        "foreign_namespaces": {},
    }
    # 语言文件里若出现别的模组 id（如 bits_n_tracks 里混着 message.bits_n_bobs.*），
    # 说明这些条目描述的是联动内容，术语必须以那个模组的官方译名为准。
    dep_ids = {d["id"].lower() for d in meta.get("deps", [])} - LOADER_DEP_IDS
    hits: dict[str, list[str]] = {}
    if dep_ids:
        for t in langs:
            if t["locale"] != args.locale:
                continue
            try:
                text, _enc = decode_bytes(read_jar_entry(jar, t["outer_jar"], t["inner_path"]))
                data, _ = load_json_pairs(text)
            except Exception:
                continue
            for k in data:
                for seg in k.split("."):
                    if seg.lower() in dep_ids:
                        hits.setdefault(seg.lower(), []).append(k)
    out["foreign_namespaces"] = {ns: len(ks) for ns, ks in hits.items()}
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    print(f"jar : {jar.name}  ({jar.stat().st_size/1024:.0f} KB, {meta['entry_count']} entries)")
    print(f"mod : id={meta['id']}  name={meta['name']}  version={meta['version']}")
    print(f"      loader={meta['loader']}  mc={meta['mc']}")
    if meta.get("mcreator"):
        print("      generator=MCreator（语言文件键序随机，extract 会自动排序）")
    if meta["nested_jars"]:
        print(f"      nested jars: {len(meta['nested_jars'])}")
    dep_list = [d for d in meta.get("deps", []) if d["id"].lower() not in LOADER_DEP_IDS]
    required = [d for d in dep_list if d.get("type") == "required"]
    optional = [d for d in dep_list if d.get("type") == "optional"]
    if required:
        print("前置依赖（必需，需先取得这些模组的 jar 对齐术语）:")
        for d in required:
            print(f"      {d['id']}  {d.get('range') or ''}")
    if optional:
        names = ", ".join(d["id"] for d in optional[:8])
        more = "…" if len(optional) > 8 else ""
        print(f"      可选联动 {len(optional)} 个: {names}{more}")
    if dep_list:
        print("      提示：可选联动也会定义核心术语，自造名词前先确认这些模组有无中文本地化")
        print("            仅当译名指向同一对象时才对齐（键在对方命名空间 / 明写联动 / 引用对方概念）；")
        print("            同名词不同物各按本模组语境处理，不因别处译过就跟着译")
    if hits:
        print("⚠ 语言文件里出现了其他模组的命名空间（这些条目属于联动内容）:")
        for ns, ks in sorted(hits.items(), key=lambda kv: -len(kv[1])):
            print(f"      {ns}: {len(ks)} 条，例如 {ks[0]}")
    if required:
        eprint("[warn] 有必需前置模组：翻译前先向用户索取本体 jar，用其自带 zh_cn 作为术语基准")
    if meta.get("has_signature"):
        print("      ⚠ 该 jar 带签名，重打包会破坏签名")
    print("语言文件:")
    if not langs:
        print("  （无 assets/*/lang/*.json）")
    for t in langs:
        ns, loc = t["namespace"], t["locale"]
        flag = "  ← 源语言" if loc == args.locale else ""
        print(f"  {t['rel']}  [{ns}/{loc}, {t['size']} B]{flag}")
    print("可用 locale:", ", ".join(sorted(by_locale)) or "无")
    return 0


def cmd_extract(args) -> int:
    jar = Path(args.jar).resolve()
    if not jar.exists():
        raise Fail(f"找不到文件: {jar}")
    if not zipfile.is_zipfile(jar):
        raise Fail(f"不是有效的 jar/zip: {jar}")

    stem = jar.stem
    work = Path(args.work).resolve() if args.work else (Path.cwd() / WORK_ROOT_NAME / stem)
    if work.exists() and any(work.iterdir()) and not args.force:
        raise Fail(f"工作目录已存在且非空: {work}\n如确认覆盖请加 --force")

    meta = parse_mod_metadata(jar)
    all_langs = list(iter_jar_lang_sources(jar))
    if not all_langs:
        raise Fail("jar 内没有 assets/*/lang/*.json，无法翻译")

    sources = [t for t in all_langs if t["locale"] == args.locale]
    if not sources:
        avail = sorted({t["locale"] for t in all_langs})
        if not args.locale_fallback:
            raise Fail(
                f"没有找到 {args.locale}.json；该 jar 含 locale: {', '.join(avail)}\n"
                f"可用 --locale 指定源语言，或 --locale-fallback 自动选取"
            )
        pick = "en_us" if "en_us" in avail else avail[0]
        sources = [t for t in all_langs if t["locale"] == pick]
        eprint(f"[warn] 未找到 {args.locale}，回退使用 {pick}")

    # JarJar 内嵌的第三方库（ctm、flywheel 之类）不该翻译：它们有自己的译名，
    # 而且我们在资源包里给空译文会盖掉它们的英文回退。
    allowed_ns = {
        x.strip().lower() for x in (getattr(args, "namespace", None) or "").split(",") if x.strip()
    }
    if allowed_ns:
        skipped = sorted({t["namespace"] for t in sources if t["namespace"].lower() not in allowed_ns})
        sources = [t for t in sources if t["namespace"].lower() in allowed_ns]
        if not sources:
            raise Fail(f"--namespace 过滤后没有剩余内容，jar 内命名空间: {', '.join(skipped) or '无'}")
        if skipped:
            eprint(f"[info] 已按 --namespace 排除: {', '.join(skipped)}")

    work.mkdir(parents=True, exist_ok=True)
    (work / "source").mkdir(exist_ok=True)
    (work / "target").mkdir(exist_ok=True)
    (work / "out").mkdir(exist_ok=True)
    (work / "reference").mkdir(exist_ok=True)

    en2zh = None
    files = []
    lang_index = {(t["outer_jar"], t["inner_path"]): t for t in all_langs}
    for t in sources:
        raw = read_jar_entry(jar, t["outer_jar"], t["inner_path"])
        text, enc = decode_bytes(raw)
        try:
            data, dups = load_json_pairs(text)
        except json.JSONDecodeError as e:
            raise Fail(f"{t['rel']} 不是合法 JSON: {e}")
        style = detect_style(text)

        # MCreator 生成的模组，en_us.json 的键序是随机的；翻译前先排序，
        # 否则人工审查和分批翻译都得自己对齐。--sort / --no-sort 可强制覆盖。
        do_sort = args.sort if args.sort is not None else bool(meta.get("mcreator"))
        if do_sort:
            data = {k: data[k] for k in sorted(data)}
            if dups:
                eprint(f"[warn] {t['rel']} 有重复键 {len(dups)} 个，排序后只保留最后一个取值")

        # 源文件名保留源语言（en_us），译文文件才是 zh_cn；两边的 job 分开命名，
        # 人工翻 source/ 和 target/ 时不会看岔。
        src_safe = t["rel"].replace("!/", "__").replace("/", "__")
        target_rel = t["rel"].replace(f"/{t['locale']}.json", f"/{args.target_locale}.json")
        tgt_safe = target_rel.replace("!/", "__").replace("/", "__")
        src_path = Path("source") / src_safe
        tgt_path = Path("target") / tgt_safe
        (work / src_path).write_text(
            dump_style(data, style) if do_sort else text, encoding="utf-8"
        )

        # 若 jar 内已有目标语言文件，先复用其中的译文，避免覆盖既有汉化
        prefill: dict[str, str] = {}
        existing_info = None
        existing = lang_index.get(
            (t["outer_jar"], str(Path(t["inner_path"]).with_name(args.target_locale + ".json")).replace("\\", "/"))
        )
        if existing and existing["locale"] == args.target_locale:
            try:
                old_text, _old_enc = decode_bytes(read_jar_entry(jar, existing["outer_jar"], existing["inner_path"]))
                old_data, _ = load_json_pairs(old_text)
                prefill = {k: v for k, v in old_data.items() if k in data and isinstance(v, str) and v}
                existing_info = {"rel": existing["rel"], "keys": len(old_data), "reused": len(prefill)}
                # 旧译留档，供 mct.py audit 复查，也便于人工比对
                (work / "reference" / tgt_safe).write_text(
                    json.dumps(old_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                if old_data:
                    if en2zh is None:
                        en2zh = load_en2zh(Path(args.glossary_home))
                    strong, weak = audit_legacy(data, old_data, en2zh)
                    existing_info["misaligned"] = len(strong)
                    existing_info["offglossary"] = len(weak)
                    report_legacy_audit(strong, weak, t["rel"])
            except Exception:
                existing_info = None

        skeleton = {}
        for k, v in data.items():
            if isinstance(v, str):
                skeleton[k] = prefill.get(k, "")
            else:
                skeleton[k] = v
        if not args.no_skeleton:
            (work / tgt_path).write_text(dump_style(skeleton, style), encoding="utf-8")
        ns = t["namespace"]
        base_name = Path(t["inner_path"]).name
        target_name = args.target_locale + Path(base_name).suffix
        target_inner = str(Path(t["inner_path"]).with_name(target_name)).replace("\\", "/")
        files.append(
            {
                "rel": t["rel"],
                "namespace": ns,
                "locale": t["locale"],
                "outer_jar": t["outer_jar"],
                "inner_path": t["inner_path"],
                "target_inner_path": target_inner,
                "source_file": str(src_path).replace("\\", "/"),
                "target_file": str(tgt_path).replace("\\", "/"),
                "encoding": enc,
                "style": style,
                "sorted": do_sort,
                "key_count": len(data),
                "duplicate_keys": dups,
                "existing_target": existing_info,
                "sha256": sha256_bytes(raw),
                "empty_value_keys": sum(1 for v in data.values() if v == ""),
                "non_string_values": sum(1 for v in data.values() if not isinstance(v, str)),
            }
        )

    man = {
        "tool": "mct",
        "surface": SURFACE_VERSION,
        "created": now_iso(),
        "jar": {
            "path": str(jar),
            "name": jar.name,
            "stem": stem,
            "size": jar.stat().st_size,
            "sha256": sha256_file(jar),
        },
        "mod": meta,
        "locale": {"source": sources[0]["locale"], "target": args.target_locale},
        "files": files,
        "other_locales": sorted({t["locale"] for t in all_langs} - {sources[0]["locale"]}),
    }
    save_manifest(work, man)

    gp = work / "glossary.json"
    if not gp.exists():
        gp.write_text(
            json.dumps(
                {
                    "mod_name_zh": "",
                    "terms": {},
                    "do_not_translate": [],
                    "notes": "项目内术语表。terms 里的英文词条会在 check 时强制校验是否使用统一译法。",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    if args.json:
        print(json.dumps(man, ensure_ascii=False, indent=2))
        return 0
    total = sum(f["key_count"] for f in files)
    print(f"工作目录 : {work}")
    print(f"模组     : {meta['id']} ({meta['name']}) {meta['version']}  mc={meta['mc']} loader={meta['loader']}")
    for f in files:
        extra = []
        if f["duplicate_keys"]:
            extra.append(f"重复键 {len(f['duplicate_keys'])}")
        if f["non_string_values"]:
            extra.append(f"非字符串值 {f['non_string_values']}")
        print(f"  {f['rel']} -> {f['target_inner_path']}  {f['key_count']} 键（{f['encoding']}）"
              + ("，已按键名排序" if f.get("sorted") else ""))
        if f.get("existing_target"):
            e = f["existing_target"]
            print(f"    ↺ jar 内已有 {e['rel']}（{e['keys']} 条），沿用其中 {e['reused']} 条既有译文")
        if extra:
            print("    ⚠ " + "，".join(extra))
    print(f"合计     : {total} 条待翻译")
    print(f"下一步   : mct.py keys \"{work}\"")
    return 0


def cmd_merge(args) -> int:
    """把分散在多个 jar / 内嵌模块里的语言文件合并成一份，生成可翻译的工作目录。"""
    jars = [Path(p).resolve() for p in args.jars]
    if not jars:
        raise Fail("至少需要一个 jar")
    for j in jars:
        if not j.exists():
            raise Fail(f"找不到文件: {j}")
        if not zipfile.is_zipfile(j):
            raise Fail(f"不是有效的 jar/zip: {j}")

    work = Path(args.work).resolve() if args.work else (
        Path.cwd() / WORK_ROOT_NAME / (jars[0].stem + "-merged")
    )
    if work.exists() and any(work.iterdir()) and not args.force:
        raise Fail(f"工作目录已存在且非空: {work}\n如确认覆盖请加 --force")

    sources_by_ns: dict[str, list[dict]] = {}
    targets_by_ns: dict[str, list[dict]] = {}
    for j in jars:
        for t in iter_jar_lang_sources(j):
            entry = dict(t)
            entry["jar"] = j
            if t["locale"] == args.locale:
                sources_by_ns.setdefault(t["namespace"], []).append(entry)
            elif t["locale"] == args.target_locale:
                targets_by_ns.setdefault(t["namespace"], []).append(entry)

    if not sources_by_ns:
        raise Fail(f"没有任何 {args.locale}.json，无法合并")

    allowed = set(args.namespace) if args.namespace else None
    skipped_ns = sorted(ns for ns in sources_by_ns if allowed and ns not in allowed)
    if allowed:
        sources_by_ns = {k: v for k, v in sources_by_ns.items() if k in allowed}
        if not sources_by_ns:
            raise Fail(f"--namespace 过滤后没有剩余内容，可用: {', '.join(skipped_ns)}")

    work.mkdir(parents=True, exist_ok=True)
    for sub in ("source", "target", "out", "reference"):
        (work / sub).mkdir(exist_ok=True)

    en2zh = None
    files = []
    summary = []
    for ns in sorted(sources_by_ns):
        merged: dict[str, str] = {}
        key_source: dict[str, str] = {}
        conflicts: list[dict] = []
        for entry in sources_by_ns[ns]:
            raw = read_jar_entry(entry["jar"], entry["outer_jar"], entry["inner_path"])
            text, _enc = decode_bytes(raw)
            try:
                data, _dups = load_json_pairs(text)
            except json.JSONDecodeError as e:
                raise Fail(f"{entry['rel']} 不是合法 JSON: {e}")
            for k, v in data.items():
                if k in merged and merged[k] != v:
                    conflicts.append(
                        {"key": k, "kept": merged[k], "ignored": v, "from": entry["rel"]}
                    )
                    if args.on_conflict == "last":
                        merged[k] = v
                        key_source[k] = entry["rel"]
                    continue
                if k not in merged:
                    key_source[k] = entry["rel"]
                merged[k] = v

        if conflicts and args.on_conflict == "error":
            detail = "；".join(f"{c['key']}" for c in conflicts[:5])
            raise Fail(f"命名空间 {ns} 有 {len(conflicts)} 个键冲突（如 {detail}），请用 --on-conflict 指定取舍")

        if args.sort:
            merged = {k: merged[k] for k in sorted(merged)}
            key_source = {k: key_source[k] for k in merged}

        style = {"indent": 2, "newline": "\n", "trailing_newline": True, "had_bom": False, "compact": False}
        safe = f"assets__{ns}__lang__{args.locale}.json"
        target_safe = f"assets__{ns}__lang__{args.target_locale}.json"
        (work / "source" / safe).write_text(dump_style(merged, style), encoding="utf-8")

        # 既有译文：写进 reference 供人工核对，同时作为骨架预填
        existing_raw: dict[str, str] = {}
        existing_modules = []
        for entry in targets_by_ns.get(ns, []):
            raw = read_jar_entry(entry["jar"], entry["outer_jar"], entry["inner_path"])
            text, _enc = decode_bytes(raw)
            try:
                data, _ = load_json_pairs(text)
            except json.JSONDecodeError:
                continue
            existing_modules.append(entry["rel"])
            for k, v in data.items():
                if isinstance(v, str) and k not in existing_raw:
                    existing_raw[k] = v
        existing_info = None
        if existing_raw:
            (work / "reference" / target_safe).write_text(
                json.dumps(existing_raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            prefill = {k: v for k, v in existing_raw.items() if k in merged and v}
            existing_info = {
                "rel": ", ".join(existing_modules),
                "keys": len(existing_raw),
                "reused": len(prefill),
                "missing_in_source": len([k for k in existing_raw if k not in merged]),
            }
            if en2zh is None:
                en2zh = load_en2zh(Path(args.glossary_home))
            strong, weak = audit_legacy(merged, existing_raw, en2zh)
            existing_info["misaligned"] = len(strong)
            existing_info["offglossary"] = len(weak)
            report_legacy_audit(strong, weak, ns)
        else:
            prefill = {}

        skeleton = {k: (prefill.get(k, "") if isinstance(v, str) else v) for k, v in merged.items()}
        if not args.no_skeleton:
            (work / "target" / target_safe).write_text(dump_style(skeleton, style), encoding="utf-8")

        files.append(
            {
                "rel": f"assets/{ns}/lang/{args.locale}.json（{len(sources_by_ns[ns])} 个模块合并）",
                "namespace": ns,
                "locale": args.locale,
                "outer_jar": None,
                "inner_path": f"assets/{ns}/lang/{args.locale}.json",
                "target_inner_path": f"assets/{ns}/lang/{args.target_locale}.json",
                "source_file": f"source/{safe}",
                "target_file": f"target/{target_safe}",
                "encoding": "utf-8",
                "style": style,
                "key_count": len(merged),
                "duplicate_keys": [],
                "existing_target": existing_info,
                "modules": [e["rel"] for e in sources_by_ns[ns]],
                "conflicts": len(conflicts),
                "sha256": "",
                "empty_value_keys": sum(1 for v in merged.values() if v == ""),
                "non_string_values": sum(1 for v in merged.values() if not isinstance(v, str)),
            }
        )
        summary.append((ns, len(sources_by_ns[ns]), len(merged), len(existing_raw), existing_info, conflicts))

    key_sources = {}
    for f in files:
        src = work / f["source_file"]
        data, _ = load_json_pairs(src.read_text(encoding="utf-8"))
        for k in data:
            key_sources.setdefault(k, []).append(f["namespace"])
    (work / "key_sources.json").write_text(
        json.dumps(key_sources, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    man = {
        "tool": "mct",
        "surface": SURFACE_VERSION,
        "created": now_iso(),
        "mode": "merged",
        "jar": {
            "path": str(jars[0]),
            "name": jars[0].name,
            "stem": jars[0].stem,
            "size": sum(j.stat().st_size for j in jars),
            "sha256": sha256_file(jars[0]),
            "inputs": [str(j) for j in jars],
        },
        "mod": parse_mod_metadata(jars[0]),
        "locale": {"source": args.locale, "target": args.target_locale},
        "files": files,
        "skipped_namespaces": skipped_ns,
    }
    save_manifest(work, man)

    gp = work / "glossary.json"
    if not gp.exists():
        gp.write_text(
            json.dumps(
                {
                    "mod_name_zh": "",
                    "terms": {},
                    "do_not_translate": [],
                    "notes": "项目内术语表。terms 里的英文词条会在 check 时强制校验是否使用统一译法。",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    if args.json:
        print(json.dumps(man, ensure_ascii=False, indent=2))
        return 0
    print(f"工作目录 : {work}")
    for ns, nmod, nkeys, nold, info, conflicts in summary:
        print(f"  命名空间 {ns}：{nmod} 个模块 -> {nkeys} 个键")
        if info:
            print(
                f"    既有译文 {info['keys']} 条，命中当前 {info['reused']} 条，"
                f"其余 {info['missing_in_source']} 条在当前版本已不存在"
            )
        if conflicts:
            print(f"    ⚠ {len(conflicts)} 个键在多模块间取值不同，已保留首个（--on-conflict 可改）")
    if skipped_ns:
        print(f"  已跳过命名空间：{', '.join(skipped_ns)}")
    print(f"下一步   : mct.py keys \"{work}\"")
    return 0


def _iter_target_files(work: Path, man: dict):
    for f in man["files"]:
        src = work / f["source_file"]
        tgt = work / f["target_file"]
        src_text = src.read_text(encoding="utf-8")
        if src_text.startswith("\ufeff"):
            src_text = src_text[1:]
        src_data, _ = load_json_pairs(src_text)
        tgt_data = None
        tgt_text = ""
        if tgt.exists():
            tgt_text = tgt.read_text(encoding="utf-8")
            try:
                tgt_data, _dups = load_json_pairs(tgt_text.lstrip("\ufeff"))
            except json.JSONDecodeError as e:
                raise Fail(f"{f['target_file']} 不是合法 JSON: {e}")
        yield f, src_data, tgt_data, tgt_text


def cmd_keys(args) -> int:
    work = Path(args.work).resolve()
    man = load_manifest(work)
    g = load_project_glossary(work)
    conn = open_glossary_db(Path(args.glossary_home)) if not args.no_glossary else None
    lookup = load_term_lookup(conn) if conn else {}
    rows = []
    for f, src_data, tgt_data, _t in _iter_target_files(work, man):
        for k, v in src_data.items():
            cur = (tgt_data or {}).get(k)
            if tgt_data is None:
                todo = True
            elif v == "":
                todo = k not in tgt_data
            else:
                todo = cur is None or cur == "" or cur == v
            if args.missing and not todo:
                continue
            if args.min_chars and not (isinstance(v, str) and len(v) >= args.min_chars):
                continue
            if args.complex is not None:
                simple = looks_simple(v, args.max_words)
                if simple == args.complex:
                    continue
            rows.append((k, v, cur))
    if args.cluster:
        rank = Counter()
        for _f, src_data, _t, _tt in _iter_target_files(work, man):
            rank.update(cluster_rank(src_data))
        rows.sort(key=lambda r: (key_cluster(r[0], r[1], rank), r[0]))
    if args.offset:
        rows = rows[args.offset :]
    if args.limit:
        rows = rows[: args.limit]
    if args.json:
        payload = []
        for k, en, cur in rows:
            item = {"key": k, "en": en}
            if cur:
                item["zh"] = cur
            if lookup:
                hits = find_term_matches(en, lookup)
                if hits:
                    item["terms"] = [
                        {"en": h["en"], "zh": "/".join(h["zh"]), "reason": h["reason"]}
                        for h in hits
                    ]
            payload.append(item)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")

    for k, en, cur in rows:
        cols = [k, esc(en)]
        if args.terms and lookup:
            hits = find_term_matches(en, lookup)
            cols.append("; ".join(f"{h['en']}={ '/'.join(h['zh']) }" for h in hits))
        if args.show_current:
            cols.append(esc(cur or ""))
        print("\t".join(cols))
    eprint(f"[mct] {len(rows)} 条")
    return 0


def cmd_nominal(args) -> int:
    """译前名词盘点：列出反复出现的专有名词、大写短语、重复英文条目。"""
    work = Path(args.work).resolve()
    man = load_manifest(work)

    tokens: Counter = Counter()
    phrases: Counter = Counter()
    dupes: dict[str, list[str]] = {}
    total = 0
    for _f, src_data, _t, _tt in _iter_target_files(work, man):
        for k, v in src_data.items():
            if not isinstance(v, str):
                continue
            total += 1
            for tok in capitalized_tokens(v):
                tokens[tok] += 1
            words = [w.strip(".,:;!?()[]") for w in v.split()]
            words = [w for w in words if w]
            for n in (2, 3):
                for i in range(len(words) - n + 1):
                    chunk = words[i : i + n]
                    if all(CAP_TOKEN_RE.fullmatch(w) for w in chunk):
                        phrases[" ".join(chunk)] += 1
            if v.strip():
                dupes.setdefault(v, []).append(k)

    hot_tokens = [(t, c) for t, c in tokens.most_common() if c >= args.min]
    hot_phrases = [(p, c) for p, c in phrases.most_common() if c >= args.min]
    repeated = {v: ks for v, ks in dupes.items() if len(ks) >= 2}

    if args.json:
        print(json.dumps(
            {
                "keys": total,
                "tokens": [{"en": t, "count": c} for t, c in hot_tokens],
                "phrases": [{"en": p, "count": c} for p, c in hot_phrases],
                "repeated": [{"en": v, "count": len(ks), "keys": ks} for v, ks in
                             sorted(repeated.items(), key=lambda kv: -len(kv[1]))],
            },
            ensure_ascii=False, indent=2))
        return 0

    print(f"共 {total} 条；大写词 {len(hot_tokens)} 个、大写短语 {len(hot_phrases)} 个、"
          f"重复英文 {len(repeated)} 组（出现 ≥{args.min} 次）\n")

    print(f"== 高频专名（≥{args.min} 次，先定名再翻正文）==")
    for t, c in hot_tokens[: args.limit]:
        print(f"  {c:4d}  {t}")

    print(f"\n== 高频大写短语（≥{args.min} 次）==")
    for p, c in hot_phrases[: args.limit]:
        print(f"  {c:4d}  {p}")

    print(f"\n== 重复英文条目（同一条英文出现在多个键，只翻一次）==")
    for v, ks in sorted(repeated.items(), key=lambda kv: -len(kv[1]))[: args.limit]:
        print(f"  {len(ks):3d}× {v}")
        for k in ks[:4]:
            print(f"        {k}")
        if len(ks) > 4:
            print(f"        …… 另有 {len(ks) - 4} 处")
    return 0


def cmd_apply(args) -> int:
    work = Path(args.work).resolve()
    man = load_manifest(work)
    payload = json.loads(Path(args.file).read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise Fail("译文文件必须是 {key: 译文} 形式的 JSON 对象")
    for k, v in payload.items():
        if not isinstance(v, str):
            raise Fail(f"{k} 的译文不是字符串: {v!r}")

    if args.by_en:
        # 按英文原文套用，同一句英文只翻一次
        known_en: set[str] = set()
        for _f, src_data, _tgt, _t in _iter_target_files(work, man):
            known_en |= {v for v in src_data.values() if isinstance(v, str)}
        unknown_en = [e for e in payload if e not in known_en]
        applied = 0
        for f, src_data, tgt_data, _t in _iter_target_files(work, man):
            tgt_data = dict(tgt_data) if tgt_data is not None else {}
            changed = False
            for k, en in src_data.items():
                if isinstance(en, str) and en in payload:
                    tgt_data[k] = payload[en]
                    applied += 1
                    changed = True
            if changed:
                ordered = {k: tgt_data.get(k, "") for k in src_data}
                style = (
                    detect_style((work / f["target_file"]).read_text(encoding="utf-8"))
                    if (work / f["target_file"]).exists()
                    else f["style"]
                )
                (work / f["target_file"]).write_text(dump_style(ordered, style), encoding="utf-8")
        print(f"应用 {applied} 条（按英文原文套用）")
        if unknown_en:
            print(f"⚠ {len(unknown_en)} 条英文在源文件中找不到，已忽略（前 3 个：{', '.join(unknown_en[:3])}）")
        return 0

    all_src_keys: set[str] = set()
    for f, src_data, _tgt, _t in _iter_target_files(work, man):
        all_src_keys |= set(src_data)
    unknown_keys = [k for k in payload if k not in all_src_keys]

    applied = 0
    for f, src_data, tgt_data, _t in _iter_target_files(work, man):
        tgt_data = dict(tgt_data) if tgt_data is not None else {}
        changed = False
        for k, v in payload.items():
            if k in src_data:
                tgt_data[k] = v
                applied += 1
                changed = True
        if changed:
            ordered = {k: tgt_data.get(k, "") for k in src_data}
            style = (
                detect_style((work / f["target_file"]).read_text(encoding="utf-8"))
                if (work / f["target_file"]).exists()
                else f["style"]
            )
            (work / f["target_file"]).write_text(dump_style(ordered, style), encoding="utf-8")
    print(f"应用 {applied} 条")
    if unknown_keys:
        print(f"⚠ {len(unknown_keys)} 条键在源文件中不存在，已忽略（前 5 个：{', '.join(unknown_keys[:5])}）")
    return 0


def cmd_fmt(args) -> int:
    work = Path(args.work).resolve()
    man = load_manifest(work)
    for f, src_data, tgt_data, _t in _iter_target_files(work, man):
        if tgt_data is None:
            continue
        ordered = {}
        for k in src_data:
            if k in tgt_data:
                ordered[k] = tgt_data[k]
        missing = [k for k in src_data if k not in tgt_data]
        extra = [k for k in tgt_data if k not in src_data]
        (work / f["target_file"]).write_text(dump_style(ordered, f["style"]), encoding="utf-8")
        print(f"重排 {f['target_file']}：{len(ordered)} 键")
        if missing:
            print(f"  ⚠ 缺 {len(missing)} 键（前 3：{missing[:3]}）")
        if extra:
            print(f"  ⚠ 多 {len(extra)} 键（前 3：{extra[:3]}）")
    return 0


def cmd_audit(args) -> int:
    """复查 reference/ 里的旧译文：是否整体错位、是否与术语库冲突。"""
    work = Path(args.work).resolve()
    man = load_manifest(work)
    en2zh = load_en2zh(Path(args.glossary_home))
    if not en2zh:
        raise Fail("术语库索引不可用，无法审计旧译文")

    report = []
    total_strong = total_weak = total_checked = 0
    for f in man["files"]:
        src = work / f["source_file"]
        if not src.exists():
            continue
        en_map, _ = load_json_pairs(src.read_text(encoding="utf-8").lstrip("\ufeff"))
        ref = work / "reference" / Path(f["target_file"]).name
        if not ref.exists():
            continue
        old_map, _ = load_json_pairs(ref.read_text(encoding="utf-8").lstrip("\ufeff"))
        strong, weak = audit_legacy(en_map, old_map, en2zh)
        total_strong += len(strong)
        total_weak += len(weak)
        total_checked += len([k for k in old_map if k in en_map])
        report.append(
            {
                "file": Path(f["target_file"]).name,
                "old_keys": len(old_map),
                "checked": len([k for k in old_map if k in en_map]),
                "misaligned": strong,
                "off_glossary": weak,
            }
        )

    if not report:
        raise Fail("reference/ 里没有旧译文可比对（extract / merge 时若 jar 内无目标语言文件就不会生成）")

    if args.json:
        print(json.dumps(
            {"checked": total_checked, "misaligned": total_strong, "off_glossary": total_weak, "files": report},
            ensure_ascii=False, indent=2))
        return 1 if total_strong else 0

    print(f"旧译复查：比对 {total_checked} 条，强错位 {total_strong} 条，与术语库不符 {total_weak} 条")
    for item in report:
        print(f"\n  {item['file']}")
        print(f"    旧译 {item['old_keys']} 条，可与当前源对上 {item['checked']} 条")
        if item["misaligned"]:
            print(f"    ✗ 强错位 {len(item['misaligned'])} 条（旧值其实是别的键的规范译名）")
            for row in item["misaligned"][: args.max_show]:
                print(f"        {row['key']}")
                print(f"          当前英文 {row['en']}，旧值「{row['old']}」，该值应属于 {row['belongs_key']}（{row['belongs_en']}）")
        if item["off_glossary"]:
            print(f"    ? 与术语库不符 {len(item['off_glossary'])} 条（可能只是命名习惯，供参考）")
            for row in item["off_glossary"][: args.max_show]:
                print(f"        {row['key']}: 旧值「{row['old']}」，术语库给 {row['en']} 的译法是「{row['expected']}」")
    if total_strong:
        print("\n结论：这份旧译存在整段错位，建议清空重译，不要沿用。")
    elif total_weak:
        print("\n结论：没有发现整段错位，但部分旧值与术语库不符，逐条确认后再沿用。")
    else:
        print("\n结论：未发现明显问题，旧译可以作为起点沿用。")
    return 1 if total_strong else 0


CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
ASCII_WORD_RE = re.compile(r"[A-Za-z]{3,}")


def cmd_check(args) -> int:
    work = Path(args.work).resolve()
    man = load_manifest(work)
    project = load_project_glossary(work)
    conn = open_glossary_db(Path(args.glossary_home))
    lookup = load_term_lookup(conn) if conn else {}

    errors: list[dict] = []
    warns: list[dict] = []
    stats = {
        "keys": 0,
        "translated": 0,
        "identical": 0,
        "empty": 0,
        "files": 0,
    }

    term_usage: dict[str, set[str]] = {}

    for f, src_data, tgt_data, tgt_text in _iter_target_files(work, man):
        stats["files"] += 1
        if tgt_data is None:
            errors.append({"type": "missing_file", "file": f["target_file"], "msg": "译文文件不存在"})
            continue
        if not args.allow_missing:
            for k in src_data:
                if k not in tgt_data:
                    errors.append({"type": "missing_key", "key": k, "msg": "缺少键"})
        for k in tgt_data:
            if k not in src_data:
                errors.append({"type": "extra_key", "key": k, "msg": "源文件中不存在该键"})
        if not args.ignore_order:
            src_keys = list(src_data)
            tgt_keys = [k for k in tgt_data if k in src_data]
            if tgt_keys != src_keys:
                errors.append({"type": "key_order", "file": f["target_file"], "msg": "键顺序与源文件不一致，请运行 fmt"})
        if f["style"].get("had_bom"):
            warns.append({"type": "bom", "file": f["source_file"], "msg": "源文件带 BOM，已按无 BOM 输出"})

        for k, en in src_data.items():
            if k not in tgt_data:
                continue
            zh = tgt_data[k]
            stats["keys"] += 1
            if not isinstance(zh, str):
                errors.append({"type": "type", "key": k, "msg": f"值不是字符串: {zh!r}"})
                continue
            if not isinstance(en, str):
                continue
            if en == "":
                if zh != "":
                    warns.append({"type": "empty_source", "key": k, "msg": "源为空串，建议保留空串"})
                continue
            if zh == "":
                stats["empty"] += 1
                if not args.allow_missing:
                    errors.append({"type": "untranslated", "key": k, "msg": "译文为空", "en": en})
                continue
            stats["translated"] += 1
            if zh == en:
                stats["identical"] += 1
                if k in project.get("do_not_translate", []) or en in project.get("do_not_translate", []):
                    pass
                else:
                    warns.append({"type": "identical", "key": k, "msg": "译文与原文相同", "en": en, "zh": zh})
            diff = fmt_token_diff(en, zh)
            if diff:
                errors.append({"type": "format", "key": k, "msg": "；".join(diff), "en": en, "zh": zh})
            if en.startswith(" ") != zh.startswith(" "):
                warns.append({"type": "space", "key": k, "msg": "行首空格不一致", "en": en, "zh": zh})
            if en.endswith(" ") != zh.endswith(" "):
                warns.append({"type": "space", "key": k, "msg": "行尾空格不一致", "en": en, "zh": zh})
            if TIGHT_KEY_RE.search(k) and len(zh) > max(16, len(en) + 6):
                warns.append(
                    {
                        "type": "length",
                        "key": k,
                        "msg": f"GUI 文本偏长（中文 {len(zh)} 字 / 英文 {len(en)} 字符）",
                        "en": en,
                        "zh": zh,
                    }
                )
            space_hits = cjk_space_hits(zh)
            if space_hits:
                warns.append(
                    {
                        "type": "cjk-space",
                        "key": k,
                        "msg": "中文与英文/数字之间缺空格：" + "、".join(sorted(set(space_hits))[:3]),
                        "en": en,
                        "zh": zh,
                    }
                )
            # 原文没有百分号而译文里出现：多半是把 % 当成了占位符（% 单独出现不构成合法占位符，
            # 占位符比对查不出来）。真要是百分比文案，原文通常也带 %。
            if "%" in zh and "%" not in en:
                warns.append(
                    {
                        "type": "stray-percent",
                        "key": k,
                        "msg": "原文没有 % 号，译文里出现了 %",
                        "en": en,
                        "zh": zh,
                    }
                )
            if args.term_check and lookup:
                for hit in find_term_matches(en, lookup):
                    variants = hit["zh"]
                    if not variants:
                        continue
                    en_term = hit["en"]
                    # 单词且过短的术语跨领域误报率极高（Track/Press/Only/Link…），默认跳过
                    if " " not in en_term and len(en_term) < args.term_min_len:
                        continue
                    if not any(v in zh for v in variants):
                        warns.append(
                            {
                                "type": "term",
                                "key": k,
                                "msg": f"未使用术语 {hit['en']} -> {'/'.join(variants)}",
                                "en": en,
                                "zh": zh,
                                "reason": hit["reason"],
                            }
                        )
                    else:
                        # 同一词条的不同变体可能互为子串（橙/橙色、滞留/滞留型），只取命中的最长一截
                        matched = [v for v in variants if v in zh]
                        term_usage.setdefault(hit["en"], set()).add(max(matched, key=len))

    # 项目术语表一致性
    for en_term, zh_term in project.get("terms", {}).items():
        hits = 0
        for f, src_data, tgt_data, _t in _iter_target_files(work, man):
            if not tgt_data:
                continue
            for k, en in src_data.items():
                zh = tgt_data.get(k)
                # 未翻译的空值不参与术语校验，否则半成品阶段会被几百条噪声淹掉
                if not isinstance(zh, str) or not isinstance(en, str) or not zh:
                    continue
                if re.search(r"(?<![A-Za-z])" + re.escape(en_term) + r"(?![A-Za-z])", en):
                    hits += 1
                    if zh_term not in zh:
                        warns.append(
                            {
                                "type": "project_term",
                                "key": k,
                                "msg": f"未采用项目术语 {en_term} -> {zh_term}",
                                "en": en,
                                "zh": zh,
                            }
                        )
        if hits == 0:
            warns.append(
                {"type": "project_term_unused", "key": "", "msg": f"项目术语 {en_term} 未在任何条目中出现"}
            )

    # 同一英文术语多种译法
    for en_term, variants in sorted(term_usage.items()):
        if len(variants) > 1:
            warns.append(
                {
                    "type": "term_inconsistent",
                    "key": "",
                    "msg": f"术语 {en_term} 出现多种译法: {' / '.join(sorted(variants))}",
                }
            )

    report = {
        "work": str(work),
        "checked": now_iso(),
        "stats": stats,
        "puns": len(project.get("pun", {}) or {}),
        "errors": errors,
        "warnings": warns,
    }
    (work / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            f"文件 {stats['files']}  条目 {stats['keys']}  已译 {stats['translated']}  "
            f"空 {stats['empty']}  与原文相同 {stats['identical']}"
        )
        if report["puns"]:
            print(f"梗与双关留档：{report['puns']} 条（见 glossary.json 的 pun 字段）")
        if errors:
            print(f"\n✗ 错误 {len(errors)} 条：")
            for e in errors[: args.max_show]:
                key = e.get("key") or e.get("file", "")
                print(f"  [{e['type']}] {key}: {e['msg']}")
                if e.get("en") is not None and e["type"] in ("format",):
                    print(f"      en: {e['en']}")
                    print(f"      zh: {e['zh']}")
            if len(errors) > args.max_show:
                print(f"  …… 另有 {len(errors) - args.max_show} 条，见 report.json")
        if warns:
            by_type: dict[str, list] = {}
            for w in warns:
                by_type.setdefault(w["type"], []).append(w)
            print(f"\n⚠ 警告 {len(warns)} 条：")
            for t, lst in by_type.items():
                print(f"  [{t}] {len(lst)} 条")
                for w in lst[: args.max_show]:
                    key = w.get("key") or ""
                    print(f"      {key}: {w['msg']}")
                if len(lst) > args.max_show:
                    print(f"      …… 另有 {len(lst) - args.max_show} 条")
        print(f"\n报告: {work / 'report.json'}")
    return 1 if errors else 0


def _rewrite_zip_bytes(data: bytes, replacements: dict[str, bytes], removals: set[str]) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    names = src.namelist()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for item in src.infolist():
            if item.is_dir() or item.filename in removals:
                continue
            payload = replacements.get(item.filename)
            if payload is None:
                payload = src.read(item.filename)
            zi = zipfile.ZipInfo(item.filename, date_time=item.date_time)
            zi.compress_type = (
                item.compress_type
                if item.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                else zipfile.ZIP_DEFLATED
            )
            zi.external_attr = item.external_attr
            out.writestr(zi, payload)
        for name, payload in replacements.items():
            if name in names:
                continue
            zi = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            out.writestr(zi, payload)
    return buf.getvalue()


def cmd_pack(args) -> int:
    work = Path(args.work).resolve()
    man = load_manifest(work)
    jar_path = Path(man["jar"]["path"])

    # 收集每个文件的译文
    payloads: list[tuple[dict, bytes]] = []
    for f, src_data, tgt_data, _t in _iter_target_files(work, man):
        if tgt_data is None:
            raise Fail(f"缺少译文文件 {f['target_file']}，请先运行 extract")
        missing = [k for k in src_data if k not in tgt_data]
        if missing and not args.allow_missing:
            raise Fail(
                f"{f['target_file']} 缺 {len(missing)} 个键（例：{missing[:3]}）。"
                f"确认要打包残缺版本请加 --allow-missing"
            )
        ordered = {k: tgt_data.get(k, "") for k in src_data}
        payloads.append((f, dump_style(ordered, f["style"]).encode("utf-8")))

    outdir = work / "out"
    outdir.mkdir(exist_ok=True)
    produced = []

    if args.jar:
        if not jar_path.exists():
            raise Fail(f"原 jar 不存在: {jar_path}")
        if man["mod"].get("has_signature"):
            eprint("[warn] 原 jar 带签名，重打包会破坏签名（不影响多数加载器）")
        top_repl: dict[str, bytes] = {}
        top_remov: set[str] = set()
        nested_groups: dict[str, list[tuple[str, bytes]]] = {}
        for f, blob in payloads:
            if f["outer_jar"]:
                nested_groups.setdefault(f["outer_jar"], []).append((f["target_inner_path"], blob))
            else:
                top_repl[f["target_inner_path"]] = blob
        for outer, items in nested_groups.items():
            parts = outer.split("!/")
            inner_name = parts[-1]
            with zipfile.ZipFile(jar_path) as zf:
                cur = zf.read(inner_name)
            inner_repl = {p: b for p, b in items}
            new_inner = _rewrite_zip_bytes(cur, inner_repl, set())
            top_repl[inner_name] = new_inner

        original = jar_path.read_bytes()
        new_jar = _rewrite_zip_bytes(original, top_repl, top_remov)
        out_name = f"{man['jar']['stem']}-zh_cn.jar"
        out_path = Path(args.out) if args.out else outdir / out_name
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(new_jar)
        produced.append(("jar", out_path))

    if args.resourcepack:
        mc_declared = man["mod"].get("mc")
        fmt = args.pack_format or guess_pack_format(mc_declared)
        if not args.pack_format and not _parse_ver(mc_declared):
            # 模组没声明 MC 版本时回退值可能和实际游戏版本不符（例如 1.21.1 需要 34，
            # 回退是 15），游戏会提示资源包不兼容。宁可吵闹一次，也别静默出错。
            eprint(
                f"[warn] 模组未声明 MC 版本，pack_format 回退为 {fmt}；"
                f"若目标版本不是 1.20.x，请用 --pack-format 指定（1.21.1=34）"
            )
        rp_name = args.resourcepack if args.resourcepack != "auto" else f"{man['jar']['stem']}-zh_cn-resourcepack"
        if not rp_name.endswith(".zip"):
            rp_name += ".zip"
        rp_path = outdir / rp_name
        with zipfile.ZipFile(rp_path, "w", zipfile.ZIP_DEFLATED) as z:
            mcmeta = {
                "pack": {
                    "pack_format": fmt,
                    "description": f"{man['mod'].get('name') or man['jar']['stem']} 简体中文",
                }
            }
            z.writestr("pack.mcmeta", json.dumps(mcmeta, ensure_ascii=False, indent=2))
            for f, blob in payloads:
                arc = f["target_inner_path"].replace("\\", "/")
                z.writestr(arc, blob)
        produced.append(("resourcepack", rp_path))

    if not produced:
        raise Fail("没有产出任何文件：资源包被 --no-resourcepack 关掉了，也没加 --jar")

    if args.json:
        print(json.dumps({"produced": [{"kind": k, "path": str(p)} for k, p in produced]}, ensure_ascii=False, indent=2))
    else:
        for kind, p in produced:
            extra = f"  pack_format={fmt}" if kind == "resourcepack" else ""
            print(f"✓ {kind}: {p}  ({p.stat().st_size/1024:.0f} KB){extra}")
    return 0


PACK_FORMAT_TABLE = [
    ((1, 6, 1), (1, 8, 9), 1),
    ((1, 9, 0), (1, 10, 2), 2),
    ((1, 11, 0), (1, 12, 2), 3),
    ((1, 13, 0), (1, 14, 4), 4),
    ((1, 15, 0), (1, 16, 1), 5),
    ((1, 16, 2), (1, 16, 5), 6),
    ((1, 17, 0), (1, 17, 1), 7),
    ((1, 18, 0), (1, 18, 2), 8),
    ((1, 19, 0), (1, 19, 2), 9),
    ((1, 19, 3), (1, 19, 3), 12),
    ((1, 19, 4), (1, 19, 4), 13),
    ((1, 20, 0), (1, 20, 1), 15),
    ((1, 20, 2), (1, 20, 2), 18),
    ((1, 20, 3), (1, 20, 4), 22),
    ((1, 20, 5), (1, 20, 6), 32),
    ((1, 21, 0), (1, 21, 1), 34),
    ((1, 21, 2), (1, 21, 3), 42),
    ((1, 21, 4), (1, 21, 4), 46),
    ((1, 21, 5), (1, 21, 5), 55),
    ((1, 21, 6), (1, 21, 8), 64),
]


def _parse_ver(s: str | None):
    if not s:
        return None
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", s)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def guess_pack_format(mc: str | None) -> int:
    v = _parse_ver(mc)
    if not v:
        return 15
    for lo, hi, fmt in PACK_FORMAT_TABLE:
        if lo <= v <= hi:
            return fmt
    return PACK_FORMAT_TABLE[-1][2]


# ---------------------------------------------------------------- 术语库子命令


def cmd_glossary(args) -> int:
    home = Path(args.glossary_home)
    db_path = home / "index" / "glossary.db"
    action = args.action
    if action == "build":
        return glossary_build(home, args)
    conn = open_glossary_db(home)
    if conn is None:
        raise Fail(f"术语库索引不存在: {db_path}\n请先运行: mct.py glossary build")
    if action == "stats":
        for table in ("term", "memory", "qa"):
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"{table:8s}: {n}")
        srcs = conn.execute("SELECT src, COUNT(*) c FROM term GROUP BY src ORDER BY c DESC")
        print("\n术语来源:")
        for r in srcs:
            print(f"  {r['src']:28s} {r['c']}")
        return 0
    if action == "lookup":
        texts = args.text or []
        if args.file:
            texts.extend(
                line.strip() for line in Path(args.file).read_text(encoding="utf-8-sig").splitlines() if line.strip()
            )
        if not texts:
            raise Fail("没有输入文本")
        results = []
        for t in texts:
            results.append(glossary_lookup_payload(conn, t))
        if args.json:
            print(json.dumps(results, ensure_ascii=False, indent=2))
        else:
            for r in results:
                print(f"● {r['text']}")
                if r["memory"]:
                    for m in r["memory"][:6]:
                        print(f"    记忆[{m['src']}] {m['zh']}")
                if r["terms"]:
                    for t in r["terms"]:
                        line = f"    术语 {t['en']} -> {'/'.join(t['zh'])}"
                        if t["reason"]:
                            line += f"  （{t['reason']}）"
                        print(line)
                if not r["memory"] and not r["terms"]:
                    print("    （无命中）")
        return 0
    if action == "search":
        q = args.text[0] if args.text else ""
        like = f"%{q}%"
        print(f"== memory 命中 {q} ==")
        for r in conn.execute(
            "SELECT en, zh, src FROM memory WHERE en LIKE ? OR zh LIKE ? ORDER BY length(en) LIMIT ?",
            (like, like, args.limit),
        ):
            print(f"  {r['en']}  ->  {r['zh']}   [{r['src']}]")
        print(f"== term 命中 {q} ==")
        for r in conn.execute(
            "SELECT en, zh, reason, src FROM term WHERE en LIKE ? OR zh LIKE ? ORDER BY length(en) LIMIT ?",
            (like, like, args.limit),
        ):
            print(f"  {r['en']}  ->  {r['zh']}   [{r['src']}]" + (f"  ({r['reason']})" if r["reason"] else ""))
        return 0
    if action == "qa":
        if not args.key:
            raise Fail("qa 需要 --key")
        rows = conn.execute(
            "SELECT * FROM qa WHERE (key = '' OR ? REGEXP key) AND (except_key = '' OR ? NOT REGEXP except_key)",
            (args.key, args.key),
        ).fetchall() if _sqlite_regexp(conn) else conn.execute("SELECT * FROM qa").fetchall()
        for r in rows:
            if args.en:
                try:
                    if r["en"] and not re.search(r["en"], args.en):
                        continue
                except re.error:
                    pass
            print(f"- {r['id']}: en≈/{r['en']}/  要求={r['require']!r}  禁止={r['forbid']!r}")
            if r["comment"]:
                print(f"    {r['comment']}")
        return 0
    raise Fail(f"未知动作: {action}")


def _sqlite_regexp(conn) -> bool:
    try:
        conn.create_function("REGEXP", 2, lambda pat, val: 1 if re.search(pat, val or "") else 0)
        return True
    except Exception:
        return False


def _tsv_rows(path: Path):
    text = path.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    if not lines:
        return
    header = lines[0].split("\t")
    for line in lines[1:]:
        if not line.strip():
            continue
        cells = line.split("\t")
        cells += [""] * (len(header) - len(cells))
        yield dict(zip(header, cells))


def glossary_build(home: Path, args) -> int:
    import sqlite3

    raw = home / "raw"
    if not raw.exists():
        raise Fail(f"找不到原始数据目录: {raw}")
    idx = home / "index"
    idx.mkdir(parents=True, exist_ok=True)
    db_path = idx / "glossary.db"
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.executescript(
        """
        CREATE TABLE term(en TEXT, zh TEXT, reason TEXT, comment TEXT, scope TEXT, src TEXT);
        CREATE TABLE memory(en TEXT PRIMARY KEY, zh TEXT, src TEXT);
        CREATE TABLE qa(id TEXT, key TEXT, except_key TEXT, en TEXT, require TEXT,
                        forbid TEXT, mode TEXT, except_en TEXT, comment TEXT);
        """
    )
    n_term = n_mem = 0

    cfpa = raw / "cfpa"
    terms_file = cfpa / "Vanilla" / "terms" / "terms-v1.tsv"
    rows = []
    if terms_file.exists():
        for r in _tsv_rows(terms_file):
            if not r.get("en") or not r.get("zh"):
                continue
            rows.append((r["en"], r["zh"], r.get("reason", ""), r.get("comment", ""), "", "cfpa:terms"))
    # 上游另按类别拆了 terms-<cat>.tsv（biome / color / effect / enchantment / mob / tech），
    # 不加 scope：这些术语在任意键里都成立。terms-version.tsv 是「旧版本译名，勿按最新版校验」
    # 的反向清单，与 scoped 机制语义不同，暂不导入。
    terms_dir = cfpa / "Vanilla" / "terms"
    if terms_dir.exists():
        for p in sorted(terms_dir.glob("terms-*.tsv")):
            if p.name in ("terms-v1.tsv", "terms-version.tsv"):
                continue
            cat = p.stem[len("terms-") :]
            for r in _tsv_rows(p):
                if not r.get("en") or not r.get("zh"):
                    continue
                rows.append(
                    (r["en"], r["zh"], r.get("reason", ""), r.get("comment", ""), "", f"cfpa:terms:{cat}")
                )
    special_dir = cfpa / "Vanilla" / "terms" / "special"
    if special_dir.exists():
        for p in sorted(special_dir.glob("terms-*.tsv")):
            scope = p.stem[len("terms-") :]
            for r in _tsv_rows(p):
                if not r.get("en") or not r.get("zh"):
                    continue
                rows.append(
                    (r["en"], r["zh"], r.get("reason", ""), r.get("comment", ""), scope, f"cfpa:special:{scope}")
                )
    for p, src in (
        (cfpa / "Mod" / "模组简体中文翻译指南.tsv", "cfpa:mod-guide"),
        (cfpa / "Mod" / "模组材料简体中文翻译指南.tsv", "cfpa:mod-material"),
    ):
        if p.exists():
            for r in _tsv_rows(p):
                en = r.get("Term") or r.get("en") or ""
                zh = r.get("Translation") or r.get("zh") or ""
                if not en or not zh:
                    continue
                rows.append((en, zh, "", (r.get("Remark") or "") + ((" · " + r["Source"]) if r.get("Source") else ""), "", src))
    conn.executemany("INSERT INTO term VALUES (?,?,?,?,?,?)", rows)
    n_term = len(rows)

    # 记忆库：原版语言文件优先
    latest = cfpa / "Vanilla" / "latest.tsv"
    if latest.exists():
        mem = []
        for r in _tsv_rows(latest):
            en, zh = r.get("en_us", ""), r.get("zh_cn", "")
            if en and zh:
                mem.append((en, zh, "cfpa:vanilla"))
        conn.executemany("INSERT OR IGNORE INTO memory VALUES (?,?,?)", mem)
        n_mem += len(mem)

    mini = raw / "i18n-dict-extender" / "Dict-Mini.json"
    if mini.exists():
        try:
            d = json.loads(mini.read_text(encoding="utf-8"))
        except Exception as e:
            eprint(f"[warn] Dict-Mini.json 解析失败: {e}")
            d = {}
        mem = [(k, v[0], "i18n-dict") for k, v in d.items() if isinstance(v, list) and v and isinstance(k, str)]
        conn.executemany("INSERT OR IGNORE INTO memory VALUES (?,?,?)", mem)
        n_mem += len(mem)

    qa_file = cfpa / "Vanilla" / "terms" / "qa-rules.tsv"
    if qa_file.exists():
        qrows = [
            (
                r.get("id", ""),
                r.get("key", ""),
                r.get("except_key", ""),
                r.get("en", ""),
                r.get("require", ""),
                r.get("forbid", ""),
                r.get("mode", ""),
                r.get("except_en", ""),
                r.get("comment", ""),
            )
            for r in _tsv_rows(qa_file)
        ]
        conn.executemany("INSERT INTO qa VALUES (?,?,?,?,?,?,?,?,?)", qrows)

    conn.execute("CREATE INDEX idx_term_en ON term(en)")
    conn.execute("CREATE INDEX idx_memory_en ON memory(en)")
    conn.commit()
    try:
        conn.execute("VACUUM")
    except Exception:
        pass
    conn.close()
    print(f"术语库已建立: {db_path}")
    print(f"  term   : {n_term}")
    print(f"  memory : {conn_total(db_path, 'memory')}")
    print(f"  qa     : {conn_total(db_path, 'qa')}")
    return 0


def conn_total(db: Path, table: str) -> int:
    import sqlite3

    c = sqlite3.connect(str(db))
    try:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        c.close()


# ---------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mct",
        description="Minecraft 模组语言文件翻译工具链",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--glossary-home", default=str(DEFAULT_GLOSSARY_HOME), help="术语库目录")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("probe", help="查看 jar 元数据与语言文件清单")
    sp.add_argument("jar")
    sp.add_argument("--locale", default="en_us")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_probe)

    se = sub.add_parser("extract", help="提取语言文件并生成译文骨架")
    se.add_argument("jar")
    se.add_argument("--work", default=None, help="工作目录，默认 .mct/<jar名>")
    se.add_argument("--locale", default="en_us", help="源语言，默认 en_us")
    se.add_argument("--target-locale", default="zh_cn")
    se.add_argument("--locale-fallback", action="store_true", help="源语言缺失时自动回退")
    se.add_argument(
        "--namespace",
        default=None,
        help="只提取这些命名空间（逗号分隔）。用于排除 JarJar 内嵌的第三方库，如 ctm、flywheel",
    )
    se.add_argument(
        "--sort",
        dest="sort",
        action="store_true",
        default=None,
        help="强制按键名排序原文（MCreator 模组会自动开启）",
    )
    se.add_argument(
        "--no-sort",
        dest="sort",
        action="store_false",
        help="强制保留原文顺序（会覆盖 MCreator 的自动排序）",
    )
    se.add_argument("--no-skeleton", action="store_true")
    se.add_argument("--force", action="store_true")
    se.add_argument("--json", action="store_true")
    se.set_defaults(func=cmd_extract)

    sm = sub.add_parser("merge", help="合并多个 jar / 内嵌模块里的语言文件")
    sm.add_argument("jars", nargs="+")
    sm.add_argument("--work", default=None, help="工作目录，默认 .mct/<首个jar名>-merged")
    sm.add_argument("--locale", default="en_us", help="源语言，默认 en_us")
    sm.add_argument("--target-locale", default="zh_cn")
    sm.add_argument("--namespace", action="append", default=None, help="只合并指定命名空间，可重复")
    sm.add_argument(
        "--on-conflict",
        choices=["warn", "last", "error"],
        default="warn",
        help="同一键在多模块间取值不同时怎么办：warn 保留首个并告警（默认）/ last 用后者 / error 直接失败",
    )
    sm.add_argument("--no-skeleton", action="store_true")
    sm.add_argument(
        "--no-sort",
        dest="sort",
        action="store_false",
        default=True,
        help="不按键名排序（默认会按键名排序，方便人工审查与逐段翻译）",
    )
    sm.add_argument("--force", action="store_true")
    sm.add_argument("--json", action="store_true")
    sm.set_defaults(func=cmd_merge)

    sk = sub.add_parser("keys", help="列出待翻译条目")
    sk.add_argument("work")
    sk.add_argument("--missing", action="store_true", help="只列未翻译的")
    sk.add_argument("--limit", type=int, default=0)
    sk.add_argument("--offset", type=int, default=0)
    sk.add_argument("--show-current", action="store_true")
    sk.add_argument("--terms", action="store_true", help="附加术语提示列")
    sk.add_argument("--cluster", action="store_true", help="按概念簇排序（同一个专名下的条目聚在一起）")
    sk.add_argument("--complex", dest="complex", action="store_true", default=None,
                    help="只列长句/带标点的条目")
    sk.add_argument("--simple", dest="complex", action="store_false",
                    help="只列短名词条目")
    sk.add_argument("--max-words", type=int, default=6, help="判定「简单名词」的词数上限，默认 6")
    sk.add_argument("--min-chars", type=int, default=0, help="只列原文长度 ≥ N 的条目")
    sk.add_argument("--no-glossary", action="store_true")
    sk.add_argument("--json", action="store_true")
    sk.set_defaults(func=cmd_keys)

    sa = sub.add_parser("apply", help="合并 {key: 译文}")
    sa.add_argument("work")
    sa.add_argument("file")
    sa.add_argument("--by-en", action="store_true", help="文件是 {英文: 译文}，套用到所有同英文的键")
    sa.set_defaults(func=cmd_apply)

    sn = sub.add_parser("nominal", help="译前名词盘点：高频专名、大写短语、重复条目")
    sn.add_argument("work")
    sn.add_argument("--min", type=int, default=2, help="出现次数下限，默认 2")
    sn.add_argument("--limit", type=int, default=40, help="每节最多列多少条")
    sn.add_argument("--json", action="store_true")
    sn.set_defaults(func=cmd_nominal)

    sf = sub.add_parser("fmt", help="按源文件格式重排译文")
    sf.add_argument("work")
    sf.set_defaults(func=cmd_fmt)

    su = sub.add_parser("audit", help="复查 reference/ 里的旧译文是否可信")
    su.add_argument("work")
    su.add_argument("--json", action="store_true")
    su.add_argument("--max-show", type=int, default=8)
    su.set_defaults(func=cmd_audit)

    sc = sub.add_parser("check", help="校验译文")
    sc.add_argument("work")
    sc.add_argument("--allow-missing", action="store_true")
    sc.add_argument("--ignore-order", action="store_true")
    sc.add_argument("--term-check", action="store_true", default=True)
    sc.add_argument("--no-term-check", dest="term_check", action="store_false")
    sc.add_argument(
        "--term-min-len",
        type=int,
        default=6,
        help="单个英文单词术语的最短校验长度，短于此长度不报警（默认 6，避免跨领域误报）",
    )
    sc.add_argument("--json", action="store_true")
    sc.add_argument("--max-show", type=int, default=25)
    sc.set_defaults(func=cmd_check)

    pk = sub.add_parser("pack", help="打包译文（默认只产出资源包）")
    pk.add_argument("work")
    pk.add_argument(
        "--resourcepack",
        nargs="?",
        const="auto",
        default="auto",
        help="资源包文件名（不给值则自动命名）；默认产出",
    )
    pk.add_argument(
        "--no-resourcepack",
        dest="resourcepack",
        action="store_const",
        const=False,
        help="不产出资源包",
    )
    pk.add_argument("--jar", action="store_true", help="额外产出覆盖用的 jar（默认不产出）")
    pk.add_argument("--out", default=None, help="jar 输出路径（配合 --jar）")
    pk.add_argument("--pack-format", type=int, default=None)
    pk.add_argument("--allow-missing", action="store_true")
    pk.add_argument("--json", action="store_true")
    pk.set_defaults(func=cmd_pack)

    sg = sub.add_parser("glossary", help="术语库")
    sg.add_argument("action", choices=["build", "stats", "lookup", "search", "qa"])
    sg.add_argument("text", nargs="*")
    sg.add_argument("--file", default=None)
    sg.add_argument("--key", default=None)
    sg.add_argument("--en", default=None)
    sg.add_argument("--limit", type=int, default=20)
    sg.add_argument("--json", action="store_true")
    sg.set_defaults(func=cmd_glossary)

    # --glossary-home 既是全局参数，也允许写在子命令后面；SUPPRESS 保证不覆盖全局值
    for _sub in sub.choices.values():
        _sub.add_argument("--glossary-home", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    return p


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "glossary_home") or not args.glossary_home:
        args.glossary_home = str(DEFAULT_GLOSSARY_HOME)
    try:
        return args.func(args)
    except Fail as e:
        eprint(f"错误: {e}")
        return 2
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
