"""把 .pen 渲染成可直接在浏览器打开的 HTML 预览（无需安装 pen.dev）。

用法：python render_preview.py
输出：prototype-preview.html

说明：icon 节点渲染为「图标槽位」占位（线框风格），并保留 data-icon 便于后续替换为真实图标。
"""

import html
import json
from pathlib import Path

HERE = Path(__file__).parent


# ---------------------------------------------------------------- 样式计算
def px(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return f"{v:g}px"
    return None


def padding_css(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return f"{v:g}px"
    if isinstance(v, list):
        vals = [f"{x:g}px" for x in v]
        return " ".join(vals)
    return None


def style_of(n, parent_layout):
    s = {}
    t = n.get("type")
    layout = n.get("layout", "none")

    if t == "frame":
        if layout in ("horizontal", "vertical"):
            s["display"] = "flex"
            s["flex-direction"] = "row" if layout == "horizontal" else "column"
        else:
            s["position"] = "relative"
    elif t == "text":
        s["white-space"] = "pre-wrap" if n.get("textGrowth", "auto") != "auto" else "pre"
        s["overflow-wrap"] = "break-word"
    elif t in ("rectangle", "ellipse"):
        s["flex"] = "0 0 auto"

    # 尺寸
    w, h = n.get("width"), n.get("height")
    if isinstance(w, (int, float)):
        s["width"] = px(w)
        if layout in ("horizontal", "vertical"):
            s["flex"] = "0 0 auto"
    elif w == "fill_container":
        if parent_layout == "horizontal":
            s["flex"] = "1 1 0"
            s["min-width"] = "0"
        else:
            s["width"] = "100%"
    elif w == "fit_content":
        s["width"] = "fit-content"

    if isinstance(h, (int, float)):
        s["height"] = px(h)
    elif h == "fill_container":
        if parent_layout == "vertical":
            s["flex"] = "1 1 0"
            s["min-height"] = "0"
        else:
            s["height"] = "100%"
    elif h == "fit_content":
        s["height"] = "fit-content"

    # 外观
    fill = n.get("fill")
    if t == "text":
        if fill:
            s["color"] = fill
    elif t == "icon":
        pass
    else:
        if fill:
            s["background"] = fill

    if n.get("stroke"):
        s["border"] = f"1px solid {n['stroke']}"
        s["box-sizing"] = "border-box"

    if n.get("cornerRadius"):
        cr = n["cornerRadius"]
        if isinstance(cr, list):
            s["border-radius"] = " ".join(f"{x:g}px" for x in cr)
        else:
            s["border-radius"] = px(cr)

    if t == "ellipse":
        s["border-radius"] = "50%"

    pad = padding_css(n.get("padding"))
    if pad:
        s["padding"] = pad
        s["box-sizing"] = "border-box"

    if n.get("gap"):
        s["gap"] = px(n["gap"])

    if n.get("justifyContent"):
        s["justify-content"] = n["justifyContent"].replace("_", "-")
    if n.get("alignItems"):
        s["align-items"] = n["alignItems"]

    if n.get("clip"):
        s["overflow"] = "hidden"

    if t == "text":
        if n.get("fontSize"):
            s["font-size"] = px(n["fontSize"])
        if n.get("fontWeight"):
            s["font-weight"] = n["fontWeight"]
        if n.get("fontFamily"):
            s["font-family"] = n["fontFamily"]
        if n.get("lineHeight"):
            s["line-height"] = f"{n['lineHeight']:g}"  # CSS 与 pen 同为倍数语义
        if n.get("textAlign"):
            s["text-align"] = n["textAlign"]
        s["flex-shrink"] = "0"
        if s.get("flex") is None and "width" in s:
            s["flex"] = "0 0 auto"

    if t in ("rectangle", "ellipse") and "flex" not in s:
        s["flex"] = "0 0 auto"

    return ";".join(f"{k}:{v}" for k, v in s.items())


# ---------------------------------------------------------------- 渲染
def render(n, parent_layout="vertical"):
    t = n.get("type")

    if t == "icon":
        size = n.get("width", 16)
        color = n.get("fill", "#98A2B3")
        name = html.escape(str(n.get("icon", "")))
        st = (
            f"width:{size:g}px;height:{size:g}px;flex:0 0 auto;"
            f"border:1px solid {color};border-radius:3px;opacity:.75;box-sizing:border-box"
        )
        return f'<div class="icon" data-icon="{name}" style="{st}"></div>'

    st = style_of(n, parent_layout)
    style_attr = f' style="{st}"' if st else ""

    if t == "text":
        content = html.escape(str(n.get("content", "")))
        return f'<div class="txt"{style_attr}>{content}</div>'

    inner = ""
    layout = n.get("layout", "none")
    kids = n.get("children") or []
    if kids:
        inner = "".join(render(c, layout) for c in kids)

    cls = "node"
    if t == "frame":
        cls = "frame"
    return f'<div class="{cls}"{style_attr}>{inner}</div>'


def build_html(doc, scale_init=0.42):
    boards = doc["children"]
    labels = []
    board_html = []
    for b in boards:
        name = html.escape(str(b.get("name") or b.get("id")))
        x = b.get("x", 0)
        y = b.get("y", 0)
        labels.append(f'<div class="blabel" style="left:{x}px;top:{y - 34}px">{name}</div>')
        b2 = dict(b)
        b2.pop("name", None)
        # 画板必须显式 left/top —— 否则 absolute 元素会按静态位置堆叠在同一处
        frag = render(b2, "vertical").replace('class="frame"', 'class="frame board"', 1)
        frag = frag.replace(
            '<div class="frame board" style="',
            f'<div class="frame board" style="left:{x}px;top:{y}px;',
            1,
        )
        board_html.append(frag)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>生产级 ETL-Agent 数据集成平台 · 原型预览</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: #E7EBF1;
    font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif;
    color: #111827;
  }}
  .toolbar {{
    position: fixed; z-index: 99; top: 0; left: 0; right: 0; height: 46px;
    background: #FFFFFFEE; backdrop-filter: blur(6px);
    border-bottom: 1px solid #D8DFE8;
    display: flex; align-items: center; gap: 12px; padding: 0 16px;
    font-size: 13px;
  }}
  .toolbar b {{ font-size: 13.5px; }}
  .toolbar .meta {{ color: #6B7280; font-size: 12px; }}
  .toolbar .sp {{ flex: 1; }}
  .toolbar button {{
    font: inherit; font-size: 12px; padding: 5px 11px; border-radius: 6px;
    border: 1px solid #CBD5E1; background: #fff; cursor: pointer; color: #374151;
  }}
  .toolbar button:hover {{ border-color: #2563EB; color: #2563EB; }}
  .toolbar button.on {{ background: #2563EB; border-color: #2563EB; color: #fff; }}
  .canvas {{
    position: relative; width: 4860px; height: 2050px;
    transform-origin: 0 0; margin: 84px 0 0 0;
  }}
  .board {{
    position: absolute;
    box-shadow: 0 2px 10px rgba(15,23,42,.10);
    border: 1px solid #D3DAE4;
    border-radius: 10px;
    overflow: hidden;
    background: #F4F6F9;
  }}
  .blabel {{
    position: absolute; z-index: 5;
    font-size: 15px; font-weight: 700; color: #334155;
    letter-spacing: .3px;
  }}
  .node, .frame, .txt {{ min-width: 0; }}
  .icon {{ display: inline-block; }}
</style>
</head>
<body>
<div class="toolbar">
  <b>生产级 ETL-Agent 数据集成平台 · 原型（5 个页面）</b>
  <span class="meta">依据 PRD v1.2 §7 · SPEC v1.1</span>
  <span class="sp"></span>
  <button data-s="0.25">25%</button>
  <button data-s="0.42" class="on">42%</button>
  <button data-s="0.65">65%</button>
  <button data-s="1">100%</button>
</div>
<div class="canvas" id="canvas">
{"".join(labels)}
{"".join(board_html)}
</div>
<script>
  var canvas = document.getElementById('canvas');
  function apply(s) {{
    canvas.style.transform = 'scale(' + s + ')';
    document.querySelectorAll('.toolbar button').forEach(function (b) {{
      b.classList.toggle('on', parseFloat(b.dataset.s) === s);
    }});
  }}
  document.querySelectorAll('.toolbar button').forEach(function (b) {{
    b.addEventListener('click', function () {{ apply(parseFloat(b.dataset.s)); }});
  }});
  apply({scale_init});
</script>
</body>
</html>
"""


def main():
    doc = json.loads((HERE / "prototype.pen").read_text(encoding="utf-8"))
    out = HERE / "prototype-preview.html"
    out.write_text(build_html(doc), encoding="utf-8")
    print(f"[OK] 已生成 {out}")


if __name__ == "__main__":
    main()
