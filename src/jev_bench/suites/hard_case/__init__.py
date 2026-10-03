"""Independent long-context insurance-claim benchmark."""


def load_tests(loader, tests, pattern):
    """Keep root unittest discovery separate; select hard_case/tests explicitly."""
    return tests
