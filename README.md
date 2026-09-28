# 边缘设备升级系统（第一版）

面向边缘设备的 A/B 双槽升级框架。第一版只用**项目生成的测试包**和**虚拟设备/虚拟启动检查**验证升级流程，不接真实设备、不接云端；后续适配真实设备时替换分段写入后端与启动检查即可。

## 能力范围

- 升级包检查：型号支持列表、基线版本（`min_base_version`）、清单字段、逐分片 SHA-256、整镜像 SHA-256。
- 分段写入：按 `chunk_size` 把镜像分段落盘到备用槽 `image.bin`（按偏移直写 + fsync），支持按真实字节续传。
- 试启动：备用槽标记为 `trial`，虚拟 bootloader 读固件头部模拟健康检查，最多 3 次尝试。
- 失败回退：试启动失败把新槽标记为 `bad`，切回旧槽启动；旧版本在新版本确认可用前始终保留。
- 掉电恢复：状态使用 JSON 事务日志（临时文件 + fsync + 原子 rename）。恢复时**只信磁盘上真实写入的镜像字节**，不信任任何“成功标记”。
- 重复投递：同一升级包（版本 + 镜像哈希）已安装则幂等返回 `already_installed`。
- 版本不兼容：型号不支持或基线版本不满足时返回 `incompatible`，不改动任何槽。

## 目录布局（设备本地状态）

```
device/
  device.json                 型号/序列号
  state/
    updater_state.json        升级事务日志（writing / trial）
    bootctrl.json             虚拟 bootloader 控制块
  slots/
    slot_a/image.bin          A 槽固件（旧版本保留）
    slot_a/slot_meta.json
    slot_b/...                B 槽固件
```

## 升级结果码

| 结果 | 含义 |
| --- | --- |
| `success` | 试启动通过并确认新版本 |
| `already_installed` | 同一升级包已安装，重复投递被忽略 |
| `incompatible` | 型号不支持或基线版本不满足 |
| `package_invalid` | 清单/分片/整镜像校验失败 |
| `rolled_back` | 试启动失败，已回退旧版本 |
| `interrupted` | 分段写入中途断电，可再次调用恢复 |
| `trial_pending` | 试启动流程尚未结束 |

## 运行

```bash
# 端到端演示（正常、去重、不兼容、回退、写中断电、试启动前断电）
python -m edgeup.demo

# 测试
pytest
```

## 后续适配真实设备

- `slots.SlotManager`：把文件偏移写替换为对 Flash 分区的分段编程/读回校验。
- `bootctl.BootControl`：把 JSON 控制块替换为真实 bootloader 的 A/B 标记与健康检查接口。
- 包投递：`package.PackageReader` 目前读取目录包，可替换为 OTA 下载/解压实现，校验与分段逻辑不变。
