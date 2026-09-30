"""闸口业务规则：OCR 识别、地磅称重、放行抬杆的状态流转与异常兜底都收在这里。

一条记录对应一辆车经过一条闸道的一次通行：
OCR 识别车牌 -> 地磅取重 -> 放行/人工抬杆 -> 现场确认。
设备（OCR、地磅、栏杆）的一次性抖动会在服务内自动重试一次，仍然失败才落到异常状态。
"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "gate"

# 登记时必须给出的字段；进出方向允许暂缺，缺了也要正常展示并点出来。
REQUIRED_FIELDS = ["闸口编号", "闸口类型", "车道编号"]
# 列表上需要逐字段兜底展示的业务列
DISPLAY_FIELDS = ["闸口编号", "闸口类型", "车道编号", "进出方向", "车牌号码", "OCR识别", "地磅称重", "放行抬杆", "闸口状态"]

# 通行环节状态（列表与闸口总览共用这一套口径，避免两个页面状态两样）
STATUS_WAIT_OCR = "待识别"
STATUS_WAIT_WEIGH = "待称重"
STATUS_WAIT_RELEASE = "待放行"
STATUS_LIFT_FAIL = "抬杆失败"
STATUS_CONFLICT = "放行冲突"
STATUS_BLOCK_PENDING = "拦截待确认"
STATUS_RELEASED = "已放行"
STATUS_BLOCKED = "已拦截"
FLOW_STATUSES = [
    STATUS_WAIT_OCR,
    STATUS_WAIT_WEIGH,
    STATUS_WAIT_RELEASE,
    STATUS_LIFT_FAIL,
    STATUS_CONFLICT,
    STATUS_BLOCK_PENDING,
    STATUS_RELEASED,
    STATUS_BLOCKED,
]

# 设备子状态：成功 / 重试中 / 失败
DEVICE_OK = "成功"
DEVICE_RETRY = "重试中"
DEVICE_FAIL = "失败"
OCR_FAIL_REASON = "未识别到车牌（画面模糊或车牌被遮挡）"
SCALE_FAIL_REASON = "地磅仪表无读数"
SCALE_RETRY_HINT = "地磅首次无读数，已自动重新取重一次"
LIFT_FAIL_REASON = "栏杆电机未响应"

_PLATE_EMPTY = ("", "暂无", "无", "—", "-", "未识别", "无车牌", "车牌未识别")
_DIRECTION_IN = ("进场", "入口", "进闸", "入", "in", "entry", "enter")
_DIRECTION_OUT = ("出场", "出口", "出闸", "出", "out", "exit", "leave")


class GateService:
    def __init__(self) -> None:
        self._bootstrapped = False

    def bootstrap(self) -> None:
        """把存量记录（如示例数据）统一按当前规则推导一次状态，避免老数据口径不一致。"""
        if self._bootstrapped:
            return
        for row in store.rows(MODULE):
            self._sync(row)
        self._bootstrapped = True

    # ------------------------------------------------------------------ 读取
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        direction: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in str(row.get("闸口编号", ""))
                or keyword in str(row.get("车牌号码", ""))
                or keyword in str(row.get("车道编号", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if direction:
            rows = [row for row in rows if row.get("进出方向") == self._norm_direction(direction)]
        total = len(rows)
        start = max(page - 1, 0) * size
        page_rows = rows[start:start + size]
        return [self._serialize(row) for row in page_rows], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return self._serialize(row) if row else None

    def all_entries(self) -> list[dict[str, Any]]:
        return [self._serialize(row) for row in store.rows(MODULE)]

    def overview(self) -> dict[str, Any]:
        """闸口总览：在场车数按抬杆成功记录重算（进场放行车牌 - 出场放行车牌）。"""
        rows = store.rows(MODULE)

        def _plates(target_direction: str) -> set[str]:
            return {
                plate.upper()
                for row in rows
                if row.get("status") == STATUS_RELEASED
                and row.get("进出方向") == target_direction
                and (plate := (self._plate_of(row) or ""))
            }

        plates_in = _plates("进场")
        plates_out = _plates("出场")
        on_site = len(plates_in - plates_out)
        released = sum(1 for row in rows if row.get("status") == STATUS_RELEASED)
        blocked = sum(1 for row in rows if row.get("status") == STATUS_BLOCKED)
        abnormal = sum(1 for row in rows if self._is_abnormal(row))
        return {
            "cards": [
                {"label": "在场车数", "value": on_site},
                {"label": "已抬杆放行", "value": released},
                {"label": "已拦截", "value": blocked},
                {"label": "异常待处理", "value": abnormal},
            ],
            "onSite": on_site,
            "released": released,
            "blocked": blocked,
            "abnormal": abnormal,
        }

    # ------------------------------------------------------------------ 登记
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        plate = self._plate_of(values)
        entry: dict[str, Any] = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "闸口编号": str(values.get("闸口编号", "")).strip(),
            "闸口类型": str(values.get("闸口类型", "")).strip(),
            "车道编号": str(values.get("车道编号", "")).strip(),
            "进出方向": self._norm_direction(values.get("进出方向")) or "",
            "车牌号码": plate or "",
            # 登记后等 OCR 抓拍：有车牌抓拍即成功，无车牌直接给失败原因并允许重识别
            "ocr状态": DEVICE_OK if plate else DEVICE_FAIL,
            "ocr原因": "" if plate else OCR_FAIL_REASON,
            "称重数值": None,
            "称重状态": None,
            "称重原因": "",
            "放行抬杆": None,
            "抬杆原因": "",
            "自动放行结论": None,
            "人工抬杆结论": None,
            "地磅抖动": bool(values.get("地磅抖动")),
            "地磅故障": bool(values.get("地磅故障")),
            "栏杆抖动": bool(values.get("栏杆抖动")),
            "栏杆故障": bool(values.get("栏杆故障")),
            "OCR重试次数": 0,
            "抬杆重试次数": 0,
        }
        rows.append(entry)
        self._sync(entry)
        return self._serialize(entry), []

    # ------------------------------------------------------------------ 动作
    def run_action(self, entry_id: int, action: str, values: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str, bool]:
        values = values or {}
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"进出闸 {entry_id} 不存在或已归档", False
        dispatch = {
            "重新识别": self._action_ocr,
            "地磅取重": self._action_weigh,
            "放行抬杆": self._action_auto_release,
            "重试抬杆": self._action_auto_release,
            "人工抬杆": self._action_manual_lift,
            "现场确认": self._action_confirm,
        }
        if action not in dispatch:
            return None, f"动作「{action}」不属于闸口通行可执行范围", False
        message, ok = dispatch[action](entry, values)
        self._sync(entry)
        return self._serialize(entry), message, ok

    # --- OCR：认不出车牌先说明原因，给「重新识别」；抖动一次后第二次成功 ---
    def _action_ocr(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[str, bool]:
        manual = self._plate_of(values)
        if manual is not None:
            entry["车牌号码"] = manual
            entry["ocr状态"] = DEVICE_OK
            entry["ocr原因"] = "车牌由现场人工补录"
            return "车牌已由现场人工补录，可以继续称重", True
        plate = self._plate_of(entry)
        entry["OCR重试次数"] = int(entry.get("OCR重试次数", 0)) + 1
        if plate is not None:
            if entry.get("ocr状态") == DEVICE_OK:
                return f"车牌已识别为 {plate}，无需重复识别", True
            entry["ocr状态"] = DEVICE_OK
            entry["ocr原因"] = ""
            return f"重新识别成功，车牌 {plate}，请继续地磅取重", True
        # 无车牌：认不出时明确说明原因，现场调整后可继续点「重新识别」
        entry["ocr状态"] = DEVICE_FAIL
        entry["ocr原因"] = OCR_FAIL_REASON
        return f"{OCR_FAIL_REASON}，请调整摄像头/补光后重新识别，或由现场人工补录车牌", False

    # --- 地磅：取不到重量时自动重新取一次；重量一旦取到，后续任何失败都不清掉 ---
    def _action_weigh(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[str, bool]:
        if entry.get("ocr状态") != DEVICE_OK:
            return "车牌尚未识别成功，暂不能称重，请先重新识别车牌", False
        if entry.get("称重状态") == DEVICE_OK:
            return f"已称重量 {entry['称重数值']} 吨，重量已保留，无需重复取重", True
        # 持续故障被现场排除后，重试时可带 device_reset=true 复位仪表
        if values.get("device_reset"):
            entry["地磅故障"] = False
        unstable = bool(entry.pop("地磅抖动", False))
        weight = self._read_scale(entry)
        if unstable and weight is None:
            # 第一次恰好遇到抖动导致无读数：立刻自动补取一次
            weight = self._read_scale(entry)
        if weight is None:
            entry["称重状态"] = DEVICE_FAIL
            entry["称重原因"] = SCALE_FAIL_REASON
            return f"{SCALE_FAIL_REASON}，请检查/复位地磅仪表后点击「地磅取重」重试", False
        entry["称重数值"] = weight
        entry["称重状态"] = DEVICE_OK
        entry["称重原因"] = ""
        hint = SCALE_RETRY_HINT + "，" if unstable else ""
        return f"{hint}地磅取重成功：{weight} 吨，可继续放行", True

    def _read_scale(self, entry: dict[str, Any]) -> float | None:
        """模拟地磅仪表读数（只读、不写状态）：登记时标注持续故障的车读不到数。"""
        if entry.get("地磅故障"):
            return None
        return round(18 + (int(entry.get("id", 0)) * 7) % 25 + 0.35, 2)

    # --- 自动放行：同车牌同方向只放行一次、抬杆失败回退且保留重量 ---
    def _action_auto_release(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[str, bool]:
        if entry.get("status") == STATUS_RELEASED:
            return "该记录已放行，同一车牌同方向重复抬杆只放行一次，不能再次抬杆", False
        if entry.get("status") == STATUS_BLOCKED:
            return "该记录已作终局拦截，如与现场不符请重新登记通行", False
        plate = self._plate_of(entry)
        if entry.get("ocr状态") != DEVICE_OK or plate is None:
            return "OCR 尚未识别出车牌，系统不能自动放行；请重新识别或走人工抬杆", False
        if entry.get("称重状态") != DEVICE_OK or entry.get("称重数值") is None:
            return "地磅尚未称出重量，系统不能自动放行；请先取重", False
        direction = entry.get("进出方向") or ""
        duplicate = self._find_released(plate, direction, exclude_id=int(entry["id"]))
        if duplicate is not None:
            return f"车牌 {plate} 的{direction}已在记录 #{duplicate} 放行过，同一车牌同方向重复抬杆只放行一次", False
        # 栏杆：一次性抖动在服务内自动补抬一次；持续故障落到「抬杆失败」，可走「重试抬杆」
        entry["抬杆重试次数"] = int(entry.get("抬杆重试次数", 0)) + 1
        if values.get("device_reset"):
            entry["栏杆故障"] = False
        lift_ok = self._drive_lift(entry)
        if not lift_ok:
            lift_ok = self._drive_lift(entry)
        if not lift_ok:
            # 关键兜底：不写自动放行结论、状态回退为未放行，称重数值原样保留
            entry["放行抬杆"] = DEVICE_FAIL
            entry["抬杆原因"] = LIFT_FAIL_REASON
            return f"{LIFT_FAIL_REASON}，已回退为未放行，重量 {entry['称重数值']} 吨已保留，请重试抬杆", False
        decision = "拦截" if str(values.get("decision") or "放行").strip() in ("拦截", "block", "拒绝") else "放行"
        entry["自动放行结论"] = decision
        entry["放行抬杆"] = DEVICE_OK
        entry["抬杆原因"] = ""
        return self._reconcile(entry, actor="自动放行")

    def _drive_lift(self, entry: dict[str, Any]) -> bool:
        """模拟栏杆控制器：持续故障抬不起来；一次性抖动只让第一次失败，自动重试即恢复。"""
        if entry.get("栏杆故障"):
            return False
        if entry.pop("栏杆抖动", False):
            return False
        return True

    # --- 人工抬杆：同样查重；与自动放行结论冲突时挂「放行冲突」，等现场确认 ---
    def _action_manual_lift(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[str, bool]:
        if entry.get("status") == STATUS_RELEASED:
            return "该记录已放行，同一车牌同方向重复抬杆只放行一次，不能再次抬杆", False
        manual = self._plate_of(values)
        if manual is not None:
            entry["车牌号码"] = manual
            if entry.get("ocr状态") != DEVICE_OK:
                entry["ocr状态"] = DEVICE_OK
                entry["ocr原因"] = "车牌由现场人工补录"
        plate = self._plate_of(entry)
        if plate is None:
            return "人工抬杆也需要先明确车牌，请补录车牌后再抬杆", False
        decision = "拦截" if str(values.get("decision") or "放行").strip() in ("拦截", "block", "拒绝") else "放行"
        if decision == "放行":
            direction = entry.get("进出方向") or ""
            duplicate = self._find_released(plate, direction, exclude_id=int(entry["id"]))
            if duplicate is not None:
                return f"车牌 {plate} 的{direction}已在记录 #{duplicate} 放行过，同一车牌同方向重复抬杆只放行一次", False
        entry["人工抬杆结论"] = decision
        entry["放行抬杆"] = DEVICE_OK
        entry["抬杆原因"] = ""
        return self._reconcile(entry, actor="人工抬杆")

    def _reconcile(self, entry: dict[str, Any], *, actor: str) -> tuple[str, bool]:
        """合并自动放行与人工抬杆两路结论；冲突以现场确认为准。"""
        auto = entry.get("自动放行结论")
        manual = entry.get("人工抬杆结论")
        if auto is not None and manual is not None and auto != manual:
            plate = self._plate_of(entry) or ""
            return (
                f"自动放行结论为「{auto}」、人工抬杆为「{manual}」，已挂「放行冲突」，"
                f"以现场确认为准（车牌 {plate}，重量 {entry.get('称重数值') or '未称重'} 吨）"
            ), False
        decision = manual if actor == "人工抬杆" else auto
        other = auto if actor == "人工抬杆" else manual
        if decision == "放行":
            if other == decision:
                return f"自动放行与人工抬杆结论一致，栏杆已抬起放行（重量 {entry.get('称重数值') or '未称重'} 吨）", True
            return f"{actor}已放行，栏杆已抬起", True
        return f"{actor}结论为拦截，车辆未放行；如与现场不符请走「现场确认」", True

    # --- 现场确认：冲突/系统拦截的最终裁决，以现场为准 ---
    def _action_confirm(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[str, bool]:
        decision = str(values.get("decision") or "").strip()
        if decision not in ("放行", "拦截"):
            return "现场确认必须给出明确结论：放行或拦截", False
        if entry.get("status") in (STATUS_RELEASED, STATUS_BLOCKED):
            return "该通行记录已经终局确认，不能重复现场确认", False
        plate = self._plate_of(entry)
        if decision == "放行":
            direction = entry.get("进出方向") or ""
            duplicate = self._find_released(plate or "", direction, exclude_id=int(entry["id"]))
            if duplicate is not None:
                return f"车牌 {plate} 的{direction}已在记录 #{duplicate} 放行过，同一车牌同方向重复抬杆只放行一次", False
            entry["放行抬杆"] = DEVICE_OK
            entry["抬杆原因"] = ""
        else:
            entry["放行抬杆"] = DEVICE_FAIL
            entry["抬杆原因"] = "现场确认拦截"
        # 现场结论同时覆盖两路意见，避免再次冲突
        entry["自动放行结论"] = decision
        entry["人工抬杆结论"] = decision
        if decision == "放行":
            return "已按现场确认结论「放行」执行，栏杆已抬起", True
        return "已按现场确认结论「拦截」执行，车辆不予放行", True

    def _find_released(self, plate: str, direction: str, *, exclude_id: int) -> int | None:
        """同一车牌 + 同一进出方向已成功放行过的记录（重复抬杆只放行一次）。

        按方向区分是因为车辆合法的一进一出本就是两次抬杆；同一次通行（同方向）
        因重复提交/重复识别产生的抬杆才需要去重。
        """
        if not plate:
            return None
        plate = plate.strip().upper()
        for row in store.rows(MODULE):
            if int(row.get("id", 0)) == exclude_id:
                continue
            if row.get("status") != STATUS_RELEASED:
                continue
            if (row.get("进出方向") or "") != direction:
                continue
            other = self._plate_of(row)
            if other and other.upper() == plate:
                return int(row["id"])
        return None

    # ------------------------------------------------------------------ 模型
    def _sync(self, entry: dict[str, Any]) -> None:
        """根据各环节子状态推导通行状态；列表与总览共用，保证两处口径一致。"""
        ocr = entry.get("ocr状态")
        scale = entry.get("称重状态")
        auto = entry.get("自动放行结论")
        manual = entry.get("人工抬杆结论")
        both_decided = auto is not None and manual is not None

        # 抬杆设备失败且还没有任何一方给出业务结论时，卡在「抬杆失败」可重试
        if entry.get("放行抬杆") == DEVICE_FAIL and not (auto is not None or manual is not None):
            status = STATUS_LIFT_FAIL
        elif ocr != DEVICE_OK:
            status = STATUS_WAIT_OCR
        elif scale != DEVICE_OK:
            status = STATUS_WAIT_WEIGH
        elif both_decided:
            status = (
                STATUS_RELEASED
                if auto == manual == "放行"
                else STATUS_BLOCKED
                if auto == manual == "拦截"
                else STATUS_CONFLICT
            )
        elif auto is not None or manual is not None:
            decision = auto if auto is not None else manual
            if decision == "放行":
                # 单方放行了即抬杆；另一方若有异议仍可走人工/现场确认覆盖
                status = STATUS_RELEASED
            else:
                # 单方拦截不直接终局，留给另一方或现场确认裁决（以现场确认为准）
                status = STATUS_BLOCK_PENDING
        else:
            status = STATUS_WAIT_RELEASE

        entry["status"] = status
        entry["pending"] = status in (
            STATUS_WAIT_OCR,
            STATUS_WAIT_WEIGH,
            STATUS_WAIT_RELEASE,
            STATUS_LIFT_FAIL,
            STATUS_CONFLICT,
            STATUS_BLOCK_PENDING,
        )
        entry["abnormal"] = self._is_abnormal(entry)

    def _is_abnormal(self, row: dict[str, Any]) -> bool:
        return (
            row.get("status") in (STATUS_LIFT_FAIL, STATUS_CONFLICT)
            or row.get("ocr状态") == DEVICE_FAIL
            or row.get("称重状态") == DEVICE_FAIL
        )

    def _serialize(self, row: dict[str, Any]) -> dict[str, Any]:
        """内部模型 -> 列表展示模型；空字段统一给「暂无（缺：字段）」，绝不漏行。"""
        missing = [field for field in DISPLAY_FIELDS[:4] if not str(row.get(field) or "").strip()]
        plate = self._plate_of(row)
        ocr = row.get("ocr状态")
        ocr_text = {
            DEVICE_OK: f"已识别·{plate}" if plate else "已识别",
            DEVICE_RETRY: "识别重试中",
            DEVICE_FAIL: f"识别失败（{row.get('ocr原因') or OCR_FAIL_REASON}）",
        }.get(ocr, "暂无（缺：OCR识别）")

        scale = row.get("称重状态")
        weight = row.get("称重数值")
        if scale == DEVICE_OK and weight is not None:
            scale_text = f"{weight} 吨"
        elif scale == DEVICE_RETRY:
            scale_text = "重新取重中…"
        elif scale == DEVICE_FAIL:
            scale_text = f"取重失败（{row.get('称重原因') or SCALE_FAIL_REASON}）"
        else:
            scale_text = "暂无（缺：地磅称重）"

        lift = row.get("放行抬杆")
        flow_status = row.get("status")
        if flow_status == STATUS_RELEASED:
            lift_text = "已抬杆放行"
        elif flow_status == STATUS_BLOCKED:
            lift_text = "未抬杆（已拦截）"
        elif flow_status == STATUS_CONFLICT:
            lift_text = "待现场确认"
        elif flow_status == STATUS_BLOCK_PENDING:
            lift_text = "暂未抬杆（拦截待确认）"
        elif lift == DEVICE_FAIL:
            lift_text = f"抬杆失败（{row.get('抬杆原因') or LIFT_FAIL_REASON}）"
        elif lift == DEVICE_OK:
            lift_text = "设备已响应"
        else:
            lift_text = "暂无（缺：放行抬杆）"

        status = row.get("status") or STATUS_WAIT_OCR
        item = {
            "id": row.get("id"),
            "闸口编号": row.get("闸口编号") or "暂无（缺：闸口编号）",
            "闸口类型": row.get("闸口类型") or "暂无（缺：闸口类型）",
            "车道编号": row.get("车道编号") or "暂无（缺：车道编号）",
            "进出方向": row.get("进出方向") or "暂无（缺：进出方向）",
            "车牌号码": plate or "暂无（缺：车牌号码）",
            "OCR识别": ocr_text,
            "地磅称重": scale_text,
            "放行抬杆": lift_text,
            "闸口状态": status,
            "status": status,
            "missing": missing,
            "actions": self._available_actions(row),
            "异常提示": self._hint(row, missing),
            "pending": row.get("pending", True),
            "abnormal": self._is_abnormal(row),
        }
        return item

    def _available_actions(self, row: dict[str, Any]) -> list[str]:
        status = row.get("status")
        if status in (STATUS_RELEASED, STATUS_BLOCKED):
            return []
        if status == STATUS_WAIT_OCR:
            return ["重新识别", "人工抬杆"]
        if status == STATUS_WAIT_WEIGH:
            return ["地磅取重", "人工抬杆"]
        if status == STATUS_WAIT_RELEASE:
            return ["放行抬杆", "人工抬杆"]
        if status == STATUS_LIFT_FAIL:
            return ["重试抬杆", "人工抬杆", "现场确认"]
        if status in (STATUS_CONFLICT, STATUS_BLOCK_PENDING):
            # 自动/人工结论不一致或只有单方拦截：补齐另一方意见，或直接现场确认
            actions: list[str] = []
            if row.get("自动放行结论") is None:
                actions.append("放行抬杆")
            if row.get("人工抬杆结论") is None:
                actions.append("人工抬杆")
            actions.append("现场确认")
            return actions
        return ["放行抬杆", "人工抬杆"]

    def _hint(self, row: dict[str, Any], missing: list[str]) -> str:
        parts: list[str] = []
        if missing:
            parts.append("缺字段：" + "、".join(missing))
        status = row.get("status")
        if row.get("ocr状态") == DEVICE_FAIL:
            parts.append(f"OCR：{row.get('ocr原因') or OCR_FAIL_REASON}，请重新识别")
        if row.get("称重状态") == DEVICE_FAIL:
            parts.append(f"地磅：{row.get('称重原因') or SCALE_FAIL_REASON}，请重试取重")
        if status == STATUS_LIFT_FAIL:
            parts.append(f"抬杆：{row.get('抬杆原因') or LIFT_FAIL_REASON}，已回退为未放行，称重已保留")
        if status == STATUS_CONFLICT:
            parts.append("自动放行与人工抬杆结论不一致，以现场确认为准")
        if status == STATUS_BLOCK_PENDING:
            parts.append("单方给出拦截意见，尚未终局，请另一方复核或现场确认")
        return "；".join(parts)

    # ------------------------------------------------------------------ 工具
    def _plate_of(self, source: dict[str, Any]) -> str | None:
        """从内部字段或入参里取车牌；认不出（空/占位值）返回 None。"""
        for key in ("车牌号码", "plate"):
            value = str(source.get(key) or "").strip()
            if value and value not in _PLATE_EMPTY:
                return value
        return None

    def _norm_direction(self, value: Any) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return ""
        if text in _DIRECTION_IN:
            return "进场"
        if text in _DIRECTION_OUT:
            return "出场"
        return str(value).strip()


service = GateService()
