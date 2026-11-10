from pathlib import Path
import os
import shutil
import subprocess

workspace = Path(os.environ["GITHUB_WORKSPACE"])
modules_src = Path(os.environ["MODULES_SRC"])
device = workspace / "device_common_source"
kernelrelease = os.environ["KERNELRELEASE"]

def read_list(filename):
    path = device / filename
    if not path.is_file():
        raise RuntimeError(f"缺少设备模块清单: {path}")
    return [
        line.strip().split()[0]
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

vendor_load = read_list("modules.load")
recovery_include = read_list("modules.include.recovery")
recovery_load = read_list("modules.load.recovery")

if not vendor_load or not recovery_include or not recovery_load:
    raise RuntimeError("modules.load / modules.include.recovery / modules.load.recovery 不能是空文件")

module_map = {}
for src in sorted(modules_src.rglob("*.ko")):
    if not src.is_file():
        continue
    if src.name in module_map:
        raise RuntimeError(
            f"发现同名内核模块，无法按 LineageOS vendor_dlkm 布局扁平安装: "
            f"{src.name}\n{module_map[src.name]}\n{src}"
        )
    module_map[src.name] = src

if not module_map:
    raise RuntimeError(f"modules_install 下没有找到任何 .ko: {modules_src}")

missing = sorted(set(vendor_load + recovery_include + recovery_load) - set(module_map))
if missing:
    raise RuntimeError("设备树要求的模块没有编译出来:\n  " + "\n  ".join(missing))

work = workspace / "module_image_work"
if work.exists():
    shutil.rmtree(work)
work.mkdir(parents=True)

def run_depmod(base):
    result = subprocess.run(
        ["depmod", "-b", str(base), "0.0"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.stdout:
        print(result.stdout)
    if result.returncode != 0:
        raise RuntimeError(f"depmod 失败: {base}")

def read_depmap(base):
    depfile = base / "lib/modules/0.0/modules.dep"
    if not depfile.is_file():
        raise RuntimeError(f"depmod 没有生成 modules.dep: {depfile}")
    depmap = {}
    for line in depfile.read_text().splitlines():
        if ":" not in line:
            continue
        left, right = line.split(":", 1)
        name = Path(left).name
        depmap[name] = [Path(item).name for item in right.split()]
    return depmap

def dependency_closure(roots, depmap):
    result = set()
    pending = list(roots)
    while pending:
        name = pending.pop()
        if name in result:
            continue
        if name not in depmap:
            raise RuntimeError(f"depmod 结果里缺少模块依赖记录: {name}")
        result.add(name)
        pending.extend(depmap[name])
    return result

# 先对本次 modules_install 的完整模块集运行 depmod，以解析设备清单的依赖闭包。
all_base = work / "all_modules"
all_moddir = all_base / "lib/modules/0.0/vendor/lib/modules"
all_moddir.mkdir(parents=True)
for name, src in module_map.items():
    shutil.copy2(src, all_moddir / name)
run_depmod(all_base)
all_depmap = read_depmap(all_base)

# init.target.rc explicitly loads qca_cld3_wlan.ko; it is not listed in modules.load.
explicit_vendor_load = ["qca_cld3_wlan.ko"]
loaded_closure = dependency_closure(vendor_load + explicit_vendor_load, all_depmap)
missing_dependencies = sorted(loaded_closure - set(module_map))
if missing_dependencies:
    raise RuntimeError("vendor 模块依赖缺失: " + ", ".join(missing_dependencies))
# vendor_modprobe scans /vendor/lib/modules and applies modules.blocklist.
# Install every module built by this kernel, not only modules.load roots.
vendor_needed = set(module_map)
recovery_needed = dependency_closure(recovery_load, all_depmap)
recovery_allowed = set(recovery_include)
extra_recovery_deps = sorted(recovery_needed - recovery_allowed)
if extra_recovery_deps:
    raise RuntimeError(
        "modules.include.recovery 没有覆盖 modules.load.recovery 的依赖: "
        + ", ".join(extra_recovery_deps)
    )

def rewrite_modules_dep(text, expected_prefix):
    # Convert one modules.dep path token at a time.
    # Do not use [^: ] here: it also matches newlines and corrupts adjacent records.
    rewritten_lines = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line:
            continue
        module_path, separator, dependencies = line.partition(":")
        if not separator or not module_path:
            raise RuntimeError(
                f"modules.dep 格式错误，第 {line_number} 行: {line!r}"
            )
        paths = [module_path, *dependencies.split()]
        absolute_paths = ["/" + item.lstrip("/") for item in paths]
        if any(not item.startswith(expected_prefix) for item in absolute_paths):
            raise RuntimeError(
                f"modules.dep 路径不符合预期 {expected_prefix}，"
                f"第 {line_number} 行: {line!r}"
            )
        rewritten_lines.append(
            absolute_paths[0] + ":" +
            ((" " + " ".join(absolute_paths[1:])) if len(absolute_paths) > 1 else "")
        )
    return "\n".join(rewritten_lines) + "\n"

def build_partition_modules(target_dir, modules, mount_prefix):
    target_dir.mkdir(parents=True, exist_ok=True)
    for name in sorted(modules):
        shutil.copy2(module_map[name], target_dir / name)

    # 复刻 LineageOS android_build 的 depmod 布局：
    # vendor_dlkm 元数据引用 /vendor/lib/modules，Recovery ramdisk 引用 /lib/modules。
    depbase = work / ("depmod_" + ("vendor" if mount_prefix else "recovery"))
    if depbase.exists():
        shutil.rmtree(depbase)
    if mount_prefix:
        depmod_moddir = depbase / "lib/modules/0.0/vendor/lib/modules"
    else:
        depmod_moddir = depbase / "lib/modules/0.0/lib/modules"
    depmod_moddir.mkdir(parents=True)
    for name in sorted(modules):
        shutil.copy2(module_map[name], depmod_moddir / name)

    run_depmod(depbase)
    metadata_dir = depbase / "lib/modules/0.0"
    for metadata in ("modules.dep", "modules.alias", "modules.softdep"):
        src = metadata_dir / metadata
        if not src.is_file():
            raise RuntimeError(f"depmod 没有生成 {metadata}: {src}")
        text = src.read_text()
        if metadata == "modules.dep":
            expected_prefix = "/vendor/lib/modules/" if mount_prefix else "/lib/modules/"
            text = rewrite_modules_dep(text, expected_prefix)
        (target_dir / metadata).write_text(text)

# vendor_dlkm：安装本次编译的全部模块；modules.load 控制加载顺序，modules.blocklist 控制禁止加载项。
vendor_root = workspace / "vendor_dlkm_root"
if vendor_root.exists():
    shutil.rmtree(vendor_root)
vendor_modules = vendor_root / "lib/modules"
build_partition_modules(vendor_modules, vendor_needed, "vendor")
packaged_vendor_modules = {p.name for p in vendor_modules.glob("*.ko")}
if packaged_vendor_modules != set(module_map):
    missing_packaged = sorted(set(module_map) - packaged_vendor_modules)
    unexpected_packaged = sorted(packaged_vendor_modules - set(module_map))
    raise RuntimeError(
        f"vendor_dlkm 文件集合与 modules_install 不一致; 缺少={missing_packaged}; 多出={unexpected_packaged}"
    )
(vendor_modules / "modules.load").write_text("\n".join(vendor_load) + "\n")
shutil.copy2(device / "modules.blocklist", vendor_modules / "modules.blocklist")

# vendor_boot v3：6 个 include 模块都放进 ramdisk；Recovery 实际加载清单仍只用 2 项。
ramdisk_modules = workspace / "vendor_ramdisk/ramdisk/lib/modules"
if ramdisk_modules.parent.parent.parent.exists():
    shutil.rmtree(workspace / "vendor_ramdisk")
build_partition_modules(ramdisk_modules, recovery_allowed, "")
# Match AOSP BoardConfigCommon.mk:
# BOOT_KERNEL_MODULES packages modules.include.recovery; the Recovery
# load-list is emitted as modules.load.recovery (not modules.load).
(ramdisk_modules / "modules.load.recovery").write_text("\n".join(recovery_load) + "\n")

print(f"Kernel release: {kernelrelease}")
print(f"全部编译模块: {len(module_map)}")
print(f"vendor_dlkm 安装模块（含依赖）: {len(vendor_needed)}")
print(f"Recovery ramdisk 包含模块: {len(recovery_allowed)}")
print(f"Recovery 实际加载模块: {len(recovery_load)}")
