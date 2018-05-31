"""Code generation for native function bodies."""

from mypyc.common import REG_PREFIX, NATIVE_PREFIX
from mypyc.emit import Emitter
from mypyc.ops import (
    FuncIR, OpVisitor, Goto, Branch, Return, PrimitiveOp, Assign, LoadInt, LoadErrorValue, GetAttr,
    SetAttr, LoadStatic, TupleGet, Call, PyCall, PyGetAttr, IncRef, DecRef, Box, Cast, Unbox, Label,
    Register, RType, OP_BINARY, RTuple, PyMethodCall, PrimitiveOp2, EmitterInterface
)


def native_function_header(fn: FuncIR) -> str:
    args = []
    for arg in fn.args:
        args.append('{}{}{}'.format(arg.type.ctype_spaced(), REG_PREFIX, arg.name))

    return 'static {ret_type}{prefix}{name}({args})'.format(
        ret_type=fn.ret_type.ctype_spaced(),
        prefix=NATIVE_PREFIX,
        name=fn.cname,
        args=', '.join(args) or 'void')


def generate_native_function(fn: FuncIR, emitter: Emitter, source_path: str) -> None:
    declarations = Emitter(emitter.context, fn.env)
    body = Emitter(emitter.context, fn.env)
    visitor = FunctionEmitterVisitor(body, declarations, fn.name, source_path)

    declarations.emit_line('{} {{'.format(native_function_header(fn)))
    body.indent()

    for i in range(len(fn.args), fn.env.num_regs()):
        ctype = fn.env.types[i].ctype
        declarations.emit_line('{ctype} {prefix}{name};'.format(ctype=ctype,
                                                                prefix=REG_PREFIX,
                                                                name=fn.env.names[i]))

    for block in fn.blocks:
        body.emit_label(block.label)
        for op in block.ops:
            op.accept(visitor)

    body.emit_line('}')

    emitter.emit_from_emitter(declarations)
    emitter.emit_from_emitter(body)


class FunctionEmitterVisitor(OpVisitor[None], EmitterInterface):
    def __init__(self,
                 emitter: Emitter,
                 declarations: Emitter,
                 func_name: str,
                 source_path: str) -> None:
        self.emitter = emitter
        self.declarations = declarations
        self.env = self.emitter.env
        self.func_name = func_name
        self.source_path = source_path

    def temp_name(self) -> str:
        return self.emitter.temp_name()

    def visit_goto(self, op: Goto) -> None:
        self.emit_line('goto %s;' % self.label(op.label))

    BRANCH_OP_MAP = {
        Branch.INT_EQ: 'CPyTagged_IsEq',
        Branch.INT_NE: 'CPyTagged_IsNe',
        Branch.INT_LT: 'CPyTagged_IsLt',
        Branch.INT_LE: 'CPyTagged_IsLe',
        Branch.INT_GT: 'CPyTagged_IsGt',
        Branch.INT_GE: 'CPyTagged_IsGe',
    }

    def visit_branch(self, op: Branch) -> None:
        neg = '!' if op.negated else ''

        cond = ''
        if op.op == Branch.BOOL_EXPR:
            expr_result = self.reg(op.left) # right isn't used
            cond = '{}({})'.format(neg, expr_result)
        elif op.op == Branch.IS_NONE:
            compare = '!=' if op.negated else '=='
            cond = '{} {} Py_None'.format(self.reg(op.left), compare)
        elif op.op == Branch.IS_ERROR:
            typ = self.env.types[op.left]
            compare = '!=' if op.negated else '=='
            if isinstance(typ, RTuple):
                # TODO: What about empty tuple?
                item_type = typ.types[0]
                cond = '{}.f0 {} {}'.format(self.reg(op.left),
                                            compare,
                                            item_type.c_error_value())
            else:
                cond = '{} {} {}'.format(self.reg(op.left),
                                         compare,
                                         typ.c_error_value())
        else:
            left = self.reg(op.left)
            right = self.reg(op.right)
            fn = FunctionEmitterVisitor.BRANCH_OP_MAP[op.op]
            cond = '%s%s(%s, %s)' % (neg, fn, left, right)

        # For error checks, tell the compiler the branch is unlikely
        if op.traceback_entry is not None:
            cond = 'unlikely({})'.format(cond)

        self.emit_line('if ({}) {{'.format(cond))

        if op.traceback_entry is not None:
            self.emit_line('CPy_AddTraceback("%s", "%s", %d, _globals);' % (self.source_path,
                                                                            self.func_name,
                                                                            op.line))
        self.emit_lines(
            'goto %s;' % self.label(op.true),
            '} else',
            '    goto %s;' % self.label(op.false)
        )

    def visit_return(self, op: Return) -> None:
        typ = self.type(op.reg)
        regstr = self.reg(op.reg)
        self.emit_line('return %s;' % regstr)

    def visit_primitive_op2(self, op: PrimitiveOp2) -> None:
        op.desc.emit(self, op)

    def visit_primitive_op(self, op: PrimitiveOp) -> None:
        # N.B: PrimitiveOp has support for is_void ops that don't have
        # destinations, but none currently exist.
        # So that we can assert that op.desc isn't None, we can handle
        # is_void ops first.
        if op.desc.is_void:
            assert False, "No is_void ops implemented yet"

        assert op.dest is not None
        dest = self.reg(op.dest)

        if op.desc is PrimitiveOp.NONE:
            self.emit_lines(
                '{} = Py_None;'.format(dest),
                'Py_INCREF({});'.format(dest),
            )

        elif op.desc is PrimitiveOp.TRUE:
            self.emit_line('{} = 1;'.format(dest))

        elif op.desc is PrimitiveOp.FALSE:
            self.emit_line('{} = 0;'.format(dest))

        elif op.desc is PrimitiveOp.NEW_LIST:
            # TODO: This would be better split into multiple smaller ops.
            self.emit_line('%s = PyList_New(%d); ' % (dest, len(op.args)))
            for arg in op.args:
                self.emit_line('Py_INCREF(%s);' % self.reg(arg))
            self.emit_line('if (%s != NULL) {' % dest)
            for i, arg in enumerate(op.args):
                reg = self.reg(arg)
                self.emit_line('PyList_SET_ITEM(%s, %s, %s);' % (dest, i, reg))
            self.emit_line('}')

        elif op.desc is PrimitiveOp.NEW_TUPLE:
            tuple_type = self.env.types[op.dest]
            assert isinstance(tuple_type, RTuple)
            self.emitter.declare_tuple_struct(tuple_type)
            for i, arg in enumerate(op.args):
                self.emit_line('{}.f{} = {};'.format(dest, i, self.reg(arg)))
            self.emit_inc_ref(dest, tuple_type)

        elif op.desc is PrimitiveOp.NEW_DICT:
            self.emit_line('%s = PyDict_New();' % dest)

        else:
            assert False, 'Unexpected primitive op: %s' % (op.desc,)

    def visit_assign(self, op: Assign) -> None:
        dest = self.reg(op.dest)
        src = self.reg(op.src)
        self.emit_line('%s = %s;' % (dest, src))

    def visit_load_int(self, op: LoadInt) -> None:
        dest = self.reg(op.dest)
        self.emit_line('%s = %d;' % (dest, op.value * 2))

    def visit_load_error_value(self, op: LoadErrorValue) -> None:
        if isinstance(op.rtype, RTuple):
            values = [item.c_undefined_value() for item in op.rtype.types]
            tmp = self.temp_name()
            self.emit_line('%s %s = { %s };' % (op.rtype.ctype, tmp, ', '.join(values)))
            self.emit_line('%s = %s;' % (self.reg(op.dest), tmp))
        else:
            self.emit_line('%s = %s;' % (self.reg(op.dest),
                                         op.rtype.c_error_value()))

    def visit_get_attr(self, op: GetAttr) -> None:
        dest = self.reg(op.dest)
        obj = self.reg(op.obj)
        rtype = op.rtype
        self.emit_line('%s = CPY_GET_ATTR(%s, %d, %s, %s);' % (
            dest, obj,
            rtype.getter_index(op.attr),
            rtype.struct_name(),
            rtype.attr_type(op.attr).ctype))

    def visit_set_attr(self, op: SetAttr) -> None:
        dest = self.reg(op.dest)
        obj = self.reg(op.obj)
        src = self.reg(op.src)
        rtype = op.rtype
        # TODO: Track errors
        self.emit_line('%s = CPY_SET_ATTR(%s, %d, %s, %s, %s);' % (
            dest,
            obj,
            rtype.setter_index(op.attr),
            src,
            rtype.struct_name(),
            rtype.attr_type(op.attr).ctype))

    def visit_load_static(self, op: LoadStatic) -> None:
        dest = self.reg(op.dest)
        self.emit_line('%s = %s;' % (dest, op.identifier))

    def visit_py_get_attr(self, op: PyGetAttr) -> None:
        dest = self.reg(op.dest)
        left = self.reg(op.left)
        self.emit_line('{} = CPyObject_GetAttrString({}, "{}");'.format(dest, left, op.right))

    def visit_tuple_get(self, op: TupleGet) -> None:
        dest = self.reg(op.dest)
        src = self.reg(op.src)
        self.emit_line('{} = {}.f{};'.format(dest, src, op.index))
        self.emit_inc_ref(dest, op.target_type)

    def visit_call(self, op: Call) -> None:
        if op.dest is not None:
            dest = self.reg(op.dest) + ' = '
        else:
            dest = ''
        args = ', '.join(self.reg(arg) for arg in op.args)
        self.emit_line('%s%s%s(%s);' % (dest, NATIVE_PREFIX, op.fn, args))

    def visit_py_call(self, op: PyCall) -> None:
        if op.dest is not None:
            dest = self.reg(op.dest) + ' = '
        else:
            dest = ''

        function = self.reg(op.function)
        args = ', '.join(self.reg(arg) for arg in op.args)
        if args:
            args += ', '
        self.emit_line('{}PyObject_CallFunctionObjArgs({}, {}NULL);'.format(dest, function, args))

    def visit_py_method_call(self, op: PyMethodCall) -> None:
        if op.dest is not None:
            dest = self.reg(op.dest) + ' = '
        else:
            dest = ''

        obj = self.reg(op.obj)
        method = self.reg(op.method)
        args = ', '.join(self.reg(arg) for arg in op.args)
        if args:
            args += ', '
        self.emit_line('{}PyObject_CallMethodObjArgs({}, {}, {}NULL);'.format(
            dest, obj, method, args))

    def visit_inc_ref(self, op: IncRef) -> None:
        dest = self.reg(op.dest)
        self.emit_inc_ref(dest, op.target_type)

    def visit_dec_ref(self, op: DecRef) -> None:
        dest = self.reg(op.dest)
        self.emit_dec_ref(dest, op.target_type)

    def visit_box(self, op: Box) -> None:
        self.emitter.emit_box(self.reg(op.src), self.reg(op.dest), op.type)

    def visit_cast(self, op: Cast) -> None:
        self.emitter.emit_cast(self.reg(op.src), self.reg(op.dest), op.typ)

    def visit_unbox(self, op: Unbox) -> None:
        self.emitter.emit_unbox(self.reg(op.src), self.reg(op.dest), op.type)

    # Helpers

    def label(self, label: Label) -> str:
        return self.emitter.label(label)

    def reg(self, reg: Register) -> str:
        return self.emitter.reg(reg)

    def type(self, reg: Register) -> RType:
        return self.env.types[reg]

    def emit_line(self, line: str) -> None:
        self.emitter.emit_line(line)

    def emit_lines(self, *lines: str) -> None:
        self.emitter.emit_lines(*lines)

    def emit_inc_ref(self, dest: str, rtype: RType) -> None:
        self.emitter.emit_inc_ref(dest, rtype)

    def emit_dec_ref(self, dest: str, rtype: RType) -> None:
        self.emitter.emit_dec_ref(dest, rtype)

    def emit_declaration(self, line: str) -> None:
        self.declarations.emit_line(line)
