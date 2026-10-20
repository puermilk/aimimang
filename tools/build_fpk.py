#!/usr/bin/env python3
"""爱迷茫 FPK 打包脚本。

用法：
    python3 tools/build_fpk.py 1.2.6 [--no-lib]

产出：
    <仓库根>/dist/爱迷茫-<版本>.fpk
    <仓库根>/dist/爱迷茫-<版本>.fpk.sha256

包结构（自 1.2.5 起固定，勿改）：
    顶层       app.tgz  cmd/  config/  ICON.PNG  ICON_256.PNG  manifest  wizard/
    app.tgz    mitm/  server/  ui/  www/  config/

要点：
    - app/mitm/lib 是 make_lib.sh 现生成的（aiohttp + quickjs 纯目录，无符号链接），
      不随源码入库；打包前会自动生成（--no-lib 可跳过）。
    - manifest 里的 checksum 行 = app.tgz 的 md5，应用中心用它校验包体。
    - 目录条目不带结尾 “/”，与 1.2.5 起的包保持一致。
    - 不打包 __pycache__ / *.pyc（自测会生成，别带进包里）。
"""
import hashlib
import os
import re
import subprocess
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(HERE)                      # /root/fpk
APP = os.path.join(SRC, "src", "trimonline")     # 源码根（含 manifest/cmd/config/app）
APP_DIR = os.path.join(APP, "app")

# app.tgz 内的条目顺序（顶目录 + 各自递归）
APP_ITEMS = ["mitm", "server", "ui", "www"]
# app.tgz 里额外并入的顶层 config/（来自 src/trimonline/config）
APP_EXTRA = [os.path.join(APP, "config")]
# fpk 顶层条目顺序
FPK_ITEMS = ["app.tgz", "cmd", "config", "ICON.PNG", "ICON_256.PNG", "manifest", "wizard"]


def excluded(name):
    base = os.path.basename(name)
    return base == "__pycache__" or base.endswith(".pyc")


def add_tree(tf, path, arcname):
    if excluded(path):
        return
    ti = tf.gettarinfo(path, arcname=arcname)
    if ti.isdir():
        ti.name = ti.name.rstrip("/")
        tf.addfile(ti)
        for child in sorted(os.listdir(path)):
            add_tree(tf, os.path.join(path, child), arcname + "/" + child)
    else:
        with open(path, "rb") as fh:
            tf.addfile(ti, fh)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    ver = args[0]
    out_dir = os.path.join(SRC, "dist")
    os.makedirs(out_dir, exist_ok=True)
    out_fpk = os.path.join(out_dir, "爱迷茫-%s.fpk" % ver)
    out_app = os.path.join(SRC, "src", "trimonline", "app.tgz")
    # 带 checksum 的 manifest 只在打包时临时生成，不落在源码树里
    out_manifest = os.path.join(tempfile.mkdtemp(prefix="fpkbuild-"), "manifest")

    # 1) 依赖目录
    if "--no-lib" not in sys.argv:
        print("### 1) 生成 app/mitm/lib")
        subprocess.run(["bash", os.path.join(APP, "make_lib.sh")], check=True,
                       cwd=APP, stdout=subprocess.DEVNULL)

    # 2) app.tgz
    print("### 2) 打包 app.tgz")
    if os.path.exists(out_app):
        os.unlink(out_app)
    with tarfile.open(out_app, "w:gz", format=tarfile.GNU_FORMAT) as tf:
        for item in APP_ITEMS:
            add_tree(tf, os.path.join(APP_DIR, item), item)
        for path in APP_EXTRA:
            add_tree(tf, path, os.path.basename(path))

    with open(out_app, "rb") as fh:
        ck = hashlib.md5(fh.read()).hexdigest()
    print("    app.tgz = %d 字节  md5 = %s" % (os.path.getsize(out_app), ck))

    # 3) manifest（版本号 + checksum）
    with open(os.path.join(APP, "manifest"), encoding="utf-8") as fh:
        man = fh.read()
    man = re.sub(r"(?m)^version\s*=.*$", "version               = %s" % ver, man)
    man = re.sub(r"(?m)^checksum\s*=.*\n?", "", man)
    if not man.endswith("\n"):
        man += "\n"
    man += "checksum              = %s\n" % ck
    with open(out_manifest, "w", encoding="utf-8") as fh:
        fh.write(man)
    print("### 3) manifest 版本 %s  checksum = %s" % (ver, ck))

    # 4) fpk
    print("### 4) 打包 fpk")
    paths = []
    for item in FPK_ITEMS:
        p = os.path.join(APP, item) if item != "app.tgz" else out_app
        if item == "manifest":
            p = out_manifest
        paths.append((p, item))
    if os.path.exists(out_fpk):
        os.unlink(out_fpk)
    with tarfile.open(out_fpk, "w:gz", format=tarfile.GNU_FORMAT) as tf:
        for path, arcname in paths:
            if os.path.isdir(path):
                add_tree(tf, path, arcname)
            else:
                add_tree(tf, path, arcname)

    h = hashlib.sha256()
    with open(out_fpk, "rb") as fh:
        h.update(fh.read())
    with open(out_fpk + ".sha256", "w", encoding="utf-8") as fh:
        fh.write("%s  爱迷茫-%s.fpk\n" % (h.hexdigest(), ver))
    print("### 5) 产出")
    print("    %s  %d 字节" % (out_fpk, os.path.getsize(out_fpk)))
    print("    sha256 %s" % h.hexdigest())
    return 0


if __name__ == "__main__":
    sys.exit(main())
