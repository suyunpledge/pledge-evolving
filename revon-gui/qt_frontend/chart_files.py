"""PNG charts rendered offscreen without starting a second GUI event loop."""
import math
import threading

from .math_tools import expression, number, samples
from .office_files import items, text

_RENDER_LOCK = threading.Lock()


def write_chart(args, dest):
    kind = args['kind']
    if kind == 'function':
        fn = expression(args['expression'])
        low, high = number(args.get('x_min', -10)), number(args.get('x_max', 10))
        if low >= high or not math.isfinite(high - low):
            raise ValueError('x_min 必须小于 x_max，且区间长度必须有限。')
        xs = [low + (i / 500) * (high - low) for i in range(501)]
        ys = []
        for x in xs:
            try:
                ys.append(fn(x))
            except (ArithmeticError, ValueError):
                ys.append(math.nan)  # Break the curve at samples outside the domain.
        if not any(map(math.isfinite, ys)):
            raise ValueError('函数在所选区间没有可绘制的实数值。')
    elif kind in ('line', 'bar', 'scatter'):
        ys = samples(args['y'])
        xs = args.get('x', list(range(1, len(ys) + 1)))
        items(xs, 1000, '横坐标')
        if all(isinstance(v, str) for v in xs):
            if kind == 'scatter':
                raise ValueError('散点图需要数值横坐标。')
            xs = [text(v, 80) for v in xs]
        else:
            xs = samples(xs)
        if len(xs) != len(ys):
            raise ValueError('横纵坐标数量必须一致。')
    else:
        raise ValueError('未知图表类型。')

    from matplotlib import font_manager, rc_context
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    with _RENDER_LOCK:
        installed = font_manager.get_font_names()
        fonts = [f for f in ('Microsoft YaHei', 'Noto Sans CJK SC', 'PingFang SC', 'DejaVu Sans') if f in installed]
        with rc_context({'font.family': fonts, 'text.usetex': False, 'text.parse_math': False,
                         'axes.unicode_minus': False}):
            fig = Figure(figsize=(9, 5.2), dpi=140, layout='constrained')
            canvas = FigureCanvasAgg(fig)
            ax = fig.subplots()
            if isinstance(xs[0], str):
                labels, xs = xs, list(range(len(xs)))
                step = max(1, math.ceil(len(xs) / 14))
                ax.set_xticks(xs[::step], labels[::step], rotation=25 if len(labels) > 6 else 0)
            if kind == 'bar':
                ax.bar(xs, ys, color='#3476a3')
            elif kind == 'scatter':
                ax.scatter(xs, ys, s=27, color='#3476a3')
            else:
                ax.plot(xs, ys, color='#286b97', linewidth=2, marker='o' if len(xs) <= 16 else None)
            ax.set_title(text(args['title'], 120), loc='left', pad=16)
            ax.set_xlabel(text(args['x_label'], 100) if 'x_label' in args else '')
            ax.set_ylabel(text(args['y_label'], 100) if 'y_label' in args else '')
            ax.set_axisbelow(True)
            ax.grid(axis='y', alpha=.25)
            ax.spines[['top', 'right']].set_visible(False)
            canvas.print_png(dest)
