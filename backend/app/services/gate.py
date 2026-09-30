"""闸口业务规则：识别 → 称重 → 放行 的异常兜底、重试、去重与在场车重算都收在这里。

记录（一条记录对应一辆车在某条车道上的一次过闸）内部状态字段：
  闸口编号 / 闸口类型 / 车道编号 / 进出方向
  车牌（OCR 结果）、OCR状态、OCR原因、OCR次数
  重量（吨）、称重状态、称重原因、称重次数
  放行状态、放行方式、抬杆原因、放行时间
以 _ 结尾的字段是设备故障注入开关，仅用于模拟首步失败、重试恢复。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "gate"

# 登记时就必须给的字段；进出方向允许后续补录
REQUIRED_FIELDS = ["闸口编号", "闸口类型", "车道编号"]
# 放行（自动/人工）前必须齐备的字段
RELEASE_FIELDS = ["进出方向", "车牌"]

DIRECTION_IN = "进场"
DIRECTION_OUT = "出场"
DIRECTIONS = [DIRECTION_IN, DIRECTION_OUT]

# 车道阶段：决定「闸口状态」列（列表页）与抬杆记录当前所处环节
STAGE_OCR_WAIT = "待识别"       # 登记完，OCR 还没给出车牌
STAGE_OCR = "识别受阻"          # OCR 没认出，等重识别
STAGE_WEIGH_WAIT = "待称重"     # 已识别，等取重量
STAGE_WEIGH_FAIL = "称重受阻"   # 地磅没取到重量，等重新取重
STAGE_RELEASE_WAIT = "待放行"   # 识别、称重齐了，等抬杆
STAGE_LIFT_FAIL = "抬杆失败"    # 抬杆没抬起来，回到未放行，重量保留
STAGE_DONE = "已放行"
STAGE_INCOMPLETE = "信息待补"   # 方向等关键信息缺失，先补信息

FAIL_STAGES = {STAGE_OCR, STAGE_WEIGH_FAIL, STAGE_LIFT_FAIL, STAGE_INCOMPLETE}
# 闸口总览状态（车道记录向上聚合，列表页与总览页共用同一口径）
GATE_OPEN = "正常开放"
GATE_QUEUE = "排队等候"
GATE_FAULT = "设备故障"
GATE_CLOSED = "已关闭"

OCR_PENDING = "未识别"
OCR_OK = "识别成功"
OCR_FAIL = "识别失败"

WEIGH_OK = "称重成功"
WEIGH_FAIL = "称重失败"

RELEASE_PENDING = "未放行"
RELEASE_DONE = "已放行"

# 设备给不出结果时的固定原因；真实项目里来自设备/接口返回
REASON_OCR = "OCR 未能识别车牌（画面模糊或被遮挡），请重识别或人工补录车牌"
REASON_WEIGH = "地磅通讯超时，未取到重量"
REASON_LIFT = "抬杆器无应答，抬杆失败"

# 模拟设备读数（真实项目由 OCR、地磅、道闸驱动写入）
_SEED_PLATES = {
    2: "沪E7D32",
    3: "沪E7D32",
    4: "苏B55K88",
    5: "浙C90012",
    6: "苏D44710",
    7: "粤X33210",
    8: "沪D66001",
}
_SEED_WEIGHTS = {2: 31.4, 3: 31.2, 4: 28.6, 5: 26.8, 6: 17.6, 7: 22.5, 8: 29.7}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class GateService:
    # ---------- 查询 ----------
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        direction: str | None = None,
        stage: str | None = None,
        abnormal: bool | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [r for r in rows if keyword in str(r.get("闸口编号", ""))
                    or keyword in str(r.get("车道编号", ""))
                    or keyword in str(r.get("车牌", ""))]
        # 进出方向留空 = 查全部，绝不能因为条件空着就把列表过滤成一片空白
        if direction:
            rows = [r for r in rows if str(r.get("进出方向") or "").strip() == direction]
        if stage:
            rows = [r for r in rows if self._stage_of(r) == stage]
        if abnormal is not None:
            rows = [r for r in rows if self.is_abnormal(r)]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._decorate(dict(r)) for r in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return self._decorate(dict(row)) if row is not None else None

    # ---------- 登记 ----------
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [f for f in REQUIRED_FIELDS if not str(values.get(f) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        row: dict[str, Any] = {"id": max((int(r.get("id", 0)) for r in rows), default=0) + 1}
        for field in REQUIRED_FIELDS:
            row[field] = str(values.get(field)).strip()
        # 方向允许先空着，列表里点出「暂无（缺进出方向）」，由后续补录
        direction = str(values.get("进出方向") or "").strip()
        row["进出方向"] = direction or None
        row["车牌"] = None
        row["OCR状态"] = OCR_PENDING
        row["OCR原因"] = None
        row["OCR次数"] = 0
        row["重量"] = None
        row["称重状态"] = "未称重"
        row["称重原因"] = None
        row["称重次数"] = 0
        row["放行状态"] = RELEASE_PENDING
        row["放行方式"] = None
        row["抬杆原因"] = None
        row["放行时间"] = None
        row["ocr_fail_"] = False
        row["weigh_fail_"] = False
        row["lift_fail_"] = False
        self._sync_flags(row)
        rows.append(row)
        return self._decorate(dict(row)), []

    def supplement(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """补录进出方向/车牌：抬杆前关键信息缺失时的入口。"""
        row = store.find(MODULE, entry_id)
        if row is None:
            return None, f"抬杆记录 {entry_id} 不存在或已归档"
        for field in ("进出方向", "车牌"):
            value = str(values.get(field) or "").strip()
            if value:
                row[field] = value
        self._sync_flags(row)
        return self._decorate(dict(row)), "现场信息已补录，可以继续放行"

    # ---------- OCR 识别 ----------
    def recognize(self, entry_id: int, plate: str | None = None) -> tuple[dict[str, Any] | None, str, str | None]:
        """重识别：先说明认不出的原因，成功后写入车牌再往下走。

        plate 非空表示现场人工补录车牌（OCR 实在认不出时的兜底）。
        返回 (记录, 主消息, 失败原因)；记录为 None 表示业务上没成功。
        """
        row = store.find(MODULE, entry_id)
        if row is None:
            return None, f"抬杆记录 {entry_id} 不存在或已归档", None
        if row["放行状态"] == RELEASE_DONE:
            return self._decorate(dict(row)), "车辆已放行，无需再识别", None

        plate = (plate or "").strip()
        row["OCR次数"] = int(row.get("OCR次数", 0)) + 1

        if plate:
            row["车牌"] = plate
            row["OCR状态"] = OCR_OK
            row["OCR原因"] = None
            self._sync_flags(row)
            return self._decorate(dict(row)), f"已按现场录入车牌 {plate}，OCR 步骤通过", None

        if row.get("ocr_fail_") and row["OCR次数"] < 2:
            row["OCR状态"] = OCR_FAIL
            row["OCR原因"] = REASON_OCR
            self._sync_flags(row)
            return None, "OCR 未识别到车牌", REASON_OCR

        simulated = _SEED_PLATES.get(int(row["id"])) or f"沪A{10000 + int(row['id'])}"
        row["车牌"] = simulated
        row["OCR状态"] = OCR_OK
        row["OCR原因"] = None
        row["ocr_fail_"] = False
        self._sync_flags(row)
        return self._decorate(dict(row)), f"重识别成功，车牌 {simulated}", None

    # ---------- 地磅称重 ----------
    def weigh(self, entry_id: int, *, _retry: bool = False) -> tuple[dict[str, Any] | None, str, str | None, bool]:
        """取重量：取不到时在同一次请求里重新取一次接着走，绝不让前端一直转圈。

        返回 (记录, 主消息, 失败原因, 是否重试恢复)。
        """
        row = store.find(MODULE, entry_id)
        if row is None:
            return None, f"抬杆记录 {entry_id} 不存在或已归档", None, False
        if row["OCR状态"] != OCR_OK:
            return None, "车牌还没识别出来，先完成 OCR 识别再取重量", row.get("OCR原因"), False

        if not _retry:
            row["称重次数"] = int(row.get("称重次数", 0)) + 1
        if row.get("weigh_fail_") and not _retry:
            row["称重状态"] = WEIGH_FAIL
            row["称重原因"] = REASON_WEIGH
            self._sync_flags(row)
            # 第一下取不到，立刻自动重新取一次再接着走（同一个动作内完成兜底）
            return self.weigh(entry_id, _retry=True)

        weight = _SEED_WEIGHTS.get(int(row["id"]))
        if weight is None:
            weight = round(24.0 + (int(row["id"]) * 1.7) % 12, 1)
        recovered = row["称重状态"] == WEIGH_FAIL
        row["重量"] = weight
        row["称重状态"] = WEIGH_OK
        row["称重原因"] = None
        row["weigh_fail_"] = False
        self._sync_flags(row)
        message = f"重新取重成功，毛重 {weight} 吨" if recovered else f"称重完成，毛重 {weight} 吨"
        return self._decorate(dict(row)), message, None, recovered

    # ---------- 放行抬杆 ----------
    def lift(self, entry_id: int, *, manual: bool,
             plate: str | None = None, direction: str | None = None
             ) -> tuple[dict[str, Any] | None, str, list[str]]:
        """自动放行 / 人工抬杆。

        - 自动放行：车牌、方向、重量齐备且同车牌同方向未放行过才抬杆；
          抬杆失败时恢复成未放行，已称重量原样保留，可重试。
        - 人工抬杆：现场确认为准，冲突时盖过自动放行结论，但同一车牌仍只放一次。
        """
        row = store.find(MODULE, entry_id)
        if row is None:
            return None, f"抬杆记录 {entry_id} 不存在或已归档", []

        # 现场补录优先写回（人工抬杆时允许顺带把缺失信息补齐）
        direction = (direction or "").strip()
        plate = (plate or "").strip()
        if direction:
            row["进出方向"] = direction
        if plate:
            row["车牌"] = plate
            if row["OCR状态"] != OCR_OK:
                row["OCR状态"] = OCR_OK
                row["OCR原因"] = None

        missing = self._missing_of(row)
        if missing:
            self._sync_flags(row)
            return None, f"缺{'、'.join(missing)}，补录后才能抬杆", missing

        if row["放行状态"] == RELEASE_DONE:
            return self._decorate(dict(row)), (
                f"车牌 {row['车牌']} 已在 {row['放行时间']} 放行，同一车牌只放行一次"
            ), []

        # 同一个车牌 + 同一进出方向只放行一次（进场、出场各算一次）
        duplicate = self._find_released(row["车牌"], row["进出方向"], exclude_id=entry_id)
        if duplicate is not None:
            mode = "人工抬杆" if manual else "自动放行"
            return None, (
                f"车牌 {row['车牌']}（{row['进出方向']}）已在记录 {duplicate} 放行，"
                f"{mode}被拦下：同一车牌同一方向只放行一次"
            ), []

        if not manual:
            # 自动放行要求重量已经取到；取不到先回去重新取重
            if row["称重状态"] != WEIGH_OK:
                return None, "重量尚未取到，请先重新取重再放行", []
            # lift_fail_ 表示道闸下一次动作会失败一次；抬杆原因已存在说明正在重试，
            # 之前那次失败已经消费掉故障，重试应当成功（故障是瞬时的）
            if row.get("lift_fail_") and not row.get("抬杆原因"):
                row["放行状态"] = RELEASE_PENDING
                row["放行方式"] = None
                row["抬杆原因"] = REASON_LIFT
                self._sync_flags(row)
                return None, REASON_LIFT + "，已恢复为未放行，称重数据保留，可重试抬杆", []

        # 人工抬杆以现场确认为准：自动放行与现场结论冲突时盖过去
        row["放行状态"] = RELEASE_DONE
        row["放行方式"] = "人工抬杆（现场确认）" if manual else "自动放行"
        row["抬杆原因"] = None
        row["放行时间"] = _now()
        row["lift_fail_"] = False
        self._sync_flags(row)
        message = f"{row['放行方式']}完成，车牌 {row['车牌']} 已{row['进出方向']}"
        if manual:
            message += "；现场确认结论优先"
        return self._decorate(dict(row)), message, []

    # ---------- 闸口总览（列表页 & 总览页唯一口径）----------
    def summary(self) -> dict[str, Any]:
        rows = list(store.rows(MODULE))
        released_in = {str(r["车牌"]) for r in rows
                       if r["放行状态"] == RELEASE_DONE and r.get("进出方向") == DIRECTION_IN}
        released_out = {str(r["车牌"]) for r in rows
                        if r["放行状态"] == RELEASE_DONE and r.get("进出方向") == DIRECTION_OUT}
        on_site = len(released_in - released_out)  # 在场车数随抬杆记录重算

        gate_map: dict[str, dict[str, Any]] = {}
        for r in rows:
            gate = gate_map.setdefault(str(r["闸口编号"]), {
                "闸口编号": str(r["闸口编号"]),
                "闸口类型": r.get("闸口类型"),
                "总记录": 0, "待处理": 0, "异常": 0,
                "故障车道": 0, "排队车道": 0,
            })
            stage = self._stage_of(r)
            gate["总记录"] += 1
            if r["放行状态"] != RELEASE_DONE:
                gate["待处理"] += 1
            if stage in FAIL_STAGES or self._missing_of(r):
                gate["异常"] += 1
            if stage in (STAGE_OCR, STAGE_WEIGH_FAIL, STAGE_LIFT_FAIL):
                gate["故障车道"] += 1
            elif r["放行状态"] != RELEASE_DONE:
                gate["排队车道"] += 1

        gates: list[dict[str, Any]] = []
        for gate in gate_map.values():
            if gate["故障车道"]:
                status = GATE_FAULT
            elif gate["待处理"]:
                status = GATE_QUEUE
            else:
                status = GATE_OPEN
            gate["闸口状态"] = status
            gates.append(gate)
        status_count = {GATE_OPEN: 0, GATE_QUEUE: 0, GATE_FAULT: 0, GATE_CLOSED: 0}
        for gate in gates:
            status_count[gate["闸口状态"]] += 1

        return {
            "闸口总数": len(gates),
            "正常开放": status_count[GATE_OPEN],
            "排队等候": status_count[GATE_QUEUE],
            "设备故障": status_count[GATE_FAULT],
            "在场车数": on_site,
            "待处理": sum(1 for r in rows if r["放行状态"] != RELEASE_DONE),
            "异常记录": sum(1 for r in rows if self.is_abnormal(r)),
            "闸口明细": sorted(gates, key=lambda g: g["闸口编号"]),
        }

    # ---------- 内部推导 ----------
    def is_abnormal(self, row: dict[str, Any]) -> bool:
        return self._stage_of(row) in FAIL_STAGES or bool(self._missing_of(row))

    def _stage_of(self, row: dict[str, Any]) -> str:
        if row["放行状态"] == RELEASE_DONE:
            return STAGE_DONE
        missing = self._missing_of(row)
        if row["OCR状态"] == OCR_FAIL:
            return STAGE_OCR
        if row["称重状态"] == WEIGH_FAIL:
            return STAGE_WEIGH_FAIL
        if row.get("抬杆原因"):
            return STAGE_LIFT_FAIL
        if "进出方向" in missing:
            return STAGE_INCOMPLETE
        if row["OCR状态"] != OCR_OK:
            return STAGE_OCR_WAIT
        if row["称重状态"] != WEIGH_OK:
            return STAGE_WEIGH_WAIT
        return STAGE_RELEASE_WAIT

    def _missing_of(self, row: dict[str, Any]) -> list[str]:
        return [f for f in RELEASE_FIELDS if not str(row.get(f) or "").strip()]

    def _find_released(self, plate: str, direction: str, *, exclude_id: int) -> int | None:
        for r in store.rows(MODULE):
            if int(r.get("id", 0)) == exclude_id:
                continue
            if (r.get("放行状态") == RELEASE_DONE
                    and str(r.get("车牌") or "") == plate
                    and str(r.get("进出方向") or "") == direction):
                return int(r["id"])
        return None

    def _sync_flags(self, row: dict[str, Any]) -> None:
        """把推导结果回写到 status/pending/abnormal，保证 /api/overview 看板同口径。"""
        stage = self._stage_of(row)
        row["status"] = stage
        row["pending"] = row["放行状态"] != RELEASE_DONE
        row["abnormal"] = stage in FAIL_STAGES or bool(self._missing_of(row))

    def _decorate(self, row: dict[str, Any]) -> dict[str, Any]:
        """组装给前端的展示列：空字段给「暂无」并点出缺哪一项。"""
        missing = self._missing_of(row)
        stage = self._stage_of(row)

        if str(row.get("进出方向") or "").strip():
            row["进出方向_展示"] = str(row["进出方向"])
        else:
            row["进出方向_展示"] = "暂无（缺进出方向）"

        if row["OCR状态"] == OCR_OK:
            row["OCR识别"] = str(row["车牌"])
        elif row["OCR状态"] == OCR_FAIL:
            row["OCR识别"] = "暂无（OCR 未识别车牌，可重识别或人工补录）"
        else:
            row["OCR识别"] = "暂无（待识别）"

        if row["称重状态"] == WEIGH_OK:
            row["地磅称重"] = f"{row['重量']} 吨"
        elif row["称重状态"] == WEIGH_FAIL:
            row["地磅称重"] = "暂无（称重失败，已自动重新取重）"
        else:
            row["地磅称重"] = "暂无（未称重）"

        if row["放行状态"] == RELEASE_DONE:
            row["放行抬杆"] = f"已放行 · {row['放行方式']}"
        else:
            row["放行抬杆"] = "未放行"
        # 抬杆失败：状态必须显示未放行，同时保住已称重量
        if stage == STAGE_LIFT_FAIL:
            if row["重量"] is not None:
                row["地磅称重"] = f"{row['重量']} 吨（已保留）"
            row["放行抬杆"] = "未放行（抬杆失败，可重试）"

        row["闸口状态"] = stage
        row["缺失字段"] = missing
        row["OCR原因_展示"] = row.get("OCR原因")
        row["称重原因_展示"] = row.get("称重原因")
        row["抬杆原因_展示"] = row.get("抬杆原因")
        return row
