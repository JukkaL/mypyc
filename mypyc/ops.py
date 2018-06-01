"""Representation of low-level opcodes for compiler intermediate representation (IR).

Opcodes operate on abstract registers in a register machine. Each
register has a type and a name, specified in an environment. A register
can hold various things:

- local variables
- intermediate values of expressions
- condition flags (true/false)
- literals (integer literals, True, False, etc.)
"""

from abc import abstractmethod, abstractproperty
import re
from typing import (
    List, Dict, Generic, TypeVar, Optional, Any, NamedTuple, Tuple, NewType, Callable, Union,
    Iterable,
)

from mypy.nodes import Var


T = TypeVar('T')

CRegister = NewType('CRegister', int)
Label = NewType('Label', int)
Register = Union[CRegister, 'Op']


# Unfortunately we have visitors which are statement-like rather than expression-like.
# It doesn't make sense to have the visitor return Optional[Register] because every
# method either always returns no register or returns a register.
#
# Eventually we may want to separate expression visitors and statement-like visitors at
# the type level but until then returning INVALID_REGISTER from a statement-like visitor
# seems acceptable.
INVALID_REGISTER = CRegister(-99999)


# Similarly this is used for placeholder labels which aren't assigned yet (but will
# be eventually. Its kind of a hack.
INVALID_LABEL = Label(-88888)


def c_module_name(module_name: str) -> str:
    return 'module_{}'.format(module_name.replace('.', '__dot__'))


def short_name(name: str) -> str:
    if name.startswith('builtins.'):
        return name[9:]
    return name


class RType:
    """Abstract base class for runtime types (erased, only concrete; no generics)."""

    name = None  # type: str
    ctype = None  # type: str
    is_unboxed = False
    c_undefined = None  # type: str
    is_refcounted = True  # If unboxed: does the unboxed version use reference counting?

    @abstractmethod
    def accept(self, visitor: 'RTypeVisitor[T]') -> T:
        raise NotImplementedError

    @abstractmethod
    def c_undefined_value(self) -> str:
        raise NotImplementedError

    def ctype_spaced(self) -> str:
        """Adds a space after ctype for non-pointers."""
        if self.ctype[-1] == '*':
            return self.ctype
        else:
            return self.ctype + ' '

    def c_error_value(self) -> str:
        return self.c_undefined_value()

    def short_name(self) -> str:
        return short_name(self.name)

    def __str__(self) -> str:
        return short_name(self.name)

    def __repr__(self) -> str:
        return '<%s>' % self.__class__.__name__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, RType) and other.name == self.name

    def __hash__(self) -> int:
        return hash(self.name)


class RPrimitive(RType):
    """Primitive type such as 'object' or 'int'.

    These often have custom ops associated with them.
    """

    def __init__(self,
                 name: str,
                 is_unboxed: bool,
                 is_refcounted: bool,
                 ctype: str = 'PyObject *') -> None:
        self.name = name
        self.is_unboxed = is_unboxed
        self.ctype = ctype
        self.is_refcounted = is_refcounted
        if ctype == 'CPyTagged':
            self.c_undefined = 'CPY_INT_TAG'
        elif ctype == 'PyObject *':
            self.c_undefined = 'NULL'
        elif ctype == 'char':
            self.c_undefined = '2'
        else:
            assert False, 'Uncognized ctype: %r' % ctype

    def c_undefined_value(self) -> str:
        return self.c_undefined

    def accept(self, visitor: 'RTypeVisitor[T]') -> T:
        return visitor.visit_rprimitive(self)

    def __repr__(self) -> str:
        return '<RPrimitive %s>'% self.name


# Used to represent arbitrary objects and dynamically typed values
object_rprimitive = RPrimitive('builtins.object', is_unboxed=False, is_refcounted=True)

int_rprimitive = RPrimitive('builtins.int', is_unboxed=True, is_refcounted=True, ctype='CPyTagged')

bool_rprimitive = RPrimitive('builtins.bool', is_unboxed=True, is_refcounted=False, ctype='char')

none_rprimitive = RPrimitive('builtins.None', is_unboxed=False, is_refcounted=True)

list_rprimitive = RPrimitive('builtins.list', is_unboxed=False, is_refcounted=True)

dict_rprimitive = RPrimitive('builtins.dict', is_unboxed=False, is_refcounted=True)

# At the C layer, str is refered to as unicode (PyUnicode)
str_rprimitive = RPrimitive('builtins.str', is_unboxed=False, is_refcounted=True)

# Tuple of an arbitrary length (corresponds to Tuple[t, ...], with explicit '...')
tuple_rprimitive = RPrimitive('builtins.tuple', is_unboxed=False, is_refcounted=True)


def is_int_rprimitive(rtype: RType) -> bool:
    return isinstance(rtype, RPrimitive) and rtype.name == 'builtins.int'


def is_bool_rprimitive(rtype: RType) -> bool:
    return isinstance(rtype, RPrimitive) and rtype.name == 'builtins.bool'


def is_object_rprimitive(rtype: RType) -> bool:
    return isinstance(rtype, RPrimitive) and rtype.name == 'builtins.object'


def is_none_rprimitive(rtype: RType) -> bool:
    return isinstance(rtype, RPrimitive) and rtype.name == 'builtins.None'


def is_list_rprimitive(rtype: RType) -> bool:
    return isinstance(rtype, RPrimitive) and rtype.name == 'builtins.list'


def is_dict_rprimitive(rtype: RType) -> bool:
    return isinstance(rtype, RPrimitive) and rtype.name == 'builtins.dict'


def is_str_rprimitive(rtype: RType) -> bool:
    return isinstance(rtype, RPrimitive) and rtype.name == 'builtins.str'


def is_tuple_rprimitive(rtype: RType) -> bool:
    return isinstance(rtype, RPrimitive) and rtype.name == 'builtins.tuple'


class RTuple(RType):
    """Fixed-length tuple."""

    is_unboxed = True

    def __init__(self, types: List[RType]) -> None:
        self.name = 'tuple'
        self.types = tuple(types)
        self.ctype = 'struct {}'.format(self.struct_name())
        self.is_refcounted = any(t.is_refcounted for t in self.types)

    def accept(self, visitor: 'RTypeVisitor[T]') -> T:
        return visitor.visit_rtuple(self)

    def c_undefined_value(self) -> str:
        # This doesn't work since this is expected to return a C expression, but
        # defining an undefined tuple requires declaring a temp variable, such as:
        #
        #    struct foo _tmp = { <item0-undefined>, <item1-undefined>, ... };
        assert False, "Tuple undefined value can't be represented as a C expression"

    @property
    def unique_id(self) -> str:
        """Generate a unique id which is used in naming corresponding C identifiers.

        This is necessary since C does not have anonymous structural type equivalence
        in the same way python can just assign a Tuple[int, bool] to a Tuple[int, bool].

        TODO: a better unique id. (#38)
        """
        return str(abs(hash(self)))[0:15]

    def struct_name(self) -> str:
        # max c length is 31 charas, this should be enough entropy to be unique.
        return 'tuple_def_' + self.unique_id

    def __str__(self) -> str:
        return 'tuple[%s]' % ', '.join(str(typ) for typ in self.types)

    def __repr__(self) -> str:
        return '<RTuple %s>' % ', '.join(repr(typ) for typ in self.types)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, RTuple) and self.types == other.types

    def __hash__(self) -> int:
        return hash((self.name, self.types))

    def get_c_declaration(self) -> List[str]:
        result = ['struct {} {{'.format(self.struct_name())]
        i = 0
        for typ in self.types:
            result.append('    {}f{};'.format(typ.ctype_spaced(), i))
            i += 1
        result.append('};')
        result.append('')

        return result


class RInstance(RType):
    """Instance of user-defined class (compiled to C extension class)."""

    is_unboxed = False

    def __init__(self, class_ir: 'ClassIR') -> None:
        self.name = class_ir.name
        self.class_ir = class_ir
        self.ctype = 'PyObject *'

    def accept(self, visitor: 'RTypeVisitor[T]') -> T:
        return visitor.visit_rinstance(self)

    def c_undefined_value(self) -> str:
        return 'NULL'

    def struct_name(self) -> str:
        return self.class_ir.struct_name()

    def getter_index(self, name: str) -> int:
        for i, (attr, _) in enumerate(self.class_ir.attributes):
            if attr == name:
                return i * 2
        assert False, '%r has no attribute %r' % (self.name, name)

    def setter_index(self, name: str) -> int:
        return self.getter_index(name) + 1

    def method_index(self, name: str) -> int:
        base = len(self.class_ir.attributes) * 2
        for i, fn in enumerate(self.class_ir.methods):
            if fn.name == name:
                return base + i
        assert False, '%r has no attribute %r' % (self.name, name)

    def attr_type(self, name: str) -> RType:
        for i, (attr, rtype) in enumerate(self.class_ir.attributes):
            if attr == name:
                return rtype
        assert False, '%r has no attribute %r' % (self.name, name)

    def __repr__(self) -> str:
        return '<RInstance %s>' % self.name


class ROptional(RType):
    """Optional[x]"""

    is_unboxed = False

    def __init__(self, value_type: RType) -> None:
        self.name = 'optional'
        self.value_type = value_type
        self.ctype = 'PyObject *'

    def accept(self, visitor: 'RTypeVisitor[T]') -> T:
        return visitor.visit_roptional(self)

    def c_undefined_value(self) -> str:
        return 'NULL'

    def __repr__(self) -> str:
        return '<ROptional %s>' % self.value_type

    def __str__(self) -> str:
        return 'optional[%s]' % self.value_type

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ROptional) and other.value_type == self.value_type

    def __hash__(self) -> int:
        return hash(('optional', self.value_type))


class Environment:
    """Keep track of names and types of registers."""

    def __init__(self) -> None:
        self.indexes = {}  # type: Dict[Register, int]
        self.names = {}  # type: Dict[Register, str]
        self.types = {}  # type: Dict[Register, RType]
        self.symtable = {}  # type: Dict[Var, Register]
        self.temp_index = 0

    def regs(self) -> Iterable[Register]:
        return self.names.keys()

    def add(self, reg: Register, name: str, typ: RType) -> None:
        self.indexes[reg] = len(self.names)
        self.names[reg] = name
        self.types[reg] = typ

    def add_local(self, var: Var, typ: RType) -> Register:
        assert isinstance(var, Var)
        reg = CRegister(len(self.names))

        self.symtable[var] = reg
        self.add(reg, var.name(), typ)
        return reg

    def lookup(self, var: Var) -> Register:
        return self.symtable[var]

    def add_temp(self, typ: RType) -> Register:
        assert isinstance(typ, RType)
        reg = CRegister(len(self.names))
        self.add(reg, 'r%d' % self.temp_index, typ)
        self.temp_index += 1
        return reg

    def add_op(self, reg: 'RegisterOp') -> None:
        if reg.type is None:
            return
        self.add(reg, 'r%d' % self.temp_index, reg.type)
        self.temp_index += 1

    def format(self, fmt: str, *args: Any) -> str:
        result = []
        i = 0
        arglist = list(args)
        while i < len(fmt):
            n = fmt.find('%', i)
            if n < 0:
                n = len(fmt)
            result.append(fmt[i:n])
            if n < len(fmt):
                typespec = fmt[n + 1]
                arg = arglist.pop(0)
                if typespec == 'r':
                    result.append(self.names[arg])
                elif typespec == 'd':
                    result.append('%d' % arg)
                elif typespec == 'l':
                    result.append('L%d' % arg)
                elif typespec == 's':
                    result.append(str(arg))
                else:
                    raise ValueError('Invalid format sequence %{}'.format(typespec))
                i = n + 2
            else:
                i = n
        return ''.join(result)

    def to_lines(self) -> List[str]:
        result = []
        i = 0
        names = [self.names[k] for k in self.regs()]
        types = [self.types[k] for k in self.regs()]

        n = len(names)
        while i < n:
            i0 = i
            group = [names[i0]]
            while i + 1 < n and types[i + 1] == types[i0]:
                i += 1
                group.append(names[i])
            i += 1
            result.append('%s :: %s' % (', '.join(group), types[i0]))
        return result


ERR_NEVER = 0  # Never generates an exception
ERR_MAGIC = 1  # Generates magic value (c_error_value) based on target RType on exception
ERR_FALSE = 2  # Generates false (bool) on exception


class Op:
    # Source line number
    line = -1

    def __init__(self, line: int) -> None:
        self.line = line

    def can_raise(self) -> bool:
        # Override this is if Op may raise an exception. Note that currently the fact that
        # only RegisterOps may raise an exception in hard coded in some places.
        return False

    @abstractmethod
    def to_str(self, env: Environment) -> str:
        raise NotImplementedError

    @abstractmethod
    def accept(self, visitor: 'OpVisitor[T]') -> T:
        pass


class Goto(Op):
    """Unconditional jump."""

    error_kind = ERR_NEVER

    def __init__(self, label: Label, line: int = -1) -> None:
        super().__init__(line)
        self.label = label

    def __repr__(self) -> str:
        return '<Goto %d>' % self.label

    def to_str(self, env: Environment) -> str:
        return env.format('goto %l', self.label)

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_goto(self)


class Branch(Op):
    """if [not] r1 op r2 goto 1 else goto 2"""

    # Branch ops must *not* raise an exception. If a comparison, for example, can raise an
    # exception, it needs to split into two opcodes and only the first one may fail.
    error_kind = ERR_NEVER

    INT_EQ = 10
    INT_NE = 11
    INT_LT = 12
    INT_LE = 13
    INT_GT = 14
    INT_GE = 15

    # Unlike the above, these are unary operations so they only uses the "left" register
    # ("right" should be INVALID_REGISTER).
    BOOL_EXPR = 100
    IS_NONE = 101
    IS_ERROR = 102  # Check for magic c_error_value (works for arbitary types)

    op_names = {
        INT_EQ:  ('==', 'int'),
        INT_NE:  ('!=', 'int'),
        INT_LT:  ('<', 'int'),
        INT_LE:  ('<=', 'int'),
        INT_GT:  ('>', 'int'),
        INT_GE:  ('>=', 'int'),
    }

    unary_op_names = {
        BOOL_EXPR: ('%r', 'bool'),
        IS_NONE: ('%r is None', 'object'),
        IS_ERROR: ('is_error(%r)', ''),
    }

    def __init__(self, left: Register, right: Register, true_label: Label,
                 false_label: Label, op: int, line: int = -1) -> None:
        super().__init__(line)
        self.left = left
        self.right = right
        self.true = true_label
        self.false = false_label
        self.op = op
        self.negated = False
        # If not None, the true label should generate a traceback entry (func name, line number)
        self.traceback_entry = None  # type: Optional[Tuple[str, int]]

    def sources(self) -> List[Register]:
        if self.right != INVALID_REGISTER:
            return [self.left, self.right]
        else:
            return [self.left]

    def to_str(self, env: Environment) -> str:
        # Right not used for BOOL_EXPR
        if self.op in self.op_names:
            if self.negated:
                fmt = 'not %r {} %r'
            else:
                fmt = '%r {} %r'
            op, typ = self.op_names[self.op]
            fmt = fmt.format(op)
        else:
            fmt, typ = self.unary_op_names[self.op]
            if self.negated:
                fmt = 'not {}'.format(fmt)

        cond = env.format(fmt, self.left, self.right)
        tb = ''
        if self.traceback_entry:
            tb = ' (error at %s:%d)' % self.traceback_entry
        fmt = 'if {} goto %l{} else goto %l'.format(cond, tb)
        if typ:
             fmt += ' :: {}'.format(typ)
        return env.format(fmt, self.true, self.false)

    def invert(self) -> None:
        self.true, self.false = self.false, self.true
        self.negated = not self.negated

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_branch(self)


class Return(Op):
    error_kind = ERR_NEVER

    def __init__(self, reg: Register, line: int = -1) -> None:
        super().__init__(line)
        self.reg = reg

    def to_str(self, env: Environment) -> str:
        return env.format('return %r', self.reg)

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_return(self)


class Unreachable(Op):
    """Added to the end of non-None returning functions.

    Mypy statically guarantees that the end of the function is not unreachable
    if there is not a return statement.

    This prevents the block formatter from being confused due to lack of a leave
    and also leaves a nifty note in the IR. It is not generally processed by visitors.
    """

    error_kind = ERR_NEVER

    def __init__(self, line: int = -1) -> None:
        super().__init__(line)

    def to_str(self, env: Environment) -> str:
        return "unreachable"

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_unreachable(self)


class RegisterOp(Op):
    """An operation that can be written as r1 = f(r2, ..., rn).

    Takes some registers, performs an operation and generates an output.
    The output register can be None for no output.
    """

    error_kind = -1  # Can this raise exception and how is it signalled; one of ERR_*

    no_reg = False
    _type = None  # type: Optional[RType]

    def __init__(self, dest: Optional[Register], line: int) -> None:
        super().__init__(line)
        assert dest != INVALID_REGISTER
        assert self.error_kind != -1, 'error_kind not defined'
        self._dest = dest

    # These are read-only property so that subclasses can override them
    # without the Optional.
    @property
    def dest(self) -> Optional[Register]:
        return self._dest
    @property
    def type(self) -> Optional[RType]:
        return self._type

    @abstractmethod
    def sources(self) -> List[Register]:
        pass

    def can_raise(self) -> bool:
        return self.error_kind != ERR_NEVER

    def unique_sources(self) -> List[Register]:
        result = []  # type: List[Register]
        for reg in self.sources():
            if reg not in result:
                result.append(reg)
        return result


class StrictRegisterOp(RegisterOp):
    """An operation that can be written as r1 = f(r2, ..., rn), where r1 must exist.

    Like RegisterOp but without the option of r1 being None.
    """

    def __init__(self, dest: Register, line: int) -> None:
        super().__init__(dest, line)

    # We could do this soundly without any checks by duplicating
    # the fields, but that is kind of silly...
    @property
    def dest(self) -> Register:
        assert self._dest is not None
        return self._dest
    @property
    def type(self) -> RType:
        assert self._type is not None
        return self._type


class IncRef(StrictRegisterOp):
    """inc_ref r"""

    error_kind = ERR_NEVER

    def __init__(self, dest: Register, typ: RType, line: int = -1) -> None:
        assert typ.is_refcounted
        super().__init__(dest, line)
        self.target_type = typ

    def to_str(self, env: Environment) -> str:
        s = env.format('inc_ref %r', self.dest)
        if is_bool_rprimitive(self.target_type) or is_int_rprimitive(self.target_type):
            s += ' :: {}'.format(short_name(self.target_type.name))
        return s

    def sources(self) -> List[Register]:
        return [self.dest]

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_inc_ref(self)


class DecRef(StrictRegisterOp):
    """dec_ref r"""

    error_kind = ERR_NEVER

    def __init__(self, dest: Register, typ: RType, line: int = -1) -> None:
        assert typ.is_refcounted
        super().__init__(dest, line)
        self.target_type = typ

    def __repr__(self) -> str:
        return '<DecRef %r>' % self.dest

    def to_str(self, env: Environment) -> str:
        s = env.format('dec_ref %r', self.dest)
        if is_bool_rprimitive(self.target_type) or is_int_rprimitive(self.target_type):
            s += ' :: {}'.format(short_name(self.target_type.name))
        return s

    def sources(self) -> List[Register]:
        return [self.dest]

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_dec_ref(self)


class Call(RegisterOp):
    """Native call f(arg, ...)

    The call target can be a module-level function or a class.
    """

    error_kind = ERR_MAGIC

    def __init__(self, dest: Optional[Register], fn: str, args: List[Register], line: int) -> None:
        super().__init__(dest, line)
        self.fn = fn
        self.args = args

    def to_str(self, env: Environment) -> str:
        args = ', '.join(env.format('%r', arg) for arg in self.args)
        s = '%s(%s)' % (self.fn, args)
        if self.dest is not None:
            s = env.format('%r = ', self.dest) + s
        return s

    def sources(self) -> List[Register]:
        return self.args[:]

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_call(self)


class MethodCall(RegisterOp):
    """Native method call obj.m(arg, ...) """

    error_kind = ERR_MAGIC

    def __init__(self,
                 dest: Optional[Register],
                 obj: Register,
                 method: str,
                 args: List[Register],
                 receiver_type: RInstance,
                 line: int = -1) -> None:
        super().__init__(dest, line)
        self.obj = obj
        self.method = method
        self.args = args
        self.receiver_type = receiver_type

    def to_str(self, env: Environment) -> str:
        args = ', '.join(env.format('%r', arg) for arg in self.args)
        s = env.format('%r.%s(%s)', self.obj, self.method, args)
        if self.dest is not None:
            s = env.format('%r = ', self.dest) + s
        return s

    def sources(self) -> List[Register]:
        return self.args[:] + [self.obj]

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_method_call(self)


# Python-interopability operations are prefixed with Py. Typically these act as a replacement
# for native operations (without the Py prefix) which call into Python rather than compiled
# native code. For example, this is needed to call builtins.


class PyCall(RegisterOp):
    """Python call f(arg, ...).

    All registers must be unboxed. Corresponds to PyObject_CallFunctionObjArgs in C.
    """

    error_kind = ERR_MAGIC

    def __init__(self, dest: Optional[Register], function: Register, args: List[Register],
                 line: int) -> None:
        super().__init__(dest, line)
        self.function = function
        self.args = args

    def to_str(self, env: Environment) -> str:
        args = ', '.join(env.format('%r', arg) for arg in self.args)
        s = env.format('%r(%s)', self.function, args)
        if self.dest is not None:
            s = env.format('%r = ', self.dest) + s
        return s + ' :: py'

    def sources(self) -> List[Register]:
        return self.args[:] + [self.function]

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_py_call(self)


class PyMethodCall(RegisterOp):
    """Python method call obj.m(arg, ...)

    All registers must be unboxed. Corresponds to PyObject_CallMethodObjArgs in C.
    """

    error_kind = ERR_MAGIC

    def __init__(self,
            dest: Optional[Register],
            obj: Register,
            method: Register,
            args: List[Register],
            line: int = -1) -> None:
        super().__init__(dest, line)
        self.obj = obj
        self.method = method
        self.args = args

    def to_str(self, env: Environment) -> str:
        args = ', '.join(env.format('%r', arg) for arg in self.args)
        s = env.format('%r.%r(%s)', self.obj, self.method, args)
        if self.dest is not None:
            s = env.format('%r = ', self.dest) + s
        return s + ' :: py'

    def sources(self) -> List[Register]:
        return self.args[:] + [self.obj, self.method]

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_py_method_call(self)


class PyGetAttr(StrictRegisterOp):
    """dest = left.right :: py"""

    error_kind = ERR_MAGIC
    no_reg = True

    def __init__(self, type: RType, left: Register, right: str, line: int) -> None:
        super().__init__(self, line)
        self.left = left
        self.right = right
        self._type = type

    def sources(self) -> List[Register]:
        return [self.left]

    def to_str(self, env: Environment) -> str:
        return env.format('%r = %r.%s', self.dest, self.left, self.right)

    def can_raise(self) -> bool:
        return True

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_py_get_attr(self)


class EmitterInterface:
    @abstractmethod
    def reg(self, name: Register) -> str:
        raise NotImplementedError

    @abstractmethod
    def temp_name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def emit_line(self, line: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def emit_lines(self, *line: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def emit_declaration(self, line: str) -> None:
        raise NotImplementedError


EmitCallback = Callable[[EmitterInterface, List[str], str], None]

OpDescription = NamedTuple(
    'OpDescription', [('name', str),
                      ('arg_types', List[RType]),
                      ('result_type', Optional[RType]),
                      ('is_var_arg', bool),
                      ('error_kind', int),
                      ('format_str', str),
                      ('emit', EmitCallback)])


class PrimitiveOp(RegisterOp):
    """reg = op(reg, ...)

    These are register-based primitive operations that work on specific
    operand types.

    The details of the operation are defined by the 'desc'
    attribute. The mypyc.ops_* modules define the supported
    operations. mypyc.genops uses the descriptions to look for suitable
    primitive ops.
    """

    def __init__(self,
                  dest: Optional[Register],
                  args: List[Register],
                  desc: OpDescription,
                  line: int) -> None:
        if not desc.is_var_arg:
            assert len(args) == len(desc.arg_types)
        self.error_kind = desc.error_kind
        super().__init__(dest, line)
        self.args = args
        self.desc = desc

    def sources(self) -> List[Register]:
        return list(self.args)

    def __repr__(self) -> str:
        return '<PrimiveOp2 name=%r dest=%s args=%s>' % (self.desc.name,
                                                         self.dest,
                                                         self.args)

    def to_str(self, env: Environment) -> str:
        params = {}  # type: Dict[str, Any]
        if self.dest is not None and self.dest != INVALID_REGISTER:
            params['dest'] = env.format('%r', self.dest)
        args = [env.format('%r', arg) for arg in self.args]
        params['args'] = args
        params['comma_args'] = ', '.join(args)
        return self.desc.format_str.format(**params)

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_primitive_op(self)


class Assign(StrictRegisterOp):
    """dest = int"""

    error_kind = ERR_NEVER

    def __init__(self, dest: Register, src: Register, line: int = -1) -> None:
        super().__init__(dest, line)
        self.src = src

    def sources(self) -> List[Register]:
        return [self.src]

    def to_str(self, env: Environment) -> str:
        return env.format('%r = %r', self.dest, self.src)

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_assign(self)


class LoadInt(StrictRegisterOp):
    """dest = int"""

    error_kind = ERR_NEVER

    def __init__(self, dest: Register, value: int, line: int = -1) -> None:
        super().__init__(dest, line)
        self.value = value

    def sources(self) -> List[Register]:
        return []

    def to_str(self, env: Environment) -> str:
        return env.format('%r = %d', self.dest, self.value)

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_load_int(self)


class LoadErrorValue(StrictRegisterOp):
    """dest = <error value for type>"""

    error_kind = ERR_NEVER
    no_reg = True

    def __init__(self, rtype: RType, line: int = -1) -> None:
        super().__init__(self, line)
        self._type = rtype

    def sources(self) -> List[Register]:
        return []

    def to_str(self, env: Environment) -> str:
        return env.format('%r = <error> :: %s', self.dest, self.type)

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_load_error_value(self)


class GetAttr(StrictRegisterOp):
    """dest = obj.attr (for a native object)"""

    error_kind = ERR_MAGIC
    no_reg = True

    def __init__(self, obj: Register, attr: str,
                 class_type: RInstance,
                 line: int) -> None:
        super().__init__(self, line)
        self.obj = obj
        self.attr = attr
        self.class_type = class_type
        self._type = class_type.attr_type(attr)

    def sources(self) -> List[Register]:
        return [self.obj]

    def to_str(self, env: Environment) -> str:
        return env.format('%r = %r.%s', self.dest, self.obj, self.attr)

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_get_attr(self)


class SetAttr(StrictRegisterOp):
    """obj.attr = src (for a native object)"""

    error_kind = ERR_FALSE
    no_reg = True

    def __init__(self, obj: Register, attr: str, src: Register,
                 class_type: RInstance,
                 line: int) -> None:
        super().__init__(self, line)
        self.obj = obj
        self.attr = attr
        self.src = src
        self.class_type = class_type
        self._type = bool_rprimitive

    def sources(self) -> List[Register]:
        return [self.obj, self.src]

    def to_str(self, env: Environment) -> str:
        return env.format('%r.%s = %r; %r = is_error', self.obj, self.attr, self.src, self.dest)

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_set_attr(self)


class LoadStatic(StrictRegisterOp):
    """dest = name :: static"""

    error_kind = ERR_NEVER
    no_reg = True

    def __init__(self, type: RType, identifier: str, line: int = -1) -> None:
        super().__init__(self, line)
        self.identifier = identifier
        self._type = type

    def sources(self) -> List[Register]:
        return []

    def to_str(self, env: Environment) -> str:
        return env.format('%r = %s :: static', self.dest, self.identifier)

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_load_static(self)


class TupleSet(StrictRegisterOp):
    """dest = (reg, ...) (for fixed-length tuple)"""

    error_kind = ERR_NEVER
    no_reg = True

    def __init__(self, items: List[Register], typ: RTuple, line: int) -> None:
        super().__init__(self, line)
        self.items = items
        self.tuple_type = typ
        self._type = typ

    def sources(self) -> List[Register]:
        return self.items[:]

    def to_str(self, env: Environment) -> str:
        item_str = ', '.join(env.format('%r', item) for item in self.items)
        return env.format('%r = (%s)', self.dest, item_str)

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_tuple_set(self)


class TupleGet(StrictRegisterOp):
    """dest = src[n] (for fixed-length tuple)"""

    error_kind = ERR_NEVER
    no_reg = True

    def __init__(self, src: Register, index: int, target_type: RType,
                 line: int) -> None:
        super().__init__(self, line)
        self.src = src
        self.index = index
        self._type = target_type

    def sources(self) -> List[Register]:
        return [self.src]

    def to_str(self, env: Environment) -> str:
        return env.format('%r = %r[%d]', self.dest, self.src, self.index)

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_tuple_get(self)


class Cast(StrictRegisterOp):
    """dest = cast(type, src)

    Perform a runtime type check (no representation or value conversion).

    DO NOT increment reference counts.
    """

    error_kind = ERR_MAGIC
    no_reg = True

    def __init__(self, src: Register, typ: RType, line: int) -> None:
        super().__init__(self, line)
        self.src = src
        self._type = typ

    def sources(self) -> List[Register]:
        return [self.src]

    def to_str(self, env: Environment) -> str:
        return env.format('%r = cast(%s, %r)', self.dest, self.type, self.src)

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_cast(self)


class Box(StrictRegisterOp):
    """dest = box(type, src)

    This converts from a potentially unboxed representation to a straight Python object.
    Only supported for types with an unboxed representation.
    """

    error_kind = ERR_NEVER
    no_reg = True

    def __init__(self, src: Register, typ: RType, line: int = -1) -> None:
        super().__init__(self, line)
        self.src = src
        self.src_type = typ
        self._type = object_rprimitive

    def sources(self) -> List[Register]:
        return [self.src]

    def to_str(self, env: Environment) -> str:
        return env.format('%r = box(%s, %r)', self.dest, self.src_type, self.src)

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_box(self)


class Unbox(StrictRegisterOp):
    """dest = unbox(type, src)

    This is similar to a cast, but it also changes to a (potentially) unboxed runtime
    representation. Only supported for types with an unboxed representation.
    """

    error_kind = ERR_MAGIC
    no_reg = True

    def __init__(self, src: Register, typ: RType, line: int) -> None:
        super().__init__(self, line)
        self.src = src
        self._type = typ

    def sources(self) -> List[Register]:
        return [self.src]

    def to_str(self, env: Environment) -> str:
        return env.format('%r = unbox(%s, %r)', self.dest, self.type, self.src)

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_unbox(self)


class BasicBlock:
    """Basic IR block.

    Ends with a jump, branch, or return.

    When building the IR, ops that raise exceptions can be included in
    the middle of a basic block, but the exceptions aren't checked.
    Afterwards we perform a transform that inserts explicit checks for
    all error conditions and splits basic blocks accordingly to preserve
    the invariant that a jump, branch or return can only ever appear
    as the final op in a block. Manually inserting error checking ops
    would be boring and error-prone.

    Ops that may terminate the program aren't treated as exits.
    """

    def __init__(self, label: Label) -> None:
        self.label = label
        self.ops = []  # type: List[Op]


class RuntimeArg:
    def __init__(self, name: str, typ: RType) -> None:
        self.name = name
        self.type = typ

    def __repr__(self) -> str:
        return 'RuntimeArg(name=%s, type=%s)' % (self.name, self.type)


class FuncIR:
    """Intermediate representation of a function with contextual information."""

    def __init__(self,
                 name: str,
                 class_name: Optional[str],
                 args: List[RuntimeArg],
                 ret_type: RType,
                 blocks: List[BasicBlock],
                 env: Environment) -> None:
        self.name = name
        self.class_name = class_name
        # TODO: escape ___ in names
        self.cname = name if not class_name else class_name + '___' + name
        self.args = args
        self.ret_type = ret_type
        self.blocks = blocks
        self.env = env

    def __str__(self) -> str:
        return '\n'.join(format_func(self))


class ClassIR:
    """Intermediate representation of a class.

    This also describes the runtime structure of native instances.
    """

    # TODO: Use dictionary for attributes in addition to (or instead of) list.

    def __init__(self,
                 name: str,
                 attributes: List[Tuple[str, RType]]) -> None:
        self.name = name
        self.attributes = attributes
        self.methods = []  # type: List[FuncIR]

    def struct_name(self) -> str:
        return '{}Object'.format(self.name)

    def get_method(self, name: str) -> Optional[FuncIR]:
        matches = [func for func in self.methods if func.name == name]
        return matches[0] if matches else None

    @property
    def type_struct(self) -> str:
        return '{}Type'.format(self.name)


class ModuleIR:
    """Intermediate representation of a module."""

    def __init__(self,
            imports: List[str],
            unicode_literals: Dict[str, str],
            functions: List[FuncIR],
            classes: List[ClassIR]) -> None:
        self.imports = imports[:]
        self.unicode_literals = unicode_literals
        self.functions = functions
        self.classes = classes

        if 'builtins' not in self.imports:
            self.imports.insert(0, 'builtins')


def type_struct_name(class_name: str) -> str:
    return '{}Type'.format(class_name)


class OpVisitor(Generic[T]):
    def visit_goto(self, op: Goto) -> T:
        pass

    def visit_branch(self, op: Branch) -> T:
        pass

    def visit_return(self, op: Return) -> T:
        pass

    def visit_unreachable(self, op: Unreachable) -> T:
        pass

    def visit_primitive_op(self, op: PrimitiveOp) -> T:
        pass

    def visit_assign(self, op: Assign) -> T:
        pass

    def visit_load_int(self, op: LoadInt) -> T:
        pass

    def visit_load_error_value(self, op: LoadErrorValue) -> T:
        pass

    def visit_get_attr(self, op: GetAttr) -> T:
        pass

    def visit_set_attr(self, op: SetAttr) -> T:
        pass

    def visit_load_static(self, op: LoadStatic) -> T:
        pass

    def visit_py_get_attr(self, op: PyGetAttr) -> T:
        pass

    def visit_tuple_get(self, op: TupleGet) -> T:
        pass

    def visit_tuple_set(self, op: TupleSet) -> T:
        pass

    def visit_inc_ref(self, op: IncRef) -> T:
        pass

    def visit_dec_ref(self, op: DecRef) -> T:
        pass

    def visit_call(self, op: Call) -> T:
        pass

    def visit_py_call(self, op: PyCall) -> T:
        pass

    def visit_method_call(self, op: MethodCall) -> T:
        pass

    def visit_py_method_call(self, op: PyMethodCall) -> T:
        pass

    def visit_cast(self, op: Cast) -> T:
        pass

    def visit_box(self, op: Box) -> T:
        pass

    def visit_unbox(self, op: Unbox) -> T:
        pass


def format_blocks(blocks: List[BasicBlock], env: Environment) -> List[str]:
    lines = []
    for i, block in enumerate(blocks):
        last = i == len(blocks) - 1

        lines.append(env.format('%l:', block.label))
        ops = block.ops
        if (isinstance(ops[-1], Goto) and i + 1 < len(blocks) and
                ops[-1].label == blocks[i + 1].label):
            # Hide the last goto if it just goes to the next basic block.
            ops = ops[:-1]
        for op in ops:
            line = '    ' + op.to_str(env)
            lines.append(line)

        if not isinstance(block.ops[-1], (Goto, Branch, Return, Unreachable)):
            # Each basic block needs to exit somewhere.
            lines.append('    [MISSING BLOCK EXIT OPCODE]')
    return lines


def format_func(fn: FuncIR) -> List[str]:
    lines = []
    lines.append('def {}({}):'.format(fn.name, ', '.join(arg.name
                                                         for arg in fn.args)))
    for line in fn.env.to_lines():
        lines.append('    ' + line)
    code = format_blocks(fn.blocks, fn.env)
    lines.extend(code)
    return lines


class RTypeVisitor(Generic[T]):
    @abstractmethod
    def visit_rprimitive(self, typ: RPrimitive) -> T:
        raise NotImplementedError

    @abstractmethod
    def visit_rinstance(self, typ: RInstance) -> T:
        raise NotImplementedError

    @abstractmethod
    def visit_roptional(self, typ: ROptional) -> T:
        raise NotImplementedError

    @abstractmethod
    def visit_rtuple(self, typ: RTuple) -> T:
        raise NotImplementedError


# Import various modules that set up global state.
import mypyc.ops_int
import mypyc.ops_list
import mypyc.ops_dict
import mypyc.ops_tuple
import mypyc.ops_misc
