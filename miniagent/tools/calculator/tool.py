from __future__ import annotations

import ast
import asyncio
import math
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from typing import Literal

import mpmath
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..models import ExecutionContext, ExecutionTraits, ToolExecutionError, ToolOutput, ToolSpec

class CalculatorInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expression: str
    precision: int = Field(default=15, ge=1, le=100)
    @field_validator("expression")
    @classmethod
    def expression_valid(cls, value: str) -> str:
        value=value.strip()
        if not value or len(value)>1024: raise ValueError("expression must contain 1..1024 characters")
        return value
class CalculatorMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    precision: int; exact: bool
class CalculatorData(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["integer","real"]; value: str
class CalculatorOutput(ToolOutput):
    metadata: CalculatorMetadata; data: CalculatorData

FUNCTIONS={"abs":abs,"sqrt":mpmath.sqrt,"exp":mpmath.exp,"ln":mpmath.log,"log":mpmath.log,"log10":mpmath.log10,"log2":lambda x:mpmath.log(x,2),"sin":mpmath.sin,"cos":mpmath.cos,"tan":mpmath.tan,"asin":mpmath.asin,"acos":mpmath.acos,"atan":mpmath.atan,"atan2":mpmath.atan2,"sinh":mpmath.sinh,"cosh":mpmath.cosh,"tanh":mpmath.tanh,"floor":mpmath.floor,"ceil":mpmath.ceil,"round":round,"min":min,"max":max,"degrees":mpmath.degrees,"radians":mpmath.radians}
CONSTANTS={"pi":mpmath.pi,"e":mpmath.e,"tau":2*mpmath.pi}

def _check(tree: ast.AST) -> None:
    nodes=list(ast.walk(tree))
    if len(nodes)>128: raise ValueError("expression exceeds AST resource budget")
    def depth(node, level=0):
        if level>32: raise ValueError("expression nesting is too deep")
        for child in ast.iter_child_nodes(node): depth(child,level+1)
    depth(tree)
    allowed=(ast.Expression,ast.Constant,ast.UnaryOp,ast.UAdd,ast.USub,ast.BinOp,ast.Add,ast.Sub,ast.Mult,ast.Div,ast.FloorDiv,ast.Mod,ast.Pow,ast.Call,ast.Name,ast.Load)
    for node in nodes:
        if not isinstance(node,allowed): raise ValueError("unsupported expression syntax")
        if isinstance(node,ast.Constant) and (not isinstance(node.value,(int,float)) or isinstance(node.value,bool)): raise ValueError("only numeric literals are allowed")
        if isinstance(node,ast.Name) and node.id not in FUNCTIONS and node.id not in CONSTANTS: raise ValueError("unknown name")
        if isinstance(node,ast.Call) and (not isinstance(node.func,ast.Name) or node.func.id not in FUNCTIONS or node.keywords): raise ValueError("unsupported function call")

def _evaluate(expr: str, precision: int) -> tuple[str,str,bool]:
    tree=ast.parse(expr,mode="eval"); _check(tree)
    with localcontext() as ctx, mpmath.workdps(precision+10):
        ctx.prec=precision+10
        def visit(node):
            if isinstance(node,ast.Expression): return visit(node.body)
            if isinstance(node,ast.Constant): return int(node.value) if isinstance(node.value,int) else mpmath.mpf(str(node.value))
            if isinstance(node,ast.Name): return CONSTANTS[node.id]
            if isinstance(node,ast.UnaryOp): return +visit(node.operand) if isinstance(node,ast.UAdd) else -visit(node.operand)
            if isinstance(node,ast.BinOp):
                a,b=visit(node.left),visit(node.right)
                if isinstance(node.op,ast.Add): return a+b
                if isinstance(node.op,ast.Sub): return a-b
                if isinstance(node.op,ast.Mult): return a*b
                if isinstance(node.op,ast.Div): return a/b
                if isinstance(node.op,ast.FloorDiv): return mpmath.floor(a/b)
                if isinstance(node.op,ast.Mod): return a-b*mpmath.floor(a/b)
                if isinstance(node.op,ast.Pow):
                    if abs(b)>10000: raise ValueError("exponent exceeds resource budget")
                    return a**b
            if isinstance(node,ast.Call): return FUNCTIONS[node.func.id](*(visit(arg) for arg in node.args))
            raise ValueError("unsupported expression syntax")
        value=visit(tree)
    if isinstance(value,int) or (isinstance(value,mpmath.mpf) and value == mpmath.floor(value) and all(isinstance(n,ast.Constant) for n in ast.walk(tree) if isinstance(n,ast.Constant))):
        text=str(int(value)); return text,"integer",True
    if not mpmath.isfinite(value): raise ArithmeticError("result is not finite")
    text=mpmath.nstr(value,n=precision)
    if text in ("-0","0.0"): text="0"
    return text,"real",False

def resolve_targets(args, workspace_root): return ()
def classify(args, targets): return ExecutionTraits(concurrency_safe=True)
async def handler(args: BaseModel, context: ExecutionContext) -> CalculatorOutput:
    assert isinstance(args,CalculatorInput)
    try: value,kind,exact=await asyncio.to_thread(_evaluate,args.expression,args.precision)
    except ZeroDivisionError as exc: raise ToolExecutionError("Division by zero.") from exc
    except (ValueError, ArithmeticError) as exc: raise ToolExecutionError(str(exc)) from exc
    return CalculatorOutput(content=value,metadata=CalculatorMetadata(precision=args.precision,exact=exact),data=CalculatorData(kind=kind,value=value))

calculator_spec=SPEC=ToolSpec(name="calculator",description="Evaluate a restricted numeric expression with configurable decimal precision.",input_model=CalculatorInput,output_model=CalculatorOutput,handler=handler,prompt_ref="miniagent.tools.calculator.prompt:PROMPT",resolve_targets=resolve_targets,classify=classify,timeout_seconds=5.0)
