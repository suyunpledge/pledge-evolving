"""Finite real-number expressions for chat calculations and function plots."""
import ast
import math
import operator
import statistics

BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
          ast.Div: operator.truediv, ast.Mod: operator.mod, ast.Pow: math.pow}
FUNCTIONS = {name: getattr(math, name) for name in
             ('sin', 'cos', 'tan', 'asin', 'acos', 'atan', 'sqrt', 'log', 'log10', 'exp', 'floor', 'ceil')}
FUNCTIONS.update(abs=abs, round=round)
CONSTANTS = {'pi': math.pi, 'e': math.e, 'tau': math.tau}


def number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('需要有限的实数。')
    return float(value)


def samples(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 1000:
        raise ValueError('请提供 1 至 1000 个数值。')
    return [number(v) for v in value]


def expression(source):
    if not isinstance(source, str) or not 1 <= len(source.strip()) <= 256:
        raise ValueError('数学表达式长度应在 1 至 256 个字符之间。')
    try:
        root = ast.parse(source.strip().replace('^', '**'), mode='eval').body
    except SyntaxError:
        raise ValueError('无法解析数学表达式。') from None
    if sum(1 for _ in ast.walk(root)) > 96:
        raise ValueError('表达式过长，请拆分计算。')

    def build(node):
        if isinstance(node, ast.Constant):
            value = number(node.value)
            return lambda x: value
        if isinstance(node, ast.Name):
            if node.id == 'x':
                def variable(x):
                    if x is None:
                        raise ValueError('含 x 的表达式需要提供 x_values。')
                    return x
                return variable
            if node.id in CONSTANTS:
                return lambda x: CONSTANTS[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            child = build(node.operand)
            sign = 1 if isinstance(node.op, ast.UAdd) else -1
            return lambda x: sign * child(x)
        if isinstance(node, ast.BinOp) and type(node.op) in BINARY:
            left, right = build(node.left), build(node.right)
            operation = BINARY[type(node.op)]
            return lambda x: number(operation(left(x), right(x)))
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS
                and len(node.args) == 1 and not node.keywords):
            fn, child = FUNCTIONS[node.func.id], build(node.args[0])
            return lambda x: number(fn(child(x)))
        raise ValueError('仅支持数值、x、数学常量、四则运算、幂和已列出的数学函数。')

    return build(root)


def evaluate(args):
    fn = expression(args['expression'])
    if 'x_values' not in args:
        return {'value': fn(None)}
    xs = samples(args['x_values'])
    return {'x': xs, 'y': [fn(x) for x in xs]}


def summarize(args):
    values = samples(args['values'])
    return {'count': len(values), 'sum': number(math.fsum(values)), 'mean': statistics.mean(values),
            'median': statistics.median(values), 'min': min(values), 'max': max(values),
            'population_stdev': statistics.pstdev(values)}
