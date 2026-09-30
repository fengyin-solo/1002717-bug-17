"""闸口异常兜底回归测试：只依赖标准库，运行方式  python3 backend/scripts/check_gate.py

覆盖：
- 进出方向空着不把列表过滤空；空字段显示「暂无」并点出缺项
- OCR 认不出先给原因、可重识别 / 人工补录
- 地磅取不到时后端自动重新取一次接着走
- 抬杆失败恢复未放行、重量保留、可重试
- 自动放行与人工抬杆冲突时以现场确认为准
- 同车牌同方向只放行一次（进场、出场各算一次）
- 在场车数随抬杆记录重算；闸口总览状态与看板 pending/abnormal 同口径
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.gate import (  # noqa: E402
    GateService,
    RELEASE_DONE,
    RELEASE_PENDING,
)
from app.store import store  # noqa: E402


def fresh_service() -> GateService:
    store.reset("gate")
    return GateService()


class GateFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = fresh_service()

    def test_empty_direction_keeps_full_list(self) -> None:
        _, total_none = self.svc.list_entries(direction=None)
        _, total_blank = self.svc.list_entries(direction="")
        self.assertEqual((total_none, total_blank), (8, 8))

    def test_missing_fields_are_named(self) -> None:
        row = self.svc.get_entry(7)
        self.assertEqual(row["进出方向_展示"], "暂无（缺进出方向）")
        self.assertIn("进出方向", row["缺失字段"])
        row4 = self.svc.get_entry(4)
        self.assertTrue(row4["OCR识别"].startswith("暂无"))
        self.assertIn("OCR", row4["OCR识别"])

    def test_ocr_failure_then_retry_and_manual_plate(self) -> None:
        # 种子里记录 4 第一次尝试已失败，失败原因必须可读
        seeded = store.find("gate", 4)
        self.assertIsNotNone(seeded["OCR原因"])
        # 再点重识别即恢复
        entry, _msg, _reason = self.svc.recognize(4)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["车牌"], "苏B55K88")
        # 人工补录车牌直接通过
        entry, _msg, _reason = self.svc.recognize(8, plate="沪Z00000")
        self.assertEqual(entry["车牌"], "沪Z00000")

    def test_weigh_auto_retries_once(self) -> None:
        entry, _msg, _reason, recovered = self.svc.weigh(5)
        self.assertTrue(recovered)
        self.assertEqual(entry["重量"], 26.8)
        self.assertEqual(entry["称重次数"], 2)
        self.assertEqual(entry["地磅称重"], "26.8 吨")

    def test_lift_failure_keeps_weight_and_retries(self) -> None:
        row = self.svc.get_entry(6)
        self.assertEqual(row["放行状态"], RELEASE_PENDING)
        self.assertEqual(row["重量"], 17.6)
        self.assertIn("已保留", row["地磅称重"])
        entry, _msg, _missing = self.svc.lift(6, manual=False)
        self.assertEqual(entry["放行状态"], RELEASE_DONE)
        self.assertEqual(entry["重量"], 17.6)

    def test_lift_failure_full_cycle_on_new_record(self) -> None:
        entry, missing = self.svc.create_entry({
            "闸口编号": "GATE-04", "闸口类型": "出口闸",
            "车道编号": "L-31", "进出方向": "出场",
        })
        self.assertFalse(missing)
        nid = int(entry["id"])
        self.svc.recognize(nid)
        self.svc.weigh(nid)
        store.find("gate", nid)["lift_fail_"] = True  # type: ignore[index]
        entry, message, _missing = self.svc.lift(nid, manual=False)
        self.assertIsNone(entry)
        self.assertIn("抬杆失败", message)
        self.assertIsNotNone(self.svc.get_entry(nid)["重量"])
        entry, _msg, _missing = self.svc.lift(nid, manual=False)
        self.assertEqual(entry["放行状态"], RELEASE_DONE)

    def test_duplicate_plate_blocked_for_both_modes(self) -> None:
        entry, message, _missing = self.svc.lift(3, manual=False)
        self.assertIsNone(entry)
        self.assertIn("只放行一次", message)
        entry, message, _missing = self.svc.lift(3, manual=True)
        self.assertIsNone(entry)
        self.assertIn("只放行一次", message)

    def test_entry_and_exit_of_same_plate_both_allowed(self) -> None:
        # 记录 1 进场、记录 2 出场，同车牌但方向不同，都已放行
        self.assertEqual(self.svc.get_entry(1)["放行状态"], RELEASE_DONE)
        self.assertEqual(self.svc.get_entry(2)["放行状态"], RELEASE_DONE)

    def test_supplement_direction_before_lift(self) -> None:
        entry, _msg, missing = self.svc.lift(7, manual=True)
        self.assertIsNone(entry)
        self.assertIn("进出方向", missing)
        entry, _msg = self.svc.supplement(7, {"进出方向": "进场"})
        self.assertEqual(entry["进出方向"], "进场")
        entry, message, _missing = self.svc.lift(7, manual=True)
        self.assertEqual(entry["放行状态"], RELEASE_DONE)
        self.assertIn("现场确认", message)

    def test_manual_lift_overrides_auto_prerequisites(self) -> None:
        self.svc.recognize(8, plate="沪Z00000")
        entry, _msg, _missing = self.svc.lift(8, manual=False)
        self.assertIsNone(entry)  # 没称重量，自动放行被拦
        entry, _msg, _missing = self.svc.lift(8, manual=True)
        self.assertEqual(entry["放行状态"], RELEASE_DONE)
        self.assertEqual(entry["放行方式"], "人工抬杆（现场确认）")

    def test_on_site_count_recomputed(self) -> None:
        # 基线：沪A12345 已进已出，在场 0
        self.assertEqual(self.svc.summary()["在场车数"], 0)
        # 7 补方向后人工放行（进场，新车牌），在场变 1
        self.svc.supplement(7, {"进出方向": "进场"})
        self.svc.lift(7, manual=True)
        self.assertEqual(self.svc.summary()["在场车数"], 1)
        # 6 出场放行（新车牌），不影响进场集合，仍为 1
        self.svc.lift(6, manual=False)
        self.assertEqual(self.svc.summary()["在场车数"], 1)

    def test_gate_status_recovers_from_faults(self) -> None:
        self.assertEqual(self.svc.summary()["设备故障"], 2)
        self.svc.recognize(4)
        self.svc.weigh(5)
        self.svc.lift(6, manual=False)
        self.assertEqual(self.svc.summary()["设备故障"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
