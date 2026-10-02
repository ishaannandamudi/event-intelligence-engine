"""Small deterministic centered/scaled, twice-orthogonalized QR OLS."""

from math import fsum, hypot, isfinite, sqrt

from eie.impact.models import FitStatus, RegressionFit


def dot(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    return fsum(x * y for x, y in zip(a, b, strict=True))


def solve_upper(r: list[list[float]], y: tuple[float, ...]) -> tuple[float, ...]:
    x = [0.0] * len(y)
    for i in reversed(range(len(y))):
        x[i] = (y[i] - fsum(r[i][j] * x[j] for j in range(i + 1, len(y)))) / r[i][i]
    return tuple(x)


def ols(
    y: tuple[float, ...],
    x: tuple[tuple[float, ...], ...],
    names: tuple[str, ...],
    *,
    intercept: bool,
    minimum: int,
    maximum_condition: float,
) -> tuple[FitStatus, RegressionFit | None]:
    n, k = len(y), len(names)
    p = k + int(intercept)
    if n < max(minimum, p + 1) or k == 0:
        return FitStatus.INSUFFICIENT, None
    try:
        columns = tuple(tuple(row[j] for row in x) for j in range(k))
        if any(all(v == column[0] for v in column) for column in columns):
            return FitStatus.DEGENERATE, None
        centers = tuple(fsum(v / n for v in c) if intercept else 0.0 for c in columns)
        scales = tuple(hypot(*(v - m for v in c)) for c, m in zip(columns, centers, strict=True))
        if any(not isfinite(s) for s in scales):
            return FitStatus.NUMERIC_UNAVAILABLE, None
        if any(s == 0 for s in scales):
            return FitStatus.DEGENERATE, None
        design = ([tuple(1 / sqrt(n) for _ in y)] if intercept else []) + [
            tuple((v - m) / s for v in c) for c, m, s in zip(columns, centers, scales, strict=True)
        ]
        q: list[tuple[float, ...]] = []
        r = [[0.0] * p for _ in range(p)]
        for j, column in enumerate(design):
            v = column
            for _ in range(2):
                for i, basis in enumerate(q):
                    projection = dot(basis, v)
                    r[i][j] += projection
                    v = tuple(a - projection * b for a, b in zip(v, basis, strict=True))
            length = hypot(*v)
            if length <= 1e-12:
                return FitStatus.SINGULAR, None
            r[j][j] = length
            q.append(tuple(a / length for a in v))
        inverse_columns = tuple(
            solve_upper(r, tuple(float(i == j) for i in range(p))) for j in range(p)
        )
        inverse = tuple(tuple(c[i] for c in inverse_columns) for i in range(p))
        norm_r = max(fsum(abs(r[i][j]) for i in range(p)) for j in range(p))
        norm_inverse = max(fsum(abs(inverse[i][j]) for i in range(p)) for j in range(p))
        condition = max(1.0, norm_r * norm_inverse)
        if condition > maximum_condition:
            return FitStatus.SINGULAR, None
        # Center y to avoid losing small residuals under a large intercept.
        center_y = fsum(v / n for v in y) if intercept else 0.0
        centered_y = tuple(v - center_y for v in y)
        gamma = solve_upper(r, tuple(dot(c, centered_y) for c in q))
        slopes = tuple(gamma[j + int(intercept)] / scales[j] for j in range(k))
        alpha = center_y + gamma[0] / sqrt(n) - dot(slopes, centers) if intercept else 0.0
        coefficients = (alpha, *slopes) if intercept else slopes
        residuals = tuple(
            yy - fsum(gg * column[i] for gg, column in zip(gamma, design, strict=True))
            for i, yy in enumerate(centered_y)
        )
        residual_scale = hypot(*residuals) / sqrt(n - p)
        variance = residual_scale**2
        if residual_scale > 0 and variance == 0:
            return FitStatus.NUMERIC_UNAVAILABLE, None
        total = hypot(*(v - center_y for v in y)) ** 2
        r_squared = None if total == 0 else 1 - variance * (n - p) / total
        # Map QR parameter covariance to the original return units.
        transform = [[0.0] * p for _ in range(p)]
        if intercept:
            transform[0][0] = 1 / sqrt(n)
            for j in range(k):
                transform[0][j + 1] = -centers[j] / scales[j]
        for j in range(k):
            transform[j + int(intercept)][j + int(intercept)] = 1 / scales[j]
        mapped = tuple(
            tuple(fsum(transform[i][a] * inverse[a][j] for a in range(p)) for j in range(p))
            for i in range(p)
        )
        covariance = tuple(tuple(variance * dot(a, b) for b in mapped) for a in mapped)
        errors = tuple(sqrt(max(0.0, covariance[j][j])) for j in range(p))
        values = (
            *coefficients,
            *errors,
            variance,
            condition,
            *residuals,
            *(v for row in covariance for v in row),
        )
        if (
            any(not isfinite(v) for v in values)
            or r_squared is not None
            and not isfinite(r_squared)
        ):
            return FitStatus.NUMERIC_UNAVAILABLE, None
        return FitStatus.AVAILABLE, RegressionFit(
            (("INTERCEPT", *names) if intercept else names),
            coefficients,
            errors,
            covariance,
            variance,
            r_squared,
            condition,
            n - p,
            residuals,
        )
    except (ArithmeticError, ValueError):
        return FitStatus.NUMERIC_UNAVAILABLE, None
