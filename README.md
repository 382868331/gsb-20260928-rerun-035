# 边缘设备升级系统（虚拟设备验证）

纯 Python（仅标准库）实现的 OTA 升级编排，不接真实设备、不连云端，
全部用本地文件模拟设备，先把升级流程跑对，后续再把 `VirtualDevice`
替换/适配为真实设备适配层。

在 A/B 槽升级之上，系统支持**带数据格式变化的版本升级**：隔离迁移、
程序与数据同事务提交、试启动/确认分离，以及多设备按批次推进与失败熔断。

## 能力

- **升级包检查**：ZIP 包内含 `manifest.json` + `payload.bin`，校验结构、长度、SHA-256、
  payload 版本一致性，以及设备当前版本是否落在 `min/max_device_version` 兼容区间。
- **分段写入**：payload 按 offset 顺序追加到暂存区，每次写入 `flush + fsync`；
  支持重复分片重传（内容一致则忽略，不一致报冲突），拒绝分片之间留空档。
- **试启动**：完整固件校验通过后原子写入**非活动槽**（A/B 双槽），打试启动标记，
  从候选槽启动并做健康检查；健康确认通过后才切换活动槽。
- **失败回退**：启动失败、健康检查失败、或“试启动后未确认就重启”，都回到旧槽。
  **旧版本在新版本确认前始终保留、从不被覆盖。**
- **数据格式迁移（隔离副本）**：新固件用 `data_format` 声明所需数据格式；
  试启动前把 `data/current` 复制到 `data/trial` 并**只在副本上迁移**，
  活动数据在健康确认前一个字节都不改；迁移失败立即整套回退。
- **程序与数据同一事务**：确认时两阶段提交（落提交标记 → 切槽指针 → 切数据），
  任一步断电重启都能对账：要么新程序+新格式，要么旧程序+旧格式，
  **绝不只回退程序却留下新格式数据**。旧数据保存在 `data/previous`。
- **试启动 / 确认分离**：`start_trial()` 做迁移+候选槽健康检查，`confirm()`
  才提交；试启动后失联返回 `lost_contact` 并保留旧程序+旧数据在线，
  确认重复到达幂等（`already_confirmed`）。
- **多设备批次**：`BatchUpgradeOrchestrator` 按批次推进同一份升级包；
  批内任一设备失败（下载中断/迁移失败/启动失败/不健康/失联）则**停止后续批次**，
  已确认设备的状态各自独立保留；每台设备结果落盘 `fleet_report.json`。
- **断电恢复**：状态以 `state/upgrade_state.json`（原子写）记录阶段，但重启时
  **以磁盘真实内容对账**：暂存区按真实文件长度续传，不信任状态里的成功/进度数字；
  试启动未确认则整套（程序+数据）自动回退；半完成提交则补齐。

## 异常的不同结果

| 场景 | 结果 |
| --- | --- |
| 升级中途断电 | 重新打开后进入 `downloading` 续传，`received` 取暂存文件真实长度；试启动阶段断电则整套回退 |
| 重复收到同一包 | 下载中→`resumed_same` 续传；已确认→`already_installed`；曾回退→`previous_rolled_back` 拒绝 |
| 版本不兼容 | `incompatible`，在写任何磁盘数据之前拒绝 |
| 数据迁移失败 | `migration_failed`，丢弃隔离副本，旧程序与旧格式数据继续成套运行；坏包重投被拒绝 |
| 试启动后失联 | `lost_contact`，现场保留、旧程序+旧数据在线；确认期外重启则整套回退 |
| 某设备失败（批量） | 本批结束后停止后续批次；已确认设备状态独立保留，未开始设备标记 `skipped` |

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
  data/current/data.json    活动程序使用的数据（data_format 标注格式）
  data/trial/data.json      试启动期间的隔离迁移副本
  data/previous/data.json   上次升级前的旧格式数据（回滚保底）
  fleet_report.json         多设备批次编排报告（仅批量升级时）
```

## 快速开始

```bash
python3 -m pytest        # 36 个用例
python3 demo.py          # 成功/断电续传/不兼容/失败回退
python3 demo_v2.py       # 数据迁移/失联/重复确认/批次熔断

# 生成测试包
python3 tools/make_package.py out/v2.fwpkg --version 2.0.0 --min-device 1.0.0
python3 tools/make_package.py out/bad.fwpkg --version 2.1.0 --boot-fail
python3 tools/make_package.py out/v2fmt.fwpkg --version 2.0.0 --data-format 2
python3 tools/make_package.py out/bad-migrate.fwpkg --version 3.0.0 --data-format 2 --migrate-fail
```

## 代码用法

```python
from edge_upgrade import UpgradeManager, build_package, load_package

manager = UpgradeManager.initialize("./device", factory_version="1.0.0")
pkg = load_package(build_package("./v2.fwpkg", version="2.0.0",
                                min_device_version="1.0.0", data_format=2))

manager.receive_package(pkg)                  # 检查与分流
for i in range(0, len(pkg.payload), 64):      # 分段写入（任意处可“断电”）
    manager.write_segment(i, pkg.payload[i:i+64])
manager.finish_download()                     # sha256 校验 + 装入非活动槽
manager.start_trial()                         # 隔离迁移 + 试启动健康检查
outcome = manager.confirm()                   # 显式健康确认：程序+数据同事务提交
# outcome.result == ActivateResult.COMMITTED  # 失败则 *_ROLLED_BACK
# boot_and_confirm() 仍可用：试启动通过后立即确认（兼容第一版 API）

# 进程重启后
manager = UpgradeManager.open("./device")    # 自动按真实磁盘内容对账
```

多设备批次：

```python
from edge_upgrade.fleet import BatchUpgradeOrchestrator, DeviceFault

fleet = BatchUpgradeOrchestrator("./fleet", chunk_size=64)
for device_id in ("d1", "d2", "d3", "d4"):
    fleet.register_device(device_id, batch=0 if device_id in ("d1", "d2") else 1)
fleet.set_fault("d2", DeviceFault.MIGRATION_FAIL)   # 注入失败演练
report = fleet.run_batches(pkg, [["d1", "d2"], ["d3", "d4"]])
report.halted            # True：第二批未推进
report.succeeded()       # d1 的状态独立保留
record = fleet.resume_device("d1", pkg)    # 下载中断设备按真实字节续传
```

## 适配真实设备

把 `edge_upgrade/device.py` 的 `VirtualDevice` 换成实现同一组方法的真实驱动
（槽写入、`boot()`、健康检查、在线状态等）；数据侧把 `DataStore` 适配为真实
数据目录/快照机制，在 `migrations.REGISTRY` 注册各格式间的迁移函数。
`manager.py` 的状态机与恢复逻辑无需改动。

## 状态机

`idle → downloading → pending_trial → confirmed`

`pending_trial/downloading` 任一失败或未确认重启 → `rolled_back`
（活动槽保持旧程序、活动数据保持旧格式，两者成套）。

## 数据格式迁移

固件 payload（以及 `manifest.json`）带 `data_format` 字段。内置示例迁移为
1 → 2：`records` 从裸字符串列表 `["a"]` 变为对象列表 `[{"id": 0, "value": "a"}]`。
迁移函数注册在 `edge_upgrade/migrations.py` 的 `REGISTRY`，每步只升一个格式号，
可用 `register_migration()` 扩展；不支持降级（回退走 `data/previous` 旧副本）。
迁移是纯函数转换，先把旧数据原样复制到 `data/trial`，再原子替换为迁移结果，
因此迁移到一半失败、坏包重复投递，都不会第二次破坏旧数据。

