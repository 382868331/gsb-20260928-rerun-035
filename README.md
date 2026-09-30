# 边缘设备升级系统（第二版 · 数据迁移与批次推进 · 虚拟设备验证）

纯 Python（仅标准库）实现的 OTA 升级编排，第一版不接真实设备、不连云端，
全部用本地文件模拟设备，先把升级流程跑对，后续再把 `VirtualDevice`
替换/适配为真实设备适配层。

第二版在程序 A/B 双槽之外增加**配对数据槽**，支持带数据格式变化的版本升级：
迁移在隔离副本上进行，健康确认前旧程序与旧格式数据始终能一起恢复；
并提供多设备按批次推进与失败熔断的编排层。

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
- **数据格式迁移**：升级包清单声明目标 `data_format`；固件校验后先把活动槽数据
  **复制成隔离副本**到候选槽，再在副本上逐级执行纯函数式迁移（`migration.py`，
  当前内置 v1 到 v2）。迁移失败只废掉副本，活动槽旧数据从不原地修改。
- **程序+数据联动事务**：固件与该版本的配套数据放在同名槽内，提交只是原子切换
  活动指针；启动失败、健康失败、失联、确认前重启，一律回退到旧程序加旧格式
  数据，并清理候选槽的新固件/新数据，杜绝只回退程序却留下新格式。
- **确认幂等与失联**：试启动 `boot_trial` 健康通过后停在 `awaiting_confirm`；
  `confirm()` 提交，确认重复到达返回同一事实且不产生副作用；确认超时未到可调用
  `mark_lost_contact()` 回退。
- **批次编排**（`fleet.py`）：设备各自独立目录与状态，按批次顺序推进；批次内
  出现失败可熔断，后续批次标记 `skipped` 并停止推进；成功设备状态单独保留。
  失败设备重新收到同一包时，设备事务返回 `previous_rolled_back`，旧数据不会被
  再次破坏。
- **一致性核对**：`UpgradeManager.consistency_check()` 与 `Rollout.audit()`
  核对活动槽固件版本、数据格式/可读性与升级事务登记是否一致。

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
  data/A/{data.json,info.json}  与槽配对的业务数据及格式版本
  data/B/{data.json,info.json}
  staging/firmware.part 分段暂存（真实已写内容）
  staging/expected.json  目标槽、期望大小与 sha256
  state/upgrade_state.json
  fleet/devices/<id>/       多设备编排时每台设备各自的上述目录
  fleet/rollouts/<name>.json  批次计划与每设备状态
```

## 快速开始

```bash
python3 -m pytest        # 37 个用例
python3 demo.py          # 含数据迁移/失联/重复确认/批次熔断 端到端演示

# 生成测试包
python3 tools/make_package.py out/v2.fwpkg --version 2.0.0 --min-device 1.0.0
python3 tools/make_package.py out/bad.fwpkg --version 2.1.0 --boot-fail
python3 tools/make_package.py out/v2.fwpkg --version 2.0.0 --data-format 2
python3 tools/make_package.py out/bad-migrate.fwpkg --version 2.0.0 --data-format 2 --migration-fail
```

## 代码用法

```python
from edge_upgrade import (
    UpgradeManager, FleetOrchestrator, build_package, load_package,
)

manager = UpgradeManager.initialize("./device", factory_version="1.0.0")
pkg = load_package(build_package("./v2.fwpkg", version="2.0.0", min_device_version="1.0.0"))

manager.receive_package(pkg)                  # 检查与分流
for i in range(0, len(pkg.payload), 64):      # 分段写入（任意处可“断电”）
    manager.write_segment(i, pkg.payload[i:i+64])
manager.finish_download()     # sha256 校验装槽，随后在隔离副本上迁移数据格式
trial = manager.boot_trial()                  # 试启动 + 健康检查
outcome = manager.confirm()                   # 健康确认后提交；重复确认幂等
# 确认超时未到（设备失联）：manager.mark_lost_contact()
# outcome.result == ActivateResult.COMMITTED  # 失败则 *_ROLLED_BACK

# 进程重启后
manager = UpgradeManager.open("./device")    # 自动按真实磁盘内容对账
manager.consistency_check()                  # 核对版本/数据/事务一致性

# 多设备分批（失败熔断，成功设备状态单独保留）
fleet = FleetOrchestrator("./fleet")
fleet.register_device("dev-1")
rollout = fleet.create_rollout("nightly", pkg, batches=[["dev-1"], ["dev-2"]])
rollout.advance()       # 推进下一批；run_device(cut_chunk=2) 模拟下载中断续传
rollout.audit()         # 逐台核对版本、数据与事务
```

## 适配真实设备

把 `edge_upgrade/device.py` 的 `VirtualDevice` 换成实现同一组方法的真实驱动
（槽写入、`boot()`、健康检查等），`manager.py` 的状态机与恢复逻辑无需改动。

## 状态机

`idle → downloading → pending_trial(awaiting_confirm) → confirmed`

迁移失败、试启动失败/不健康、失联、未确认重启 → `rolled_back`
（活动槽保持旧版本 + 旧格式数据，候选槽被清理）。
