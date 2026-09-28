"""升级编排引擎：检查、分段写入、试启动、确认/回退与掉电恢复。"""
from __future__ import annotations

from pathlib import Path

from .bootctl import BootControl, MAX_BOOT_ATTEMPTS
from .device import load_device_info
from .layout import Layout
from .models import InstallResult, ResultCode
from .package import PackageError, PackageReader
from .persistence import atomic_write_json, read_json
from .slots import SlotManager
from .version import version_at_least


class SimulatedPowerCut(RuntimeError):
    """模拟升级中途断电，由 fail point 触发；现场状态全部保留在磁盘。"""


class UpgradeEngine:
    PHASE_IDLE = "idle"
    PHASE_WRITING = "writing"
    PHASE_TRIAL = "trial"

    def __init__(self, root: str | Path):
        self.layout = Layout(root)
        self.layout.ensure()
        self.slots = SlotManager(self.layout)
        self.boot = BootControl(self.layout)

    # ------------------------------------------------------------------ journal

    def _save_journal(self, **fields) -> None:
        journal = {"phase": self.PHASE_IDLE}
        journal.update(fields)
        atomic_write_json(self.layout.journal_file, journal)

    def _clear_journal(self) -> None:
        if self.layout.journal_file.exists():
            self.layout.journal_file.unlink()

    def _journal(self) -> dict | None:
        return read_json(self.layout.journal_file)

    @staticmethod
    def _package_id(pkg: PackageReader) -> str:
        return f"{pkg.version}@{pkg.manifest['image_sha256'][:12]}"

    # ----------------------------------------------------------------- install

    def install(
        self,
        package_dir: str | Path,
        *,
        crash_after_segment: int | None = None,
        crash_before_trial_boot: bool = False,
        crash_before_commit: bool = False,
    ) -> InstallResult:
        """接收并安装一个升级包；重复调用即等价于重启后恢复 / 重复投递。"""
        try:
            pkg = PackageReader(package_dir)
            pkg.check_readable()
        except PackageError as exc:
            return InstallResult(ResultCode.PACKAGE_INVALID, str(exc))

        package_id = self._package_id(pkg)
        journal = self._journal()

        # 未完成事务优先：同包走恢复，异包作废旧事务；之后才考虑重复投递。
        if journal:
            if journal.get("package_id") != package_id:
                self._abandon_journal(journal)
                journal = None
            elif journal["phase"] == self.PHASE_TRIAL:
                return self._resume_trial(pkg, journal, crash_before_commit)

        # 重复投递：同一包已在运行槽确认安装，直接幂等返回，不重写。
        if journal is None and self._active_slot_runs_package(pkg):
            return InstallResult(
                ResultCode.ALREADY_INSTALLED,
                f"{package_id} 已安装并运行，忽略重复投递",
                to_version=pkg.version,
                slot=self.boot.state["active"],
            )

        resumed = journal is not None

        if not resumed:
            result = self._check_compatible(pkg)
            if result:
                return result
            journal = self._start_journal(pkg, package_id)
        else:
            journal = self._refresh_journal_paths(journal, package_dir, package_id)

        result = self._write_segments(pkg, journal, resumed, crash_after_segment)
        if result is not None:
            return result

        return self._run_trial(pkg, journal, crash_before_trial_boot, crash_before_commit)

    # ------------------------------------------------------------- compatibility

    def _check_compatible(self, pkg: PackageReader) -> InstallResult | None:
        info = load_device_info(self.layout.root)
        if info["model"] not in pkg.manifest["supported_models"]:
            return InstallResult(
                ResultCode.INCOMPATIBLE_VERSION,
                f"型号 {info['model']} 不在支持列表 {pkg.manifest['supported_models']}",
                to_version=pkg.version,
            )
        current = self.boot.active_version()
        minimum = pkg.manifest["min_base_version"]
        if current is not None and not version_at_least(current, minimum):
            return InstallResult(
                ResultCode.INCOMPATIBLE_VERSION,
                f"当前版本 {current} 低于升级基线 {minimum}",
                from_version=current,
                to_version=pkg.version,
            )
        return None

    # ------------------------------------------------------------------ writing

    def _start_journal(self, pkg: PackageReader, package_id: str) -> dict:
        fallback = self.boot.state["active"]
        target = next(s for s in ("A", "B") if s != fallback)
        from_version = self.boot.active_version()
        journal = {
            "phase": self.PHASE_WRITING,
            "package_id": package_id,
            "package_path": str(Path(pkg.pkg_dir).resolve()),
            "version": pkg.version,
            "target_slot": target,
            "fallback_slot": fallback,
            "from_version": from_version,
        }
        self._save_journal(**journal)
        return journal

    def _refresh_journal_paths(self, journal: dict, package_dir: str | Path, package_id: str) -> dict:
        journal["package_path"] = str(Path(package_dir).resolve())
        self._save_journal(**journal)
        return journal

    def _abandon_journal(self, journal: dict) -> None:
        target = journal.get("target_slot")
        # 只清掉未确认的备用槽，活动槽（旧版本）绝不动。
        if target and target != self.boot.state["active"]:
            self.slots.clear_slot(target)
        self._clear_journal()

    def _write_segments(self, pkg, journal, resumed, crash_after_segment) -> InstallResult | None:
        target = journal["target_slot"]
        # 恢复的唯一依据是 image.bin 的真实字节，journal 不记录已写进度。
        already = self.slots.staged_segments(target, pkg)
        if already == len(pkg.manifest["chunks"]):
            if not self.slots.image_complete(target, pkg):
                return InstallResult(ResultCode.PACKAGE_INVALID, "目标槽镜像不完整", slot=target)
        else:
            # fail point 直接抛出，等价于设备硬断电；现场全部保留在磁盘。
            self.slots.write_segmented(
                target, pkg, resume=True,
                crash_after_segment=crash_after_segment,
            )
        return None

    # -------------------------------------------------------------------- trial

    def _enter_trial(self, pkg, journal) -> None:
        target = journal["target_slot"]
        journal["phase"] = self.PHASE_TRIAL
        self._save_journal(**journal)
        self.boot.mark_for_trial(target)

    def _run_trial(self, pkg, journal, crash_before_trial_boot, crash_before_commit) -> InstallResult:
        self._enter_trial(pkg, journal)
        if crash_before_trial_boot:
            raise SimulatedPowerCut("已标记试启动槽，首次启动前断电")
        return self._trial_boot_loop(pkg, journal, crash_before_commit)

    def _resume_trial(self, pkg, journal, crash_before_commit) -> InstallResult:
        """重启后恢复：先核对目标槽真实镜像，再依据 bootctrl 的真实状态继续。"""
        target = journal["target_slot"]
        if not self.slots.image_complete(target, pkg):
            # 镜像其实没写完（状态文件不可信），退回写入阶段。
            journal["phase"] = self.PHASE_WRITING
            self._save_journal(**journal)
            result = self._write_segments(pkg, journal, True, None)
            if result is not None:
                return result
            self._enter_trial(pkg, journal)
        entry = self.boot.slot_status(target)
        if entry["status"] == "confirmed":
            self._clear_journal()
            return InstallResult(ResultCode.SUCCESS, "恢复时发现新版本已确认",
                                 to_version=pkg.version, slot=target, resumed=True)
        if entry["status"] in ("empty", "bad") or self.boot.state["active"] != target:
            return self._finish_rollback(pkg, journal, resumed=True)
        # status == trial 或 booted：继续把启动/确认流程走完
        return self._trial_boot_loop(pkg, journal, crash_before_commit, resumed=True)

    def _trial_boot_loop(self, pkg, journal, crash_before_commit, resumed=False) -> InstallResult:
        target = journal["target_slot"]
        fallback = journal["fallback_slot"]
        while True:
            report = self.boot.boot()  # 虚拟上电：读固件头部做健康检查
            if report.healthy:
                if crash_before_commit:
                    raise SimulatedPowerCut("试启动成功，写入确认标记前断电")
                self.boot.commit(target)
                self._clear_journal()
                return InstallResult(
                    ResultCode.SUCCESS,
                    f"新版本 {pkg.version} 试启动通过，已在 {target} 槽确认",
                    from_version=journal.get("from_version"),
                    to_version=pkg.version,
                    slot=target,
                    resumed=resumed,
                    boot_attempts=self.boot.slot_status(target)["boot_attempts"],
                )
            if self.boot.slot_status(target)["status"] == "bad":
                return self._finish_rollback(pkg, journal, resumed=resumed)

    def _finish_rollback(self, pkg, journal, *, resumed: bool) -> InstallResult:
        fallback = journal["fallback_slot"]
        report = self.boot.rollback(fallback)
        self._clear_journal()
        return InstallResult(
            ResultCode.ROLLED_BACK,
            f"新版本 {pkg.version} 试启动 {MAX_BOOT_ATTEMPTS} 次失败，"
            f"已回退到 {fallback} 槽旧版本 {report.version or '?'}",
            from_version=journal.get("from_version"),
            to_version=pkg.version,
            slot=fallback,
            resumed=resumed,
            boot_attempts=MAX_BOOT_ATTEMPTS,
        )

    # -------------------------------------------------------------------- helpers

    def _active_slot_runs_package(self, pkg: PackageReader) -> bool:
        active = self.boot.state.get("active")
        if not active:
            return False
        if self.boot.slot_status(active)["status"] not in ("confirmed", "booted"):
            return False
        return self.slots.image_complete(active, pkg)
