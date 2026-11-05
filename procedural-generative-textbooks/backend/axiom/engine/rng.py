"""Deterministic randomness for world generation (ARCHITECTURE.md section 6).

``SeededRNG`` wraps ``numpy.random.Generator(PCG64)``. Seeds are derived with BLAKE2b
(never Python's salted ``hash()``), and child streams created with :meth:`SeededRNG.spawn`
depend only on the parent's seed and the child's name, so adding draws to one stream never
changes another. Nothing in this module reads the wall clock.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, TypeVar

import numpy as np
from numpy.typing import ArrayLike, NDArray
from scipy import stats

if TYPE_CHECKING:
    from axiom.models import Benchmark

__all__ = [
    "SeededRNG",
    "hash_seed",
    "derive_seed",
    "benchmark_quantile",
    "empirical_knots",
    "fit_lognormal",
    "fit_normal",
    "FIRST_NAMES",
    "LAST_NAMES",
    "COMPANY_STEMS",
    "COMPANY_SUFFIXES",
    "INDUSTRY_SUFFIXES",
    "CITIES",
]

logger = logging.getLogger("axiom.rng")

T = TypeVar("T")

_Z10 = float(stats.norm.ppf(0.10))  # -1.2815515655446004
_Z90 = float(stats.norm.ppf(0.90))
_PROBS = np.array([0.10, 0.25, 0.50, 0.75, 0.90])
_ZS = stats.norm.ppf(_PROBS)


def _blake64(text: str) -> int:
    return int.from_bytes(hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest(), "big")


def hash_seed(archetype: str, seed: int, version: str) -> int:
    """Stable 64-bit seed from ``(archetype, seed, version)`` via BLAKE2b."""
    return _blake64(f"axiom|{archetype}|{int(seed)}|{version}")


def derive_seed(parent_seed: int, name: str) -> int:
    """Stable 64-bit child seed from a parent seed and a stream name."""
    return _blake64(f"axiom-spawn|{int(parent_seed)}|{name}")


# --------------------------------------------------------------------------- benchmark quantiles


def empirical_knots(p10: float, p25: float, p50: float, p75: float, p90: float) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Knots ``(probs, values)`` of the monotone piecewise-linear quantile function.

    Tails are linear from p10 down to ``p10 - (p25 - p10)`` at u=0 and from p90 up to
    ``p90 + (p90 - p75)`` at u=1.
    """
    lo = p10 - (p25 - p10)
    hi = p90 + (p90 - p75)
    probs = np.array([0.0, 0.10, 0.25, 0.50, 0.75, 0.90, 1.0])
    values = np.array([lo, p10, p25, p50, p75, p90, hi], dtype=float)
    values = np.maximum.accumulate(values)  # guard against float noise
    return probs, values


def fit_lognormal(p10: float, p50: float, p90: float) -> tuple[float, float]:
    """Least-squares ``(mu, sigma)`` of ``ln X = mu + sigma * z`` through p10/p50/p90.

    Requires strictly positive percentiles.
    """
    if min(p10, p50, p90) <= 0:
        raise ValueError("lognormal fit requires positive percentiles")
    z = np.array([_Z10, 0.0, _Z90])
    y = np.log(np.array([p10, p50, p90], dtype=float))
    a = np.vstack([np.ones_like(z), z]).T
    (mu, sigma), *_ = np.linalg.lstsq(a, y, rcond=None)
    return float(mu), float(max(sigma, 1e-12))


def fit_normal(qs: Sequence[float]) -> tuple[float, float]:
    """Least-squares ``(mean, sd)`` through the five percentiles p10..p90."""
    a = np.vstack([np.ones_like(_ZS), _ZS]).T
    (mu, sd), *_ = np.linalg.lstsq(a, np.asarray(qs, dtype=float), rcond=None)
    return float(mu), float(max(sd, 1e-12))


def _beta_params(mean: float, sd: float) -> tuple[float, float]:
    var = sd * sd
    max_var = mean * (1 - mean)
    if not (0 < mean < 1) or max_var <= 0:
        raise ValueError(f"beta requires mean in (0,1), got {mean}")
    var = min(max(var, 1e-12), max_var * 0.999)
    common = max_var / var - 1
    return mean * common, (1 - mean) * common


def benchmark_quantile(benchmark: Benchmark, u: ArrayLike) -> NDArray[np.float64]:
    """Quantile function of a benchmark's declared distribution, evaluated at ``u`` in [0, 1].

    Falls back to the empirical interpolation if the declared family cannot be fitted
    (e.g. lognormal with non-positive percentiles, beta outside (0, 1)).
    """
    uu = np.clip(np.asarray(u, dtype=float), 1e-9, 1 - 1e-9)
    qs = benchmark.percentiles()
    if qs[0] == qs[-1]:
        return np.full_like(uu, qs[2], dtype=float)
    dist = benchmark.distribution
    try:
        if dist == "lognormal":
            mu, sigma = fit_lognormal(qs[0], qs[2], qs[4])
            return np.exp(mu + sigma * stats.norm.ppf(uu))
        if dist == "normal":
            mu, sd = fit_normal(qs)
            return mu + sd * stats.norm.ppf(uu)
        if dist == "beta":
            a, b = _beta_params(qs[2], (qs[3] - qs[1]) / 1.3489795)
            return np.asarray(stats.beta.ppf(uu, a, b), dtype=float)
        if dist == "triangular":
            lo = qs[0] - (qs[1] - qs[0])
            hi = qs[4] + (qs[4] - qs[3])
            mode = min(max(qs[2], lo), hi)
            width = hi - lo
            c = (mode - lo) / width
            return np.asarray(stats.triang.ppf(uu, c, loc=lo, scale=width), dtype=float)
    except ValueError as exc:
        logger.debug("benchmark %s: %s fit failed (%s); using empirical", benchmark.key, dist, exc)
    probs, values = empirical_knots(*qs)
    return np.interp(uu, probs, values)


# --------------------------------------------------------------------------- word lists

FIRST_NAMES: tuple[str, ...] = (
    "Aaliyah", "Abebe", "Adaeze", "Adrian", "Aiko", "Alejandro", "Amara", "Anika", "Arjun", "Astrid",
    "Ayesha", "Bao", "Beatriz", "Bilal", "Bjorn", "Camila", "Chen", "Chidi", "Clara", "Dae-jung",
    "Daniela", "Darius", "Dmitri", "Elena", "Emeka", "Esra", "Fatima", "Felipe", "Freya", "Gabriel",
    "Hana", "Hamza", "Ingrid", "Isabela", "Jamal", "Javier", "Jia", "Jonas", "Kai", "Kalani",
    "Kavya", "Kenji", "Kofi", "Lars", "Layla", "Leila", "Liam", "Lucia", "Malik", "Mariana",
    "Mateo", "Mei", "Miguel", "Mira", "Nadia", "Naveen", "Nia", "Noor", "Olga", "Omar",
    "Oren", "Priya", "Rafael", "Ravi", "Rosa", "Saanvi", "Samir", "Sana", "Sergei", "Siobhan",
    "Sofia", "Tariq", "Thandiwe", "Tomas", "Uma", "Valentina", "Wei", "Yara", "Yusuf", "Zainab",
    "Zoe", "Hiroshi", "Ifeoma", "Marcus", "Grace", "Daniel", "Hannah", "Owen", "Rachel", "Victor",
)

LAST_NAMES: tuple[str, ...] = (
    "Abara", "Adeyemi", "Agarwal", "Alvarez", "Andersen", "Bauer", "Becker", "Bennett", "Bhatt", "Brennan",
    "Carvalho", "Castillo", "Chakraborty", "Chen", "Cohen", "Costa", "Dang", "Das", "Delgado", "Dubois",
    "Eriksen", "Fernandes", "Fischer", "Fujita", "Garcia", "Ghosh", "Gonzalez", "Gupta", "Haddad", "Hansen",
    "Hernandez", "Hoang", "Holm", "Ibrahim", "Ivanova", "Jensen", "Johansson", "Kahale", "Kapoor", "Kim",
    "Kowalski", "Kumar", "Larsen", "Lee", "Lindqvist", "Lopez", "Mahlangu", "Martins", "Mendoza", "Moreau",
    "Murphy", "Nakamura", "Nguyen", "Novak", "Nwosu", "O'Brien", "Okafor", "Okonkwo", "Olsen", "Ortiz",
    "Patel", "Petrov", "Popescu", "Quispe", "Rahman", "Ramirez", "Rossi", "Sato", "Schmidt", "Shah",
    "Silva", "Singh", "Sokolov", "Suzuki", "Tanaka", "Torres", "Tran", "Varga", "Wang", "Weber",
    "Williams", "Yamamoto", "Yilmaz", "Zhang", "Zielinski", "Osei", "Mensah", "Achterberg", "Reyes", "Kaur",
)

COMPANY_STEMS: tuple[str, ...] = (
    "Alder", "Amber", "Apex", "Arbor", "Atlas", "Aurora", "Basalt", "Beacon", "Birch", "Bluewater",
    "Bramble", "Brightline", "Cascade", "Cedar", "Cinder", "Cobalt", "Compass", "Copper", "Crestview", "Cypress",
    "Delta", "Driftwood", "Ember", "Evergreen", "Falcon", "Fathom", "Fjord", "Foundry", "Granite", "Halcyon",
    "Harbor", "Helix", "Heron", "Horizon", "Ironwood", "Juniper", "Keystone", "Kestrel", "Lantern", "Larkspur",
    "Lumen", "Maple", "Meridian", "Mesa", "Northwind", "Oakmont", "Onyx", "Orchard", "Pinnacle", "Prairie",
    "Quarry", "Redwood", "Ridgeline", "Saffron", "Sequoia", "Silverline", "Summit", "Tamarack", "Tidewater", "Vantage",
    "Verdant", "Willow", "Zephyr", "Quill", "Nimbus", "Solstice", "Tessera", "Corvid", "Marigold", "Obsidian",
)

COMPANY_SUFFIXES: tuple[str, ...] = (
    "Inc.", "Co.", "Corp.", "Holdings", "Group", "Partners", "Labs", "Works", "Industries", "Systems",
    "Technologies", "Brands", "& Co.", "Collective", "Ventures",
)

INDUSTRY_SUFFIXES: dict[str, tuple[str, ...]] = {
    "saas": ("Software", "Cloud", "Analytics", "Platforms", "Data", "AI", "Systems"),
    "ecommerce": ("Goods", "Supply", "Market", "Outfitters", "Direct", "Home"),
    "manufacturing": ("Manufacturing", "Fabrication", "Industries", "Components", "Precision", "Machining"),
    "3pl": ("Logistics", "Freight", "Fulfillment", "Distribution", "Transport", "Supply Chain"),
    "retail": ("Retail", "Stores", "Mercantile", "Provisions", "Outfitters", "Trading"),
    "any": ("Group", "Holdings", "Partners", "Enterprises"),
}

CITIES: tuple[str, ...] = (
    "Austin, TX", "Denver, CO", "Columbus, OH", "Raleigh, NC", "Portland, OR", "Pittsburgh, PA",
    "Minneapolis, MN", "Salt Lake City, UT", "Atlanta, GA", "Chicago, IL", "Boston, MA", "Seattle, WA",
    "Nashville, TN", "Phoenix, AZ", "Kansas City, MO", "Milwaukee, WI", "Toronto, ON", "Dublin, IE",
    "Berlin, DE", "Amsterdam, NL", "London, UK", "Singapore, SG", "Bengaluru, IN", "Lagos, NG",
    "Nairobi, KE", "Sao Paulo, BR", "Mexico City, MX", "Melbourne, AU", "Tallinn, EE", "Lisbon, PT",
)


# --------------------------------------------------------------------------- SeededRNG


class SeededRNG:
    """Deterministic random stream (numpy PCG64) with named, order-independent child streams."""

    def __init__(self, seed: int, name: str = "root") -> None:
        """Create a stream from an explicit 64-bit seed. Prefer :meth:`for_world` / :meth:`spawn`."""
        self.seed: int = int(seed) & 0xFFFFFFFFFFFFFFFF
        self.name: str = name
        self._gen: np.random.Generator = np.random.Generator(np.random.PCG64(self.seed))

    @classmethod
    def for_world(cls, archetype: str, seed: int, version: str) -> SeededRNG:
        """Root stream of a world, seeded from ``hash_seed(archetype, seed, version)``."""
        return cls(hash_seed(archetype, seed, version), name=f"{archetype}:{seed}:{version}")

    def __repr__(self) -> str:
        return f"SeededRNG(name={self.name!r}, seed={self.seed})"

    @property
    def generator(self) -> np.random.Generator:
        """The underlying numpy Generator (for vectorised draws)."""
        return self._gen

    def spawn(self, name: str) -> SeededRNG:
        """Child stream determined only by this stream's seed and ``name`` (not by draw history)."""
        return SeededRNG(derive_seed(self.seed, name), name=f"{self.name}/{name}")

    # ------------------------------------------------------------------ primitives

    def random(self, size: int | tuple[int, ...] | None = None) -> float | NDArray[np.float64]:
        """Uniform [0, 1)."""
        return self._gen.random(size) if size is not None else float(self._gen.random())

    def uniform(self, low: float = 0.0, high: float = 1.0, size: int | tuple[int, ...] | None = None) -> float | NDArray[np.float64]:
        """Uniform [low, high)."""
        return self._gen.uniform(low, high, size) if size is not None else float(self._gen.uniform(low, high))

    def integers(self, low: int, high: int, size: int | tuple[int, ...] | None = None) -> int | NDArray[np.int64]:
        """Integers in [low, high)."""
        return self._gen.integers(low, high, size) if size is not None else int(self._gen.integers(low, high))

    def normal(self, mean: float = 0.0, sd: float = 1.0, size: int | tuple[int, ...] | None = None) -> float | NDArray[np.float64]:
        """Normal(mean, sd)."""
        return self._gen.normal(mean, sd, size) if size is not None else float(self._gen.normal(mean, sd))

    def bernoulli(self, p: float) -> bool:
        """True with probability ``p``."""
        return bool(self._gen.random() < p)

    def poisson(self, lam: float, size: int | tuple[int, ...] | None = None) -> int | NDArray[np.int64]:
        """Poisson(lam)."""
        return self._gen.poisson(lam, size) if size is not None else int(self._gen.poisson(lam))

    def lognormal_from_percentiles(self, p10: float, p50: float, p90: float, size: int | tuple[int, ...] | None = None) -> float | NDArray[np.float64]:
        """Lognormal draw with ``(mu, sigma)`` least-squares fitted to p10/p50/p90."""
        mu, sigma = fit_lognormal(p10, p50, p90)
        return self._gen.lognormal(mu, sigma, size) if size is not None else float(self._gen.lognormal(mu, sigma))

    def beta_from_mean_sd(self, mean: float, sd: float, size: int | tuple[int, ...] | None = None) -> float | NDArray[np.float64]:
        """Beta draw by method of moments (variance clamped to the feasible range)."""
        a, b = _beta_params(mean, sd)
        return self._gen.beta(a, b, size) if size is not None else float(self._gen.beta(a, b))

    def triangular(self, low: float, mode: float, high: float, size: int | tuple[int, ...] | None = None) -> float | NDArray[np.float64]:
        """Triangular(low, mode, high)."""
        if high <= low:
            return np.full(size, low, dtype=float) if size is not None else float(low)
        mode = min(max(mode, low), high)
        return self._gen.triangular(low, mode, high, size) if size is not None else float(self._gen.triangular(low, mode, high))

    def pareto(self, alpha: float, xm: float = 1.0, size: int | tuple[int, ...] | None = None) -> float | NDArray[np.float64]:
        """Classic Pareto with shape ``alpha`` and scale (minimum) ``xm``."""
        if size is not None:
            return xm * (1.0 + self._gen.pareto(alpha, size))
        return float(xm * (1.0 + self._gen.pareto(alpha)))

    def choice(self, items: Sequence[T]) -> T:
        """One uniformly chosen element (type preserved)."""
        if not items:
            raise ValueError("choice from empty sequence")
        return items[int(self._gen.integers(0, len(items)))]

    def sample(self, items: Sequence[T], k: int) -> list[T]:
        """``k`` distinct elements without replacement."""
        if k > len(items):
            raise ValueError(f"cannot sample {k} from {len(items)} items")
        idx = self._gen.choice(len(items), size=k, replace=False)
        return [items[int(i)] for i in idx]

    def weighted_choice(self, items: Sequence[T], weights: Sequence[float]) -> T:
        """One element chosen with probability proportional to ``weights``."""
        if len(items) != len(weights) or not items:
            raise ValueError("items and weights must be non-empty and equal length")
        w = np.asarray(weights, dtype=float)
        if np.any(w < 0) or w.sum() <= 0:
            raise ValueError("weights must be non-negative with positive sum")
        return items[int(self._gen.choice(len(items), p=w / w.sum()))]

    def shuffled(self, items: Sequence[T]) -> list[T]:
        """A shuffled copy of ``items``."""
        idx = self._gen.permutation(len(items))
        return [items[int(i)] for i in idx]

    def date_jitter(self, base: dt.date, max_days: int, *, forward_only: bool = False) -> dt.date:
        """``base`` shifted by a uniform integer number of days in [-max_days, max_days] (or [0, max_days])."""
        low = 0 if forward_only else -max_days
        return base + dt.timedelta(days=int(self._gen.integers(low, max_days + 1)))

    # ------------------------------------------------------------------ benchmarks

    def sample_benchmark(self, benchmark: Benchmark) -> float:
        """One draw from a benchmark's declared distribution (inverse-CDF on one uniform)."""
        u = float(self._gen.random())
        return float(benchmark_quantile(benchmark, u))

    def sample_benchmark_n(self, benchmark: Benchmark, n: int) -> NDArray[np.float64]:
        """``n`` draws from a benchmark's declared distribution."""
        return benchmark_quantile(benchmark, self._gen.random(n))

    # ------------------------------------------------------------------ names

    def first_name(self) -> str:
        """A first name from the built-in list."""
        return self.choice(FIRST_NAMES)

    def last_name(self) -> str:
        """A last name from the built-in list."""
        return self.choice(LAST_NAMES)

    def person_name(self) -> str:
        """``"First Last"``."""
        return f"{self.first_name()} {self.last_name()}"

    def pick_names(self, n: int) -> list[str]:
        """``n`` distinct person names."""
        if n > len(FIRST_NAMES) * len(LAST_NAMES):
            raise ValueError("too many names requested")
        seen: set[str] = set()
        out: list[str] = []
        while len(out) < n:
            name = self.person_name()
            if name not in seen:
                seen.add(name)
                out.append(name)
        return out

    def company_name(self, industry: str | None = None) -> str:
        """A fictional company name, e.g. ``"Kestrel Logistics"`` or ``"Halcyon Labs"``."""
        stem = self.choice(COMPANY_STEMS)
        pool = INDUSTRY_SUFFIXES.get(industry or "any", INDUSTRY_SUFFIXES["any"])
        if self._gen.random() < 0.7:
            return f"{stem} {self.choice(pool)}"
        return f"{stem} {self.choice(COMPANY_SUFFIXES)}"

    def city(self) -> str:
        """An HQ city."""
        return self.choice(CITIES)

    @staticmethod
    def ticker_like(company_name: str) -> str:
        """Deterministic 3-4 letter pseudo-ticker for a company name (no randomness)."""
        words = ["".join(ch for ch in w if ch.isalpha()).upper() for w in company_name.replace("&", " ").split()]
        words = [w for w in words if w]
        if not words:
            return "XXX"
        if len(words) >= 3:
            ticker = "".join(w[0] for w in words)[:4]
        elif len(words) == 2:
            ticker = words[0][:2] + words[1][0]
        else:
            ticker = words[0][:4]
        return ticker.ljust(3, "X")
