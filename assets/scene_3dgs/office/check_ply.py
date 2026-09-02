"""诊断 ply 完整性:比对 header 声明的顶点/属性数 与实际文件大小,判断是否被截断。
用法:python assets/scene_3dgs/office/check_ply.py
"""
import os

d = os.path.dirname(os.path.abspath(__file__))
for name in ["pgsr_office.ply", "pgsr_office_transformed.ply"]:
    p = os.path.join(d, "3dgs", name)
    if not os.path.exists(p):
        print(f"{name}: 不存在")
        continue
    nverts = nprops = hdr = 0
    is_bin = True
    with open(p, "rb") as f:
        while True:
            ln = f.readline()
            hdr += len(ln)
            s = ln.decode("ascii", "replace").strip()
            if s.startswith("format ascii"):
                is_bin = False
            if s.startswith("element vertex"):
                nverts = int(s.split()[-1])
            elif s.startswith("property"):
                nprops += 1
            elif s == "end_header":
                break
    size = os.path.getsize(p)
    expect = hdr + nverts * nprops * 4          # 假设属性全 float32(4B)
    data_actual = size - hdr
    data_expect = expect - hdr
    pct = 100.0 * data_actual / max(data_expect, 1)
    ok = is_bin and abs(size - expect) < nverts * 4
    print(f"{name}: {nverts:,} 顶点 × {nprops} 属性 | 文件 {size:,}B | 期望≈{expect:,}B "
          f"| 数据部分是期望的 {pct:.0f}% | {'✓ 完整' if ok else '✗ 不完整(被截断)'}")
