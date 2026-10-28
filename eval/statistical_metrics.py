"""Statistical descriptions of recorded task samples using SciPy implementations."""

import numpy as np
from scipy.stats import binomtest, bootstrap


def proportion(passed, total):
    if type(passed) is not int or type(total) is not int or not 0 <= passed <= total:
        raise ValueError("invalid success counts")
    interval = (binomtest(passed, total).proportion_ci(method="wilson") if total else None)
    return {"passed": passed, "total": total, "rate": passed / total if total else None,
            "wilson_95": [float(interval.low), float(interval.high)] if interval else None,
            "sampling_note": "curated cases; binomial interval is descriptive, "
                             "not proof of production success probability"}


def paired_outcomes(before, after):
    if len(before) != len(after) or not before:
        raise ValueError("paired outcomes must be nonempty and equally sized")
    wins = sum(not a and b for a, b in zip(before, after, strict=True))
    losses = sum(a and not b for a, b in zip(before, after, strict=True))
    return {"pairs": len(before), "wins": wins, "losses": losses,
            "unchanged": len(before) - wins - losses,
            "exact_mcnemar_p": float(binomtest(wins, wins + losses).pvalue)
            if wins + losses else 1.0,
            "interpretation": "not significant does not establish equivalence or noninferiority"}


def paired_delta(before, after):
    if len(before) != len(after) or len(before) < 2:
        return {"pairs": len(before), "mean_delta": None, "paired_bootstrap_95": None}
    delta = np.asarray(after, dtype=float) - np.asarray(before, dtype=float)
    if not np.isfinite(delta).all():
        raise ValueError("paired values must be finite")
    if np.ptp(delta) == 0:
        bounds = [float(delta[0]), float(delta[0])]
    else:
        result = bootstrap((delta,), np.mean, n_resamples=2000, method="basic", rng=0)
        bounds = [float(result.confidence_interval.low), float(result.confidence_interval.high)]
    return {"pairs": len(before), "mean_delta": float(np.mean(delta)),
            "paired_bootstrap_95": bounds, "resampling_unit": "complete case"}
