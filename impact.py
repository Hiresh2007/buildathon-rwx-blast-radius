"""Select candidate tests using only facts measured on the unchanged SQLGlot tree."""

import ast
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath


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


class Source:
    def __init__(self, text, path):
        self.text = text
        tree = ast.parse(text, filename=path)
        self.tree = tree
        self.scopes = sorted(
            (node for node in ast.walk(tree) if isinstance(node, (*_FUNCTIONS, ast.ClassDef))),
            key=lambda node: (node.end_lineno - _decl_start(node), -node.lineno),
        )
        # A statement's span includes its full multiline condition and continuation lines.
        self.statements = sorted(
            (node for node in ast.walk(tree) if isinstance(node, ast.stmt)),
            key=lambda node: (node.end_lineno - node.lineno, -node.lineno),
        )

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


class ImpactIndex:
    def __init__(self, map_data, tests, source_root=None):
        if source_root is None:
            local = Path(__file__).parent / ".cache" / "sqlglot"
            source_root = os.environ.get("SQLGLOT_ROOT") or (
                local if local.is_dir() else "/opt/sqlglot"
            )
        self.root = Path(source_root)
        live = {test["id"]: test["number"] for test in tests}
        self.numbers = [live.get(name) for name in map_data["ids"]]
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

    @lru_cache(maxsize=16)
    def source(self, path):
        return Source((self.root / path).read_text(encoding="utf-8"), path)

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
        if ast.dump(candidate) == ast.dump(source.tree):
            return bits
    finally:
        new[1].value = new_value
    return None


def _python_change(change, index):
    source = index.source(change.path)
    changed, replaced, inserted = _apply(source.text, change.hunks)
    tree = ast.parse(changed, filename=change.path)
    # Comments inside string literals or changes involving indentation alter the AST.
    # Explicit consumers of the file itself may still observe an equivalent rewrite.
    reads = index.reads.get(change.path, 0)
    if ast.dump(tree) == ast.dump(source.tree):
        return reads
    compile(tree, change.path, "exec")
    setting = _setting_change(change.path, source, tree, replaced, inserted, index)
    if setting is not None:
        return setting | reads | index.incomplete
    covered = index.files.get(change.path, {})
    if not covered:
        return index.all_bits
    selected = reads | index.incomplete
    during_collection = index.import_lines.get(change.path, set())
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


def choose_tests(patch_text, index, tests=None):
    """Return numbers from the live test manifest, falling back for unmodelled changes."""
    if index is None:
        return {test["number"] for test in tests}
    try:
        bits = 0
        for change in _parse_diff(patch_text):
            if change.path.endswith(".py"):
                bits |= _python_change(change, index)
            elif change.path in index.reads:
                bits |= index.reads[change.path] | index.incomplete
            else:
                return set(index.all)
            if bits == index.all_bits:
                return set(index.all)
        return index.tests_from_bits(bits)
    except (PatchError, OSError, UnicodeError, SyntaxError, RecursionError, ValueError):
        return set(index.all)
