"""Align transcript windows to slides: similarity + a best path through the deck. Pure.

Scores: cosine similarity of window and slide embeddings, centred per window (minus the
window's median over slides), so generic chatter scores ~0 against everything.

States per window: (slide k, on) = "about slide k", scoring c[w, k]; or (slide k, off) =
"a tangent while slide k is up", scoring a constant `tangent`. Tangents therefore stay
unaligned instead of dragging the path to some other slide. Windows the cleaning pass
flagged as admin (intro, logistics, after-class chatter) score `admin_tangent` instead:
embeddings can't tell that talk from content, but the flag can.

Moves between windows (delta = k' - k): stay free; next slide nearly free; skipping ahead
costs a little per skipped slide; going back costs more (lecturers do revisit slides).
With timestamps, a forward jump of more than `pace_jump` slides within `pace_seconds`
costs extra. The first window prefers the first few slides.

Viterbi gives the path; forward-backward over the same scores (temperature T) gives each
window's probability of its chosen state. A window is unaligned if the path calls it a
tangent or that probability is below `min_confidence`.
"""

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class AlignParams:
    tangent: float = 0.04
    admin_tangent: float = 0.15  # measured on GRASP Part I: +9 pts exact (docs/eval.md)
    next_slide: float = 0.005
    skip_base: float = 0.03
    skip_step: float = 0.015
    back_base: float = 0.06
    back_step: float = 0.01
    start_free: int = 3
    start_step: float = 0.01
    pace_jump: int = 3
    pace_seconds: float = 30
    pace_penalty: float = 0.05
    temperature: float = 0.02
    min_confidence: float = 0.35


@dataclass(frozen=True)
class Alignment:
    slide: int | None  # index into the slide list; None = unaligned
    score: float | None  # cosine similarity to that slide
    confidence: float  # probability of the chosen state


def cosine_matrix(windows: np.ndarray, slides: np.ndarray) -> np.ndarray:
    w = windows / np.maximum(np.linalg.norm(windows, axis=1, keepdims=True), 1e-12)
    s = slides / np.maximum(np.linalg.norm(slides, axis=1, keepdims=True), 1e-12)
    return w @ s.T


def move_costs(m: int, p: AlignParams) -> np.ndarray:
    """cost[k, k2] of moving from slide k to slide k2 between consecutive windows."""
    delta = np.arange(m)[None, :] - np.arange(m)[:, None]
    cost = np.zeros((m, m))
    cost[delta == 1] = p.next_slide
    ahead = delta > 1
    cost[ahead] = p.skip_base + p.skip_step * (delta[ahead] - 1)
    back = delta < 0
    cost[back] = p.back_base + p.back_step * (-delta[back])
    return cost


def align(
    sim: np.ndarray,
    starts_ms: list[int | None] | None = None,
    params: AlignParams | None = None,
    admin: list[bool] | None = None,
) -> list[Alignment]:
    """`admin`: per window, whether it was flagged as admin talk."""
    p = params or AlignParams()
    n, m = sim.shape
    if n == 0:
        return []
    if m == 0:
        return [Alignment(None, None, 0.0)] * n
    centred = sim - np.median(sim, axis=1, keepdims=True)
    tangent = np.full(n, p.tangent)
    if admin is not None:
        tangent = np.where(np.asarray(admin, dtype=bool), p.admin_tangent, p.tangent)
    # Emissions for 2m states: [on_0..on_{m-1}, off_0..off_{m-1}]
    emit = np.concatenate([centred, np.repeat(tangent[:, None], m, axis=1)], axis=1)
    base = move_costs(m, p)

    def trans(w: int) -> np.ndarray:
        """2m x 2m transition scores into window w (mode changes are free)."""
        cost = base.copy()
        timed = starts_ms and w > 0 and starts_ms[w] is not None and starts_ms[w - 1] is not None
        if timed and (starts_ms[w] - starts_ms[w - 1]) / 1000 < p.pace_seconds:
            delta = np.arange(m)[None, :] - np.arange(m)[:, None]
            cost = cost + np.where(delta > p.pace_jump, p.pace_penalty, 0.0)
        tiled = np.tile(-cost, (2, 2))
        return tiled

    start = -np.maximum(0, np.arange(m) - (p.start_free - 1)) * p.start_step
    start = np.concatenate([start, start])

    # Viterbi
    score = start + emit[0]
    back = np.zeros((n, 2 * m), dtype=int)
    for w in range(1, n):
        t = score[:, None] + trans(w)
        back[w] = np.argmax(t, axis=0)
        score = t[back[w], np.arange(2 * m)] + emit[w]
    path = [int(np.argmax(score))]
    for w in range(n - 1, 0, -1):
        path.append(int(back[w][path[-1]]))
    path.reverse()

    posterior = _posteriors(emit, start, trans, p.temperature)

    out = []
    for w, state in enumerate(path):
        conf = float(posterior[w, state])
        k = state % m
        if state >= m or conf < p.min_confidence:
            out.append(Alignment(None, float(sim[w, k]), conf))
        else:
            out.append(Alignment(k, float(sim[w, k]), conf))
    return out


def _logsumexp(a: np.ndarray, axis: int) -> np.ndarray:
    top = np.max(a, axis=axis, keepdims=True)
    return np.squeeze(top, axis=axis) + np.log(np.sum(np.exp(a - top), axis=axis))


def _posteriors(emit, start, trans, temperature) -> np.ndarray:
    n, s = emit.shape
    e = emit / temperature
    alpha = np.zeros((n, s))
    alpha[0] = start / temperature + e[0]
    for w in range(1, n):
        alpha[w] = _logsumexp(alpha[w - 1][:, None] + trans(w) / temperature, axis=0) + e[w]
    beta = np.zeros((n, s))
    for w in range(n - 2, -1, -1):
        beta[w] = _logsumexp(trans(w + 1) / temperature + (e[w + 1] + beta[w + 1])[None, :], 1)
    log_post = alpha + beta
    log_post -= _logsumexp(log_post, axis=1)[:, None]
    return np.exp(log_post)
