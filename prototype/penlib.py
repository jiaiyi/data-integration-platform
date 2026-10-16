"""pen.dev (.pen) 原型生成工具库。

约定：所有节点用工厂函数构造，避免手写 JSON。
注意 .pen schema 的几个硬约束：
  - id 不能含 "/"（"/" 是 ref 的 descendants 路径分隔符）
  - lineHeight 是 fontSize 的 **倍数**（1.5 = 150%），不是 px
  - 给定 width 的文本块必须设 textGrowth="fixed-width" 才会换行
  - 不要用空文本占位；对齐需求用 justifyContent / alignItems 表达
"""

import itertools

_c = itertools.count(1)

# ---- 设计令牌 ----
BG = "#F4F6F9"
CARD = "#FFFFFF"
BORDER = "#E3E8EF"
BORDER_STRONG = "#CBD5E1"
TEXT = "#111827"
SUB = "#5B6472"
MUTED = "#98A2B3"
PRIMARY = "#2563EB"
PRIMARY_SOFT = "#EFF6FF"
GREEN = "#059669"
GREEN_SOFT = "#ECFDF5"
AMBER = "#B45309"
AMBER_SOFT = "#FFFBEB"
RED = "#DC2626"
RED_SOFT = "#FEF2F2"
PURPLE = "#7C3AED"
PURPLE_SOFT = "#F5F3FF"
TEAL = "#0D9488"
TEAL_SOFT = "#F0FDFA"
FONT = "Microsoft YaHei"

# 画板尺寸与网格
BOARD_W = 1440
BOARD_H = 920
COL_STEP = 1560
ROW_STEP = 1020


def uid(prefix="n"):
    return f"{prefix}{next(_c)}"


# ---------------------------------------------------------------- 基础节点
def T(
    content, size=13, weight="400", color=None, align="left", w=None, h=None, lh=1.5, family=None
):
    """文本节点。给定 w 不给 h 时自动换行（fixed-width）。"""
    n = {
        "id": uid("t"),
        "type": "text",
        "content": content,
        "fontSize": size,
        "fontWeight": weight,
        "fontFamily": family or FONT,
        "fill": color or TEXT,
        "textAlign": align,
        "lineHeight": lh,
    }
    if w is not None:
        n["width"] = w
        n["textGrowth"] = "fixed-width" if h is None else "fixed-width-height"
    if h is not None:
        n["height"] = h
    return n


def R(w, h, fill, radius=0, stroke=None):
    n = {"id": uid("r"), "type": "rectangle", "width": w, "height": h, "fill": fill}
    if radius:
        n["cornerRadius"] = radius
    if stroke:
        n.update({"stroke": stroke, "strokeWidth": 1, "strokeAlignment": "inner"})
    return n


def I(icon, size=16, color=None):
    return {
        "id": uid("i"),
        "type": "icon",
        "library": "lucide",
        "icon": icon,
        "width": size,
        "height": size,
        "fill": color or SUB,
    }


def F(
    children,
    w=None,
    h=None,
    fill=None,
    layout="vertical",
    gap=0,
    padding=None,
    radius=0,
    stroke=None,
    justify=None,
    align=None,
    clip=False,
    x=None,
    y=None,
    nid=None,
    name=None,
):
    n = {"id": nid or uid("f"), "type": "frame", "layout": layout, "children": children}
    if name:
        n["name"] = name
    if w is not None:
        n["width"] = w
    if h is not None:
        n["height"] = h
    if x is not None:
        n["x"] = x
    if y is not None:
        n["y"] = y
    if fill:
        n["fill"] = fill
    if gap:
        n["gap"] = gap
    if padding is not None:
        n["padding"] = padding
    if radius:
        n["cornerRadius"] = radius
    if stroke:
        n.update({"stroke": stroke, "strokeWidth": 1, "strokeAlignment": "inner"})
    if justify:
        n["justifyContent"] = justify
    if align:
        n["alignItems"] = align
    if clip:
        n["clip"] = True
    return n


# ---------------------------------------------------------------- 复合组件
def H(children, align="center", **kw):
    """水平 frame 的语法糖（默认垂直居中）。"""
    return F(children, layout="horizontal", align=align, **kw)


def V(children, **kw):
    return F(children, layout="vertical", **kw)


def hr(color=None, w=None):
    """水平分隔线。"""
    line = R(w if w is not None else "fill_container", 1, color or BORDER)
    return line


def badge(text, fg=SUB, bg=None):
    """小标签。"""
    return H(
        [T(text, size=11, weight="600", color=fg, lh=1.2)],
        padding=[3, 8],
        radius=999,
        fill=bg or PRIMARY_SOFT,
    )


def btn(text, variant="primary", icon=None, w=None):
    """按钮。variant: primary / ghost / danger / soft"""
    palette = {
        "primary": (PRIMARY, "#FFFFFF"),
        "ghost": ("#FFFFFF", SUB),
        "danger": (RED_SOFT, RED),
        "soft": (PRIMARY_SOFT, PRIMARY),
    }
    fill, fg = palette[variant]
    kids = []
    if icon:
        kids = kids + [I(icon, 14, fg)]
    kids = kids + [T(text, size=12, weight="600", color=fg, lh=1.2)]
    kw = {"padding": [7, 13], "radius": 6, "gap": 6, "fill": fill}
    if w is not None:
        kw["w"] = w
    if variant == "ghost":
        kw["stroke"] = BORDER_STRONG
    return H(kids, **kw)


def kpi(label, value, delta=None, delta_color=GREEN, accent=PRIMARY):
    """指标卡：标签 + 大数字 + 可选变化值。"""
    top = H([T(label, size=12, color=SUB)], justify="space_between")
    row_kids = [T(value, size=26, weight="700", color=TEXT, lh=1.2)]
    if delta:
        row_kids = row_kids + [T(delta, size=11, weight="600", color=delta_color, lh=1.2)]
    return V(
        [top, H(row_kids, gap=8, align="end")],
        w="fill_container",
        h="fill_container",
        fill=CARD,
        radius=10,
        stroke=BORDER,
        padding=16,
        gap=10,
    )


def card(title, children, subtitle=None, right=None, pad=16, gap=12):
    """带标题的卡片。"""
    head = []
    if right:
        head = [
            H([T(title, size=14, weight="700", color=TEXT, lh=1.3)], justify="space_between"),
            right,
        ]
        head = [
            H(
                [T(title, size=14, weight="700", color=TEXT, lh=1.3), right],
                justify="space_between",
                w="fill_container",
            )
        ]
    else:
        head = [T(title, size=14, weight="700", color=TEXT, lh=1.3)]
    if subtitle:
        head = head + [T(subtitle, size=11.5, color=MUTED, lh=1.4, w="fill_container")]
    return V(
        head + children,
        w="fill_container",
        fill=CARD,
        radius=10,
        stroke=BORDER,
        padding=pad,
        gap=gap,
    )


def table(headers, rows, widths, head_bg=None):
    """简易表格。rows 为二维数组，元素可为 str 或已构造好的节点。"""
    head_cells = []
    for i, htxt in enumerate(headers):
        head_cells = head_cells + [
            H([T(htxt, size=11.5, weight="700", color=SUB, lh=1.2)], w=widths[i])
        ]
    head = H(head_cells, w="fill_container", fill=head_bg or "#F8FAFC", padding=[9, 12])
    body = []
    for r in rows:
        cells = []
        for i, cell in enumerate(r):
            if isinstance(cell, str):
                cells = cells + [H([T(cell, size=12, color=TEXT, lh=1.3)], w=widths[i])]
            else:
                cells = cells + [H([cell], w=widths[i])]
        body = body + [H(cells, w="fill_container", padding=[9, 12])]
        body = body + [hr("#F1F5F9")]
    return V([head] + body, w="fill_container", fill=CARD, radius=10, stroke=BORDER, clip=True)


def meter(value, total, color=PRIMARY, h=6, bg="#EEF2F7", w=150):
    """横向进度/占比条。"""
    w_full = w
    filled = int(w_full * (float(value) / float(total))) if total else 0
    filled = max(filled, 2)
    return V(
        [H([R(filled, h, color), R(max(w_full - filled, 0), h, bg)], gap=0, w=w_full)],
        w=w_full,
        h=h,
    )


def vbar(label, pct, color=PRIMARY, w=52):
    """竖向柱（用于分布示意）。track 为 100% 对应的像素高度。"""
    track = 96
    filled = int(track * pct / 100.0)
    filled = max(filled, 4)
    return V(
        [
            R(w, filled, color, radius=4),
            T(label, size=10, color=MUTED, lh=1.2, align="center", w=w),
            T(f"{pct}%", size=10, weight="600", color=SUB, lh=1.2, align="center", w=w),
        ],
        w=w,
        gap=6,
        align="center",
        justify="end",
    )


def field(label, value, mono=False):
    """键值对展示行。"""
    fam = "Consolas" if mono else None
    return H(
        [
            T(label, size=11.5, color=MUTED, w=104, lh=1.3),
            T(value, size=11.5, weight="600", color=TEXT, lh=1.3, w="fill_container", family=fam),
        ],
        gap=10,
        align="start",
        w="fill_container",
    )


def kbrow(k, v, vcolor=None):
    return H(
        [
            T(k, size=11, color=MUTED, lh=1.3, w=76),
            T(v, size=11, weight="600", color=vcolor or TEXT, lh=1.3, w="fill_container"),
        ],
        gap=8,
        w="fill_container",
    )


def nav_item(label, icon, active=False):
    fg = PRIMARY if active else SUB
    kids = [
        I(icon, 16, fg),
        T(label, size=12.5, weight="600" if active else "400", color=fg, lh=1.2),
    ]
    kw = {"padding": [9, 12], "radius": 8, "gap": 10, "w": "fill_container"}
    if active:
        kw["fill"] = PRIMARY_SOFT
    return H(kids, **kw)


def page(board_name, board_id, active, title, subtitle, body, x, y):
    """组装一个完整页面画板：侧栏 + 顶栏 + 内容。"""
    nav_defs = [
        ("总览", "layout-dashboard", active == "overview"),
        ("数据连接", "database", active == "connections"),
        ("Pipeline Studio", "sparkles", active == "studio"),
        ("运行中心", "activity", active == "operations"),
        ("安全治理", "shield-check", active == "governance"),
    ]
    nav_items = []
    for label, icon, act in nav_defs:
        nav_items = nav_items + [nav_item(label, icon, act)]

    brand = H(
        [
            F(
                [T("ETL", size=11, weight="700", color="#FFFFFF", lh=1.2)],
                w=28,
                h=28,
                fill=PRIMARY,
                radius=8,
                justify="center",
                align="center",
            ),
            V(
                [
                    T("ETL-Agent", size=13.5, weight="700", color=TEXT, lh=1.2),
                    T("数据集成控制台", size=10, color=MUTED, lh=1.2),
                ],
                gap=2,
            ),
        ],
        gap=10,
        padding=[16, 12, 14, 12],
    )

    sidebar = V(
        [
            brand,
            hr("#EDF1F6"),
            V(nav_items, padding=[8, 10], gap=3, w="fill_container"),
            V([hr("#EDF1F6")], w="fill_container", h="fill_container", justify="end"),
            V(
                [
                    H(
                        [
                            I("git-branch", 12, MUTED),
                            T("harness-v2-20260808", size=10, color=MUTED, lh=1.2),
                        ],
                        gap=6,
                    ),
                    H(
                        [
                            I("server", 12, MUTED),
                            T("环境 local · v0.1.0", size=10, color=MUTED, lh=1.2),
                        ],
                        gap=6,
                    ),
                ],
                padding=[10, 12, 14, 12],
                gap=6,
                w="fill_container",
                fill="#FAFBFC",
            ),
        ],
        w=228,
        h="fill_container",
        fill=CARD,
        gap=0,
    )

    topbar = V(
        [
            F(
                [
                    V(
                        [
                            T(title, size=16, weight="700", color=TEXT, lh=1.3),
                            T(subtitle, size=11, color=MUTED, lh=1.3),
                        ],
                        gap=3,
                    ),
                    H(
                        [
                            H(
                                [
                                    I("folder-kanban", 13, SUB),
                                    T("项目 · 电商数据平台", size=11.5, color=SUB, lh=1.2),
                                    I("chevron-down", 12, MUTED),
                                ],
                                gap=6,
                                padding=[6, 10],
                                radius=8,
                                fill="#F8FAFC",
                                stroke=BORDER,
                            ),
                            H(
                                [
                                    I("user-round", 13, SUB),
                                    T("张三（数据工程师）", size=11.5, color=SUB, lh=1.2),
                                    I("chevron-down", 12, MUTED),
                                ],
                                gap=6,
                                padding=[6, 10],
                                radius=8,
                                fill="#F8FAFC",
                                stroke=BORDER,
                            ),
                        ],
                        gap=8,
                    ),
                ],
                layout="horizontal",
                justify="space_between",
                align="center",
                w="fill_container",
                padding=[0, 22],
                h=70,
                fill=CARD,
            ),
            hr(),
        ],
        w="fill_container",
        gap=0,
    )

    main = V(
        [topbar, F([body], w="fill_container", h="fill_container", padding=18, clip=True)],
        w="fill_container",
        h="fill_container",
        fill=BG,
        gap=0,
    )

    board = H(
        [sidebar, main],
        w=BOARD_W,
        h=BOARD_H,
        x=x,
        y=y,
        gap=0,
        fill=BG,
        clip=True,
        nid=board_id,
        name=board_name,
    )
    board["layout"] = "horizontal"
    return board
