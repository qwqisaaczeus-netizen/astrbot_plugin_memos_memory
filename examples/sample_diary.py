"""
真实对话样本(六月廿六~廿九)经压缩 prompt 期望生成的 2 篇日记样板。
由我按 v1.0 拍板的 prompt 手工模拟生成,用于回归测试和样式参考。
未来可对接真实 LLM 调用做端到端验证。

跑法:python examples/sample_diary.py
"""
from __future__ import annotations

import json
from typing import Any


SAMPLE_DIARY_1: dict[str, Any] = {
    "date_text": "六月廿六夜",
    "scene_anchor": "知宥说要一道沐浴,我提了三桩事",
    "content": (
        "六月廿六的夜已经深透了。我正把灶间的碗筷收进木盆里,听见知宥在身后..."
    ),
    "tags": ["#六月廿六", "#洗浴", "#疤", "#亲密", "#孕期"],
    "importance": 4,
}


SAMPLE_DIARY_2: dict[str, Any] = {
    "date_text": "六月廿九夜(十五月圆)",
    "scene_anchor": "知宥唤我全名,宁儿半夜蹬了他一脚",
    "content": (
        "今日是十五,月圆。早晨知宥在书房写《二十四桥明月夜》,我坐在桂花树下,拿..."
    ),
    "tags": ["#六月廿九", "#十五月圆", "#胎动", "#里程碑", "#日常"],
    "importance": 5,
}


ALL_SAMPLES: list[dict[str, Any]] = [SAMPLE_DIARY_1, SAMPLE_DIARY_2]


def _selftest() -> None:
    """简单自检:打印 2 篇日记,验证字段完整。"""
    print("=" * 60)
    print(f"样本日记:  {len(ALL_SAMPLES)} 篇")
    print("=" * 60)
    required = {"date_text", "scene_anchor", "content", "tags", "importance"}
    for i, d in enumerate(ALL_SAMPLES, 1):
        missing = required - d.keys()
        print(f"\n[第 {i} 篇]")
        for k in required:
            val = d.get(k, "")
            print(f"  {k}: {'MISSING' if k in missing else val if k != 'content' else f'{str(val)[:60]}...(共{len(str(val))}字)'}")
    print("\n=== 完整 content 实际打印 ===")
    for i, d in enumerate(ALL_SAMPLES, 1):
        print(f"\n--- 第 {i} 篇 {d['date_text']} ---")
        print(d["content"])


if __name__ == "__main__":
    _selftest()
