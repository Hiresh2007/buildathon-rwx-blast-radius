import ast
import unittest

from forwarding_decorators import deferred_decorators, forwarding_decorators


class ForwardingDecoratorTests(unittest.TestCase):
    def test_wrapped_function_body_is_deferred(self):
        source = '''from __future__ import annotations
from functools import wraps

def decorate(target):
    @wraps(target)
    def wrapper(*args, **kwargs):
        return target(*args, **kwargs)
    return wrapper
'''
        self.assertEqual(set(forwarding_decorators(ast.parse(source))), {"decorate"})
        for changed in (source.replace('    @wraps', '    target()\n    @wraps'),
                        source.replace('def wrapper(*args, **kwargs)', 'def wrapper(arg=target())'),
                        source.replace('@wraps(target)', '@wraps(other)'),
                        source.replace('target', 'wraps'),
                        source.replace('import wraps', 'import wraps, reduce as wraps'),
                        source.replace('from __future__ import annotations', ''),
                        source + '\ntry:\n    pass\nexcept Exception as wraps:\n    pass\n',
                        source + '\nwraps = lambda target: target\n',
                        source + '\nglobals()["decorate"] = replacement\n'):
            with self.subTest(source=changed):
                self.assertNotIn("decorate", forwarding_decorators(ast.parse(changed)))

    def test_changes_to_the_factory_and_class_namespace(self):
        fixture = """from __future__ import annotations
from functools import wraps

def decorate(fn):
    @wraps(fn)
    def wrapper(*args):
        return fn(*args)
    return wrapper

class Example:
    @decorate
    def result(self):
        return 1
"""
        def check(before, after):
            old, new = ast.parse(before), ast.parse(after)
            return deferred_decorators(old.body[-1].body[0], old, new)

        self.assertTrue(check(fixture, fixture.replace('return 1', 'return 2')))
        self.assertFalse(check(fixture, fixture.replace('return fn(*args)', 'return 2')))
        self.assertFalse(check(fixture, fixture.replace('@wraps(fn)', '@callback(fn)')))
        custom = fixture.replace('class Example:', 'class Example(metaclass=Meta):')
        self.assertFalse(check(custom, custom))
