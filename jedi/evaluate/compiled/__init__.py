"""
Imitate the parser representation.
"""
import inspect
import re
import sys
import os

from jedi._compatibility import builtins as _builtins, exec_function
from jedi import debug
from jedi.parser.representation import Base
from jedi.cache import underscore_memoization
from jedi.evaluate.sys_path import get_sys_path
from . import fake


class PyObject(Base):
    # comply with the parser
    start_pos = 0, 0
    asserts = []
    path = None  # modules have this attribute - set it to None.

    def __init__(self, obj, parent=None):
        self.obj = obj
        self.parent = parent
        self.doc = inspect.getdoc(obj)

    def __repr__(self):
        return '<%s: %s>' % (type(self).__name__, self.obj)

    def get_parent_until(self, *args, **kwargs):
        # compiled modules only use functions and classes/methods (2 levels)
        return getattr(self.parent, 'parent', self.parent) or self.parent or self

    @underscore_memoization
    def _parse_function_doc(self):
        if self.doc is None:
            return '', ''

        return _parse_function_doc(self.doc)

    def type(self):
        cls = self._cls().obj
        if inspect.isclass(cls):
            return 'class'
        elif inspect.ismodule(cls):
            return 'module'
        elif inspect.isbuiltin(cls) or inspect.ismethod(cls) \
                or inspect.ismethoddescriptor(cls):
            return 'def'

    def is_executable_class(self):
        return inspect.isclass(self.obj)

    @underscore_memoization
    def _cls(self):
        # Ensures that a PyObject is returned that is not an instance (like list)
        if fake.is_class_instance(self.obj):
            try:
                c = self.obj.__class__
            except AttributeError:
                # happens with numpy.core.umath._UFUNC_API (you get it
                # automatically by doing `import numpy`.
                c = type(None)
            return PyObject(c, self.parent)
        return self

    def get_defined_names(self):
        cls = self._cls()
        for name in dir(cls.obj):
            yield PyName(cls, name)

    def instance_names(self):
        # TODO REMOVE (temporary until the Instance method is removed)
        return self.get_defined_names()

    def get_subscope_by_name(self, name):
        if name in dir(self._cls().obj):
            return PyName(self._cls(), name).parent
        else:
            raise KeyError("CompiledObject doesn't have an attribute '%s'." % name)

    @property
    def name(self):
        # might not exist sometimes (raises AttributeError)
        return self._cls().obj.__name__

    def execute_function(self, evaluator, params):
        if self.type() != 'def':
            return
        for name in self._parse_function_doc()[1].split():
            try:
                bltn_obj = _create_from_name(builtin, builtin, name)
            except AttributeError:
                continue
            else:
                if isinstance(bltn_obj, PyObject):
                    yield bltn_obj
                else:
                    for result in evaluator.execute(bltn_obj, params):
                        yield result

    @property
    @underscore_memoization
    def subscopes(self):
        """
        Returns only the faked scopes - the other ones are not important for
        internal analysis.
        """
        module = self.get_parent_until()
        faked_subscopes = []
        for name in dir(self._cls().obj):
            f = fake.get_faked(module.obj, self.obj, name)
            if f:
                f.parent = self
                faked_subscopes.append(f)
        return faked_subscopes

    def get_self_attributes(self):
        return []  # Instance compatibility

    def get_imports(self):
        return []  # Builtins don't have imports


class PyName(object):
    def __init__(self, obj, name):
        self._obj = obj
        self.name = name
        self.start_pos = 0, 0  # an illegal start_pos, to make sorting easy.

    def get_parent_until(self):
        return self.parent.get_parent_until()

    def __str__(self):
        return self.name

    def __repr__(self):
        return '<%s: (%s).%s>' % (type(self).__name__, self._obj.name, self.name)

    @property
    @underscore_memoization
    def parent(self):
        module = self._obj.get_parent_until()
        return _create_from_name(module, self._obj, self.name)

    @property
    def names(self):
        return [self.name]  # compatibility with parser.representation.Name

    def get_code(self):
        return self.name

import os.path as osp
import imp
def get_parent_until(path):
    """
    Given a file path, determine the full module path

    e.g. '/usr/lib/python2.7/dist-packages/numpy/core/__init__.pyc' yields
    'numpy.core'
    """
    dirname = osp.dirname(path)
    try:
        mod = osp.basename(path)
        mod = osp.splitext(mod)[0]
        imp.find_module(mod, [dirname])
    except ImportError:
        return
    items = [mod]
    while 1:
        items.append(osp.basename(dirname))
        try:
            dirname = osp.dirname(dirname)
            imp.find_module('__init__', [dirname + os.sep])
        except ImportError:
            break
    return '.'.join(reversed(items))


def load_module(path, name):
    if not name:
        name = os.path.basename(path)
        name = name.rpartition('.')[0]  # cut file type (normally .so)

    # sometimes there are endings like `_sqlite3.cpython-32mu`
    name = re.sub(r'\..*', '', name)

    if path:
        dot_path = []
        p = path
        # if path is not in sys.path, we need to make a well defined import
        # like `from numpy.core import umath.`
        while p and p not in sys.path:
            p, sep, mod = p.rpartition(os.path.sep)
            dot_path.insert(0, mod.partition('.')[0])
        if p:
            name = ".".join(dot_path)
            path = p
        else:
            path = os.path.dirname(path)

    sys_path = get_sys_path()
    if path:
        sys_path.insert(0, path)

    temp, sys.path = sys.path, sys_path
    content = {}
    try:
        exec_function('import %s as module' % name, content)
        module = content['module']
    except AttributeError:
        # use sys.modules, because you cannot access some modules
        # directly. -> github issue #59
        module = sys.modules[name]
    except ImportError:
        name = get_parent_until(path)
        if name in sys.modules:
            module = sys.modules[name]
        else:
            module = __import__(name, fromlist=[name.rpartition('.')[-1]])
    sys.path = temp
    return PyObject(module)


docstr_defaults = {
    'floating point number': 'float',
    'character': 'str',
    'integer': 'int',
    'dictionary': 'dict',
    'string': 'str',
}


def _parse_function_doc(doc):
    """
    Takes a function and returns the params and return value as a tuple.
    This is nothing more than a docstring parser.

    TODO docstrings like utime(path, (atime, mtime)) and a(b [, b]) -> None
    TODO docstrings like 'tuple of integers'
    """
    # parse round parentheses: def func(a, (b,c))
    try:
        count = 0
        start = doc.index('(')
        for i, s in enumerate(doc[start:]):
            if s == '(':
                count += 1
            elif s == ')':
                count -= 1
            if count == 0:
                end = start + i
                break
        param_str = doc[start + 1:end]
    except (ValueError, UnboundLocalError):
        # ValueError for doc.index
        # UnboundLocalError for undefined end in last line
        debug.dbg('no brackets found - no param')
        end = 0
        param_str = ''
    else:
        # remove square brackets, that show an optional param ( = None)
        def change_options(m):
            args = m.group(1).split(',')
            for i, a in enumerate(args):
                if a and '=' not in a:
                    args[i] += '=None'
            return ','.join(args)

        while True:
            param_str, changes = re.subn(r' ?\[([^\[\]]+)\]',
                                         change_options, param_str)
            if changes == 0:
                break
    param_str = param_str.replace('-', '_')  # see: isinstance.__doc__

    # parse return value
    r = re.search('-[>-]* ', doc[end:end + 7])
    if r is None:
        ret = ''
    else:
        index = end + r.end()
        # get result type, which can contain newlines
        pattern = re.compile(r'(,\n|[^\n-])+')
        ret_str = pattern.match(doc, index).group(0).strip()
        # New object -> object()
        ret_str = re.sub(r'[nN]ew (.*)', r'\1()', ret_str)

        ret = docstr_defaults.get(ret_str, ret_str)

    return param_str, ret


class Builtin(PyObject):
    def get_defined_names(self):
        # Filter None, because it's really just a keyword, nobody wants to
        # access it.
        return [d for d in super(Builtin, self).get_defined_names() if d.name != 'None']


builtin = Builtin(_builtins)
magic_function_class = PyObject(type(load_module), parent=builtin)


def _create_from_name(module, parent, name):
    faked = fake.get_faked(module.obj, parent.obj, name)
    # only functions are necessary.
    if faked is not None:
        faked.parent = parent
        return faked

    try:
        obj = getattr(parent.obj, name)
    except AttributeError:
        # happens e.g. in properties of
        # PyQt4.QtGui.QStyleOptionComboBox.currentText
        # -> just set it to None
        obj = None
    return PyObject(obj, parent)


def create(obj, parent=builtin, module=None):
    """
    A very weird interface class to this module. The more options provided the
    more acurate loading compiled objects is.
    """
    if not inspect.ismodule(parent):
        faked = fake.get_faked(module and module.obj, obj)
        if faked is not None:
            faked.parent = parent
            return faked

    return PyObject(obj, parent)
