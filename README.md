# 边缘设备升级系统（第一版 · 虚拟设备验证）

纯 Python（仅标准库）实现的 OTA 升级编排，第一版不接真实设备、不连云端，
全部用本地文件模拟设备，先把升级流程跑对，后续再把 `VirtualDevice`
替换/适配为真实设备适配层。

## 能力

- **升级包检查**：ZIP 包内含 `manifest.json` + `payload.bin`，校验结构、长度、SHA-256、
  payload 版本一致性，以及设备当前版本是否落在 `min/max_device_version` 兼容区间。
- **分段写入**：payload 按 offset 顺序追加到暂存区，每次写入 `flush + fsync`；
  支持重复分片重传（内容一致则忽略，不一致报冲突），拒绝分片之间留空档。
- **试启动**：完整固件校验通过后原子写入**非活动槽**（A/B 双槽），打试启动标记，
  从候选槽启动并做健康检查；检查通过才切换活动槽。
- **失败回退**：启动失败、健康检查失败、或“试启动后未确认就重启”，都回到旧槽。
  **旧版本在新版本确认前始终保留、从不被覆盖。**
- **断电恢复**：状态以 `state/upgrade_state.json`（原子写）记录阶段，但重启时
  **以磁盘真实内容对账**：暂存区按真实文件长度续传，不信任状态里的成功/进度数字；
  试启动未确认则自动回退。

## 三种异常的不同结果

| 场景 | 结果 |
| --- | --- |
| 升级中途断电 | 重新打开后进入 `downloading` 续传，`received` 取暂存文件真实长度；试启动阶段断电则自动回退 |
| 重复收到同一包 | 下载中→`resumed_same` 续传；已确认→`already_installed`；曾回退→`previous_rolled_back` 拒绝 |
| 版本不兼容 | `incompatible`，在写任何磁盘数据之前拒绝 |

## 磁盘布局（虚拟设备根目录）

```
root/
  boot/active           活动槽指针 A/B（原子替换）
  boot/trial.json       试启动标记（存在=待确认）
  slots/A/{firmware.bin,info.json}
  slots/B/{firmware.bin,info.json}
  staging/firmware.part 分段暂存（真实已写内容）
  staging/expected.json  目标槽、期望大小与 sha256
  state/upgrade_state.json
```

## 快速开始

```bash
python3 -m pytest        # 19 个用例
python3 demo.py          # 成功/断电续传/不兼容/失败回退 端到端演示

# 生成测试包
python3 tools/make_package.py out/v2.fwpkg --version 2.0.0 --min-device 1.0.0
python3 tools/make_package.py out/bad.fwpkg --version 2.1.0 --boot-fail
```

## 代码用法

```python
from edge_upgrade import UpgradeManager, build_package, load_package

manager = UpgradeManager.initialize("./device", factory_version="1.0.0")
pkg = load_package(build_package("./v2.fwpkg", version="2.0.0", min_device_version="1.0.0"))

manager.receive_package(pkg)                  # 检查与分流
for i in range(0, len(pkg.payload), 64):      # 分段写入（任意处可“断电”）
    manager.write_segment(i, pkg.payload[i:i+64])
manager.finish_download()                     # sha256 校验 + 装入非活动槽
outcome = manager.boot_and_confirm()          # 试启动 + 健康检查
# outcome.result == ActivateResult.COMMITTED  # 失败则 *_ROLLED_BACK

# 进程重启后
manager = UpgradeManager.open("./device")    # 自动按真实磁盘内容对账
```

## 适配真实设备

把 `edge_upgrade/device.py` 的 `VirtualDevice` 换成实现同一组方法的真实驱动
（槽写入、`boot()`、健康检查等），`manager.py` 的状态机与恢复逻辑无需改动。

## 状态机

`idle → downloading → pending_trial → confirmed`

`pending_trial/downloading` 任一失败或未确认重启 → `rolled_back`（活动槽保持旧版本）。
