"""
get_available_object.py - 查询 robocasa 物体库中可用的物体类别和分组

robocasa 物体库包含 198 个物体类别和 230 个分组。
类别是单个物体（如 pot、apple），分组是多个类别的集合（如 cookware = pan + pot + saucepan + kettle）。
在 objects.yaml 中 obj_groups 字段填类别名或分组名都可以。
查看分组详情时会显示模型来源（objaverse/lightwheel/aigen）和可用模型数量。

使用方式:
    python get_available_object.py                  # 列出所有物体类别
    python get_available_object.py --groups          # 列出所有分组
    python get_available_object.py --search pot      # 搜索包含 pot 的类别
    python get_available_object.py --detail cookware # 查看 cookware 分组的详细信息
"""

import argparse
from robocasa.models.objects.kitchen_objects import OBJ_CATEGORIES, OBJ_GROUPS


def list_categories():
    """列出所有物体类别"""
    print(f"\n物体类别 (共 {len(OBJ_CATEGORIES)} 个):")
    print("-" * 50)
    for i, name in enumerate(sorted(OBJ_CATEGORIES.keys()), 1):
        print(f"  {i:>3d}. {name}")


def list_groups():
    """列出所有物体分组"""
    print(f"\n物体分组 (共 {len(OBJ_GROUPS)} 个):")
    print("-" * 50)
    for name in sorted(OBJ_GROUPS.keys()):
        cats = OBJ_GROUPS[name]
        if len(cats) <= 5:
            print(f"  {name}: {', '.join(cats)}")
        else:
            print(f"  {name}: ({len(cats)} 个) {', '.join(cats[:5])}, ...")


def search(keyword):
    """搜索包含关键词的类别"""
    keyword = keyword.lower()
    matches = [n for n in sorted(OBJ_CATEGORIES.keys()) if keyword in n]
    if matches:
        print(f"\n包含 '{keyword}' 的类别 (共 {len(matches)} 个):")
        for name in matches:
            print(f"  - {name}")
    else:
        print(f"\n没有找到包含 '{keyword}' 的类别")


def show_detail(group_name):
    """显示某个分组的详细信息"""
    if group_name in OBJ_GROUPS:
        cats = OBJ_GROUPS[group_name]
        print(f"\n分组 '{group_name}' 包含 {len(cats)} 个类别:")
        for cat in cats:
            info = _get_cat_info(cat)
            print(f"  - {cat}: {info}")
    elif group_name in OBJ_CATEGORIES:
        info = _get_cat_info(group_name)
        print(f"\n类别 '{group_name}': {info}")
    else:
        print(f"\n未找到 '{group_name}'")


def _get_cat_info(cat_name):
    """获取类别的属性摘要"""
    cat_dict = OBJ_CATEGORIES.get(cat_name, {})
    props = []
    for reg_type, cat in cat_dict.items():
        parts = [f"[{reg_type}]"]
        if cat.graspable:
            parts.append("可抓取")
        if cat.washable:
            parts.append("可水洗")
        if cat.microwavable:
            parts.append("可微波")
        if cat.cookable:
            parts.append("可烹饪")
        if cat.fridgable:
            parts.append("可冷藏")
        parts.append(f"{len(cat.mjcf_paths)} 个模型")
        props.append(" ".join(parts))
    return " | ".join(props) if props else "无模型"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="查询 robocasa 可用物体")
    parser.add_argument("--groups", action="store_true", help="列出所有物体分组")
    parser.add_argument("--search", type=str, help="搜索包含关键词的类别")
    parser.add_argument("--detail", type=str, help="查看某个类别/分组的详细信息")
    args = parser.parse_args()

    if args.search:
        search(args.search)
    elif args.detail:
        show_detail(args.detail)
    elif args.groups:
        list_groups()
    else:
        list_categories()
