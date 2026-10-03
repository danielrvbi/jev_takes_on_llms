from jev_bench.suites.spec import Suite


def get_suite(name):
    if name not in {"benchmark", "hard_case"}:
        raise ValueError(f"Unknown suite: {name}")
    return Suite(name)
