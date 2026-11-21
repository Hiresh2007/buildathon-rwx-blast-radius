"""Select candidate tests using only facts measured on the unchanged SQLGlot tree."""

import ast
import copy
import gzip
from io import BytesIO
import json
import os
import re
from tokenize import detect_encoding
from collections import deque
from dataclasses import dataclass
from functools import cached_property, lru_cache
from pathlib import Path, PurePosixPath

from arguments import ArgumentIndex
from ast_syntax import same_syntax
from cache_impact import CacheConsumers
from default_impact import DefaultIndex, functions
from execution import ExecutionIndex, ScopeChange, changes as changed_scopes, scalar_replacements
from lookup_domains import LookupDomains
from none_comparisons import NoneComparisons
from truth_guards import TruthGuards
from predicates import exemptions, load_domains
from symbols import SymbolIndex
from table_values import TableValues


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


class PatchError(ValueError):
    pass


@dataclass
class FileDiff:
    path: str
    hunks: list


def _path(header, prefix):
    name = header.split("\t", 1)[0]
    if not name.startswith(prefix):
        raise PatchError("unsupported file header")
    path = PurePosixPath(name[len(prefix):])
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise PatchError("invalid relative path")
    return path.as_posix()


def _parse_diff(text):
    """Consume hunk counts so hunk text resembling a file header stays in the hunk."""
    lines = text.splitlines()
    files = []
    i = 0
    current = None
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            current = None
        elif line.startswith("--- "):
            if i + 1 >= len(lines) or not lines[i + 1].startswith("+++ "):
                raise PatchError("missing new file header")
            old = _path(line[4:], "a/")
            new = _path(lines[i + 1][4:], "b/")
            if old != new:
                raise PatchError("file moved")
            current = FileDiff(old, [])
            files.append(current)
            i += 1
        elif line.startswith("@@"):
            match = _HUNK.match(line)
            if current is None or not match:
                raise PatchError("unsupported hunk")
            start, removed, _, added = match.groups()
            old_count = int(removed or 1)
            new_count = int(added or 1)
            offset = int(start) - int(old_count > 0)
            contents = []
            while old_count or new_count:
                i += 1
                if i >= len(lines):
                    raise PatchError("truncated hunk")
                entry = lines[i]
                if entry == "\\ No newline at end of file":
                    continue
                if not entry or entry[0] not in " +-":
                    raise PatchError("invalid hunk")
                old_count -= entry[0] != "+"
                new_count -= entry[0] != "-"
                if old_count < 0 or new_count < 0:
                    raise PatchError("incorrect hunk lengths")
                contents.append(entry)
            current.hunks.append((offset, contents))
        elif line.startswith(("Binary files ", "GIT binary patch", "rename ", "copy ",
                              "new file mode ", "deleted file mode ", "old mode ", "new mode ")):
            raise PatchError("non-textual file change")
        i += 1
    if not files or any(not change.hunks for change in files):
        raise PatchError("no textual hunks")
    return files


def _apply(source, hunks):
    """Reconstruct the candidate and record replaced lines and pure insertion points."""
    original = source.splitlines()
    modified = []
    replaced = set()
    inserted = set()
    position = 0
    for offset, hunk in hunks:
        if offset < position or offset > len(original):
            raise PatchError("invalid hunk position")
        modified.extend(original[position:offset])
        position = offset
        added = removed = False
        for entry in [*hunk, None]:
            if entry is None or entry[0] == " ":
                if added and not removed:
                    inserted.add(position)  # number of original lines before this insertion
                added = removed = False
            if entry is None:
                break
            action, content = entry[0], entry[1:]
            if action != "+":
                if position >= len(original) or original[position] != content:
                    raise PatchError("hunk does not match the pinned source")
                position += 1
            if action == "-":
                replaced.add(position)
                removed = True
            else:
                modified.append(content)
                if action == "+":
                    added = True
    modified.extend(original[position:])
    return "\n".join(modified) + "\n", replaced, inserted


def _body_start(fn):
    first = fn.body[0]
    starts = [first.lineno]
    starts.extend(dec.lineno for dec in getattr(first, "decorator_list", ()))
    return min(starts)


def _decl_start(node):
    return min([node.lineno] + [dec.lineno for dec in getattr(node, "decorator_list", ())])


def _statements(tree):
    # Python expressions cannot contain statements. Preserve breadth-first order
    # through handlers, match cases and all other containers of statements.
    pending = deque([tree])
    while pending:
        for child in ast.iter_child_nodes(pending.popleft()):
            if isinstance(child, ast.expr):
                continue
            if isinstance(child, ast.stmt):
                yield child
            pending.append(child)


class Source:
    def __init__(self, text, path):
        self.text = text
        tree = ast.parse(text, filename=path)
        self.tree = tree
        # A statement's span includes its full multiline condition and continuation lines.
        self.statements = sorted(
            _statements(tree),
            key=lambda node: (node.end_lineno - node.lineno, -node.lineno),
        )

        self.scopes = sorted(
            (node for node in self.statements if isinstance(node, (*_FUNCTIONS, ast.ClassDef))),
            key=lambda node: (node.end_lineno - _decl_start(node), -node.lineno),
        )

    @cached_property
    def syntax(self):
        return ast.dump(self.tree)

    def runtime_scope(self, line):
        for scope in self.scopes:
            if _decl_start(scope) <= line <= scope.end_lineno:
                if isinstance(scope, _FUNCTIONS) and scope.lineno < line and _body_start(scope) <= line:
                    return scope
                break
        return None

    def statement(self, line):
        return next((node for node in self.statements
                     if node.lineno <= line <= node.end_lineno), None)


def _read_map(name):
    with gzip.open(Path(__file__).with_name(name), "rt", encoding="utf-8") as stream:
        return json.load(stream)


class ImpactIndex:
    def __init__(self, map_data, tests, source_root=None):
        if source_root is None:
            local = Path(__file__).parent / ".cache" / "sqlglot"
            source_root = os.environ.get("SQLGLOT_ROOT") or (
                local if local.is_dir() else "/opt/sqlglot"
            )
        self.root = Path(source_root)
        here = Path(__file__).resolve().parent
        ids = map_data["ids"]
        self.execution = ExecutionIndex(_read_map("execution.json.gz"), ids, _read_map("children.json.gz"),
                                        _read_map("import-blocks.json.gz"))
        self.symbols = SymbolIndex(_read_map("symbols.json.gz"), ids, self.root)
        self.domains = load_domains(here / "predicates.json.gz", ids)
        self.arguments = ArgumentIndex(here / "arguments.json.gz", ids)
        self.defaults = DefaultIndex(here / "default-impact.json.gz", ids)
        self.table_values = TableValues(here / "table-values.json.gz", ids)
        self.truth_guards = TruthGuards(here / "truth-guards.json.gz", ids)
        self.none_comparisons = NoneComparisons(here / "none-comparisons.json.gz", ids)
        self.lookups = LookupDomains(here / "lookup-domains.json.gz", ids, here / "membership-values.json.gz")
        deps = _read_map("dependencies.json.gz")
        if deps["ids"] != ids:
            raise ValueError("isolated test map uses another inventory")
        self.isolated = {path: int(bits, 16) for path, bits in deps["files"].items()}
        self.unknown = int(deps["unknown"], 16)
        live = {test["id"]: test["number"] for test in tests}
        self.numbers = [live.get(name) for name in map_data["ids"]]
        self.test_files = {}
        self.test_names = {name.partition("[")[0] for name in ids}
        for i, name in enumerate(ids):
            path = name.partition("::")[0]
            self.test_files[path] = self.test_files.get(path, 0) | (1 << i)
        self.unmeasured = set(live.values()).difference(self.numbers)
        self.all = set(live.values())
        self.all_bits = (1 << len(self.numbers)) - 1
        masks = [int(encoded, 16) for encoded in map_data["sets"]]
        self.files = {
            path: {int(line): masks[key] for line, key in covered.items()}
            for path, covered in map_data["files"].items()
        }
        self.reads = {path: masks[key] for path, key in map_data["reads"].items()}
        self.incomplete = masks[map_data["incomplete"]]
        self.import_lines = {path: set(lines) for path, lines in map_data.get("import_lines", {}).items()}
        self.config = {key: masks[index] for key, index in map_data.get("config", {}).items()}
        self.cache_consumers = CacheConsumers(_read_map("cache-consumers.json.gz"), self)

    @lru_cache(maxsize=16)
    def source(self, path):
        return Source((self.root / path).read_text(encoding="utf-8"), path)

    def related_tests(self, patch):
        """Whole-suite execution takes priority over a dependency seen only on import."""
        observed = isolated = 0
        try:
            for change in _parse_diff(patch):
                for mask in self.files.get(change.path, {}).values():
                    observed |= mask
                observed |= self.reads.get(change.path, 0)
                isolated |= self.isolated.get(change.path, 0)
        except PatchError:
            return []
        return [self.tests_from_bits(observed), self.tests_from_bits(isolated & ~observed)]

    def tests_from_bits(self, bits):
        selected = set(self.unmeasured)
        while bits:
            bit = bits & -bits
            number = self.numbers[bit.bit_length() - 1]
            if number is not None:
                selected.add(number)
            bits ^= bit
        return selected


def _line_range(covered, start, end):
    bits = 0
    for line in range(start, end + 1):
        bits |= covered.get(line, 0)
    return bits


def _scalar_setting(tree, line):
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef) or not cls.lineno <= line <= cls.end_lineno:
            continue
        for stmt in cls.body:
            if stmt.lineno <= line <= stmt.end_lineno:
                targets = (stmt.targets if isinstance(stmt, ast.Assign) else
                           [stmt.target] if isinstance(stmt, ast.AnnAssign) else [])
                if (len(targets) == 1 and isinstance(targets[0], ast.Name)
                        and isinstance(stmt.value, ast.Constant)):
                    return f"{cls.name}.{targets[0].id}", stmt
    return None


def _setting_change(path, source, candidate, replaced, inserted, index):
    """Only a same-type scalar value can change. Every other AST node must match."""
    if inserted or not replaced or not index.config:
        return None
    line = min(replaced)
    old = _scalar_setting(source.tree, line)
    new = _scalar_setting(candidate, line)
    if not old or not new or old[0] != new[0]:
        return None
    bits = index.config.get(f"{path}:{old[0]}")
    old_value = old[1].value
    new_value = new[1].value
    if bits is None or type(old_value.value) is not type(new_value.value):
        return None
    # Compare the entire candidate with the scalar restored. This also guards
    # against multi-hunk edits to nearby class members, imports, or decorators.
    new[1].value = old_value
    try:
        if same_syntax(candidate, source.tree):
            return bits
    finally:
        new[1].value = new_value
    return None


def _same_outside_scopes(source, candidate, scopes):
    """Ignore only bodies of named functions that the original edit actually touches."""
    def signature(tree):
        class Skeleton(ast.NodeTransformer):
            def visit_FunctionDef(self, node):
                if (node.lineno, node.name) in scopes:
                    node.body = []
                    return node
                return self.generic_visit(node)
            visit_AsyncFunctionDef = visit_FunctionDef

        return Skeleton().visit(tree)

    return same_syntax(signature(ast.parse(source)), signature(candidate))


def _balanced_replacements(hunks):
    for _, hunk in hunks:
        delta = 0
        for line in [*hunk, " "]:
            if line[0] == " ":
                if delta:
                    return False
            else:
                delta += 1 if line[0] == "+" else -1
    return True


def _prepare_change(change, index):
    source = index.source(change.path)
    changed, replaced, inserted = _apply(source.text, change.hunks)
    # The interpreter honors a coding cookie in the first two lines of each file.
    # Compiling bytes checks that cookie and the complete module's lexical scope.
    raw = changed.encode("utf-8")
    if detect_encoding(BytesIO(raw).readline)[0] != detect_encoding(BytesIO(source.text.encode("utf-8")).readline)[0]:
        raise PatchError("changed source encoding")
    tree = compile(raw, change.path, "exec", ast.PyCF_ONLY_AST, dont_inherit=True)
    compile(tree, change.path, "exec", dont_inherit=True)
    return source, changed, tree, replaced, inserted


def _units(tree, prefix=()):
    """Index nonoverlapping class fields, function bodies and argument defaults."""
    for i, node in enumerate(tree.body):
        path = (*prefix, ("body", i))
        if isinstance(node, ast.ClassDef):
            yield from _units(node, path)
        elif isinstance(node, ast.FunctionDef):
            yield (*path, ("body", None)), (_body_start(node), node.end_lineno, node.body)
            for name in ("defaults", "kw_defaults"):
                for j, default in enumerate(getattr(node.args, name)):
                    if isinstance(default, ast.Constant):
                        yield (*path, ("args", None), (name, j)), (default.lineno, default.end_lineno, default)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            yield path, (node.lineno, node.end_lineno, node)


def _replace_unit(root, path, value):
    # Copy just the list of statements on the path. Both input trees remain
    # available to the source cache and the individual declaration checks.
    node = copy.copy(root)
    (field, i), *rest = path
    original = getattr(root, field)
    if i is None:
        updated = _replace_unit(original, rest, value) if rest else value
    else:
        updated = original.copy()
        updated[i] = _replace_unit(original[i], rest, value) if rest else value
    setattr(node, field, updated)
    return node


def _separate_declarations(change, prepared):
    source, changed, tree, replaced, inserted = prepared
    if (inserted or not 2 <= len(replaced) <= 12 or not _balanced_replacements(change.hunks)
            or all(source.runtime_scope(line) is not None for line in replaced)):
        return [prepared]
    originals = dict(_units(source.tree))
    revisions = dict(_units(tree))
    groups = {}
    for path, (start, end, before) in originals.items():
        lines = {line for line in replaced if start <= line <= end}
        if not lines:
            continue
        if path not in revisions or type(before) is not type(revisions[path][2]):
            return [prepared]
        if not same_syntax(before, revisions[path][2]):
            groups[path] = lines
    if not 2 <= len(groups) <= 4:
        return [prepared]
    # Restoring the old nodes must reproduce the whole original syntax tree.
    # This catches lines that move a declaration, change a decorator or alter
    # parsing beyond the boundaries of an otherwise eligible replacement.
    restored = tree
    for path in groups:
        restored = _replace_unit(restored, path, originals[path][2])
    if not same_syntax(restored, source.tree):
        return [prepared]
    return [(source, changed, _replace_unit(source.tree, path, revisions[path][2]), lines, set())
            for path, lines in groups.items()]


def _declaration_change(change, index, prepared, *, combined=False):
    source, _, tree, replaced, inserted = prepared
    setting = _setting_change(change.path, source, tree, replaced, inserted, index)
    if setting is not None:
        cold = index.isolated.get(change.path, index.all_bits) | index.unknown
        return (setting | index.reads.get(change.path, 0) | index.execution.child_reads.get(change.path, 0)
                | (cold & index.incomplete))
    table = index.table_values.select(change.path, source, tree, replaced, inserted, index, allow_shape=combined)
    if table is not None:
        return table
    return index.defaults.select(change.path, source, tree, replaced, inserted, index, combined=combined)


def _test_module_change(change, index, prepared):
    path = change.path
    if (path not in index.test_files or path not in index.isolated
            or not PurePosixPath(path).name.startswith("test_")):
        return None
    source, _, tree, _, _ = prepared
    if any(isinstance(n, ast.Name) and n.id == "pytest_plugins" for n in ast.walk(source.tree)):
        return None
    eligible = {(node.lineno, name) for name, node in functions(source.tree)
                if node.name.startswith("test_") and not node.decorator_list
                and f"{path}::{name.replace('.', '::')}" in index.test_names}
    original = ExecutionIndex.baseline(source.text, path)
    changed = changed_scopes(original, compile(tree, path, "exec", dont_inherit=True))
    if not changed or any((before.co_firstlineno, before.co_qualname) not in eligible
                          or not scalar_replacements(before, after) for before, after in changed):
        return None
    # Preserve the entire module, derived test classes, isolated imports and file
    # readers. Pytest setup, fixtures, collection and shared helpers are outside
    # this rule. Only existing test bodies may contain local substitutions.
    return (index.test_files[path] | index.isolated[path] | index.unknown
            | index.reads.get(path, 0) | index.execution.child_reads.get(path, 0))


def _python_change(change, index, prepared):
    source, changed, tree, replaced, inserted = prepared
    reads = index.reads.get(change.path, 0)
    # A test whose subprocess escaped the whole-suite tracer is still represented
    # in its own clean process tree's dependency map.
    cold = index.isolated.get(change.path, 0) | index.unknown
    partial = index.incomplete & cold
    line_bits = index.all_bits
    if _balanced_replacements(change.hunks):
        line_bits = _line_change(change.path, source, replaced, inserted, index)
    execution_bits = index.execution.select(change.path, source.text, tree)
    if execution_bits is not None:
        # Main-process line coverage cannot rule out a call observed only in a child.
        line_bits = (line_bits & execution_bits) | (execution_bits & index.execution.child_complete)
        partial &= ~index.execution.child_complete
        reads |= index.execution.child_reads.get(change.path, 0)
    else:
        if line_bits != index.all_bits:
            scopes = set()
            for line in replaced:
                scope = source.runtime_scope(line)
                if scope is not None:
                    scopes.add((scope.lineno, scope.name))
            # Never mutate the cached or candidate ASTs used by the other analyses.
            if not _same_outside_scopes(source.text, ast.parse(changed), scopes):
                line_bits = index.all_bits
        if line_bits == index.all_bits:
            symbols = index.symbols.select(change.path, source.tree, tree, source.text, changed)
            if symbols is not None:
                line_bits = symbols
    exempt = exemptions(change.path, source.tree, tree, index.domains, replaced, inserted)
    exempt |= index.arguments.exemptions(change.path, source.tree, tree, replaced, inserted)
    exempt |= index.lookups.exemptions(change.path, source, tree, replaced, inserted) & ~index.execution.untraced
    for observations in (index.none_comparisons, index.truth_guards):
        exempt |= observations.exemptions(change.path, source, tree, replaced, inserted) & ~index.execution.untraced
    return (line_bits & ~exempt) | reads | partial


def _line_change(path, source, replaced, inserted, index):
    covered = index.files.get(path, {})
    if not covered:
        return index.all_bits
    selected = 0
    during_collection = index.import_lines.get(path, set())
    for line in replaced:
        scope = source.runtime_scope(line)
        statement = source.statement(line)
        if scope is None or statement is None:
            # Class bodies, defaults, decorators and imports can affect other modules
            # before pytest has entered any individual test.
            return index.all_bits
        if any(statement.lineno <= n <= statement.end_lineno for n in during_collection):
            return index.all_bits
        selected |= _line_range(covered, statement.lineno, statement.end_lineno)
    for before in inserted:
        left = source.runtime_scope(before)
        right = source.runtime_scope(before + 1)
        if left is None or left is not right:
            return index.all_bits
        if any(_body_start(left) <= n <= left.end_lineno for n in during_collection):
            return index.all_bits
        # New branches have no measured lines. Any caller of the enclosing function
        # could take the new branch.
        selected |= _line_range(covered, left.lineno, left.end_lineno)
    return selected


def choose_tests(patch_text, index, tests=None, *, protected=None):
    """Return numbers from the live test manifest, falling back for unmodelled changes."""
    if index is None:
        return {test["number"] for test in tests}
    try:
        changes = _parse_diff(patch_text)
        if len({change.path for change in changes}) != len(changes):
            return set(index.all)
        bits = 0
        code = []
        test_changed = False
        for change in changes:
            if change.path.endswith(".py"):
                prepared = _prepare_change(change, index)
                baseline, _, candidate, _, _ = prepared
                if same_syntax(baseline.tree, candidate):
                    bits |= index.reads.get(change.path, 0) | index.execution.child_reads.get(change.path, 0)
                elif change.path.startswith("tests/"):
                    consumers = _test_module_change(change, index, prepared)
                    if consumers is None:
                        return set(index.all)
                    bits |= consumers
                    test_changed = True
                elif not change.path.startswith("sqlglot/"):
                    return set(index.all)
                else:
                    code.extend((change, part) for part in _separate_declarations(change, prepared))
            elif change.path in index.reads:
                # Every test that opens the changed fixture remains eligible, even
                # when a source edit in this patch also alters its interpretation.
                _apply((index.root / change.path).read_text(encoding="utf-8"), change.hunks)
                bits |= index.reads[change.path] | (index.incomplete & (
                    index.isolated.get(change.path, 0) | index.unknown))
            else:
                return set(index.all)
        for change, prepared in code:
            cache_bits = index.cache_consumers.floor(change.path, prepared[3], prepared[4])
            if cache_bits and protected is not None:
                protected.update(index.tests_from_bits(cache_bits))
            combined = len(code) > 1 or test_changed
            declaration = _declaration_change(change, index, prepared, combined=combined)
            if declaration is not None:
                bits |= declaration | cache_bits
            elif not combined:
                bits |= _python_change(change, index, prepared) | cache_bits
            else:
                # Before two edits can interact, the unchanged execution must
                # reach a modified instruction, load a changed configuration
                # value, or omit a changed function argument. Use the union of
                # these observations, retaining child and cache consumers.
                # Operand-specific exemptions for independent edits cannot be
                # combined: a value changed upstream may reach either function.
                source, _, tree, _, _ = prepared
                calls = index.execution.select(change.path, source.text, tree, local_substitutions_only=True,
                                               original_tree=source.tree)
                if calls is None:
                    return set(index.all)
                cold = index.isolated.get(change.path, 0) | index.unknown
                partial = index.incomplete & cold & ~index.execution.child_complete
                bits |= (calls | cache_bits | partial | index.reads.get(change.path, 0)
                         | index.execution.child_reads.get(change.path, 0))
        return index.tests_from_bits(bits)
    except (ScopeChange, OSError, UnicodeError, SyntaxError, RecursionError, ValueError):
        return set(index.all)
