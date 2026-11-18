# -*- coding: utf-8 -*-
"""mct.py 回归自测。

跑法：
    python selftest.py            # 或者按技能里的 _run.ps1 + Run-Py 方式调用

覆盖：普通模组提取 / MCreator 自动排序 / --no-sort / 多模块合并 / 旧译错位审计 /
apply + check 的错误检出 / 资源包结构。

子进程一律把输出重定向到文件（不用管道）——DSH 沙箱里管道会失败。
"""
import json
import pathlib
import shutil
import subprocess
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent
MCT = ROOT / "mct.py"
GLOSSARY = ROOT.parent / "glossary"
RUN = ROOT / ".selftest"
PY = sys.executable

FAILED = []
PASSED = 0


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL  {name}" + (f"\n        {detail}" if detail else ""))


def run(*args):
    RUN.mkdir(parents=True, exist_ok=True)
    out, err = RUN / "out.txt", RUN / "err.txt"
    with open(out, "w", encoding="utf-8") as o, open(err, "w", encoding="utf-8") as e:
        rc = subprocess.call([PY, str(MCT), *args], stdout=o, stderr=e)
    return rc, out.read_text(encoding="utf-8"), err.read_text(encoding="utf-8")


def make_jar(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, payload in entries.items():
            if isinstance(payload, (bytes, bytearray)):
                pass
            elif isinstance(payload, str):
                pass
            else:
                payload = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            z.writestr(name, payload)
    return path


def keys_of(p):
    return list(json.loads(pathlib.Path(p).read_text(encoding="utf-8")))


def main():
    if RUN.exists():
        shutil.rmtree(RUN)
    RUN.mkdir(parents=True)
    jars = RUN / "jars"

    # ---------------------------------------------------------------- 普通模组
    print("\n[1] 普通模组：extract 保留原序")
    plain_lang = {"item.z.third": "Third", "block.z.alpha": "Alpha", "item.z.second": "Second"}
    plain = make_jar(
        jars / "plain-1.0.0.jar",
        {
            "fabric.mod.json": {"schemaVersion": 1, "id": "z", "name": "Z", "version": "1.0.0",
                                "depends": {"minecraft": ">=1.21.1"}},
            "assets/z/lang/en_us.json": plain_lang,
        },
    )
    rc, _, err = run("extract", str(plain), "--work", str(RUN / "w-plain"), "--locale-fallback")
    check("extract 退出码 0", rc == 0, err)
    src = RUN / "w-plain" / "source" / "assets__z__lang__en_us.json"
    check("源文件按源语言命名", src.exists(), f"实际: {list((RUN / 'w-plain' / 'source').glob('*'))}")
    check("普通模组不排序", keys_of(src) == list(plain_lang), str(keys_of(src)))
    check("骨架与源同键", keys_of(RUN / "w-plain" / "target" / "assets__z__lang__zh_cn.json") == list(plain_lang))

    # ---------------------------------------------------------------- MCreator
    print("\n[2] MCreator 模组：自动按键名排序")
    mc_lang = {"item.z.third": "Third", "block.z.alpha": "Alpha", "item.z.second": "Second"}
    mc = make_jar(
        jars / "mcmod-1.0.0.jar",
        {
            "fabric.mod.json": {"schemaVersion": 1, "id": "z", "name": "Z", "version": "1.0.0"},
            "net/mcreator/z/ZMod.class": b"\xca\xfe",
            "assets/z/lang/en_us.json": mc_lang,
        },
    )
    rc, out, err = run("extract", str(mc), "--work", str(RUN / "w-mc"))
    check("MCreator extract 退出码 0", rc == 0, err)
    rc, pout, perr = run("probe", str(mc))
    check("probe 标注 MCreator", "generator=MCreator" in pout, pout + perr)
    check("自动排序", keys_of(RUN / "w-mc" / "source" / "assets__z__lang__en_us.json") == sorted(mc_lang))

    rc, _, err = run("extract", str(mc), "--work", str(RUN / "w-mc-nosort"), "--no-sort")
    check("--no-sort 保留原序", rc == 0 and
          keys_of(RUN / "w-mc-nosort" / "source" / "assets__z__lang__en_us.json") == list(mc_lang), err)

    # ---------------------------------------------------------------- 多模块合并
    print("\n[3] merge：同命名空间合并 + 排序 + 冲突")
    a = make_jar(jars / "mod-a.jar", {"fabric.mod.json": {"id": "s"}, "assets/s/lang/en_us.json":
                                      {"item.s.b": "B", "item.s.a": "A"}})
    b = make_jar(jars / "mod-b.jar", {"fabric.mod.json": {"id": "s"}, "assets/s/lang/en_us.json":
                                      {"item.s.c": "C", "item.s.a": "A-different"}})
    rc, out, err = run("merge", str(a), str(b), "--work", str(RUN / "w-merge"))
    check("merge 退出码 0", rc == 0, err)
    merged = RUN / "w-merge" / "source" / "assets__s__lang__en_us.json"
    k = keys_of(merged)
    check("合并后 3 个键", len(k) == 3, str(k))
    check("合并后已排序", k == sorted(k), str(k))
    check("冲突有告警", "取值不同" in err or "取值不同" in out, err + out)
    check("默认保留首个取值", json.loads(merged.read_text(encoding="utf-8"))["item.s.a"] == "A")

    # ---------------------------------------------------------------- 旧译审计
    print("\n[4] audit：检出旧译错位")
    shifted = make_jar(
        jars / "legacy-1.0.0.jar",
        {
            "fabric.mod.json": {"schemaVersion": 1, "id": "lg", "name": "LG", "version": "1.0.0"},
            "assets/lg/lang/en_us.json": {"item.lg.a": "Knight", "item.lg.b": "Spoopy"},
            # 旧译文：把 Knight 的译名挂到了 Spoopy 的键上
            "assets/lg/lang/zh_cn.json": {"item.lg.a": "骑士", "item.lg.b": "骑士"},
        },
    )
    rc, out, err = run("extract", str(shifted), "--work", str(RUN / "w-lg"),
                       "--glossary-home", str(GLOSSARY))
    check("extract 时即时告警错位", "错位" in err or "错位" in out, err + out)
    check("旧译写入 reference", (RUN / "w-lg" / "reference" / "assets__lg__lang__zh_cn.json").exists())
    rc, out, err = run("audit", str(RUN / "w-lg"), "--glossary-home", str(GLOSSARY))
    check("audit 检出强错位并返回 1", rc == 1 and "强错位" in out, out + err)

    # ---------------------------------------------------------------- 校验与打包
    print("\n[5] apply + check + pack")
    rc, _, err = run("apply", str(RUN / "w-plain"), str(RUN / "bad.json")) if False else (0, "", "")
    (RUN / "good.json").write_text(json.dumps(
        {"item.z.third": "第三", "block.z.alpha": "阿尔法", "item.z.second": "第二"},
        ensure_ascii=False), encoding="utf-8")
    rc, out, err = run("apply", str(RUN / "w-plain"), str(RUN / "good.json"))
    check("apply 全部应用", rc == 0 and "应用 3 条" in out, out + err)
    rc, out, err = run("fmt", str(RUN / "w-plain"))
    rc, out, err = run("check", str(RUN / "w-plain"))
    check("干净译文 check 通过", rc == 0, out + err)

    (RUN / "bad.json").write_text(json.dumps(
        {"item.z.third": "第三 %s", "block.z.alpha": "阿尔法 ", "item.z.second": "第二"},
        ensure_ascii=False), encoding="utf-8")
    run("apply", str(RUN / "w-plain"), str(RUN / "bad.json"))
    run("fmt", str(RUN / "w-plain"))
    rc, out, err = run("check", str(RUN / "w-plain"))
    check("多余占位符被检出", "format" in out and rc == 1, out)
    check("行尾空格被检出", "space" in out, out)

    run("apply", str(RUN / "w-plain"), str(RUN / "good.json"))
    rc, out, err = run("pack", str(RUN / "w-plain"))
    rp = RUN / "w-plain" / "out" / "plain-1.0.0-zh_cn-resourcepack.zip"
    check("资源包已生成", rc == 0 and rp.exists(), out + err)
    if rp.exists():
        with zipfile.ZipFile(rp) as z:
            names = sorted(z.namelist())
            meta = json.loads(z.read("pack.mcmeta").decode("utf-8"))
        check("包内只有 mcmeta 与译文", names == ["assets/z/lang/zh_cn.json", "pack.mcmeta"], str(names))
        check("pack_format 已推断", isinstance(meta["pack"].get("pack_format"), int), str(meta))
    check("pack 不生成 jar", not list((RUN / "w-plain" / "out").glob("*.jar")),
          str(list((RUN / "w-plain" / "out").glob("*"))))

    # ---------------------------------------------------------------- 翻译阶段工具
    print("\n[6] 译前盘点 / 按英文套用 / 排版警告")
    dup = make_jar(
        jars / "dup-1.0.0.jar",
        {
            "fabric.mod.json": {"schemaVersion": 1, "id": "d", "name": "D", "version": "1.0.0"},
            "assets/d/lang/en_us.json": {
                "item.d.a": "Same",
                "item.d.b": "Same",
                "item.d.c": "Other",
                "gui.d.label": "OK",
            },
        },
    )
    run("extract", str(dup), "--work", str(RUN / "w-dup"))
    rc, out, err = run("nominal", str(RUN / "w-dup"), "--min", "2")
    check("nominal 列出重复英文", rc == 0 and "重复英文" in out and "Same" in out, out + err)

    (RUN / "byen.json").write_text(
        json.dumps({"Same": "相同", "Other": "其它"}, ensure_ascii=False), encoding="utf-8")
    rc, out, err = run("apply", str(RUN / "w-dup"), str(RUN / "byen.json"), "--by-en")
    check("apply --by-en 一次铺到两个键", "应用 3 条" in out, out + err)
    zh = json.loads((RUN / "w-dup" / "target" / "assets__d__lang__zh_cn.json").read_text(encoding="utf-8"))
    check("同英文译法一致", zh["item.d.a"] == zh["item.d.b"] == "相同", str(zh))

    (RUN / "bad2.json").write_text(
        json.dumps({"Same": "已储存FE电量", "OK": "这是一个非常长的中文按钮文本用来测试"},
                   ensure_ascii=False), encoding="utf-8")
    run("apply", str(RUN / "w-dup"), str(RUN / "bad2.json"), "--by-en")
    rc, out, err = run("check", str(RUN / "w-dup"))
    check("cjk-space 缺空格被检出", "cjk-space" in out, out)
    check("length 超长被检出", "length" in out, out)

    # ---------------------------------------------------------------- 汇总
    print(f"\n{'=' * 46}")
    print(f"PASS {PASSED}  FAIL {len(FAILED)}")
    if FAILED:
        print("失败项:")
        for f in FAILED:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
