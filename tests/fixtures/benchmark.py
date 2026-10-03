from tests.fixtures.audit import audited_result
from jev_bench.suites.benchmark.responses import validate_result
from jev_bench.run.benchmark import measured_row as production_measured_row

def values():
    return {
        "requires_web_probability": 0.5,
        "is_safe_probability": 0.9,
        "route_probabilities": {"answer_directly": 0.2, "web_search": 0.2,
                                "refuse": 0.1, "ask_clarification": 0.0},
        "freshness_probabilities": {"0": 0.0, "1": 0.0, "2": 0.0,
                                    "3": 0.2, "4": 0.2, "5": 0.0},
    }


def result():
    return audited_result(validate_result(values(), {"fixture": True}, 10, 20))


def measured_row(model, case, repetition, response, latency):
    return production_measured_row(model, case, repetition, audited_result(response, model, case.message), latency)
