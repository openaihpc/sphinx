"""
    sphinx.domains.c
    ~~~~~~~~~~~~~~~~

    The C language domain.

    :copyright: Copyright 2007-2020 by the Sphinx team, see AUTHORS.
    :license: BSD, see LICENSE for details.
"""

import re
from typing import (
    Any, Callable, Dict, Generator, Iterator, List, Type, TypeVar, Tuple, Union
)
from typing import cast

from docutils import nodes
from docutils.nodes import Element, Node, TextElement, system_message
from docutils.parsers.rst import directives

from sphinx import addnodes
from sphinx.addnodes import pending_xref
from sphinx.application import Sphinx
from sphinx.builders import Builder
from sphinx.directives import ObjectDescription
from sphinx.domains import Domain, ObjType
from sphinx.environment import BuildEnvironment
from sphinx.locale import _, __
from sphinx.roles import SphinxRole, XRefRole
from sphinx.transforms import SphinxTransform
from sphinx.transforms.post_transforms import ReferencesResolver
from sphinx.util import logging
from sphinx.util.cfamily import (
    NoOldIdError, ASTBaseBase, ASTBaseParenExprList,
    verify_description_mode, StringifyTransform,
    BaseParser, DefinitionError, UnsupportedMultiCharacterCharLiteral,
    identifier_re, anon_identifier_re, integer_literal_re, octal_literal_re,
    hex_literal_re, binary_literal_re, integers_literal_suffix_re,
    float_literal_re, float_literal_suffix_re,
    char_literal_re
)
from sphinx.util.docfields import Field, TypedField
from sphinx.util.docutils import SphinxDirective
from sphinx.util.nodes import make_refnode

logger = logging.getLogger(__name__)
T = TypeVar('T')

# https://en.cppreference.com/w/c/keyword
_keywords = [
    'auto', 'break', 'case', 'char', 'const', 'continue', 'default', 'do', 'double',
    'else', 'enum', 'extern', 'float', 'for', 'goto', 'if', 'inline', 'int', 'long',
    'register', 'restrict', 'return', 'short', 'signed', 'sizeof', 'static', 'struct',
    'switch', 'typedef', 'union', 'unsigned', 'void', 'volatile', 'while',
    '_Alignas', 'alignas', '_Alignof', 'alignof', '_Atomic', '_Bool', 'bool',
    '_Complex', 'complex', '_Generic', '_Imaginary', 'imaginary',
    '_Noreturn', 'noreturn', '_Static_assert', 'static_assert',
    '_Thread_local', 'thread_local',
]

# these are ordered by preceedence
_expression_bin_ops = [
    ['||', 'or'],
    ['&&', 'and'],
    ['|', 'bitor'],
    ['^', 'xor'],
    ['&', 'bitand'],
    ['==', '!=', 'not_eq'],
    ['<=', '>=', '<', '>'],
    ['<<', '>>'],
    ['+', '-'],
    ['*', '/', '%'],
    ['.*', '->*']
]
_expression_unary_ops = ["++", "--", "*", "&", "+", "-", "!", "not", "~", "compl"]
_expression_assignment_ops = ["=", "*=", "/=", "%=", "+=", "-=",
                              ">>=", "<<=", "&=", "and_eq", "^=", "xor_eq", "|=", "or_eq"]

_max_id = 1
_id_prefix = [None, 'c.', 'Cv2.']
# Ids are used in lookup keys which are used across pickled files,
# so when _max_id changes, make sure to update the ENV_VERSION.

_string_re = re.compile(r"[LuU8]?('([^'\\]*(?:\\.[^'\\]*)*)'"
                        r'|"([^"\\]*(?:\\.[^"\\]*)*)")', re.S)


class _DuplicateSymbolError(Exception):
    def __init__(self, symbol: "Symbol", declaration: "ASTDeclaration") -> None:
        assert symbol
        assert declaration
        self.symbol = symbol
        self.declaration = declaration

    def __str__(self) -> str:
        return "Internal C duplicate symbol error:\n%s" % self.symbol.dump(0)


class ASTBase(ASTBaseBase):
    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        raise NotImplementedError(repr(self))


# Names
################################################################################

class ASTIdentifier(ASTBaseBase):
    def __init__(self, identifier: str) -> None:
        assert identifier is not None
        assert len(identifier) != 0
        self.identifier = identifier

    def is_anon(self) -> bool:
        return self.identifier[0] == '@'

    # and this is where we finally make a difference between __str__ and the display string

    def __str__(self) -> str:
        return self.identifier

    def get_display_string(self) -> str:
        return "[anonymous]" if self.is_anon() else self.identifier

    def describe_signature(self, signode: TextElement, mode: str, env: "BuildEnvironment",
                           prefix: str, symbol: "Symbol") -> None:
        # note: slightly different signature of describe_signature due to the prefix
        verify_description_mode(mode)
        if mode == 'markType':
            targetText = prefix + self.identifier
            pnode = addnodes.pending_xref('', refdomain='c',
                                          reftype='identifier',
                                          reftarget=targetText, modname=None,
                                          classname=None)
            # key = symbol.get_lookup_key()
            # pnode['c:parent_key'] = key
            if self.is_anon():
                pnode += nodes.strong(text="[anonymous]")
            else:
                pnode += nodes.Text(self.identifier)
            signode += pnode
        elif mode == 'lastIsName':
            if self.is_anon():
                signode += nodes.strong(text="[anonymous]")
            else:
                signode += addnodes.desc_name(self.identifier, self.identifier)
        elif mode == 'noneIsName':
            if self.is_anon():
                signode += nodes.strong(text="[anonymous]")
            else:
                signode += nodes.Text(self.identifier)
        else:
            raise Exception('Unknown description mode: %s' % mode)


class ASTNestedName(ASTBase):
    def __init__(self, names: List[ASTIdentifier], rooted: bool) -> None:
        assert len(names) > 0
        self.names = names
        self.rooted = rooted

    @property
    def name(self) -> "ASTNestedName":
        return self

    def get_id(self, version: int) -> str:
        return '.'.join(str(n) for n in self.names)

    def _stringify(self, transform: StringifyTransform) -> str:
        res = '.'.join(transform(n) for n in self.names)
        if self.rooted:
            return '.' + res
        else:
            return res

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        verify_description_mode(mode)
        # just print the name part, with template args, not template params
        if mode == 'noneIsName':
            signode += nodes.Text(str(self))
        elif mode == 'param':
            name = str(self)
            signode += nodes.emphasis(name, name)
        elif mode == 'markType' or mode == 'lastIsName' or mode == 'markName':
            # Each element should be a pending xref targeting the complete
            # prefix.
            prefix = ''
            first = True
            names = self.names[:-1] if mode == 'lastIsName' else self.names
            # If lastIsName, then wrap all of the prefix in a desc_addname,
            # else append directly to signode.
            # TODO: also for C?
            #  NOTE: Breathe previously relied on the prefix being in the desc_addname node,
            #       so it can remove it in inner declarations.
            dest = signode
            if mode == 'lastIsName':
                dest = addnodes.desc_addname()
            if self.rooted:
                prefix += '.'
                if mode == 'lastIsName' and len(names) == 0:
                    signode += nodes.Text('.')
                else:
                    dest += nodes.Text('.')
            for i in range(len(names)):
                ident = names[i]
                if not first:
                    dest += nodes.Text('.')
                    prefix += '.'
                first = False
                txt_ident = str(ident)
                if txt_ident != '':
                    ident.describe_signature(dest, 'markType', env, prefix, symbol)
                prefix += txt_ident
            if mode == 'lastIsName':
                if len(self.names) > 1:
                    dest += addnodes.desc_addname('.', '.')
                    signode += dest
                self.names[-1].describe_signature(signode, mode, env, '', symbol)
        else:
            raise Exception('Unknown description mode: %s' % mode)


################################################################################
# Expressions
################################################################################

class ASTExpression(ASTBase):
    pass


# Primary expressions
################################################################################

class ASTLiteral(ASTExpression):
    pass


class ASTBooleanLiteral(ASTLiteral):
    def __init__(self, value: bool) -> None:
        self.value = value

    def _stringify(self, transform: StringifyTransform) -> str:
        if self.value:
            return 'true'
        else:
            return 'false'

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        signode.append(nodes.Text(str(self)))


class ASTNumberLiteral(ASTLiteral):
    def __init__(self, data: str) -> None:
        self.data = data

    def _stringify(self, transform: StringifyTransform) -> str:
        return self.data

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        txt = str(self)
        signode.append(nodes.Text(txt, txt))


class ASTCharLiteral(ASTLiteral):
    def __init__(self, prefix: str, data: str) -> None:
        self.prefix = prefix  # may be None when no prefix
        self.data = data
        decoded = data.encode().decode('unicode-escape')
        if len(decoded) == 1:
            self.value = ord(decoded)
        else:
            raise UnsupportedMultiCharacterCharLiteral(decoded)

    def _stringify(self, transform: StringifyTransform) -> str:
        if self.prefix is None:
            return "'" + self.data + "'"
        else:
            return self.prefix + "'" + self.data + "'"

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        txt = str(self)
        signode.append(nodes.Text(txt, txt))


class ASTStringLiteral(ASTLiteral):
    def __init__(self, data: str) -> None:
        self.data = data

    def _stringify(self, transform: StringifyTransform) -> str:
        return self.data

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        txt = str(self)
        signode.append(nodes.Text(txt, txt))


class ASTIdExpression(ASTExpression):
    def __init__(self, name: ASTNestedName):
        # note: this class is basically to cast a nested name as an expression
        self.name = name

    def _stringify(self, transform: StringifyTransform) -> str:
        return transform(self.name)

    def get_id(self, version: int) -> str:
        return self.name.get_id(version)

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        self.name.describe_signature(signode, mode, env, symbol)


class ASTParenExpr(ASTExpression):
    def __init__(self, expr):
        self.expr = expr

    def _stringify(self, transform: StringifyTransform) -> str:
        return '(' + transform(self.expr) + ')'

    def get_id(self, version: int) -> str:
        return self.expr.get_id(version)

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        signode.append(nodes.Text('(', '('))
        self.expr.describe_signature(signode, mode, env, symbol)
        signode.append(nodes.Text(')', ')'))


# Postfix expressions
################################################################################

class ASTPostfixOp(ASTBase):
    pass


class ASTPostfixCallExpr(ASTPostfixOp):
    def __init__(self, lst: Union["ASTParenExprList", "ASTBracedInitList"]) -> None:
        self.lst = lst

    def _stringify(self, transform: StringifyTransform) -> str:
        return transform(self.lst)

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        self.lst.describe_signature(signode, mode, env, symbol)


class ASTPostfixArray(ASTPostfixOp):
    def __init__(self, expr: ASTExpression) -> None:
        self.expr = expr

    def _stringify(self, transform: StringifyTransform) -> str:
        return '[' + transform(self.expr) + ']'

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        signode.append(nodes.Text('['))
        self.expr.describe_signature(signode, mode, env, symbol)
        signode.append(nodes.Text(']'))


class ASTPostfixInc(ASTPostfixOp):
    def _stringify(self, transform: StringifyTransform) -> str:
        return '++'

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        signode.append(nodes.Text('++'))


class ASTPostfixDec(ASTPostfixOp):
    def _stringify(self, transform: StringifyTransform) -> str:
        return '--'

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        signode.append(nodes.Text('--'))


class ASTPostfixMember(ASTPostfixOp):
    def __init__(self, name):
        self.name = name

    def _stringify(self, transform: StringifyTransform) -> str:
        return '.' + transform(self.name)

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        signode.append(nodes.Text('.'))
        self.name.describe_signature(signode, 'noneIsName', env, symbol)


class ASTPostfixMemberOfPointer(ASTPostfixOp):
    def __init__(self, name):
        self.name = name

    def _stringify(self, transform: StringifyTransform) -> str:
        return '->' + transform(self.name)

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        signode.append(nodes.Text('->'))
        self.name.describe_signature(signode, 'noneIsName', env, symbol)


class ASTPostfixExpr(ASTExpression):
    def __init__(self, prefix: ASTExpression, postFixes: List[ASTPostfixOp]):
        self.prefix = prefix
        self.postFixes = postFixes

    def _stringify(self, transform: StringifyTransform) -> str:
        res = [transform(self.prefix)]
        for p in self.postFixes:
            res.append(transform(p))
        return ''.join(res)

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        self.prefix.describe_signature(signode, mode, env, symbol)
        for p in self.postFixes:
            p.describe_signature(signode, mode, env, symbol)


# Unary expressions
################################################################################

class ASTUnaryOpExpr(ASTExpression):
    def __init__(self, op: str, expr: ASTExpression):
        self.op = op
        self.expr = expr

    def _stringify(self, transform: StringifyTransform) -> str:
        if self.op[0] in 'cn':
            return self.op + " " + transform(self.expr)
        else:
            return self.op + transform(self.expr)

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        signode.append(nodes.Text(self.op))
        if self.op[0] in 'cn':
            signode.append(nodes.Text(" "))
        self.expr.describe_signature(signode, mode, env, symbol)


class ASTSizeofType(ASTExpression):
    def __init__(self, typ):
        self.typ = typ

    def _stringify(self, transform: StringifyTransform) -> str:
        return "sizeof(" + transform(self.typ) + ")"

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        signode.append(nodes.Text('sizeof('))
        self.typ.describe_signature(signode, mode, env, symbol)
        signode.append(nodes.Text(')'))


class ASTSizeofExpr(ASTExpression):
    def __init__(self, expr: ASTExpression):
        self.expr = expr

    def _stringify(self, transform: StringifyTransform) -> str:
        return "sizeof " + transform(self.expr)

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        signode.append(nodes.Text('sizeof '))
        self.expr.describe_signature(signode, mode, env, symbol)


class ASTAlignofExpr(ASTExpression):
    def __init__(self, typ: "ASTType"):
        self.typ = typ

    def _stringify(self, transform: StringifyTransform) -> str:
        return "alignof(" + transform(self.typ) + ")"

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        signode.append(nodes.Text('alignof('))
        self.typ.describe_signature(signode, mode, env, symbol)
        signode.append(nodes.Text(')'))


# Other expressions
################################################################################

class ASTCastExpr(ASTExpression):
    def __init__(self, typ: "ASTType", expr: ASTExpression):
        self.typ = typ
        self.expr = expr

    def _stringify(self, transform: StringifyTransform) -> str:
        res = ['(']
        res.append(transform(self.typ))
        res.append(')')
        res.append(transform(self.expr))
        return ''.join(res)

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        signode.append(nodes.Text('('))
        self.typ.describe_signature(signode, mode, env, symbol)
        signode.append(nodes.Text(')'))
        self.expr.describe_signature(signode, mode, env, symbol)


class ASTBinOpExpr(ASTBase):
    def __init__(self, exprs: List[ASTExpression], ops: List[str]):
        assert len(exprs) > 0
        assert len(exprs) == len(ops) + 1
        self.exprs = exprs
        self.ops = ops

    def _stringify(self, transform: StringifyTransform) -> str:
        res = []
        res.append(transform(self.exprs[0]))
        for i in range(1, len(self.exprs)):
            res.append(' ')
            res.append(self.ops[i - 1])
            res.append(' ')
            res.append(transform(self.exprs[i]))
        return ''.join(res)

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        self.exprs[0].describe_signature(signode, mode, env, symbol)
        for i in range(1, len(self.exprs)):
            signode.append(nodes.Text(' '))
            signode.append(nodes.Text(self.ops[i - 1]))
            signode.append(nodes.Text(' '))
            self.exprs[i].describe_signature(signode, mode, env, symbol)


class ASTAssignmentExpr(ASTExpression):
    def __init__(self, exprs: List[ASTExpression], ops: List[str]):
        assert len(exprs) > 0
        assert len(exprs) == len(ops) + 1
        self.exprs = exprs
        self.ops = ops

    def _stringify(self, transform: StringifyTransform) -> str:
        res = []
        res.append(transform(self.exprs[0]))
        for i in range(1, len(self.exprs)):
            res.append(' ')
            res.append(self.ops[i - 1])
            res.append(' ')
            res.append(transform(self.exprs[i]))
        return ''.join(res)

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        self.exprs[0].describe_signature(signode, mode, env, symbol)
        for i in range(1, len(self.exprs)):
            signode.append(nodes.Text(' '))
            signode.append(nodes.Text(self.ops[i - 1]))
            signode.append(nodes.Text(' '))
            self.exprs[i].describe_signature(signode, mode, env, symbol)


class ASTFallbackExpr(ASTExpression):
    def __init__(self, expr: str):
        self.expr = expr

    def _stringify(self, transform: StringifyTransform) -> str:
        return self.expr

    def get_id(self, version: int) -> str:
        return str(self.expr)

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        signode += nodes.Text(self.expr)


################################################################################
# Types
################################################################################

class ASTTrailingTypeSpec(ASTBase):
    pass


class ASTTrailingTypeSpecFundamental(ASTTrailingTypeSpec):
    def __init__(self, name: str) -> None:
        self.name = name

    def _stringify(self, transform: StringifyTransform) -> str:
        return self.name

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        signode += nodes.Text(str(self.name))


class ASTTrailingTypeSpecName(ASTTrailingTypeSpec):
    def __init__(self, prefix: str, nestedName: ASTNestedName) -> None:
        self.prefix = prefix
        self.nestedName = nestedName

    @property
    def name(self) -> ASTNestedName:
        return self.nestedName

    def _stringify(self, transform: StringifyTransform) -> str:
        res = []
        if self.prefix:
            res.append(self.prefix)
            res.append(' ')
        res.append(transform(self.nestedName))
        return ''.join(res)

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        if self.prefix:
            signode += addnodes.desc_annotation(self.prefix, self.prefix)
            signode += nodes.Text(' ')
        self.nestedName.describe_signature(signode, mode, env, symbol=symbol)


class ASTFunctionParameter(ASTBase):
    def __init__(self, arg: "ASTTypeWithInit", ellipsis: bool = False) -> None:
        self.arg = arg
        self.ellipsis = ellipsis

    def _stringify(self, transform: StringifyTransform) -> str:
        if self.ellipsis:
            return '...'
        else:
            return transform(self.arg)

    def describe_signature(self, signode: Any, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        verify_description_mode(mode)
        if self.ellipsis:
            signode += nodes.Text('...')
        else:
            self.arg.describe_signature(signode, mode, env, symbol=symbol)


class ASTParameters(ASTBase):
    def __init__(self, args: List[ASTFunctionParameter]) -> None:
        self.args = args

    @property
    def function_params(self) -> List[ASTFunctionParameter]:
        return self.args

    def _stringify(self, transform: StringifyTransform) -> str:
        res = []
        res.append('(')
        first = True
        for a in self.args:
            if not first:
                res.append(', ')
            first = False
            res.append(str(a))
        res.append(')')
        return ''.join(res)

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        verify_description_mode(mode)
        paramlist = addnodes.desc_parameterlist()
        for arg in self.args:
            param = addnodes.desc_parameter('', '', noemph=True)
            if mode == 'lastIsName':  # i.e., outer-function params
                arg.describe_signature(param, 'param', env, symbol=symbol)
            else:
                arg.describe_signature(param, 'markType', env, symbol=symbol)
            paramlist += param
        signode += paramlist


class ASTDeclSpecsSimple(ASTBaseBase):
    def __init__(self, storage: str, threadLocal: str, inline: bool,
                 restrict: bool, volatile: bool, const: bool, attrs: List[Any]) -> None:
        self.storage = storage
        self.threadLocal = threadLocal
        self.inline = inline
        self.restrict = restrict
        self.volatile = volatile
        self.const = const
        self.attrs = attrs

    def mergeWith(self, other: "ASTDeclSpecsSimple") -> "ASTDeclSpecsSimple":
        if not other:
            return self
        return ASTDeclSpecsSimple(self.storage or other.storage,
                                  self.threadLocal or other.threadLocal,
                                  self.inline or other.inline,
                                  self.volatile or other.volatile,
                                  self.const or other.const,
                                  self.restrict or other.restrict,
                                  self.attrs + other.attrs)

    def _stringify(self, transform: StringifyTransform) -> str:
        res = []  # type: List[str]
        res.extend(transform(attr) for attr in self.attrs)
        if self.storage:
            res.append(self.storage)
        if self.threadLocal:
            res.append(self.threadLocal)
        if self.inline:
            res.append('inline')
        if self.restrict:
            res.append('restrict')
        if self.volatile:
            res.append('volatile')
        if self.const:
            res.append('const')
        return ' '.join(res)

    def describe_signature(self, modifiers: List[Node]) -> None:
        def _add(modifiers: List[Node], text: str) -> None:
            if len(modifiers) > 0:
                modifiers.append(nodes.Text(' '))
            modifiers.append(addnodes.desc_annotation(text, text))

        for attr in self.attrs:
            if len(modifiers) > 0:
                modifiers.append(nodes.Text(' '))
            modifiers.append(attr.describe_signature(modifiers))
        if self.storage:
            _add(modifiers, self.storage)
        if self.threadLocal:
            _add(modifiers, self.threadLocal)
        if self.inline:
            _add(modifiers, 'inline')
        if self.restrict:
            _add(modifiers, 'restrict')
        if self.volatile:
            _add(modifiers, 'volatile')
        if self.const:
            _add(modifiers, 'const')


class ASTDeclSpecs(ASTBase):
    def __init__(self, outer: str,
                 leftSpecs: ASTDeclSpecsSimple,
                 rightSpecs: ASTDeclSpecsSimple,
                 trailing: ASTTrailingTypeSpec) -> None:
        # leftSpecs and rightSpecs are used for output
        # allSpecs are used for id generation TODO: remove?
        self.outer = outer
        self.leftSpecs = leftSpecs
        self.rightSpecs = rightSpecs
        self.allSpecs = self.leftSpecs.mergeWith(self.rightSpecs)
        self.trailingTypeSpec = trailing

    def _stringify(self, transform: StringifyTransform) -> str:
        res = []  # type: List[str]
        l = transform(self.leftSpecs)
        if len(l) > 0:
            res.append(l)
        if self.trailingTypeSpec:
            if len(res) > 0:
                res.append(" ")
            res.append(transform(self.trailingTypeSpec))
            r = str(self.rightSpecs)
            if len(r) > 0:
                if len(res) > 0:
                    res.append(" ")
                res.append(r)
        return "".join(res)

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        verify_description_mode(mode)
        modifiers = []  # type: List[Node]

        def _add(modifiers: List[Node], text: str) -> None:
            if len(modifiers) > 0:
                modifiers.append(nodes.Text(' '))
            modifiers.append(addnodes.desc_annotation(text, text))

        self.leftSpecs.describe_signature(modifiers)

        for m in modifiers:
            signode += m
        if self.trailingTypeSpec:
            if len(modifiers) > 0:
                signode += nodes.Text(' ')
            self.trailingTypeSpec.describe_signature(signode, mode, env,
                                                     symbol=symbol)
            modifiers = []
            self.rightSpecs.describe_signature(modifiers)
            if len(modifiers) > 0:
                signode += nodes.Text(' ')
            for m in modifiers:
                signode += m


# Declarator
################################################################################

class ASTArray(ASTBase):
    def __init__(self, static: bool, const: bool, volatile: bool, restrict: bool,
                 vla: bool, size: ASTExpression):
        self.static = static
        self.const = const
        self.volatile = volatile
        self.restrict = restrict
        self.vla = vla
        self.size = size
        if vla:
            assert size is None
        if size is not None:
            assert not vla

    def _stringify(self, transform: StringifyTransform) -> str:
        el = []
        if self.static:
            el.append('static')
        if self.restrict:
            el.append('restrict')
        if self.volatile:
            el.append('volatile')
        if self.const:
            el.append('const')
        if self.vla:
            return '[' + ' '.join(el) + '*]'
        elif self.size:
            el.append(transform(self.size))
        return '[' + ' '.join(el) + ']'

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        verify_description_mode(mode)
        signode.append(nodes.Text("["))
        addSpace = False

        def _add(signode: TextElement, text: str) -> bool:
            if addSpace:
                signode += nodes.Text(' ')
            signode += addnodes.desc_annotation(text, text)
            return True

        if self.static:
            addSpace = _add(signode, 'static')
        if self.restrict:
            addSpace = _add(signode, 'restrict')
        if self.volatile:
            addSpace = _add(signode, 'volatile')
        if self.const:
            addSpace = _add(signode, 'const')
        if self.vla:
            signode.append(nodes.Text('*'))
        elif self.size:
            if addSpace:
                signode += nodes.Text(' ')
            self.size.describe_signature(signode, mode, env, symbol)
        signode.append(nodes.Text("]"))


class ASTDeclarator(ASTBase):
    @property
    def name(self) -> ASTNestedName:
        raise NotImplementedError(repr(self))

    @property
    def function_params(self) -> List[ASTFunctionParameter]:
        raise NotImplementedError(repr(self))

    def require_space_after_declSpecs(self) -> bool:
        raise NotImplementedError(repr(self))


class ASTDeclaratorNameParam(ASTDeclarator):
    def __init__(self, declId: ASTNestedName,
                 arrayOps: List[ASTArray], param: ASTParameters) -> None:
        self.declId = declId
        self.arrayOps = arrayOps
        self.param = param

    @property
    def name(self) -> ASTNestedName:
        return self.declId

    @property
    def function_params(self) -> List[ASTFunctionParameter]:
        return self.param.function_params

    # ------------------------------------------------------------------------

    def require_space_after_declSpecs(self) -> bool:
        return self.declId is not None

    def _stringify(self, transform: StringifyTransform) -> str:
        res = []
        if self.declId:
            res.append(transform(self.declId))
        for op in self.arrayOps:
            res.append(transform(op))
        if self.param:
            res.append(transform(self.param))
        return ''.join(res)

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        verify_description_mode(mode)
        if self.declId:
            self.declId.describe_signature(signode, mode, env, symbol)
        for op in self.arrayOps:
            op.describe_signature(signode, mode, env, symbol)
        if self.param:
            self.param.describe_signature(signode, mode, env, symbol)


class ASTDeclaratorNameBitField(ASTDeclarator):
    def __init__(self, declId: ASTNestedName, size: ASTExpression):
        self.declId = declId
        self.size = size

    @property
    def name(self) -> ASTNestedName:
        return self.declId

    # ------------------------------------------------------------------------

    def require_space_after_declSpecs(self) -> bool:
        return self.declId is not None

    def _stringify(self, transform: StringifyTransform) -> str:
        res = []
        if self.declId:
            res.append(transform(self.declId))
        res.append(" : ")
        res.append(transform(self.size))
        return ''.join(res)

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        verify_description_mode(mode)
        if self.declId:
            self.declId.describe_signature(signode, mode, env, symbol)
        signode += nodes.Text(' : ', ' : ')
        self.size.describe_signature(signode, mode, env, symbol)


class ASTDeclaratorPtr(ASTDeclarator):
    def __init__(self, next: ASTDeclarator, restrict: bool, volatile: bool, const: bool,
                 attrs: Any) -> None:
        assert next
        self.next = next
        self.restrict = restrict
        self.volatile = volatile
        self.const = const
        self.attrs = attrs

    @property
    def name(self) -> ASTNestedName:
        return self.next.name

    @property
    def function_params(self) -> List[ASTFunctionParameter]:
        return self.next.function_params

    def require_space_after_declSpecs(self) -> bool:
        return self.const or self.volatile or self.restrict or \
            len(self.attrs) > 0 or \
            self.next.require_space_after_declSpecs()

    def _stringify(self, transform: StringifyTransform) -> str:
        res = ['*']
        for a in self.attrs:
            res.append(transform(a))
        if len(self.attrs) > 0 and (self.restrict or self.volatile or self.const):
            res.append(' ')
        if self.restrict:
            res.append('restrict')
        if self.volatile:
            if self.restrict:
                res.append(' ')
            res.append('volatile')
        if self.const:
            if self.restrict or self.volatile:
                res.append(' ')
            res.append('const')
        if self.const or self.volatile or self.restrict or len(self.attrs) > 0:
            if self.next.require_space_after_declSpecs():
                res.append(' ')
        res.append(transform(self.next))
        return ''.join(res)

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        verify_description_mode(mode)
        signode += nodes.Text("*")
        for a in self.attrs:
            a.describe_signature(signode)
        if len(self.attrs) > 0 and (self.restrict or self.volatile or self.const):
            signode += nodes.Text(' ')

        def _add_anno(signode: TextElement, text: str) -> None:
            signode += addnodes.desc_annotation(text, text)

        if self.restrict:
            _add_anno(signode, 'restrict')
        if self.volatile:
            if self.restrict:
                signode += nodes.Text(' ')
            _add_anno(signode, 'volatile')
        if self.const:
            if self.restrict or self.volatile:
                signode += nodes.Text(' ')
            _add_anno(signode, 'const')
        if self.const or self.volatile or self.restrict or len(self.attrs) > 0:
            if self.next.require_space_after_declSpecs():
                signode += nodes.Text(' ')
        self.next.describe_signature(signode, mode, env, symbol)


class ASTDeclaratorParen(ASTDeclarator):
    def __init__(self, inner: ASTDeclarator, next: ASTDeclarator) -> None:
        assert inner
        assert next
        self.inner = inner
        self.next = next
        # TODO: we assume the name and params are in inner

    @property
    def name(self) -> ASTNestedName:
        return self.inner.name

    @property
    def function_params(self) -> List[ASTFunctionParameter]:
        return self.inner.function_params

    def require_space_after_declSpecs(self) -> bool:
        return True

    def _stringify(self, transform: StringifyTransform) -> str:
        res = ['(']
        res.append(transform(self.inner))
        res.append(')')
        res.append(transform(self.next))
        return ''.join(res)

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        verify_description_mode(mode)
        signode += nodes.Text('(')
        self.inner.describe_signature(signode, mode, env, symbol)
        signode += nodes.Text(')')
        self.next.describe_signature(signode, "noneIsName", env, symbol)


# Initializer
################################################################################

class ASTParenExprList(ASTBaseParenExprList):
    def __init__(self, exprs: List[ASTExpression]) -> None:
        self.exprs = exprs

    def _stringify(self, transform: StringifyTransform) -> str:
        exprs = [transform(e) for e in self.exprs]
        return '(%s)' % ', '.join(exprs)

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        verify_description_mode(mode)
        signode.append(nodes.Text('('))
        first = True
        for e in self.exprs:
            if not first:
                signode.append(nodes.Text(', '))
            else:
                first = False
            e.describe_signature(signode, mode, env, symbol)
        signode.append(nodes.Text(')'))


class ASTBracedInitList(ASTBase):
    def __init__(self, exprs: List[ASTExpression], trailingComma: bool) -> None:
        self.exprs = exprs
        self.trailingComma = trailingComma

    def _stringify(self, transform: StringifyTransform) -> str:
        exprs = [transform(e) for e in self.exprs]
        trailingComma = ',' if self.trailingComma else ''
        return '{%s%s}' % (', '.join(exprs), trailingComma)

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        verify_description_mode(mode)
        signode.append(nodes.Text('{'))
        first = True
        for e in self.exprs:
            if not first:
                signode.append(nodes.Text(', '))
            else:
                first = False
            e.describe_signature(signode, mode, env, symbol)
        if self.trailingComma:
            signode.append(nodes.Text(','))
        signode.append(nodes.Text('}'))


class ASTInitializer(ASTBase):
    def __init__(self, value: Union[ASTBracedInitList, ASTExpression],
                 hasAssign: bool = True) -> None:
        self.value = value
        self.hasAssign = hasAssign

    def _stringify(self, transform: StringifyTransform) -> str:
        val = transform(self.value)
        if self.hasAssign:
            return ' = ' + val
        else:
            return val

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        verify_description_mode(mode)
        if self.hasAssign:
            signode.append(nodes.Text(' = '))
        self.value.describe_signature(signode, 'markType', env, symbol)


class ASTType(ASTBase):
    def __init__(self, declSpecs: ASTDeclSpecs, decl: ASTDeclarator) -> None:
        assert declSpecs
        assert decl
        self.declSpecs = declSpecs
        self.decl = decl

    @property
    def name(self) -> ASTNestedName:
        return self.decl.name

    @property
    def function_params(self) -> List[ASTFunctionParameter]:
        return self.decl.function_params

    def _stringify(self, transform: StringifyTransform) -> str:
        res = []
        declSpecs = transform(self.declSpecs)
        res.append(declSpecs)
        if self.decl.require_space_after_declSpecs() and len(declSpecs) > 0:
            res.append(' ')
        res.append(transform(self.decl))
        return ''.join(res)

    def get_type_declaration_prefix(self) -> str:
        if self.declSpecs.trailingTypeSpec:
            return 'typedef'
        else:
            return 'type'

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        verify_description_mode(mode)
        self.declSpecs.describe_signature(signode, 'markType', env, symbol)
        if (self.decl.require_space_after_declSpecs() and
                len(str(self.declSpecs)) > 0):
            signode += nodes.Text(' ')
        # for parameters that don't really declare new names we get 'markType',
        # this should not be propagated, but be 'noneIsName'.
        if mode == 'markType':
            mode = 'noneIsName'
        self.decl.describe_signature(signode, mode, env, symbol)


class ASTTypeWithInit(ASTBase):
    def __init__(self, type: ASTType, init: ASTInitializer) -> None:
        self.type = type
        self.init = init

    @property
    def name(self) -> ASTNestedName:
        return self.type.name

    def _stringify(self, transform: StringifyTransform) -> str:
        res = []
        res.append(transform(self.type))
        if self.init:
            res.append(transform(self.init))
        return ''.join(res)

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        verify_description_mode(mode)
        self.type.describe_signature(signode, mode, env, symbol)
        if self.init:
            self.init.describe_signature(signode, mode, env, symbol)


class ASTMacroParameter(ASTBase):
    def __init__(self, arg: ASTNestedName, ellipsis: bool = False) -> None:
        self.arg = arg
        self.ellipsis = ellipsis

    def _stringify(self, transform: StringifyTransform) -> str:
        if self.ellipsis:
            return '...'
        else:
            return transform(self.arg)

    def describe_signature(self, signode: Any, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        verify_description_mode(mode)
        if self.ellipsis:
            signode += nodes.Text('...')
        else:
            self.arg.describe_signature(signode, mode, env, symbol=symbol)


class ASTMacro(ASTBase):
    def __init__(self, ident: ASTNestedName, args: List[ASTMacroParameter]) -> None:
        self.ident = ident
        self.args = args

    @property
    def name(self) -> ASTNestedName:
        return self.ident

    def _stringify(self, transform: StringifyTransform) -> str:
        res = []
        res.append(transform(self.ident))
        if self.args is not None:
            res.append('(')
            first = True
            for arg in self.args:
                if not first:
                    res.append(', ')
                first = False
                res.append(transform(arg))
            res.append(')')
        return ''.join(res)

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        verify_description_mode(mode)
        self.ident.describe_signature(signode, mode, env, symbol)
        if self.args is None:
            return
        paramlist = addnodes.desc_parameterlist()
        for arg in self.args:
            param = addnodes.desc_parameter('', '', noemph=True)
            arg.describe_signature(param, 'param', env, symbol=symbol)
            paramlist += param
        signode += paramlist


class ASTStruct(ASTBase):
    def __init__(self, name: ASTNestedName) -> None:
        self.name = name

    def get_id(self, version: int, objectType: str, symbol: "Symbol") -> str:
        return symbol.get_full_nested_name().get_id(version)

    def _stringify(self, transform: StringifyTransform) -> str:
        return transform(self.name)

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        verify_description_mode(mode)
        self.name.describe_signature(signode, mode, env, symbol=symbol)


class ASTUnion(ASTBase):
    def __init__(self, name: ASTNestedName) -> None:
        self.name = name

    def get_id(self, version: int, objectType: str, symbol: "Symbol") -> str:
        return symbol.get_full_nested_name().get_id(version)

    def _stringify(self, transform: StringifyTransform) -> str:
        return transform(self.name)

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        verify_description_mode(mode)
        self.name.describe_signature(signode, mode, env, symbol=symbol)


class ASTEnum(ASTBase):
    def __init__(self, name: ASTNestedName) -> None:
        self.name = name

    def get_id(self, version: int, objectType: str, symbol: "Symbol") -> str:
        return symbol.get_full_nested_name().get_id(version)

    def _stringify(self, transform: StringifyTransform) -> str:
        return transform(self.name)

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        verify_description_mode(mode)
        self.name.describe_signature(signode, mode, env, symbol=symbol)


class ASTEnumerator(ASTBase):
    def __init__(self, name: ASTNestedName, init: ASTInitializer) -> None:
        self.name = name
        self.init = init

    def get_id(self, version: int, objectType: str, symbol: "Symbol") -> str:
        return symbol.get_full_nested_name().get_id(version)

    def _stringify(self, transform: StringifyTransform) -> str:
        res = []
        res.append(transform(self.name))
        if self.init:
            res.append(transform(self.init))
        return ''.join(res)

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", symbol: "Symbol") -> None:
        verify_description_mode(mode)
        self.name.describe_signature(signode, mode, env, symbol)
        if self.init:
            self.init.describe_signature(signode, 'markType', env, symbol)


class ASTDeclaration(ASTBaseBase):
    def __init__(self, objectType: str, directiveType: str, declaration: Any,
                 semicolon: bool = False) -> None:
        self.objectType = objectType
        self.directiveType = directiveType
        self.declaration = declaration
        self.semicolon = semicolon

        self.symbol = None  # type: Symbol
        # set by CObject._add_enumerator_to_parent
        self.enumeratorScopedSymbol = None  # type: Symbol

    @property
    def name(self) -> ASTNestedName:
        return self.declaration.name

    @property
    def function_params(self) -> List[ASTFunctionParameter]:
        if self.objectType != 'function':
            return None
        return self.declaration.function_params

    def get_id(self, version: int, prefixed: bool = True) -> str:
        if self.objectType == 'enumerator' and self.enumeratorScopedSymbol:
            return self.enumeratorScopedSymbol.declaration.get_id(version, prefixed)
        id_ = self.symbol.get_full_nested_name().get_id(version)
        if prefixed:
            return _id_prefix[version] + id_
        else:
            return id_

    def get_newest_id(self) -> str:
        return self.get_id(_max_id, True)

    def _stringify(self, transform: StringifyTransform) -> str:
        res = transform(self.declaration)
        if self.semicolon:
            res += ';'
        return res

    def describe_signature(self, signode: TextElement, mode: str,
                           env: "BuildEnvironment", options: Dict) -> None:
        verify_description_mode(mode)
        assert self.symbol
        # The caller of the domain added a desc_signature node.
        # Always enable multiline:
        signode['is_multiline'] = True
        # Put each line in a desc_signature_line node.
        mainDeclNode = addnodes.desc_signature_line()
        mainDeclNode.sphinx_line_type = 'declarator'
        mainDeclNode['add_permalink'] = not self.symbol.isRedeclaration
        signode += mainDeclNode

        if self.objectType == 'member':
            pass
        elif self.objectType == 'function':
            pass
        elif self.objectType == 'macro':
            pass
        elif self.objectType == 'struct':
            mainDeclNode += addnodes.desc_annotation('struct ', 'struct ')
        elif self.objectType == 'union':
            mainDeclNode += addnodes.desc_annotation('union ', 'union ')
        elif self.objectType == 'enum':
            mainDeclNode += addnodes.desc_annotation('enum ', 'enum ')
        elif self.objectType == 'enumerator':
            mainDeclNode += addnodes.desc_annotation('enumerator ', 'enumerator ')
        elif self.objectType == 'type':
            prefix = self.declaration.get_type_declaration_prefix()
            prefix += ' '
            mainDeclNode += addnodes.desc_annotation(prefix, prefix)
        else:
            assert False
        self.declaration.describe_signature(mainDeclNode, mode, env, self.symbol)
        if self.semicolon:
            mainDeclNode += nodes.Text(';')


class SymbolLookupResult:
    def __init__(self, symbols: Iterator["Symbol"], parentSymbol: "Symbol",
                 ident: ASTIdentifier) -> None:
        self.symbols = symbols
        self.parentSymbol = parentSymbol
        self.ident = ident


class LookupKey:
    def __init__(self, data: List[Tuple[ASTIdentifier, str]]) -> None:
        self.data = data

    def __str__(self) -> str:
        return '[{}]'.format(', '.join("({}, {})".format(
            ident, id_) for ident, id_ in self.data))


class Symbol:
    debug_indent = 0
    debug_indent_string = "  "
    debug_lookup = False
    debug_show_tree = False

    @staticmethod
    def debug_print(*args: Any) -> None:
        print(Symbol.debug_indent_string * Symbol.debug_indent, end="")
        print(*args)

    def _assert_invariants(self) -> None:
        if not self.parent:
            # parent == None means global scope, so declaration means a parent
            assert not self.declaration
            assert not self.docname
        else:
            if self.declaration:
                assert self.docname

    def __setattr__(self, key: str, value: Any) -> None:
        if key == "children":
            assert False
        else:
            return super().__setattr__(key, value)

    def __init__(self, parent: "Symbol", ident: ASTIdentifier,
                 declaration: ASTDeclaration, docname: str) -> None:
        self.parent = parent
        # declarations in a single directive are linked together
        self.siblingAbove = None  # type: Symbol
        self.siblingBelow = None  # type: Symbol
        self.ident = ident
        self.declaration = declaration
        self.docname = docname
        self.isRedeclaration = False
        self._assert_invariants()

        # Remember to modify Symbol.remove if modifications to the parent change.
        self._children = []  # type: List[Symbol]
        self._anonChildren = []  # type: List[Symbol]
        # note: _children includes _anonChildren
        if self.parent:
            self.parent._children.append(self)
        if self.declaration:
            self.declaration.symbol = self

        # Do symbol addition after self._children has been initialised.
        self._add_function_params()

    def _fill_empty(self, declaration: ASTDeclaration, docname: str) -> None:
        self._assert_invariants()
        assert not self.declaration
        assert not self.docname
        assert declaration
        assert docname
        self.declaration = declaration
        self.declaration.symbol = self
        self.docname = docname
        self._assert_invariants()
        # and symbol addition should be done as well
        self._add_function_params()

    def _add_function_params(self) -> None:
        if Symbol.debug_lookup:
            Symbol.debug_indent += 1
            Symbol.debug_print("_add_function_params:")
        # Note: we may be called from _fill_empty, so the symbols we want
        #       to add may actually already be present (as empty symbols).

        # add symbols for function parameters, if any
        if self.declaration is not None and self.declaration.function_params is not None:
            for p in self.declaration.function_params:
                if p.arg is None:
                    continue
                nn = p.arg.name
                if nn is None:
                    continue
                # (comparing to the template params: we have checked that we are a declaration)
                decl = ASTDeclaration('functionParam', None, p)
                assert not nn.rooted
                assert len(nn.names) == 1
                self._add_symbols(nn, decl, self.docname)
        if Symbol.debug_lookup:
            Symbol.debug_indent -= 1

    def remove(self) -> None:
        if self.parent is None:
            return
        assert self in self.parent._children
        self.parent._children.remove(self)
        self.parent = None

    def clear_doc(self, docname: str) -> None:
        newChildren = []  # type: List[Symbol]
        for sChild in self._children:
            sChild.clear_doc(docname)
            if sChild.declaration and sChild.docname == docname:
                sChild.declaration = None
                sChild.docname = None
                if sChild.siblingAbove is not None:
                    sChild.siblingAbove.siblingBelow = sChild.siblingBelow
                if sChild.siblingBelow is not None:
                    sChild.siblingBelow.siblingAbove = sChild.siblingAbove
                sChild.siblingAbove = None
                sChild.siblingBelow = None
            newChildren.append(sChild)
        self._children = newChildren

    def get_all_symbols(self) -> Iterator["Symbol"]:
        yield self
        for sChild in self._children:
            for s in sChild.get_all_symbols():
                yield s

    @property
    def children_recurse_anon(self) -> Iterator["Symbol"]:
        for c in self._children:
            yield c
            if not c.ident.is_anon():
                continue
            yield from c.children_recurse_anon

    def get_lookup_key(self) -> "LookupKey":
        # The pickle files for the environment and for each document are distinct.
        # The environment has all the symbols, but the documents has xrefs that
        # must know their scope. A lookup key is essentially a specification of
        # how to find a specific symbol.
        symbols = []
        s = self
        while s.parent:
            symbols.append(s)
            s = s.parent
        symbols.reverse()
        key = []
        for s in symbols:
            if s.declaration is not None:
                # TODO: do we need the ID?
                key.append((s.ident, s.declaration.get_newest_id()))
            else:
                key.append((s.ident, None))
        return LookupKey(key)

    def get_full_nested_name(self) -> ASTNestedName:
        symbols = []
        s = self
        while s.parent:
            symbols.append(s)
            s = s.parent
        symbols.reverse()
        names = []
        for s in symbols:
            names.append(s.ident)
        return ASTNestedName(names, rooted=False)

    def _find_first_named_symbol(self, ident: ASTIdentifier,
                                 matchSelf: bool, recurseInAnon: bool) -> "Symbol":
        # TODO: further simplification from C++ to C
        if Symbol.debug_lookup:
            Symbol.debug_print("_find_first_named_symbol ->")
        res = self._find_named_symbols(ident, matchSelf, recurseInAnon,
                                       searchInSiblings=False)
        try:
            return next(res)
        except StopIteration:
            return None

    def _find_named_symbols(self, ident: ASTIdentifier,
                            matchSelf: bool, recurseInAnon: bool,
                            searchInSiblings: bool) -> Iterator["Symbol"]:
        # TODO: further simplification from C++ to C
        if Symbol.debug_lookup:
            Symbol.debug_indent += 1
            Symbol.debug_print("_find_named_symbols:")
            Symbol.debug_indent += 1
            Symbol.debug_print("self:")
            print(self.to_string(Symbol.debug_indent + 1), end="")
            Symbol.debug_print("ident:            ", ident)
            Symbol.debug_print("matchSelf:        ", matchSelf)
            Symbol.debug_print("recurseInAnon:    ", recurseInAnon)
            Symbol.debug_print("searchInSiblings: ", searchInSiblings)

        def candidates() -> Generator["Symbol", None, None]:
            s = self
            if Symbol.debug_lookup:
                Symbol.debug_print("searching in self:")
                print(s.to_string(Symbol.debug_indent + 1), end="")
            while True:
                if matchSelf:
                    yield s
                if recurseInAnon:
                    yield from s.children_recurse_anon
                else:
                    yield from s._children

                if s.siblingAbove is None:
                    break
                s = s.siblingAbove
                if Symbol.debug_lookup:
                    Symbol.debug_print("searching in sibling:")
                    print(s.to_string(Symbol.debug_indent + 1), end="")

        for s in candidates():
            if Symbol.debug_lookup:
                Symbol.debug_print("candidate:")
                print(s.to_string(Symbol.debug_indent + 1), end="")
            if s.ident == ident:
                if Symbol.debug_lookup:
                    Symbol.debug_indent += 1
                    Symbol.debug_print("matches")
                    Symbol.debug_indent -= 3
                yield s
                if Symbol.debug_lookup:
                    Symbol.debug_indent += 2
        if Symbol.debug_lookup:
            Symbol.debug_indent -= 2

    def _symbol_lookup(self, nestedName: ASTNestedName,
                       onMissingQualifiedSymbol: Callable[["Symbol", ASTIdentifier], "Symbol"],  # NOQA
                       ancestorLookupType: str, matchSelf: bool,
                       recurseInAnon: bool, searchInSiblings: bool) -> SymbolLookupResult:
        # TODO: further simplification from C++ to C
        # ancestorLookupType: if not None, specifies the target type of the lookup
        if Symbol.debug_lookup:
            Symbol.debug_indent += 1
            Symbol.debug_print("_symbol_lookup:")
            Symbol.debug_indent += 1
            Symbol.debug_print("self:")
            print(self.to_string(Symbol.debug_indent + 1), end="")
            Symbol.debug_print("nestedName:        ", nestedName)
            Symbol.debug_print("ancestorLookupType:", ancestorLookupType)
            Symbol.debug_print("matchSelf:         ", matchSelf)
            Symbol.debug_print("recurseInAnon:     ", recurseInAnon)
            Symbol.debug_print("searchInSiblings:  ", searchInSiblings)

        names = nestedName.names

        # find the right starting point for lookup
        parentSymbol = self
        if nestedName.rooted:
            while parentSymbol.parent:
                parentSymbol = parentSymbol.parent
        if ancestorLookupType is not None:
            # walk up until we find the first identifier
            firstName = names[0]
            while parentSymbol.parent:
                if parentSymbol.find_identifier(firstName,
                                                matchSelf=matchSelf,
                                                recurseInAnon=recurseInAnon,
                                                searchInSiblings=searchInSiblings):
                    break
                parentSymbol = parentSymbol.parent

        if Symbol.debug_lookup:
            Symbol.debug_print("starting point:")
            print(parentSymbol.to_string(Symbol.debug_indent + 1), end="")

        # and now the actual lookup
        for ident in names[:-1]:
            symbol = parentSymbol._find_first_named_symbol(
                ident, matchSelf=matchSelf, recurseInAnon=recurseInAnon)
            if symbol is None:
                symbol = onMissingQualifiedSymbol(parentSymbol, ident)
                if symbol is None:
                    if Symbol.debug_lookup:
                        Symbol.debug_indent -= 2
                    return None
            # We have now matched part of a nested name, and need to match more
            # so even if we should matchSelf before, we definitely shouldn't
            # even more. (see also issue #2666)
            matchSelf = False
            parentSymbol = symbol

        if Symbol.debug_lookup:
            Symbol.debug_print("handle last name from:")
            print(parentSymbol.to_string(Symbol.debug_indent + 1), end="")

        # handle the last name
        ident = names[-1]

        symbols = parentSymbol._find_named_symbols(
            ident, matchSelf=matchSelf,
            recurseInAnon=recurseInAnon,
            searchInSiblings=searchInSiblings)
        if Symbol.debug_lookup:
            symbols = list(symbols)  # type: ignore
            Symbol.debug_indent -= 2
        return SymbolLookupResult(symbols, parentSymbol, ident)

    def _add_symbols(self, nestedName: ASTNestedName,
                     declaration: ASTDeclaration, docname: str) -> "Symbol":
        # TODO: further simplification from C++ to C
        # Used for adding a whole path of symbols, where the last may or may not
        # be an actual declaration.

        if Symbol.debug_lookup:
            Symbol.debug_indent += 1
            Symbol.debug_print("_add_symbols:")
            Symbol.debug_indent += 1
            Symbol.debug_print("nn:    ", nestedName)
            Symbol.debug_print("decl:  ", declaration)
            Symbol.debug_print("doc:   ", docname)

        def onMissingQualifiedSymbol(parentSymbol: "Symbol", ident: ASTIdentifier) -> "Symbol":
            if Symbol.debug_lookup:
                Symbol.debug_indent += 1
                Symbol.debug_print("_add_symbols, onMissingQualifiedSymbol:")
                Symbol.debug_indent += 1
                Symbol.debug_print("ident: ", ident)
                Symbol.debug_indent -= 2
            return Symbol(parent=parentSymbol, ident=ident,
                          declaration=None, docname=None)

        lookupResult = self._symbol_lookup(nestedName,
                                           onMissingQualifiedSymbol,
                                           ancestorLookupType=None,
                                           matchSelf=False,
                                           recurseInAnon=False,
                                           searchInSiblings=False)
        assert lookupResult is not None  # we create symbols all the way, so that can't happen
        symbols = list(lookupResult.symbols)
        if len(symbols) == 0:
            if Symbol.debug_lookup:
                Symbol.debug_print("_add_symbols, result, no symbol:")
                Symbol.debug_indent += 1
                Symbol.debug_print("ident:       ", lookupResult.ident)
                Symbol.debug_print("declaration: ", declaration)
                Symbol.debug_print("docname:     ", docname)
                Symbol.debug_indent -= 1
            symbol = Symbol(parent=lookupResult.parentSymbol,
                            ident=lookupResult.ident,
                            declaration=declaration,
                            docname=docname)
            if Symbol.debug_lookup:
                Symbol.debug_indent -= 2
            return symbol

        if Symbol.debug_lookup:
            Symbol.debug_print("_add_symbols, result, symbols:")
            Symbol.debug_indent += 1
            Symbol.debug_print("number symbols:", len(symbols))
            Symbol.debug_indent -= 1

        if not declaration:
            if Symbol.debug_lookup:
                Symbol.debug_print("no delcaration")
                Symbol.debug_indent -= 2
            # good, just a scope creation
            # TODO: what if we have more than one symbol?
            return symbols[0]

        noDecl = []
        withDecl = []
        dupDecl = []
        for s in symbols:
            if s.declaration is None:
                noDecl.append(s)
            elif s.isRedeclaration:
                dupDecl.append(s)
            else:
                withDecl.append(s)
        if Symbol.debug_lookup:
            Symbol.debug_print("#noDecl:  ", len(noDecl))
            Symbol.debug_print("#withDecl:", len(withDecl))
            Symbol.debug_print("#dupDecl: ", len(dupDecl))

        # With partial builds we may start with a large symbol tree stripped of declarations.
        # Essentially any combination of noDecl, withDecl, and dupDecls seems possible.
        # TODO: make partial builds fully work. What should happen when the primary symbol gets
        #  deleted, and other duplicates exist? The full document should probably be rebuild.

        # First check if one of those with a declaration matches.
        # If it's a function, we need to compare IDs,
        # otherwise there should be only one symbol with a declaration.
        def makeCandSymbol() -> "Symbol":
            if Symbol.debug_lookup:
                Symbol.debug_print("begin: creating candidate symbol")
            symbol = Symbol(parent=lookupResult.parentSymbol,
                            ident=lookupResult.ident,
                            declaration=declaration,
                            docname=docname)
            if Symbol.debug_lookup:
                Symbol.debug_print("end:   creating candidate symbol")
            return symbol

        if len(withDecl) == 0:
            candSymbol = None
        else:
            candSymbol = makeCandSymbol()

            def handleDuplicateDeclaration(symbol: "Symbol", candSymbol: "Symbol") -> None:
                if Symbol.debug_lookup:
                    Symbol.debug_indent += 1
                    Symbol.debug_print("redeclaration")
                    Symbol.debug_indent -= 1
                    Symbol.debug_indent -= 2
                # Redeclaration of the same symbol.
                # Let the new one be there, but raise an error to the client
                # so it can use the real symbol as subscope.
                # This will probably result in a duplicate id warning.
                candSymbol.isRedeclaration = True
                raise _DuplicateSymbolError(symbol, declaration)

            if declaration.objectType != "function":
                assert len(withDecl) <= 1
                handleDuplicateDeclaration(withDecl[0], candSymbol)
                # (not reachable)

            # a function, so compare IDs
            candId = declaration.get_newest_id()
            if Symbol.debug_lookup:
                Symbol.debug_print("candId:", candId)
            for symbol in withDecl:
                oldId = symbol.declaration.get_newest_id()
                if Symbol.debug_lookup:
                    Symbol.debug_print("oldId: ", oldId)
                if candId == oldId:
                    handleDuplicateDeclaration(symbol, candSymbol)
                    # (not reachable)
            # no candidate symbol found with matching ID
        # if there is an empty symbol, fill that one
        if len(noDecl) == 0:
            if Symbol.debug_lookup:
                Symbol.debug_print("no match, no empty, candSybmol is not None?:", candSymbol is not None)  # NOQA
                Symbol.debug_indent -= 2
            if candSymbol is not None:
                return candSymbol
            else:
                return makeCandSymbol()
        else:
            if Symbol.debug_lookup:
                Symbol.debug_print(
                    "no match, but fill an empty declaration, candSybmol is not None?:",
                    candSymbol is not None)  # NOQA
                Symbol.debug_indent -= 2
            if candSymbol is not None:
                candSymbol.remove()
            # assert len(noDecl) == 1
            # TODO: enable assertion when we at some point find out how to do cleanup
            # for now, just take the first one, it should work fine ... right?
            symbol = noDecl[0]
            # If someone first opened the scope, and then later
            # declares it, e.g,
            # .. namespace:: Test
            # .. namespace:: nullptr
            # .. class:: Test
            symbol._fill_empty(declaration, docname)
            return symbol

    def merge_with(self, other: "Symbol", docnames: List[str],
                   env: "BuildEnvironment") -> None:
        if Symbol.debug_lookup:
            Symbol.debug_indent += 1
            Symbol.debug_print("merge_with:")
        assert other is not None
        for otherChild in other._children:
            ourChild = self._find_first_named_symbol(
                ident=otherChild.ident, matchSelf=False,
                recurseInAnon=False)
            if ourChild is None:
                # TODO: hmm, should we prune by docnames?
                self._children.append(otherChild)
                otherChild.parent = self
                otherChild._assert_invariants()
                continue
            if otherChild.declaration and otherChild.docname in docnames:
                if not ourChild.declaration:
                    ourChild._fill_empty(otherChild.declaration, otherChild.docname)
                elif ourChild.docname != otherChild.docname:
                    name = str(ourChild.declaration)
                    msg = __("Duplicate C declaration, also defined in '%s'.\n"
                             "Declaration is '%s'.")
                    msg = msg % (ourChild.docname, name)
                    logger.warning(msg, location=otherChild.docname)
                else:
                    # Both have declarations, and in the same docname.
                    # This can apparently happen, it should be safe to
                    # just ignore it, right?
                    pass
            ourChild.merge_with(otherChild, docnames, env)
        if Symbol.debug_lookup:
            Symbol.debug_indent -= 1

    def add_name(self, nestedName: ASTNestedName) -> "Symbol":
        if Symbol.debug_lookup:
            Symbol.debug_indent += 1
            Symbol.debug_print("add_name:")
        res = self._add_symbols(nestedName, declaration=None, docname=None)
        if Symbol.debug_lookup:
            Symbol.debug_indent -= 1
        return res

    def add_declaration(self, declaration: ASTDeclaration, docname: str) -> "Symbol":
        if Symbol.debug_lookup:
            Symbol.debug_indent += 1
            Symbol.debug_print("add_declaration:")
        assert declaration
        assert docname
        nestedName = declaration.name
        res = self._add_symbols(nestedName, declaration, docname)
        if Symbol.debug_lookup:
            Symbol.debug_indent -= 1
        return res

    def find_identifier(self, ident: ASTIdentifier,
                        matchSelf: bool, recurseInAnon: bool, searchInSiblings: bool
                        ) -> "Symbol":
        if Symbol.debug_lookup:
            Symbol.debug_indent += 1
            Symbol.debug_print("find_identifier:")
            Symbol.debug_indent += 1
            Symbol.debug_print("ident:           ", ident)
            Symbol.debug_print("matchSelf:       ", matchSelf)
            Symbol.debug_print("recurseInAnon:   ", recurseInAnon)
            Symbol.debug_print("searchInSiblings:", searchInSiblings)
            print(self.to_string(Symbol.debug_indent + 1), end="")
            Symbol.debug_indent -= 2
        current = self
        while current is not None:
            if Symbol.debug_lookup:
                Symbol.debug_indent += 2
                Symbol.debug_print("trying:")
                print(current.to_string(Symbol.debug_indent + 1), end="")
                Symbol.debug_indent -= 2
            if matchSelf and current.ident == ident:
                return current
            children = current.children_recurse_anon if recurseInAnon else current._children
            for s in children:
                if s.ident == ident:
                    return s
            if not searchInSiblings:
                break
            current = current.siblingAbove
        return None

    def direct_lookup(self, key: "LookupKey") -> "Symbol":
        if Symbol.debug_lookup:
            Symbol.debug_indent += 1
            Symbol.debug_print("direct_lookup:")
            Symbol.debug_indent += 1
        s = self
        for name, id_ in key.data:
            res = None
            for cand in s._children:
                if cand.ident == name:
                    res = cand
                    break
            s = res
            if Symbol.debug_lookup:
                Symbol.debug_print("name:          ", name)
                Symbol.debug_print("id:            ", id_)
                if s is not None:
                    print(s.to_string(Symbol.debug_indent + 1), end="")
                else:
                    Symbol.debug_print("not found")
            if s is None:
                if Symbol.debug_lookup:
                    Symbol.debug_indent -= 2
                return None
        if Symbol.debug_lookup:
            Symbol.debug_indent -= 2
        return s

    def find_declaration(self, nestedName: ASTNestedName, typ: str,
                         matchSelf: bool, recurseInAnon: bool) -> "Symbol":
        # templateShorthand: missing template parameter lists for templates is ok
        if Symbol.debug_lookup:
            Symbol.debug_indent += 1
            Symbol.debug_print("find_declaration:")

        def onMissingQualifiedSymbol(parentSymbol: "Symbol",
                                     ident: ASTIdentifier) -> "Symbol":
            return None

        lookupResult = self._symbol_lookup(nestedName,
                                           onMissingQualifiedSymbol,
                                           ancestorLookupType=typ,
                                           matchSelf=matchSelf,
                                           recurseInAnon=recurseInAnon,
                                           searchInSiblings=False)
        if Symbol.debug_lookup:
            Symbol.debug_indent -= 1
        if lookupResult is None:
            return None

        symbols = list(lookupResult.symbols)
        if len(symbols) == 0:
            return None
        return symbols[0]

    def to_string(self, indent: int) -> str:
        res = [Symbol.debug_indent_string * indent]
        if not self.parent:
            res.append('::')
        else:
            if self.ident:
                res.append(str(self.ident))
            else:
                res.append(str(self.declaration))
            if self.declaration:
                res.append(": ")
                if self.isRedeclaration:
                    res.append('!!duplicate!! ')
                res.append(str(self.declaration))
        if self.docname:
            res.append('\t(')
            res.append(self.docname)
            res.append(')')
        res.append('\n')
        return ''.join(res)

    def dump(self, indent: int) -> str:
        res = [self.to_string(indent)]
        for c in self._children:
            res.append(c.dump(indent + 1))
        return ''.join(res)


class DefinitionParser(BaseParser):
    # those without signedness and size modifiers
    # see https://en.cppreference.com/w/cpp/language/types
    _simple_fundamental_types = (
        'void', '_Bool', 'bool', 'char', 'int', 'float', 'double',
        '__int64',
    )

    _prefix_keys = ('struct', 'enum', 'union')

    @property
    def language(self) -> str:
        return 'C'

    @property
    def id_attributes(self):
        return self.config.c_id_attributes

    @property
    def paren_attributes(self):
        return self.config.c_paren_attributes

    def _parse_string(self) -> str:
        if self.current_char != '"':
            return None
        startPos = self.pos
        self.pos += 1
        escape = False
        while True:
            if self.eof:
                self.fail("Unexpected end during inside string.")
            elif self.current_char == '"' and not escape:
                self.pos += 1
                break
            elif self.current_char == '\\':
                escape = True
            else:
                escape = False
            self.pos += 1
        return self.definition[startPos:self.pos]

    def _parse_literal(self) -> ASTLiteral:
        # -> integer-literal
        #  | character-literal
        #  | floating-literal
        #  | string-literal
        #  | boolean-literal -> "false" | "true"
        self.skip_ws()
        if self.skip_word('true'):
            return ASTBooleanLiteral(True)
        if self.skip_word('false'):
            return ASTBooleanLiteral(False)
        pos = self.pos
        if self.match(float_literal_re):
            self.match(float_literal_suffix_re)
            return ASTNumberLiteral(self.definition[pos:self.pos])
        for regex in [binary_literal_re, hex_literal_re,
                      integer_literal_re, octal_literal_re]:
            if self.match(regex):
                self.match(integers_literal_suffix_re)
                return ASTNumberLiteral(self.definition[pos:self.pos])

        string = self._parse_string()
        if string is not None:
            return ASTStringLiteral(string)

        # character-literal
        if self.match(char_literal_re):
            prefix = self.last_match.group(1)  # may be None when no prefix
            data = self.last_match.group(2)
            try:
                return ASTCharLiteral(prefix, data)
            except UnicodeDecodeError as e:
                self.fail("Can not handle character literal. Internal error was: %s" % e)
            except UnsupportedMultiCharacterCharLiteral:
                self.fail("Can not handle character literal"
                          " resulting in multiple decoded characters.")
        return None

    def _parse_paren_expression(self) -> ASTExpression:
        # "(" expression ")"
        if self.current_char != '(':
            return None
        self.pos += 1
        res = self._parse_expression()
        self.skip_ws()
        if not self.skip_string(')'):
            self.fail("Expected ')' in end of parenthesized expression.")
        return ASTParenExpr(res)

    def _parse_primary_expression(self) -> ASTExpression:
        # literal
        # "(" expression ")"
        # id-expression -> we parse this with _parse_nested_name
        self.skip_ws()
        res = self._parse_literal()  # type: ASTExpression
        if res is not None:
            return res
        res = self._parse_paren_expression()
        if res is not None:
            return res
        nn = self._parse_nested_name()
        if nn is not None:
            return ASTIdExpression(nn)
        return None

    def _parse_initializer_list(self, name: str, open: str, close: str
                                ) -> Tuple[List[ASTExpression], bool]:
        # Parse open and close with the actual initializer-list inbetween
        # -> initializer-clause '...'[opt]
        #  | initializer-list ',' initializer-clause '...'[opt]
        # TODO: designators
        self.skip_ws()
        if not self.skip_string_and_ws(open):
            return None, None
        if self.skip_string(close):
            return [], False

        exprs = []
        trailingComma = False
        while True:
            self.skip_ws()
            expr = self._parse_expression()
            self.skip_ws()
            exprs.append(expr)
            self.skip_ws()
            if self.skip_string(close):
                break
            if not self.skip_string_and_ws(','):
                self.fail("Error in %s, expected ',' or '%s'." % (name, close))
            if self.current_char == close and close == '}':
                self.pos += 1
                trailingComma = True
                break
        return exprs, trailingComma

    def _parse_paren_expression_list(self) -> ASTParenExprList:
        # -> '(' expression-list ')'
        # though, we relax it to also allow empty parens
        # as it's needed in some cases
        #
        # expression-list
        # -> initializer-list
        exprs, trailingComma = self._parse_initializer_list("parenthesized expression-list",
                                                            '(', ')')
        if exprs is None:
            return None
        return ASTParenExprList(exprs)

    def _parse_braced_init_list(self) -> ASTBracedInitList:
        # -> '{' initializer-list ','[opt] '}'
        #  | '{' '}'
        exprs, trailingComma = self._parse_initializer_list("braced-init-list", '{', '}')
        if exprs is None:
            return None
        return ASTBracedInitList(exprs, trailingComma)

    def _parse_postfix_expression(self) -> ASTPostfixExpr:
        # -> primary
        #  | postfix "[" expression "]"
        #  | postfix "[" braced-init-list [opt] "]"
        #  | postfix "(" expression-list [opt] ")"
        #  | postfix "." id-expression
        #  | postfix "->" id-expression
        #  | postfix "++"
        #  | postfix "--"

        prefix = self._parse_primary_expression()

        # and now parse postfixes
        postFixes = []  # type: List[ASTPostfixOp]
        while True:
            self.skip_ws()
            if self.skip_string_and_ws('['):
                expr = self._parse_expression()
                self.skip_ws()
                if not self.skip_string(']'):
                    self.fail("Expected ']' in end of postfix expression.")
                postFixes.append(ASTPostfixArray(expr))
                continue
            if self.skip_string('.'):
                if self.skip_string('*'):
                    # don't steal the dot
                    self.pos -= 2
                elif self.skip_string('..'):
                    # don't steal the dot
                    self.pos -= 3
                else:
                    name = self._parse_nested_name()
                    postFixes.append(ASTPostfixMember(name))
                    continue
            if self.skip_string('->'):
                if self.skip_string('*'):
                    # don't steal the arrow
                    self.pos -= 3
                else:
                    name = self._parse_nested_name()
                    postFixes.append(ASTPostfixMemberOfPointer(name))
                    continue
            if self.skip_string('++'):
                postFixes.append(ASTPostfixInc())
                continue
            if self.skip_string('--'):
                postFixes.append(ASTPostfixDec())
                continue
            lst = self._parse_paren_expression_list()
            if lst is not None:
                postFixes.append(ASTPostfixCallExpr(lst))
                continue
            break
        return ASTPostfixExpr(prefix, postFixes)

    def _parse_unary_expression(self) -> ASTExpression:
        # -> postfix
        #  | "++" cast
        #  | "--" cast
        #  | unary-operator cast -> (* | & | + | - | ! | ~) cast
        # The rest:
        #  | "sizeof" unary
        #  | "sizeof" "(" type-id ")"
        #  | "alignof" "(" type-id ")"
        self.skip_ws()
        for op in _expression_unary_ops:
            # TODO: hmm, should we be able to backtrack here?
            if op[0] in 'cn':
                res = self.skip_word(op)
            else:
                res = self.skip_string(op)
            if res:
                expr = self._parse_cast_expression()
                return ASTUnaryOpExpr(op, expr)
        if self.skip_word_and_ws('sizeof'):
            if self.skip_string_and_ws('('):
                typ = self._parse_type(named=False)
                self.skip_ws()
                if not self.skip_string(')'):
                    self.fail("Expecting ')' to end 'sizeof'.")
                return ASTSizeofType(typ)
            expr = self._parse_unary_expression()
            return ASTSizeofExpr(expr)
        if self.skip_word_and_ws('alignof'):
            if not self.skip_string_and_ws('('):
                self.fail("Expecting '(' after 'alignof'.")
            typ = self._parse_type(named=False)
            self.skip_ws()
            if not self.skip_string(')'):
                self.fail("Expecting ')' to end 'alignof'.")
            return ASTAlignofExpr(typ)
        return self._parse_postfix_expression()

    def _parse_cast_expression(self) -> ASTExpression:
        # -> unary  | "(" type-id ")" cast
        pos = self.pos
        self.skip_ws()
        if self.skip_string('('):
            try:
                typ = self._parse_type(False)
                if not self.skip_string(')'):
                    self.fail("Expected ')' in cast expression.")
                expr = self._parse_cast_expression()
                return ASTCastExpr(typ, expr)
            except DefinitionError as exCast:
                self.pos = pos
                try:
                    return self._parse_unary_expression()
                except DefinitionError as exUnary:
                    errs = []
                    errs.append((exCast, "If type cast expression"))
                    errs.append((exUnary, "If unary expression"))
                    raise self._make_multi_error(errs,
                                                 "Error in cast expression.") from exUnary
        else:
            return self._parse_unary_expression()

    def _parse_logical_or_expression(self) -> ASTExpression:
        # logical-or     = logical-and      ||
        # logical-and    = inclusive-or     &&
        # inclusive-or   = exclusive-or     |
        # exclusive-or   = and              ^
        # and            = equality         &
        # equality       = relational       ==, !=
        # relational     = shift            <, >, <=, >=
        # shift          = additive         <<, >>
        # additive       = multiplicative   +, -
        # multiplicative = pm               *, /, %
        # pm             = cast             .*, ->*
        def _parse_bin_op_expr(self, opId):
            if opId + 1 == len(_expression_bin_ops):
                def parser() -> ASTExpression:
                    return self._parse_cast_expression()
            else:
                def parser() -> ASTExpression:
                    return _parse_bin_op_expr(self, opId + 1)
            exprs = []
            ops = []
            exprs.append(parser())
            while True:
                self.skip_ws()
                pos = self.pos
                oneMore = False
                for op in _expression_bin_ops[opId]:
                    if op[0] in 'abcnox':
                        if not self.skip_word(op):
                            continue
                    else:
                        if not self.skip_string(op):
                            continue
                    if op == '&' and self.current_char == '&':
                        # don't split the && 'token'
                        self.pos -= 1
                        # and btw. && has lower precedence, so we are done
                        break
                    try:
                        expr = parser()
                        exprs.append(expr)
                        ops.append(op)
                        oneMore = True
                        break
                    except DefinitionError:
                        self.pos = pos
                if not oneMore:
                    break
            return ASTBinOpExpr(exprs, ops)
        return _parse_bin_op_expr(self, 0)

    def _parse_conditional_expression_tail(self, orExprHead: Any) -> ASTExpression:
        # -> "?" expression ":" assignment-expression
        return None

    def _parse_assignment_expression(self) -> ASTExpression:
        # -> conditional-expression
        #  | logical-or-expression assignment-operator initializer-clause
        # -> conditional-expression ->
        #     logical-or-expression
        #   | logical-or-expression "?" expression ":" assignment-expression
        #   | logical-or-expression assignment-operator initializer-clause
        exprs = []
        ops = []
        orExpr = self._parse_logical_or_expression()
        exprs.append(orExpr)
        # TODO: handle ternary with _parse_conditional_expression_tail
        while True:
            oneMore = False
            self.skip_ws()
            for op in _expression_assignment_ops:
                if op[0] in 'abcnox':
                    if not self.skip_word(op):
                        continue
                else:
                    if not self.skip_string(op):
                        continue
                expr = self._parse_logical_or_expression()
                exprs.append(expr)
                ops.append(op)
                oneMore = True
            if not oneMore:
                break
        return ASTAssignmentExpr(exprs, ops)

    def _parse_constant_expression(self) -> ASTExpression:
        # -> conditional-expression
        orExpr = self._parse_logical_or_expression()
        # TODO: use _parse_conditional_expression_tail
        return orExpr

    def _parse_expression(self) -> ASTExpression:
        # -> assignment-expression
        #  | expression "," assignment-expresion
        # TODO: actually parse the second production
        return self._parse_assignment_expression()

    def _parse_expression_fallback(
            self, end: List[str],
            parser: Callable[[], ASTExpression],
            allow: bool = True) -> ASTExpression:
        # Stupidly "parse" an expression.
        # 'end' should be a list of characters which ends the expression.

        # first try to use the provided parser
        prevPos = self.pos
        try:
            return parser()
        except DefinitionError as e:
            # some places (e.g., template parameters) we really don't want to use fallback,
            # and for testing we may want to globally disable it
            if not allow or not self.allowFallbackExpressionParsing:
                raise
            self.warn("Parsing of expression failed. Using fallback parser."
                      " Error was:\n%s" % e)
            self.pos = prevPos
        # and then the fallback scanning
        assert end is not None
        self.skip_ws()
        startPos = self.pos
        if self.match(_string_re):
            value = self.matched_text
        else:
            # TODO: add handling of more bracket-like things, and quote handling
            brackets = {'(': ')', '{': '}', '[': ']'}
            symbols = []  # type: List[str]
            while not self.eof:
                if (len(symbols) == 0 and self.current_char in end):
                    break
                if self.current_char in brackets.keys():
                    symbols.append(brackets[self.current_char])
                elif len(symbols) > 0 and self.current_char == symbols[-1]:
                    symbols.pop()
                self.pos += 1
            if len(end) > 0 and self.eof:
                self.fail("Could not find end of expression starting at %d."
                          % startPos)
            value = self.definition[startPos:self.pos].strip()
        return ASTFallbackExpr(value.strip())

    def _parse_nested_name(self) -> ASTNestedName:
        names = []  # type: List[Any]

        self.skip_ws()
        rooted = False
        if self.skip_string('.'):
            rooted = True
        while 1:
            self.skip_ws()
            if not self.match(identifier_re):
                self.fail("Expected identifier in nested name.")
            identifier = self.matched_text
            # make sure there isn't a keyword
            if identifier in _keywords:
                self.fail("Expected identifier in nested name, "
                          "got keyword: %s" % identifier)
            ident = ASTIdentifier(identifier)
            names.append(ident)

            self.skip_ws()
            if not self.skip_string('.'):
                break
        return ASTNestedName(names, rooted)

    def _parse_trailing_type_spec(self) -> ASTTrailingTypeSpec:
        # fundamental types
        self.skip_ws()
        for t in self._simple_fundamental_types:
            if self.skip_word(t):
                return ASTTrailingTypeSpecFundamental(t)

        # TODO: this could/should be more strict
        elements = []
        if self.skip_word_and_ws('signed'):
            elements.append('signed')
        elif self.skip_word_and_ws('unsigned'):
            elements.append('unsigned')
        while 1:
            if self.skip_word_and_ws('short'):
                elements.append('short')
            elif self.skip_word_and_ws('long'):
                elements.append('long')
            else:
                break
        if self.skip_word_and_ws('char'):
            elements.append('char')
        elif self.skip_word_and_ws('int'):
            elements.append('int')
        elif self.skip_word_and_ws('double'):
            elements.append('double')
        elif self.skip_word_and_ws('__int64'):
            elements.append('__int64')
        if len(elements) > 0:
            return ASTTrailingTypeSpecFundamental(' '.join(elements))

        # prefixed
        prefix = None
        self.skip_ws()
        for k in self._prefix_keys:
            if self.skip_word_and_ws(k):
                prefix = k
                break

        nestedName = self._parse_nested_name()
        return ASTTrailingTypeSpecName(prefix, nestedName)

    def _parse_parameters(self, paramMode: str) -> ASTParameters:
        self.skip_ws()
        if not self.skip_string('('):
            if paramMode == 'function':
                self.fail('Expecting "(" in parameters.')
            else:
                return None

        args = []
        self.skip_ws()
        if not self.skip_string(')'):
            while 1:
                self.skip_ws()
                if self.skip_string('...'):
                    args.append(ASTFunctionParameter(None, True))
                    self.skip_ws()
                    if not self.skip_string(')'):
                        self.fail('Expected ")" after "..." in parameters.')
                    break
                # note: it seems that function arguments can always be named,
                # even in function pointers and similar.
                arg = self._parse_type_with_init(outer=None, named='single')
                # TODO: parse default parameters # TODO: didn't we just do that?
                args.append(ASTFunctionParameter(arg))

                self.skip_ws()
                if self.skip_string(','):
                    continue
                elif self.skip_string(')'):
                    break
                else:
                    self.fail(
                        'Expecting "," or ")" in parameters, '
                        'got "%s".' % self.current_char)
        return ASTParameters(args)

    def _parse_decl_specs_simple(self, outer: str, typed: bool) -> ASTDeclSpecsSimple:
        """Just parse the simple ones."""
        storage = None
        threadLocal = None
        inline = None
        restrict = None
        volatile = None
        const = None
        attrs = []
        while 1:  # accept any permutation of a subset of some decl-specs
            self.skip_ws()
            if not storage:
                if outer == 'member':
                    if self.skip_word('auto'):
                        storage = 'auto'
                        continue
                    if self.skip_word('register'):
                        storage = 'register'
                        continue
                if outer in ('member', 'function'):
                    if self.skip_word('static'):
                        storage = 'static'
                        continue
                    if self.skip_word('extern'):
                        storage = 'extern'
                        continue
            if outer == 'member' and not threadLocal:
                if self.skip_word('thread_local'):
                    threadLocal = 'thread_local'
                    continue
                if self.skip_word('_Thread_local'):
                    threadLocal = '_Thread_local'
                    continue
            if outer == 'function' and not inline:
                inline = self.skip_word('inline')
                if inline:
                    continue

            if not restrict and typed:
                restrict = self.skip_word('restrict')
                if restrict:
                    continue
            if not volatile and typed:
                volatile = self.skip_word('volatile')
                if volatile:
                    continue
            if not const and typed:
                const = self.skip_word('const')
                if const:
                    continue
            attr = self._parse_attribute()
            if attr:
                attrs.append(attr)
                continue
            break
        return ASTDeclSpecsSimple(storage, threadLocal, inline,
                                  restrict, volatile, const, attrs)

    def _parse_decl_specs(self, outer: str, typed: bool = True) -> ASTDeclSpecs:
        if outer:
            if outer not in ('type', 'member', 'function'):
                raise Exception('Internal error, unknown outer "%s".' % outer)
        leftSpecs = self._parse_decl_specs_simple(outer, typed)
        rightSpecs = None

        if typed:
            trailing = self._parse_trailing_type_spec()
            rightSpecs = self._parse_decl_specs_simple(outer, typed)
        else:
            trailing = None
        return ASTDeclSpecs(outer, leftSpecs, rightSpecs, trailing)

    def _parse_declarator_name_suffix(
            self, named: Union[bool, str], paramMode: str, typed: bool
    ) -> ASTDeclarator:
        # now we should parse the name, and then suffixes
        if named == 'maybe':
            pos = self.pos
            try:
                declId = self._parse_nested_name()
            except DefinitionError:
                self.pos = pos
                declId = None
        elif named == 'single':
            if self.match(identifier_re):
                identifier = ASTIdentifier(self.matched_text)
                declId = ASTNestedName([identifier], rooted=False)
            else:
                declId = None
        elif named:
            declId = self._parse_nested_name()
        else:
            declId = None
        arrayOps = []
        while 1:
            self.skip_ws()
            if typed and self.skip_string('['):
                self.skip_ws()
                static = False
                const = False
                volatile = False
                restrict = False
                while True:
                    if not static:
                        if self.skip_word_and_ws('static'):
                            static = True
                            continue
                    if not const:
                        if self.skip_word_and_ws('const'):
                            const = True
                            continue
                    if not volatile:
                        if self.skip_word_and_ws('volatile'):
                            volatile = True
                            continue
                    if not restrict:
                        if self.skip_word_and_ws('restrict'):
                            restrict = True
                            continue
                    break
                vla = False if static else self.skip_string_and_ws('*')
                if vla:
                    if not self.skip_string(']'):
                        self.fail("Expected ']' in end of array operator.")
                    size = None
                else:
                    if self.skip_string(']'):
                        size = None
                    else:

                        def parser():
                            return self._parse_expression()
                        size = self._parse_expression_fallback([']'], parser)
                        self.skip_ws()
                        if not self.skip_string(']'):
                            self.fail("Expected ']' in end of array operator.")
                arrayOps.append(ASTArray(static, const, volatile, restrict, vla, size))
            else:
                break
        param = self._parse_parameters(paramMode)
        if param is None and len(arrayOps) == 0:
            # perhaps a bit-field
            if named and paramMode == 'type' and typed:
                self.skip_ws()
                if self.skip_string(':'):
                    size = self._parse_constant_expression()
                    return ASTDeclaratorNameBitField(declId=declId, size=size)
        return ASTDeclaratorNameParam(declId=declId, arrayOps=arrayOps,
                                      param=param)

    def _parse_declarator(self, named: Union[bool, str], paramMode: str,
                          typed: bool = True) -> ASTDeclarator:
        # 'typed' here means 'parse return type stuff'
        if paramMode not in ('type', 'function'):
            raise Exception(
                "Internal error, unknown paramMode '%s'." % paramMode)
        prevErrors = []
        self.skip_ws()
        if typed and self.skip_string('*'):
            self.skip_ws()
            restrict = False
            volatile = False
            const = False
            attrs = []
            while 1:
                if not restrict:
                    restrict = self.skip_word_and_ws('restrict')
                    if restrict:
                        continue
                if not volatile:
                    volatile = self.skip_word_and_ws('volatile')
                    if volatile:
                        continue
                if not const:
                    const = self.skip_word_and_ws('const')
                    if const:
                        continue
                attr = self._parse_attribute()
                if attr is not None:
                    attrs.append(attr)
                    continue
                break
            next = self._parse_declarator(named, paramMode, typed)
            return ASTDeclaratorPtr(next=next,
                                    restrict=restrict, volatile=volatile, const=const,
                                    attrs=attrs)
        if typed and self.current_char == '(':  # note: peeking, not skipping
            # maybe this is the beginning of params, try that first,
            # otherwise assume it's noptr->declarator > ( ptr-declarator )
            pos = self.pos
            try:
                # assume this is params
                res = self._parse_declarator_name_suffix(named, paramMode,
                                                         typed)
                return res
            except DefinitionError as exParamQual:
                msg = "If declarator-id with parameters"
                if paramMode == 'function':
                    msg += " (e.g., 'void f(int arg)')"
                prevErrors.append((exParamQual, msg))
                self.pos = pos
                try:
                    assert self.current_char == '('
                    self.skip_string('(')
                    # TODO: hmm, if there is a name, it must be in inner, right?
                    # TODO: hmm, if there must be parameters, they must b
                    # inside, right?
                    inner = self._parse_declarator(named, paramMode, typed)
                    if not self.skip_string(')'):
                        self.fail("Expected ')' in \"( ptr-declarator )\"")
                    next = self._parse_declarator(named=False,
                                                  paramMode="type",
                                                  typed=typed)
                    return ASTDeclaratorParen(inner=inner, next=next)
                except DefinitionError as exNoPtrParen:
                    self.pos = pos
                    msg = "If parenthesis in noptr-declarator"
                    if paramMode == 'function':
                        msg += " (e.g., 'void (*f(int arg))(double)')"
                    prevErrors.append((exNoPtrParen, msg))
                    header = "Error in declarator"
                    raise self._make_multi_error(prevErrors, header) from exNoPtrParen
        pos = self.pos
        try:
            return self._parse_declarator_name_suffix(named, paramMode, typed)
        except DefinitionError as e:
            self.pos = pos
            prevErrors.append((e, "If declarator-id"))
            header = "Error in declarator or parameters"
            raise self._make_multi_error(prevErrors, header) from e

    def _parse_initializer(self, outer: str = None, allowFallback: bool = True
                           ) -> ASTInitializer:
        self.skip_ws()
        if outer == 'member' and False:  # TODO
            bracedInit = self._parse_braced_init_list()
            if bracedInit is not None:
                return ASTInitializer(bracedInit, hasAssign=False)

        if not self.skip_string('='):
            return None

        bracedInit = self._parse_braced_init_list()
        if bracedInit is not None:
            return ASTInitializer(bracedInit)

        if outer == 'member':
            fallbackEnd = []  # type: List[str]
        elif outer is None:  # function parameter
            fallbackEnd = [',', ')']
        else:
            self.fail("Internal error, initializer for outer '%s' not "
                      "implemented." % outer)

        def parser():
            return self._parse_assignment_expression()

        value = self._parse_expression_fallback(fallbackEnd, parser, allow=allowFallback)
        return ASTInitializer(value)

    def _parse_type(self, named: Union[bool, str], outer: str = None) -> ASTType:
        """
        named=False|'maybe'|True: 'maybe' is e.g., for function objects which
        doesn't need to name the arguments
        """
        if outer:  # always named
            if outer not in ('type', 'member', 'function'):
                raise Exception('Internal error, unknown outer "%s".' % outer)
            assert named

        if outer == 'type':
            # We allow type objects to just be a name.
            prevErrors = []
            startPos = self.pos
            # first try without the type
            try:
                declSpecs = self._parse_decl_specs(outer=outer, typed=False)
                decl = self._parse_declarator(named=True, paramMode=outer,
                                              typed=False)
                self.assert_end(allowSemicolon=True)
            except DefinitionError as exUntyped:
                desc = "If just a name"
                prevErrors.append((exUntyped, desc))
                self.pos = startPos
                try:
                    declSpecs = self._parse_decl_specs(outer=outer)
                    decl = self._parse_declarator(named=True, paramMode=outer)
                except DefinitionError as exTyped:
                    self.pos = startPos
                    desc = "If typedef-like declaration"
                    prevErrors.append((exTyped, desc))
                    # Retain the else branch for easier debugging.
                    # TODO: it would be nice to save the previous stacktrace
                    #       and output it here.
                    if True:
                        header = "Type must be either just a name or a "
                        header += "typedef-like declaration."
                        raise self._make_multi_error(prevErrors, header) from exTyped
                    else:
                        # For testing purposes.
                        # do it again to get the proper traceback (how do you
                        # reliably save a traceback when an exception is
                        # constructed?)
                        self.pos = startPos
                        typed = True
                        declSpecs = self._parse_decl_specs(outer=outer, typed=typed)
                        decl = self._parse_declarator(named=True, paramMode=outer,
                                                      typed=typed)
        elif outer == 'function':
            declSpecs = self._parse_decl_specs(outer=outer)
            decl = self._parse_declarator(named=True, paramMode=outer)
        else:
            paramMode = 'type'
            if outer == 'member':  # i.e., member
                named = True
            declSpecs = self._parse_decl_specs(outer=outer)
            decl = self._parse_declarator(named=named, paramMode=paramMode)
        return ASTType(declSpecs, decl)

    def _parse_type_with_init(self, named: Union[bool, str], outer: str) -> ASTTypeWithInit:
        if outer:
            assert outer in ('type', 'member', 'function')
        type = self._parse_type(outer=outer, named=named)
        init = self._parse_initializer(outer=outer)
        return ASTTypeWithInit(type, init)

    def _parse_macro(self) -> ASTMacro:
        self.skip_ws()
        ident = self._parse_nested_name()
        if ident is None:
            self.fail("Expected identifier in macro definition.")
        self.skip_ws()
        if not self.skip_string_and_ws('('):
            return ASTMacro(ident, None)
        if self.skip_string(')'):
            return ASTMacro(ident, [])
        args = []
        while 1:
            self.skip_ws()
            if self.skip_string('...'):
                args.append(ASTMacroParameter(None, True))
                self.skip_ws()
                if not self.skip_string(')'):
                    self.fail('Expected ")" after "..." in macro parameters.')
                break
            if not self.match(identifier_re):
                self.fail("Expected identifier in macro parameters.")
            nn = ASTNestedName([ASTIdentifier(self.matched_text)], rooted=False)
            arg = ASTMacroParameter(nn)
            args.append(arg)
            self.skip_ws()
            if self.skip_string_and_ws(','):
                continue
            elif self.skip_string_and_ws(')'):
                break
            else:
                self.fail("Expected identifier, ')', or ',' in macro parameter list.")
        return ASTMacro(ident, args)

    def _parse_struct(self) -> ASTStruct:
        name = self._parse_nested_name()
        return ASTStruct(name)

    def _parse_union(self) -> ASTUnion:
        name = self._parse_nested_name()
        return ASTUnion(name)

    def _parse_enum(self) -> ASTEnum:
        name = self._parse_nested_name()
        return ASTEnum(name)

    def _parse_enumerator(self) -> ASTEnumerator:
        name = self._parse_nested_name()
        self.skip_ws()
        init = None
        if self.skip_string('='):
            self.skip_ws()

            def parser() -> ASTExpression:
                return self._parse_constant_expression()

            initVal = self._parse_expression_fallback([], parser)
            init = ASTInitializer(initVal)
        return ASTEnumerator(name, init)

    def parse_declaration(self, objectType: str, directiveType: str) -> ASTDeclaration:
        if objectType not in ('function', 'member',
                              'macro', 'struct', 'union', 'enum', 'enumerator', 'type'):
            raise Exception('Internal error, unknown objectType "%s".' % objectType)
        if directiveType not in ('function', 'member', 'var',
                                 'macro', 'struct', 'union', 'enum', 'enumerator', 'type'):
            raise Exception('Internal error, unknown directiveType "%s".' % directiveType)

        declaration = None  # type: Any
        if objectType == 'member':
            declaration = self._parse_type_with_init(named=True, outer='member')
        elif objectType == 'function':
            declaration = self._parse_type(named=True, outer='function')
        elif objectType == 'macro':
            declaration = self._parse_macro()
        elif objectType == 'struct':
            declaration = self._parse_struct()
        elif objectType == 'union':
            declaration = self._parse_union()
        elif objectType == 'enum':
            declaration = self._parse_enum()
        elif objectType == 'enumerator':
            declaration = self._parse_enumerator()
        elif objectType == 'type':
            declaration = self._parse_type(named=True, outer='type')
        else:
            assert False
        if objectType != 'macro':
            self.skip_ws()
            semicolon = self.skip_string(';')
        else:
            semicolon = False
        return ASTDeclaration(objectType, directiveType, declaration, semicolon)

    def parse_namespace_object(self) -> ASTNestedName:
        return self._parse_nested_name()

    def parse_xref_object(self) -> ASTNestedName:
        name = self._parse_nested_name()
        # if there are '()' left, just skip them
        self.skip_ws()
        self.skip_string('()')
        self.assert_end()
        return name

    def parse_expression(self) -> Union[ASTExpression, ASTType]:
        pos = self.pos
        res = None  # type: Union[ASTExpression, ASTType]
        try:
            res = self._parse_expression()
            self.skip_ws()
            self.assert_end()
        except DefinitionError as exExpr:
            self.pos = pos
            try:
                res = self._parse_type(False)
                self.skip_ws()
                self.assert_end()
            except DefinitionError as exType:
                header = "Error when parsing (type) expression."
                errs = []
                errs.append((exExpr, "If expression"))
                errs.append((exType, "If type"))
                raise self._make_multi_error(errs, header) from exType
        return res


def _make_phony_error_name() -> ASTNestedName:
    return ASTNestedName([ASTIdentifier("PhonyNameDueToError")], rooted=False)


class CObject(ObjectDescription):
    """
    Description of a C language object.
    """

    doc_field_types = [
        TypedField('parameter', label=_('Parameters'),
                   names=('param', 'parameter', 'arg', 'argument'),
                   typerolename='type', typenames=('type',)),
        Field('returnvalue', label=_('Returns'), has_arg=False,
              names=('returns', 'return')),
        Field('returntype', label=_('Return type'), has_arg=False,
              names=('rtype',)),
    ]

    option_spec = {
        'noindexentry': directives.flag,
    }

    def _add_enumerator_to_parent(self, ast: ASTDeclaration) -> None:
        assert ast.objectType == 'enumerator'
        # find the parent, if it exists && is an enum
        #                  then add the name to the parent scope
        symbol = ast.symbol
        assert symbol
        assert symbol.ident is not None
        parentSymbol = symbol.parent
        assert parentSymbol
        if parentSymbol.parent is None:
            # TODO: we could warn, but it is somewhat equivalent to
            # enumeratorss, without the enum
            return  # no parent
        parentDecl = parentSymbol.declaration
        if parentDecl is None:
            # the parent is not explicitly declared
            # TODO: we could warn, but?
            return
        if parentDecl.objectType != 'enum':
            # TODO: maybe issue a warning, enumerators in non-enums is weird,
            # but it is somewhat equivalent to enumeratorss, without the enum
            return
        if parentDecl.directiveType != 'enum':
            return

        targetSymbol = parentSymbol.parent
        s = targetSymbol.find_identifier(symbol.ident, matchSelf=False, recurseInAnon=True,
                                         searchInSiblings=False)
        if s is not None:
            # something is already declared with that name
            return
        declClone = symbol.declaration.clone()
        declClone.enumeratorScopedSymbol = symbol
        Symbol(parent=targetSymbol, ident=symbol.ident,
               declaration=declClone,
               docname=self.env.docname)

    def add_target_and_index(self, ast: ASTDeclaration, sig: str,
                             signode: TextElement) -> None:
        ids = []
        for i in range(1, _max_id + 1):
            try:
                id = ast.get_id(version=i)
                ids.append(id)
            except NoOldIdError:
                assert i < _max_id
        # let's keep the newest first
        ids = list(reversed(ids))
        newestId = ids[0]
        assert newestId  # shouldn't be None

        name = ast.symbol.get_full_nested_name().get_display_string().lstrip('.')
        if newestId not in self.state.document.ids:
            # always add the newest id
            assert newestId
            signode['ids'].append(newestId)
            # only add compatibility ids when there are no conflicts
            for id in ids[1:]:
                if not id:  # is None when the element didn't exist in that version
                    continue
                if id not in self.state.document.ids:
                    signode['ids'].append(id)

            self.state.document.note_explicit_target(signode)

            domain = cast(CDomain, self.env.get_domain('c'))
            if name not in domain.objects:
                domain.objects[name] = (domain.env.docname, newestId, self.objtype)

        if 'noindexentry' not in self.options:
            indexText = self.get_index_text(name)
            self.indexnode['entries'].append(('single', indexText, newestId, '', None))

    @property
    def object_type(self) -> str:
        raise NotImplementedError()

    @property
    def display_object_type(self) -> str:
        return self.object_type

    def get_index_text(self, name: str) -> str:
        return _('%s (C %s)') % (name, self.display_object_type)

    def parse_definition(self, parser: DefinitionParser) -> ASTDeclaration:
        return parser.parse_declaration(self.object_type, self.objtype)

    def describe_signature(self, signode: TextElement, ast: Any, options: Dict) -> None:
        ast.describe_signature(signode, 'lastIsName', self.env, options)

    def run(self) -> List[Node]:
        env = self.state.document.settings.env  # from ObjectDescription.run
        if 'c:parent_symbol' not in env.temp_data:
            root = env.domaindata['c']['root_symbol']
            env.temp_data['c:parent_symbol'] = root
            env.ref_context['c:parent_key'] = root.get_lookup_key()

        # When multiple declarations are made in the same directive
        # they need to know about each other to provide symbol lookup for function parameters.
        # We use last_symbol to store the latest added declaration in a directive.
        env.temp_data['c:last_symbol'] = None
        return super().run()

    def handle_signature(self, sig: str, signode: TextElement) -> ASTDeclaration:
        parentSymbol = self.env.temp_data['c:parent_symbol']  # type: Symbol

        parser = DefinitionParser(sig, location=signode, config=self.env.config)
        try:
            ast = self.parse_definition(parser)
            parser.assert_end()
        except DefinitionError as e:
            logger.warning(e, location=signode)
            # It is easier to assume some phony name than handling the error in
            # the possibly inner declarations.
            name = _make_phony_error_name()
            symbol = parentSymbol.add_name(name)
            self.env.temp_data['c:last_symbol'] = symbol
            raise ValueError from e

        try:
            symbol = parentSymbol.add_declaration(ast, docname=self.env.docname)
            # append the new declaration to the sibling list
            assert symbol.siblingAbove is None
            assert symbol.siblingBelow is None
            symbol.siblingAbove = self.env.temp_data['c:last_symbol']
            if symbol.siblingAbove is not None:
                assert symbol.siblingAbove.siblingBelow is None
                symbol.siblingAbove.siblingBelow = symbol
            self.env.temp_data['c:last_symbol'] = symbol
        except _DuplicateSymbolError as e:
            # Assume we are actually in the old symbol,
            # instead of the newly created duplicate.
            self.env.temp_data['c:last_symbol'] = e.symbol
            msg = __("Duplicate C declaration, also defined in '%s'.\n"
                     "Declaration is '%s'.")
            msg = msg % (e.symbol.docname, sig)
            logger.warning(msg, location=signode)

        if ast.objectType == 'enumerator':
            self._add_enumerator_to_parent(ast)

        # note: handle_signature may be called multiple time per directive,
        # if it has multiple signatures, so don't mess with the original options.
        options = dict(self.options)
        self.describe_signature(signode, ast, options)
        return ast

    def before_content(self) -> None:
        lastSymbol = self.env.temp_data['c:last_symbol']  # type: Symbol
        assert lastSymbol
        self.oldParentSymbol = self.env.temp_data['c:parent_symbol']
        self.oldParentKey = self.env.ref_context['c:parent_key']  # type: LookupKey
        self.env.temp_data['c:parent_symbol'] = lastSymbol
        self.env.ref_context['c:parent_key'] = lastSymbol.get_lookup_key()

    def after_content(self) -> None:
        self.env.temp_data['c:parent_symbol'] = self.oldParentSymbol
        self.env.ref_context['c:parent_key'] = self.oldParentKey

    def make_old_id(self, name: str) -> str:
        """Generate old styled node_id for C objects.

        .. note:: Old Styled node_id was used until Sphinx-3.0.
                  This will be removed in Sphinx-5.0.
        """
        return 'c.' + name


class CMemberObject(CObject):
    object_type = 'member'

    @property
    def display_object_type(self) -> str:
        # the distinction between var and member is only cosmetic
        assert self.objtype in ('member', 'var')
        return self.objtype


class CFunctionObject(CObject):
    object_type = 'function'


class CMacroObject(CObject):
    object_type = 'macro'


class CStructObject(CObject):
    object_type = 'struct'


class CUnionObject(CObject):
    object_type = 'union'


class CEnumObject(CObject):
    object_type = 'enum'


class CEnumeratorObject(CObject):
    object_type = 'enumerator'


class CTypeObject(CObject):
    object_type = 'type'


class CNamespaceObject(SphinxDirective):
    """
    This directive is just to tell Sphinx that we're documenting stuff in
    namespace foo.
    """

    has_content = False
    required_arguments = 1
    optional_arguments = 0
    final_argument_whitespace = True
    option_spec = {}  # type: Dict

    def run(self) -> List[Node]:
        rootSymbol = self.env.domaindata['c']['root_symbol']
        if self.arguments[0].strip() in ('NULL', '0', 'nullptr'):
            symbol = rootSymbol
            stack = []  # type: List[Symbol]
        else:
            parser = DefinitionParser(self.arguments[0],
                                      location=self.get_source_info(),
                                      config=self.env.config)
            try:
                name = parser.parse_namespace_object()
                parser.assert_end()
            except DefinitionError as e:
                logger.warning(e, location=self.get_source_info())
                name = _make_phony_error_name()
            symbol = rootSymbol.add_name(name)
            stack = [symbol]
        self.env.temp_data['c:parent_symbol'] = symbol
        self.env.temp_data['c:namespace_stack'] = stack
        self.env.ref_context['c:parent_key'] = symbol.get_lookup_key()
        return []


class CNamespacePushObject(SphinxDirective):
    has_content = False
    required_arguments = 1
    optional_arguments = 0
    final_argument_whitespace = True
    option_spec = {}  # type: Dict

    def run(self) -> List[Node]:
        if self.arguments[0].strip() in ('NULL', '0', 'nullptr'):
            return []
        parser = DefinitionParser(self.arguments[0],
                                  location=self.get_source_info(),
                                  config=self.env.config)
        try:
            name = parser.parse_namespace_object()
            parser.assert_end()
        except DefinitionError as e:
            logger.warning(e, location=self.get_source_info())
            name = _make_phony_error_name()
        oldParent = self.env.temp_data.get('c:parent_symbol', None)
        if not oldParent:
            oldParent = self.env.domaindata['c']['root_symbol']
        symbol = oldParent.add_name(name)
        stack = self.env.temp_data.get('c:namespace_stack', [])
        stack.append(symbol)
        self.env.temp_data['c:parent_symbol'] = symbol
        self.env.temp_data['c:namespace_stack'] = stack
        self.env.ref_context['c:parent_key'] = symbol.get_lookup_key()
        return []


class CNamespacePopObject(SphinxDirective):
    has_content = False
    required_arguments = 0
    optional_arguments = 0
    final_argument_whitespace = True
    option_spec = {}  # type: Dict

    def run(self) -> List[Node]:
        stack = self.env.temp_data.get('c:namespace_stack', None)
        if not stack or len(stack) == 0:
            logger.warning("C namespace pop on empty stack. Defaulting to gobal scope.",
                           location=self.get_source_info())
            stack = []
        else:
            stack.pop()
        if len(stack) > 0:
            symbol = stack[-1]
        else:
            symbol = self.env.domaindata['c']['root_symbol']
        self.env.temp_data['c:parent_symbol'] = symbol
        self.env.temp_data['c:namespace_stack'] = stack
        self.env.ref_context['cp:parent_key'] = symbol.get_lookup_key()
        return []


class AliasNode(nodes.Element):
    def __init__(self, sig: str, env: "BuildEnvironment" = None,
                 parentKey: LookupKey = None) -> None:
        super().__init__()
        self.sig = sig
        if env is not None:
            if 'c:parent_symbol' not in env.temp_data:
                root = env.domaindata['c']['root_symbol']
                env.temp_data['c:parent_symbol'] = root
            self.parentKey = env.temp_data['c:parent_symbol'].get_lookup_key()
        else:
            assert parentKey is not None
            self.parentKey = parentKey

    def copy(self: T) -> T:
        return self.__class__(self.sig, env=None, parentKey=self.parentKey)  # type: ignore


class AliasTransform(SphinxTransform):
    default_priority = ReferencesResolver.default_priority - 1

    def apply(self, **kwargs: Any) -> None:
        for node in self.document.traverse(AliasNode):
            sig = node.sig
            parentKey = node.parentKey
            try:
                parser = DefinitionParser(sig, location=node,
                                          config=self.env.config)
                name = parser.parse_xref_object()
            except DefinitionError as e:
                logger.warning(e, location=node)
                name = None

            if name is None:
                # could not be parsed, so stop here
                signode = addnodes.desc_signature(sig, '')
                signode.clear()
                signode += addnodes.desc_name(sig, sig)
                node.replace_self(signode)
                continue

            rootSymbol = self.env.domains['c'].data['root_symbol']  # type: Symbol
            parentSymbol = rootSymbol.direct_lookup(parentKey)  # type: Symbol
            if not parentSymbol:
                print("Target: ", sig)
                print("ParentKey: ", parentKey)
                print(rootSymbol.dump(1))
            assert parentSymbol  # should be there

            s = parentSymbol.find_declaration(
                name, 'any',
                matchSelf=True, recurseInAnon=True)
            if s is None:
                signode = addnodes.desc_signature(sig, '')
                node.append(signode)
                signode.clear()
                signode += addnodes.desc_name(sig, sig)

                logger.warning("Could not find C declaration for alias '%s'." % name,
                               location=node)
                node.replace_self(signode)
            else:
                nodes = []
                options = dict()  # type: ignore
                signode = addnodes.desc_signature(sig, '')
                nodes.append(signode)
                s.declaration.describe_signature(signode, 'markName', self.env, options)
                node.replace_self(nodes)


class CAliasObject(ObjectDescription):
    option_spec = {}  # type: Dict

    def run(self) -> List[Node]:
        if ':' in self.name:
            self.domain, self.objtype = self.name.split(':', 1)
        else:
            self.domain, self.objtype = '', self.name

        node = addnodes.desc()
        node.document = self.state.document
        node['domain'] = self.domain
        # 'desctype' is a backwards compatible attribute
        node['objtype'] = node['desctype'] = self.objtype
        node['noindex'] = True

        self.names = []  # type: List[str]
        signatures = self.get_signatures()
        for i, sig in enumerate(signatures):
            node.append(AliasNode(sig, env=self.env))

        contentnode = addnodes.desc_content()
        node.append(contentnode)
        self.before_content()
        self.state.nested_parse(self.content, self.content_offset, contentnode)
        self.env.temp_data['object'] = None
        self.after_content()
        return [node]


class CXRefRole(XRefRole):
    def process_link(self, env: BuildEnvironment, refnode: Element,
                     has_explicit_title: bool, title: str, target: str) -> Tuple[str, str]:
        refnode.attributes.update(env.ref_context)

        if not has_explicit_title:
            # major hax: replace anon names via simple string manipulation.
            # Can this actually fail?
            title = anon_identifier_re.sub("[anonymous]", str(title))

        if not has_explicit_title:
            target = target.lstrip('~')  # only has a meaning for the title
            # if the first character is a tilde, don't display the module/class
            # parts of the contents
            if title[0:1] == '~':
                title = title[1:]
                dot = title.rfind('.')
                if dot != -1:
                    title = title[dot + 1:]
        return title, target


class CExprRole(SphinxRole):
    def __init__(self, asCode: bool) -> None:
        super().__init__()
        if asCode:
            # render the expression as inline code
            self.class_type = 'c-expr'
            self.node_type = nodes.literal  # type: Type[TextElement]
        else:
            # render the expression as inline text
            self.class_type = 'c-texpr'
            self.node_type = nodes.inline

    def run(self) -> Tuple[List[Node], List[system_message]]:
        text = self.text.replace('\n', ' ')
        parser = DefinitionParser(text, location=self.get_source_info(),
                                  config=self.env.config)
        # attempt to mimic XRefRole classes, except that...
        classes = ['xref', 'c', self.class_type]
        try:
            ast = parser.parse_expression()
        except DefinitionError as ex:
            logger.warning('Unparseable C expression: %r\n%s', text, ex,
                           location=self.get_source_info())
            # see below
            return [self.node_type(text, text, classes=classes)], []
        parentSymbol = self.env.temp_data.get('cpp:parent_symbol', None)
        if parentSymbol is None:
            parentSymbol = self.env.domaindata['c']['root_symbol']
        # ...most if not all of these classes should really apply to the individual references,
        # not the container node
        signode = self.node_type(classes=classes)
        ast.describe_signature(signode, 'markType', self.env, parentSymbol)
        return [signode], []


class CDomain(Domain):
    """C language domain."""
    name = 'c'
    label = 'C'
    object_types = {
        'function': ObjType(_('function'), 'func'),
        'member': ObjType(_('member'), 'member'),
        'macro': ObjType(_('macro'), 'macro'),
        'type': ObjType(_('type'), 'type'),
        'var': ObjType(_('variable'), 'data'),
    }

    directives = {
        'member': CMemberObject,
        'var': CMemberObject,
        'function': CFunctionObject,
        'macro': CMacroObject,
        'struct': CStructObject,
        'union': CUnionObject,
        'enum': CEnumObject,
        'enumerator': CEnumeratorObject,
        'type': CTypeObject,
        # scope control
        'namespace': CNamespaceObject,
        'namespace-push': CNamespacePushObject,
        'namespace-pop': CNamespacePopObject,
        # other
        'alias': CAliasObject
    }
    roles = {
        'member': CXRefRole(),
        'data': CXRefRole(),
        'var': CXRefRole(),
        'func': CXRefRole(fix_parens=True),
        'macro': CXRefRole(),
        'struct': CXRefRole(),
        'union': CXRefRole(),
        'enum': CXRefRole(),
        'enumerator': CXRefRole(),
        'type': CXRefRole(),
        'expr': CExprRole(asCode=True),
        'texpr': CExprRole(asCode=False)
    }
    initial_data = {
        'root_symbol': Symbol(None, None, None, None),
        'objects': {},  # fullname -> docname, node_id, objtype
    }  # type: Dict[str, Union[Symbol, Dict[str, Tuple[str, str, str]]]]

    @property
    def objects(self) -> Dict[str, Tuple[str, str, str]]:
        return self.data.setdefault('objects', {})  # fullname -> docname, node_id, objtype

    def clear_doc(self, docname: str) -> None:
        if Symbol.debug_show_tree:
            print("clear_doc:", docname)
            print("\tbefore:")
            print(self.data['root_symbol'].dump(1))
            print("\tbefore end")

        rootSymbol = self.data['root_symbol']
        rootSymbol.clear_doc(docname)

        if Symbol.debug_show_tree:
            print("\tafter:")
            print(self.data['root_symbol'].dump(1))
            print("\tafter end")
            print("clear_doc end:", docname)
        for fullname, (fn, _id, _l) in list(self.objects.items()):
            if fn == docname:
                del self.objects[fullname]

    def process_doc(self, env: BuildEnvironment, docname: str,
                    document: nodes.document) -> None:
        if Symbol.debug_show_tree:
            print("process_doc:", docname)
            print(self.data['root_symbol'].dump(0))
            print("process_doc end:", docname)

    def process_field_xref(self, pnode: pending_xref) -> None:
        pnode.attributes.update(self.env.ref_context)

    def merge_domaindata(self, docnames: List[str], otherdata: Dict) -> None:
        if Symbol.debug_show_tree:
            print("merge_domaindata:")
            print("\tself:")
            print(self.data['root_symbol'].dump(1))
            print("\tself end")
            print("\tother:")
            print(otherdata['root_symbol'].dump(1))
            print("\tother end")
            print("merge_domaindata end")

        self.data['root_symbol'].merge_with(otherdata['root_symbol'],
                                            docnames, self.env)
        ourObjects = self.data['objects']
        for fullname, (fn, id_, objtype) in otherdata['objects'].items():
            if fn in docnames:
                if fullname not in ourObjects:
                    ourObjects[fullname] = (fn, id_, objtype)
                # no need to warn on duplicates, the symbol merge already does that

    def _resolve_xref_inner(self, env: BuildEnvironment, fromdocname: str, builder: Builder,
                            typ: str, target: str, node: pending_xref,
                            contnode: Element) -> Tuple[Element, str]:
        parser = DefinitionParser(target, location=node, config=env.config)
        try:
            name = parser.parse_xref_object()
        except DefinitionError as e:
            logger.warning('Unparseable C cross-reference: %r\n%s', target, e,
                           location=node)
            return None, None
        parentKey = node.get("c:parent_key", None)  # type: LookupKey
        rootSymbol = self.data['root_symbol']
        if parentKey:
            parentSymbol = rootSymbol.direct_lookup(parentKey)  # type: Symbol
            if not parentSymbol:
                print("Target: ", target)
                print("ParentKey: ", parentKey)
                print(rootSymbol.dump(1))
            assert parentSymbol  # should be there
        else:
            parentSymbol = rootSymbol
        s = parentSymbol.find_declaration(name, typ,
                                          matchSelf=True, recurseInAnon=True)
        if s is None or s.declaration is None:
            return None, None

        # TODO: check role type vs. object type

        declaration = s.declaration
        displayName = name.get_display_string()
        docname = s.docname
        assert docname

        return make_refnode(builder, fromdocname, docname,
                            declaration.get_newest_id(), contnode, displayName
                            ), declaration.objectType

    def resolve_xref(self, env: BuildEnvironment, fromdocname: str, builder: Builder,
                     typ: str, target: str, node: pending_xref,
                     contnode: Element) -> Element:
        return self._resolve_xref_inner(env, fromdocname, builder, typ,
                                        target, node, contnode)[0]

    def resolve_any_xref(self, env: BuildEnvironment, fromdocname: str, builder: Builder,
                         target: str, node: pending_xref, contnode: Element
                         ) -> List[Tuple[str, Element]]:
        with logging.suppress_logging():
            retnode, objtype = self._resolve_xref_inner(env, fromdocname, builder,
                                                        'any', target, node, contnode)
        if retnode:
            return [('c:' + self.role_for_objtype(objtype), retnode)]
        return []

    def get_objects(self) -> Iterator[Tuple[str, str, str, str, str, int]]:
        for refname, (docname, node_id, objtype) in list(self.objects.items()):
            yield (refname, refname, objtype, docname, node_id, 1)


def setup(app: Sphinx) -> Dict[str, Any]:
    app.add_domain(CDomain)
    app.add_config_value("c_id_attributes", [], 'env')
    app.add_config_value("c_paren_attributes", [], 'env')
    app.add_post_transform(AliasTransform)

    return {
        'version': 'builtin',
        'env_version': 2,
        'parallel_read_safe': True,
        'parallel_write_safe': True,
    }
