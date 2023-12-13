#!/usr/bin/env python

import re

back_slash_re = re.compile('\\[0-9"]')

def quote_expr(s):
    s = re.sub(r'(\\[0-9"])', r'\\\1', s)
    # if '\\"' in s:
    #     s = s.replace('\\"','\\\\"')
    if '"' in s and "'" in s:
        s = s.replace("'", "\\'")
    elif "'" in s:
        return f'"{s}"'
    # if '\"\"\"' in s:
    #     s = s.replace('\"\"\"', '""\\"')
    return f"'{s}'"

def get_tests():
    have_interp = False
    in_header = True
    test_names = set()
    with open("kgtests/language/test_suite.kg", "r") as f:
        for s in f:
            s = s.strip()
            if in_header:
                if s == ':" Atom "':
                    in_header = False
                else:
                    continue
            if len(s) == 0 or s == ':[err;[];.p("ok!")]':
                continue
            if s.startswith(":\""):
                have_interp = False
                if s.startswith(":\"Klong test suite"):
                    continue
                x = s[2:-1].strip()
                name = x.lower().replace(" ","_").replace('-','_').replace('/','_').replace(":", "_")
                if name in test_names:
                    name = name + "_2" # only one collision in suite
                test_names.add(name)
                print()
                print(f"    def test_{name}(self):")
                continue
            if s.startswith("t("):
                p = [x.strip() for x in s[2:-1].split(';')][1:]
                if len(p) == 2:
                    print(f"        self.assert_eval_cmp({quote_expr(p[0])}, {quote_expr(p[1])}{', klong=klong' if have_interp else ''})")
                else:
                    print(f"        self.assert_eval_test({quote_expr(s)}{', klong=klong' if have_interp else ''} )")
            else:
                if not have_interp:
                    have_interp = True
                    print(f"        klong = create_test_klong()")
                print(f"        klong({quote_expr(s)})")


if __name__ == '__main__':
    print("""import unittest
from klongpy import KlongInterpreter
from utils import *

#
# DO NOT MODIFY: this file generated by gen_py_suite.py
#
class TestCoreSuite(unittest.TestCase):

    def assert_eval_cmp(self, a, b, klong=None):
        self.assertTrue(eval_cmp(a, b, klong=klong))

    def assert_eval_test(self, a, klong=None):
        self.assertTrue(eval_test(a, klong=klong))""")
    get_tests()

    print("""
    if __name__ == '__main__':
        unittest.main()""")