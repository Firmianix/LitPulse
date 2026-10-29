"""
mark.py —— 给记忆库里的论文打标记
=================================
A) 阅读状态：
  python3 mark.py 2610.29933 useful     # 标记为"有用/想精读"
  python3 mark.py 2610.29933 read       # 标记为"已读"
  python3 mark.py 2610.29933 skip       # 标记为"忽略"
  python3 mark.py list                  # 列出记忆库概况
  python3 mark.py list useful           # 只看标记为有用的论文

B) 研究立场（确认/修正 AI 的判断；确认后 AI 以后不再覆盖）：
  python3 mark.py 2610.29933 stance support   # 支持我的课题
  python3 mark.py 2610.29933 stance oppose    # 质疑/反例
  python3 mark.py 2610.29933 stance method    # 方法可借鉴
  python3 mark.py 2610.29933 stance data      # 数据集/代码可复用
  python3 mark.py 2610.29933 stance related   # 仅相关
  python3 mark.py stance method               # 列出"方法可借鉴"的所有论文

阅读状态：new 新见 / read 已读 / useful 有用 / skip 忽略
研究立场：support 支持 / oppose 质疑 / method 方法 / data 数据 / related 仅相关
"""

import sys
import json
import os

SEEN_PATH = os.path.join("data", "seen.json")
VALID_STATUS = {"new", "read", "useful", "skip"}
VALID_STANCE = {"support", "oppose", "method", "data", "related"}
STANCE_EMOJI = {"support": "✅支持", "oppose": "⚠️质疑", "method": "🔧方法",
                "data": "📦数据", "related": "🔗相关"}


def load():
    if not os.path.exists(SEEN_PATH):
        print("还没有记忆库 data/seen.json，请先运行 python3 weekly_pulse.py")
        sys.exit(1)
    return json.load(open(SEEN_PATH, encoding="utf-8"))


def save(data):
    json.dump(data, open(SEEN_PATH, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]

    # python3 mark.py list [状态]
    if cmd == "list":
        data = load()
        want = sys.argv[2] if len(sys.argv) > 2 else None
        rows = [(pid, r) for pid, r in data.items()
                if want is None or r.get("status") == want]
        print(f"共 {len(rows)} 篇" + (f"（阅读状态={want}）" if want else "："))
        for pid, r in sorted(rows, key=lambda x: -(x[1].get("score") or 0)):
            print(f"  [{r.get('status','?'):6}] {r.get('score','-')}/10  "
                  f"{pid}  {r.get('title','')[:60]}")
        return

    # python3 mark.py stance <立场>  —— 列出某立场的论文
    if cmd == "stance" and len(sys.argv) == 3 and sys.argv[2] in VALID_STANCE:
        data = load()
        want = sys.argv[2]
        rows = [(pid, r) for pid, r in data.items() if r.get("stance") == want]
        rows.sort(key=lambda x: -(x[1].get("score") or 0))
        print(f"立场={STANCE_EMOJI[want]} 共 {len(rows)} 篇"
              f"（👤=你已确认，🤖=AI初判）：")
        for pid, r in rows:
            who = "👤" if r.get("stance_confirmed") else "🤖"
            print(f"  {who} {r.get('score','-')}/10  {pid}  {r.get('title','')[:56]}")
        return

    # python3 mark.py <论文ID> stance <立场>
    if len(sys.argv) >= 4 and sys.argv[2].lower() == "stance":
        pid, stance = sys.argv[1], sys.argv[3].lower()
        if stance not in VALID_STANCE:
            print(f"立场必须是：{VALID_STANCE}")
            return
        data = load()
        if pid not in data:
            print(f"记忆库里没有论文 {pid}，先用 list 查看可用 ID")
            return
        old = data[pid].get("stance")
        data[pid]["stance"] = stance
        data[pid]["stance_confirmed"] = True     # 人确认后锁定，AI 不再覆盖
        save(data)
        print(f"✅ 已确认 {pid} 的立场为 {STANCE_EMOJI[stance]}（原：{old}，已锁定）")
        print(f"   {data[pid].get('title','')[:70]}")
        return

    # python3 mark.py <论文ID> <阅读状态>
    if len(sys.argv) < 3:
        print("示例：python3 mark.py 2610.29933 useful")
        return
    pid, status = sys.argv[1], sys.argv[2].lower()
    if status not in VALID_STATUS:
        print(f"阅读状态必须是：{VALID_STATUS}；立场操作请加 stance，见文档头")
        return

    data = load()
    if pid not in data:
        print(f"记忆库里没有论文 {pid}，先用 list 查看可用 ID")
        return
    old = data[pid].get("status")
    data[pid]["status"] = status
    save(data)
    print(f"✅ 已把 {pid} 标记为 {status}（原状态：{old}）")
    print(f"   {data[pid].get('title','')[:70]}")


if __name__ == "__main__":
    main()
