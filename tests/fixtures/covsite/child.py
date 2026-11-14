"""Trivial measured child for tests/test_cov_child_env.py.

Lives under tests/fixtures/covsite/ (not a test module: no test_ prefix,
never discovered) but is NOT in .coveragerc's source list, so a traced
child records no rows for it — the data-file EXISTENCE is what the test
asserts, which is the part that breaks on user-site installs.
"""
x = 1
y = 2
print(x + y)
