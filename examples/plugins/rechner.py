"""Sicherer Taschenrechner – LLMs verrechnen sich gern, dieses Werkzeug nicht."""
import ast
import math
import operator

from spike.plugins import tool

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
        ast.USub: operator.neg, ast.UAdd: operator.pos}
_NAMES = {k: getattr(math, k) for k in ("pi", "e", "tau", "sqrt", "sin", "cos", "tan", "log", "log10",
                                         "log2", "exp", "floor", "ceil", "factorial", "radians", "degrees")}
_NAMES.update(abs=abs, round=round, min=min, max=max)


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        a, b = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(b) > 10000:
            raise ValueError("Exponent zu groß")
        return _OPS[type(node.op)](a, b)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Name) and node.id in _NAMES:
        return _NAMES[node.id]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _NAMES:
        return _NAMES[node.func.id](*[_eval(a) for a in node.args])
    raise ValueError(f"Nicht erlaubt: {ast.dump(node)[:60]}")


@tool("Berechnet einen mathematischen Ausdruck exakt, z. B. '(17.5*3)**2 / sqrt(2)'.",
      params={"ausdruck": "Python-Mathe-Ausdruck; erlaubt: + - * / // % **, sqrt, sin, log, pi, …"},
      label="Rechner")
def rechner(ausdruck: str) -> str:
    return f"{ausdruck} = {_eval(ast.parse(ausdruck.replace('^', '**'), mode='eval'))}"
