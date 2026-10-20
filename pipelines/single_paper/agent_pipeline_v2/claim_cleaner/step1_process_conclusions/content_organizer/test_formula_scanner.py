import sys
sys.path.insert(0, '.')
from formula_scanner import scan_formulas, is_escaped

tests = [
    ('This is \\$5 and \\$\\$10, not math.', 0),
    ('Inline $x=y$ formula.', 1),
    ('Display $$x=y$$ formula.', 1),
    ('\\text{cost is $5} outside', 0),
    ('\\verb|code|here| then $real$', 1),
    ('\\verb$code$here$ then $real$', 2),
    ('$a$ $b$ $c$', 3),
    ('$$\nmulti\nline\n$$', 1),
    ('\\footnote{ignore $this$ too} then $keep$', 1),
]

all_ok = True
for t, expected in tests:
    spans = scan_formulas(t)
    ok = len(spans) == expected
    all_ok = all_ok and ok
    status = 'OK  ' if ok else 'FAIL'
    print(status, repr(t), '->', len(spans), 'spans (expected', expected, ')')
    if not ok:
        print('    ', spans)

print()
print('ALL OK' if all_ok else 'SOME FAILED')
