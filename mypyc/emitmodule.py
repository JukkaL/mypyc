"""Generate C code for a Python C extension module from Python source code."""

from collections import OrderedDict
from typing import List, Tuple, Dict, Iterable

from mypy.build import BuildSource, build
from mypy.errors import CompileError
from mypy.options import Options

from mypyc import genops
from mypyc.common import PREFIX
from mypyc.emit import EmitterContext, Emitter, HeaderDeclaration
from mypyc.emitfunc import generate_native_function, native_function_header
from mypyc.emitclass import generate_class
from mypyc.emitwrapper import generate_wrapper_function, wrapper_function_header
from mypyc.ops import c_module_name, FuncIR, ClassIR, ModuleIR
from mypyc.refcount import insert_ref_count_opcodes
from mypyc.exceptions import insert_exception_handling


class MarkedDeclaration:
    """Add a mark, useful for topological sort."""
    def __init__(self, declaration: HeaderDeclaration, mark: bool) -> None:
        self.declaration = declaration
        self.mark = False


def compile_modules_to_c(sources: List[BuildSource], module_names: List[str], options: Options,
                         alt_lib_path: str) -> str:
    """Compile Python module(s) to C that can be used from Python C extension modules."""
    assert options.strict_optional, 'strict_optional must be turned on'
    result = build(sources=sources,
                   options=options,
                   alt_lib_path=alt_lib_path)
    if result.errors:
        raise CompileError(result.errors)

    # Generate basic IR, with missing exception and refcount handling.
    modules = [(module_name, genops.build_ir(result.files[module_name], result.types))
               for module_name in module_names]
    # Insert exception handling.
    for _, module in modules:
        for fn in module.functions:
            insert_exception_handling(fn)
    # Insert refcount handling.
    for _, module in modules:
        for fn in module.functions:
            insert_ref_count_opcodes(fn)
    # Generate C code.
    source_paths = {module_name: result.files[module_name].path
                    for module_name in module_names}
    generator = ModuleGenerator(modules, source_paths)
    return generator.generate_c_for_modules()


def generate_function_declaration(fn: FuncIR, emitter: Emitter) -> None:
    emitter.emit_lines(
        '{};'.format(native_function_header(fn, emitter.names)),
        '{};'.format(wrapper_function_header(fn, emitter.names)))


def encode_as_c_string(s: str) -> Tuple[str, int]:
    """Produce a utf-8 encoded, escaped, quoted C string and its size from a string"""
    # This is a kind of abusive way to do this...
    b = s.encode('utf-8')
    escaped = str(b)[2:-1].replace('"', '\\"')
    return '"{}"'.format(escaped), len(b)


class ModuleGenerator:
    def __init__(self,
                 modules: List[Tuple[str, ModuleIR]],
                 source_paths: Dict[str, str]) -> None:
        self.modules = modules
        self.source_paths = source_paths
        self.context = EmitterContext([name for name, _ in modules])
        self.names = self.context.names

    def generate_c_for_modules(self) -> str:
        emitter = Emitter(self.context)

        self.declare_internal_globals()

        module_irs = [module_ir for _, module_ir in self.modules]

        for module in module_irs:
            self.declare_imports(module.imports)

        for module in module_irs:
            for symbol in module.literals.values():
                self.declare_static_pyobject(symbol)

        for module in module_irs:
            for fn in module.functions:
                generate_function_declaration(fn, emitter)

        for module_name, module in self.modules:
            for cl in module.classes:
                generate_class(cl, module_name, emitter)

        emitter.emit_line()

        # Generate Python extension module definitions and module initialization functions.
        for module_name, module in self.modules:
            self.generate_module_def(emitter, module_name, module)

        for module_name, module in self.modules:
            for fn in module.functions:
                emitter.emit_line()
                generate_native_function(fn, emitter, self.source_paths[module_name])
                emitter.emit_line()
                generate_wrapper_function(fn, emitter)

        declarations = Emitter(self.context)
        declarations.emit_line('#include <Python.h>')
        declarations.emit_line('#include <CPy.h>')
        declarations.emit_line()

        for declaration in self.toposort_declarations():
            declarations.emit_lines(*declaration.body)

        return ''.join(declarations.fragments + emitter.fragments)

    def generate_module_def(self, emitter: Emitter, module_name: str, module: ModuleIR) -> None:
        # Emit module methods
        module_prefix = emitter.names.private_name(module_name)
        emitter.emit_line('static PyMethodDef {}module_methods[] = {{'.format(module_prefix))
        for fn in module.functions:
            emitter.emit_line(
                ('{{"{name}", (PyCFunction){prefix}{cname}, METH_VARARGS | METH_KEYWORDS, '
                 'NULL /* docstring */}},').format(
                    name=fn.name,
                    cname=fn.cname(emitter.names),
                    prefix=PREFIX))
        emitter.emit_line('{NULL, NULL, 0, NULL}')
        emitter.emit_line('};')
        emitter.emit_line()

        # Emit module definition struct
        emitter.emit_lines('static struct PyModuleDef {}module = {{'.format(module_prefix),
                           'PyModuleDef_HEAD_INIT,',
                           '"{}",'.format(module_name),
                           'NULL, /* docstring */',
                           '-1,       /* size of per-interpreter state of the module,',
                           '             or -1 if the module keeps state in global variables. */',
                           '{}module_methods'.format(module_prefix),
                           '};')
        emitter.emit_line()

        # Emit module init function. If we are compiling just one module, this
        # will be the C API init function. If we are compiling 2+ modules, we
        # generate a shared library for the modules and shims that call into
        # the shared library, and in this case we use an internal module
        # initialized function that will be called by the shim.
        if len(self.modules) == 1:
            declaration = 'PyMODINIT_FUNC PyInit_{}(void)'
        else:
            declaration = 'PyObject *x_PyInit_{}(void)'
        emitter.emit_lines(declaration.format(module_name),
                           '{',
                           'PyObject *m;')
        for cl in module.classes:
            type_struct = cl.type_struct
            emitter.emit_lines('if (PyType_Ready(&{}) < 0)'.format(type_struct),
                               '    return NULL;')
        emitter.emit_lines('m = PyModule_Create(&{}module);'.format(module_prefix),
                           'if (m == NULL)',
                           '    return NULL;')
        emitter.emit_lines('_globals = PyModule_GetDict(m);',
                           'if (_globals == NULL)',
                           '    return NULL;')
        self.generate_imports_init_section(module.imports, emitter)

        for literal, symbol in module.literals.items():
            if isinstance(literal, int):
                emitter.emit_lines(
                    '{} = PyLong_FromString(\"{}\", NULL, 10);'.format(
                        symbol, str(literal))
                )
            elif isinstance(literal, float):
                emitter.emit_lines(
                    '{} = PyFloat_FromDouble({});'.format(symbol, str(literal))
                )
            elif isinstance(literal, str):
                emitter.emit_lines(
                    '{} = PyUnicode_FromStringAndSize({}, {});'.format(
                        symbol, *encode_as_c_string(literal)),
                    'if ({} == NULL)'.format(symbol),
                    '    return NULL;',
                )
            else:
                assert False, ('Literals must be integers, floating point numbers, or strings,',
                               'but the provided literal is of type {}'.format(type(literal)))

        for cl in module.classes:
            name = cl.name
            type_struct = cl.type_struct
            emitter.emit_lines(
                'Py_INCREF(&{});'.format(type_struct),
                'PyModule_AddObject(m, "{}", (PyObject *)&{});'.format(name, type_struct))
        emitter.emit_line('return m;')
        emitter.emit_line('}')

    def toposort_declarations(self) -> List[HeaderDeclaration]:
        """Topologically sort the declaration dict by dependencies.

        Declarations can require other declarations to come prior in C (such as declaring structs).
        In order to guarantee that the C output will compile the declarations will thus need to
        be properly ordered. This simple DFS guarantees that we have a proper ordering.

        This runs in O(V + E).
        """
        result = []
        marked_declarations = OrderedDict()  # type: Dict[str, MarkedDeclaration]
        for k, v in self.context.declarations.items():
            marked_declarations[k] = MarkedDeclaration(v, False)

        def _toposort_visit(name: str) -> None:
            decl = marked_declarations[name]
            if decl.mark:
                return

            for child in decl.declaration.dependencies:
                _toposort_visit(child)

            result.append(decl.declaration)
            decl.mark = True

        for name, marked_declaration in marked_declarations.items():
            _toposort_visit(name)

        return result

    def declare_global(self, type_spaced: str, name: str, static: bool=True) -> None:
        static_str = 'static ' if static else ''
        if name not in self.context.declarations:
            self.context.declarations[name] = HeaderDeclaration(
                set(),
                ['{}{}{};'.format(static_str, type_spaced, name)],
            )

    def declare_internal_globals(self) -> None:
        self.declare_global('PyObject *', '_globals')

    def declare_import(self, imp: str) -> None:
        self.declare_global('CPyModule *', c_module_name(imp))

    def declare_imports(self, imps: Iterable[str]) -> None:
        for imp in imps:
            self.declare_import(imp)

    def declare_static_pyobject(self, symbol: str) -> None:
        self.declare_global('PyObject *', symbol)

    def generate_imports_init_section(self, imps: List[str], emitter: Emitter) -> None:
        for imp in imps:
            emitter.emit_line('{} = PyImport_ImportModule("{}");'.format(c_module_name(imp), imp))
            emitter.emit_line('if ({} == NULL)'.format(c_module_name(imp)))
            emitter.emit_line('    return NULL;')
